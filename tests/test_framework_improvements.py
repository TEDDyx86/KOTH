import asyncio
import json
import socket
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic_ai import ModelRetry
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import BinaryContent, ModelRequest, ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine

from app.agents.api import reload_orchestrator
from app.agents.cli_providers import _extract_json_object, _render_cli_prompt, make_cli_model
from app.agents.context import challenge_card, jury_context, specialist_context
from app.agents.jury import JuryAgent
from app.agents.orchestrator import Orchestrator
from app.agents.payloads import Blackboard, JuryDecision, TaskOrder
from app.agents.recon import detect_file_type, probe_tcp_banner, recon_bundle
from app.agents.specialists import RunDeps, SpecialistRunner
from app.agents.submission import is_placeholder_flag, validate_flag
from app.agents.writeup import build_arsenal_digest
from app.database import configure_sqlite_engine
from app.main import create_app
from app.models import Attachment, AttachmentType, Attempt, AttemptResult, AttemptType, Challenge, Event
from app.services import tracking
from app.services.storage import StorageService
from app.tooling.config import ToolSettings, ToolSpec
from app.tooling.doctor import inventory
from app.tooling.mcp_servers import BoundedMcpToolset
from app.tooling.runner import ToolRunner


def _make_env(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'imp.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    configure_sqlite_engine(engine)
    SQLModel.metadata.create_all(engine)
    storage = StorageService(tmp_path / "storage")
    with Session(engine) as session:
        ev = Event(name="KOTH-Test", flag_format=r"CTF\{.*\}")
        session.add(ev)
        session.commit()
        session.refresh(ev)
        ch = Challenge(event_id=ev.id, title="ImpChal", category="web", description="Important clue: check /admin")
        session.add(ch)
        session.commit()
        session.refresh(ch)
        yield engine, session, ev, ch, storage


def _scripted(payloads: list[dict]):
    state = {"n": 0}

    def fn(messages, info):
        idx = state["n"]
        state["n"] += 1
        payload = payloads[min(idx, len(payloads) - 1)]
        if "tool_name" in payload:
            return ModelResponse(parts=[
                ToolCallPart(tool_name=payload["tool_name"], args=json.dumps(payload["tool_args"]))
            ])
        import re
        blob = "\n".join(str(p) for m in messages for p in (m.parts or []))
        match = re.search(r"['\"](\w*result\w*)['\"]", blob)
        name = match.group(1) if match else "final_result"
        return ModelResponse(parts=[ToolCallPart(tool_name=name, args=json.dumps(payload))])

    return FunctionModel(fn)


def test_sqlite_wal_and_tool_runner_result_and_cache(tmp_path):
    for engine, session, _ev, ch, storage in _make_env(tmp_path):
        mode = session.execute(text("PRAGMA journal_mode")).scalar()
        assert str(mode).lower() == "wal"

        runner = ToolRunner(
            ToolSettings(tools={
                "python": ToolSpec(mode="native", guard="none"),
                "strings": ToolSpec(mode="native", guard="strict"),
            }),
            storage,
        )
        # rc == 0 -> partial (not failed!)
        ok_res = runner.run(session, ch, "python", ["-c", "print('hello')"])
        assert ok_res.returncode == 0
        assert session.get(Attempt, ok_res.attempt_id).result == AttemptResult.partial

        # rc != 0 -> failed
        bad_res = runner.run(session, ch, "python", ["-c", "import sys; sys.exit(3)"])
        assert bad_res.returncode == 3
        assert session.get(Attempt, bad_res.attempt_id).result == AttemptResult.failed

        # Read-only cache deduplication on unchanged sandbox
        p = storage.save(ch.id, "sample.txt", b"secret_string_123\n")
        session.add(Attachment(
            challenge_id=ch.id, name="sample.txt",
            path=str(p.relative_to(storage.storage_dir)),
            mime="text/plain", size=18, type=AttachmentType.download,
        ))
        session.commit()
        if "strings" in runner.available_tools():
            r1 = runner.run(session, ch, "strings", ["sample.txt"], use_cache=True)
            r2 = runner.run(session, ch, "strings", ["sample.txt"], use_cache=True)
            assert r1.cache_hit is False
            assert r2.cache_hit is True
            assert "[cached" in r2.stdout


def test_hybrid_auto_sandbox_fallback(tmp_path, monkeypatch):
    cfg = ToolSettings(tools={"python": ToolSpec(mode="auto", guard="none")})
    cfg.defaults.docker_image = "koth-sandbox:latest"
    storage = StorageService(tmp_path / "storage")
    runner = ToolRunner(cfg, storage)

    monkeypatch.setattr("app.tooling.runner.shutil.which", lambda name: "/usr/bin/python" if name == "python" else "/usr/bin/docker")
    monkeypatch.setattr("app.tooling.runner.docker_status_cached", lambda img: {"daemon_running": False, "image_present": False})
    assert runner._effective_mode("python") == "native"

    monkeypatch.setattr("app.tooling.runner.docker_status_cached", lambda img: {"daemon_running": True, "image_present": True})
    assert runner._effective_mode("python") == "docker"

    monkeypatch.setattr("app.tooling.doctor.docker_status", lambda img: {"daemon_running": False, "image_present": False})
    monkeypatch.setattr("app.tooling.doctor.shutil.which", lambda name: "/usr/bin/python" if name == "python" else None)
    rows = {r["name"]: r for r in inventory(cfg)}
    assert rows["python"]["mode"] == "auto→native"
    assert rows["python"]["present"] is True


def test_context_pinned_header_excludes_writeup_and_outputs(tmp_path):
    for _eng, session, _ev, ch, storage in _make_env(tmp_path):
        ch.flag_format = r"CUSTOM\{[0-9]+\}"
        session.add(ch)
        session.commit()

        # Huge auto-writeup note + many tool output attachments + many attempts
        tracking.add_note(session, ch.id, "# Writeup — ImpChal\n" + ("X" * 60_000))
        tracking.add_note(session, ch.id, "Operator hint: check /secret-admin")
        for i in range(45):
            session.add(Attachment(
                challenge_id=ch.id, name=f"curl_output_{i}.txt",
                path=f"{ch.id}/out_{i}.txt", mime="text/plain", size=100, type=AttachmentType.output,
            ))
            tracking.record_attempt(
                session, ch.id, AttemptType.tool, f"curl run {i}", AttemptResult.partial,
                tool_command=f"curl http://localhost/{i}", learning="Z" * 1200,
            )
        session.commit()

        jc = jury_context(session, ch, budget=4000, blackboard={"dead_ends": ["basic auth failed"]})
        assert "# Challenge: ImpChal" in jc
        assert "Important clue: check /admin" in jc
        assert r"CUSTOM\{[0-9]+\}" in jc
        assert "Operator hint: check /secret-admin" in jc
        assert "# Writeup — ImpChal" not in jc
        assert "curl_output_0.txt" not in jc
        assert "tool output file(s) saved in storage" in jc
        assert "basic auth failed" in jc

        sc = specialist_context(session, ch, TaskOrder(category="web", task="probe /secret-admin"), budget=3500)
        assert "# Challenge: ImpChal" in sc
        assert "## Task from Júri" in sc
        assert "probe /secret-admin" in sc
        assert "Commands already executed" in sc


def test_bounded_mcp_toolset_truncates_and_enforces_budget(tmp_path):
    for _eng, session, _ev, ch, storage in _make_env(tmp_path):
        runner = ToolRunner(ToolSettings(tools={"python": ToolSpec(mode="native", guard="none")}), storage)
        deps = RunDeps(runner=runner, session=session, challenge=ch, order=TaskOrder(category="rev", task="decompile", max_tool_runs=1))

        class FakeMcp:
            label = "fake_mcp"
            async def call_tool(self, name, tool_args, ctx, tool):
                return "A" * 12000

        bounded = BoundedMcpToolset(FakeMcp(), max_output_chars=500, default_max_calls=2)
        ctx = SimpleNamespace(deps=deps)

        out1 = asyncio.run(bounded.call_tool("ghidra_decompile", {}, ctx, None))
        assert len(out1) < 1000
        assert "mcp_outputs/ghidra_decompile_1.txt" in out1
        saved_file = runner.sandbox_workdir(ch) / "mcp_outputs" / "ghidra_decompile_1.txt"
        assert saved_file.exists() and len(saved_file.read_text(encoding="utf-8")) == 12000

        asyncio.run(bounded.call_tool("ghidra_decompile", {}, ctx, None))
        with pytest.raises(ModelRetry):
            asyncio.run(bounded.call_tool("ghidra_decompile", {}, ctx, None))


def test_specialist_list_subdir_and_usage_limit_graceful(tmp_path, monkeypatch):
    monkeypatch.setattr("app.agents.specialists.build_pydantic_toolsets", lambda category=None: [])
    for _eng, session, _ev, ch, storage in _make_env(tmp_path):
        runner = ToolRunner(ToolSettings(tools={"python": ToolSpec(mode="native", guard="none")}), storage)
        wd = runner.sandbox_workdir(ch)
        (wd / "sub").mkdir(parents=True, exist_ok=True)
        (wd / "sub" / "secret.txt").write_text("hi", encoding="utf-8")

        spec = SpecialistRunner(_scripted([
            {"tool_name": "list_sandbox_files", "tool_args": {"subdir": "sub"}},
            {"status": "NEED_HINT", "findings": ["found sub/secret.txt"]},
        ]), persona_text="spec", category="web")
        rep = spec.run(session, ch, TaskOrder(category="web", task="list sub", max_tool_runs=3), runner)
        assert rep.status == "NEED_HINT"
        assert "found sub/secret.txt" in rep.findings

        # UsageLimitExceeded returns graceful NEED_HINT instead of crashing
        def limit_model(messages, info):
            raise UsageLimitExceeded("token limit hit")

        spec2 = SpecialistRunner(FunctionModel(limit_model), persona_text="spec", category="web", max_dispatch_tokens=100)
        rep2 = spec2.run(session, ch, TaskOrder(category="web", task="heavy", max_tool_runs=2), runner)
        assert rep2.status == "NEED_HINT"
        assert "usage limit" in rep2.findings[0].lower()


def test_flag_gate_blocks_placeholders_and_unverified_submit(tmp_path):
    for _eng, session, _ev, ch, storage in _make_env(tmp_path):
        ch.target = "http://127.0.0.1:9999"
        ch.flag_format = r"IDP\{[A-Za-z0-9_]+\}"
        session.add(ch)
        session.commit()

        assert is_placeholder_flag("IDP{FLAG_FALSA}") is True
        assert is_placeholder_flag("none") is True
        assert validate_flag(session, ch, "IDP{FLAG_FALSA}").accepted is False

        runner = ToolRunner(ToolSettings(tools={"python": ToolSpec(mode="native", guard="none")}), storage)
        # Jury tries SUBMIT without any tool output containing the flag -> blocked!
        jury = JuryAgent(_scripted([
            {"action": "DISPATCH", "reason": "r1", "order": {"category": "web", "task": "test"}, "findings": []},
            {"action": "SUBMIT", "reason": "guess", "flag_candidate": "IDP{guessed_flag}", "findings": []},
            {"action": "GIVE_UP", "reason": "no real flag", "findings": []},
        ]), persona_text="jury")
        spec = SpecialistRunner(_scripted([
            {"tool_name": "run_tool", "tool_args": {"tool": "python", "args": ["-c", "print('no flag here')"]}},
            {"status": "SOLVED", "flag_candidate": "IDP{guessed_flag}", "findings": ["guessed"]},
        ]), persona_text="spec", category="web")
        orch = Orchestrator(runner=runner, jury=jury, specialists={"web": spec})
        sess = orch.start(session, ch.id, mode="auto")
        assert sess.state == "gave_up"
        assert any("submit blocked" in f or "not observed" in f for f in sess.findings)


def test_cli_providers_extract_last_json_usage_and_binary():
    raw = 'Looking at {"sid": "abc"} from earlier.\n```json\n{"action": "DISPATCH", "order": {"category": "web", "task": "go"}, "reason": "ok", "findings": []}\n```'
    parsed = _extract_json_object(raw)
    assert parsed["action"] == "DISPATCH"

    msg = ModelRequest(parts=[UserPromptPart(content=["check image", BinaryContent(data=b"\x89PNG", media_type="image/png")])])
    rendered = _render_cli_prompt([msg], SimpleNamespace(function_tools=frozenset(), output_tools=frozenset(), instructions=None))
    assert "image/png" in rendered
    assert "4 bytes" in rendered


def test_recon2_macho_zip_urls_and_tcp_banner(tmp_path):
    # Mach-O magic
    macho = tmp_path / "macho.bin"
    macho.write_bytes(b"\xcf\xfa\xed\xfe\x0c\x00\x00\x01")
    assert "Mach-O 64-bit LE" in detect_file_type(macho)

    # TCP banner grab against ephemeral local socket
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def _serve():
        conn, _ = srv.accept()
        conn.sendall(b"HELLO_TCP_BANNER\n>>> ")
        conn.close()

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    banner = probe_tcp_banner("127.0.0.1", port, timeout_s=2.0)
    srv.close()
    assert banner is not None and "HELLO_TCP_BANNER" in banner

    # IPA/ZIP + discovered URLs
    for _eng, session, _ev, ch, storage in _make_env(tmp_path):
        zip_path = tmp_path / "app.ipa"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("Payload/Test.app/TestBin", b"\xcf\xfa\xed\xfe" + b"\x00" * 32)
        saved = storage.save(ch.id, "app.ipa", zip_path.read_bytes())
        session.add(Attachment(
            challenge_id=ch.id, name="app.ipa",
            path=str(saved.relative_to(storage.storage_dir)),
            mime="application/zip", size=zip_path.stat().st_size, type=AttachmentType.download,
        ))
        session.commit()

        class FakeRunner:
            def __init__(self):
                self.storage = storage
                self.real = ToolRunner(ToolSettings(), storage)

            def stage_downloads(self, s, c):
                return self.real.stage_downloads(s, c)

            def run_raw(self, c, tool, args, **kw):
                return SimpleNamespace(stdout="https://api.ctf-target.local/v1/flag\n", stderr="", returncode=0, truncated=False)

        dig = recon_bundle(FakeRunner(), session, ch, storage=storage)
        assert "Payload/Test.app/TestBin: Mach-O 64-bit LE" in dig.summary
        assert "https://api.ctf-target.local/v1/flag" in dig.discovered_urls


def test_dead_target_circuit_breaker_update_target_and_arsenal_digest(tmp_path):
    for eng, session, _ev, ch, storage in _make_env(tmp_path):
        ch.target = "http://127.0.0.1:59999"
        session.add(ch)
        session.commit()

        runner = ToolRunner(ToolSettings(tools={"python": ToolSpec(mode="native", guard="none")}), storage)
        jury = JuryAgent(_scripted([
            {"action": "DISPATCH", "reason": "probe", "order": {"category": "web", "task": "hit target"}, "findings": [], "hypotheses": ["check /admin"]},
            {"action": "GIVE_UP", "reason": "done", "findings": []},
        ]), persona_text="jury")
        spec = SpecialistRunner(_scripted([
            {"tool_name": "run_tool", "tool_args": {"tool": "python", "args": ["-c", "print('curl: (7) Failed to connect')"]}},
            {"tool_name": "run_tool", "tool_args": {"tool": "python", "args": ["-c", "print('WinError 10061 connection refused')"]}},
            {"tool_name": "run_tool", "tool_args": {"tool": "python", "args": ["-c", "print('Ncat: Nenhuma conex\\ufffdo p\\ufffde ser feita porque a m\\ufffdquina de destino as recusou ativamente.')"]}},
            {"status": "NEED_HINT", "findings": ["target down"], "dead_ends": ["port 59999 refused"]},
        ]), persona_text="spec", category="web")
        orch = Orchestrator(runner=runner, jury=jury, specialists={"web": spec})
        sess = orch.start(session, ch.id, mode="auto")

        # Circuit breaker paused immediately after dispatch 1 without looping to max_attempts
        assert sess.dispatch_count == 1
        assert sess.state == "awaiting_approval"
        assert sess.pending_decision.action == "WAIT_USER"
        assert "unreachable" in sess.pending_decision.reason.lower()
        assert len(sess.telemetry) == 1

        # Update target & resume
        sess2 = orch.update_target_and_resume(session, sess.id, "http://127.0.0.1:50001")
        session.refresh(ch)
        assert ch.target == "http://127.0.0.1:50001"
        assert sess2.state == "gave_up"
        assert "http://127.0.0.1:50001" in sess2.blackboard.discovered_targets

        # Arsenal digest excludes raw attempt timeline noise
        digest = build_arsenal_digest(session, ch, agent_session=sess2, storage=storage)
        assert "# Técnica — ImpChal" in digest
        assert "Prova de Execução (Timeline" not in digest


def test_reload_orchestrator_preserves_active_workers_and_update_target_route(tmp_path, monkeypatch):
    for engine, session, _ev, ch, _storage in _make_env(tmp_path):
        import app.agents.api as agent_api
        fake_orch = Orchestrator(
            runner=ToolRunner(ToolSettings(), StorageService(tmp_path / "storage")),
            jury=JuryAgent(_scripted([
                {"action": "WAIT_USER", "reason": "target down", "findings": []},
                {"action": "GIVE_UP", "reason": "done after update", "findings": []},
            ]), persona_text="j"),
            specialists={},
        )
        monkeypatch.setattr(agent_api, "_make_orchestrator", lambda _s: fake_orch)
        app = create_app(engine=engine, storage_dir=tmp_path / "storage")
        fake_orch._active_workers["s1"] = ch.id
        app.state._orchestrator = fake_orch
        reload_orchestrator(app)
        assert app.state._orchestrator is fake_orch
        assert "s1" in app.state._orchestrator._active_workers

        fake_orch._active_workers.pop("s1", None)
        with TestClient(app) as c:
            s0 = fake_orch.start(session, ch.id, mode="supervised")
            r = c.post(
                f"/challenges/{ch.id}/agent/sessions/{s0.id}/update-target",
                data={"target": "http://127.0.0.1:43210", "background": "false", "mode": "supervised"},
            )
            assert r.status_code == 200
            session.expire_all()
            updated = session.get(Challenge, ch.id)
            assert updated.target == "http://127.0.0.1:43210"
