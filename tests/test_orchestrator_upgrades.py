import json
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic_ai.messages import BinaryContent, ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from sqlmodel import Session, SQLModel, create_engine

from app.agents.api import _persona_text
from app.agents.context import jury_context, specialist_context
from app.agents.jury import JuryAgent
from app.agents.models import run_with_fallback
from app.agents.orchestrator import Orchestrator
from app.agents.payloads import TaskOrder
from app.agents.planner import check_stop_criteria, reassess_strategy
from app.agents.recon import detect_file_type, recon_bundle
from app.agents.specialists import SpecialistRunner
from app.main import create_app
from app.models import Attachment, AttachmentType, Challenge, ChallengeStatus, Event
from app.services import tracking
from app.services.storage import StorageService
from app.tooling.config import load_tools_config
from app.tooling.runner import ToolRunner


@pytest.fixture
def env(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'u.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    storage = StorageService(tmp_path / "storage")
    with Session(engine) as session:
        event = Event(name="ctf", flag_format=r"CTF\{.*\}")
        session.add(event)
        session.commit()
        session.refresh(event)
        ch = Challenge(event_id=event.id, title="chal", points=500, target=None)
        session.add(ch)
        session.commit()
        session.refresh(ch)
        yield engine, session, event, ch, storage


def _scripted(payloads: list[dict]):
    state = {"n": 0}

    def fn(messages, info):
        idx = state["n"]
        state["n"] += 1
        if idx < len(payloads) and "tool" in payloads[idx]:
            return ModelResponse(parts=[ToolCallPart(
                tool_name="run_tool",
                args=json.dumps({"tool": payloads[idx]["tool"], "args": payloads[idx]["args"]}),
            )])
        blob = "\n".join(str(p) for m in messages for p in (m.parts or []))
        match = re.search(r"['\"](\w*result\w*)['\"]", blob)
        name = match.group(1) if match else "final_result"
        payload = payloads[min(idx, len(payloads) - 1)]
        payload = {k: v for k, v in payload.items() if k not in ("tool", "args")}
        return ModelResponse(parts=[ToolCallPart(tool_name=name, args=json.dumps(payload))])

    return FunctionModel(fn)


def test_recon_fresh_challenge_and_file_type(env):
    _, session, _, ch, storage = env
    assert ch.first_interaction is None
    assert ch.status == ChallengeStatus.open

    p = storage.save(ch.id, "bin.elf", b"\x7fELF\x02\x01\x01\x00hello_flag")
    session.add(Attachment(
        challenge_id=ch.id, name="bin.elf",
        path=str(p.relative_to(storage.storage_dir)),
        mime="application/octet-stream", size=18, type=AttachmentType.download,
    ))
    session.commit()

    runner = ToolRunner(load_tools_config(), storage)
    digest = recon_bundle(runner, session, ch)
    assert digest.files[0]["file_type"] == "ELF binary"
    assert "ELF binary" in digest.summary
    staged = runner.sandbox_workdir(ch) / "bin.elf"
    assert staged.exists()
    assert detect_file_type(staged) == "ELF binary"


def test_tool_runner_docker_mode_argv(tmp_path, monkeypatch, env):
    _, session, _, ch, storage = env
    cfg_file = tmp_path / "tools.yaml"
    cfg_file.write_text(
        "defaults:\n  timeout_s: 30\n  output_limit_mb: 1\n  docker_image: kalilinux/kali-rolling\n"
        "tools:\n  nmap:\n    mode: docker\n    guard: strict\n",
        encoding="utf-8",
    )
    runner = ToolRunner(load_tools_config(cfg_file), storage)
    monkeypatch.setattr("app.tooling.runner.shutil.which", lambda name: "docker.exe" if name == "docker" else None)

    captured = {}

    def fake_run(argv, cwd, stdin=None, capture_output=True, timeout=None, creationflags=0):
        captured["argv"] = argv
        captured["stdin"] = stdin
        return SimpleNamespace(returncode=0, stdout=b"ok", stderr=b"")

    monkeypatch.setattr("app.tooling.runner.subprocess.run", fake_run)
    res = runner.run_raw(ch, "nmap", ["--top-ports", "10", "localhost"])
    assert res.stdout == "ok"
    assert captured["stdin"] == subprocess.DEVNULL
    assert captured["argv"][0] == "docker.exe"
    assert "kalilinux/kali-rolling" in captured["argv"]
    assert "-w" in captured["argv"] and "/work" in captured["argv"]
    assert captured["argv"][-4:] == ["nmap", "--top-ports", "10", "localhost"]


def test_jury_context_includes_operator_notes_and_personas_exist(env):
    _, session, _, ch, _ = env
    tracking.add_note(session, ch.id, "check the LSB of header.png")
    ctx = jury_context(session, ch)
    assert "Operator notes" in ctx
    assert "check the LSB of header.png" in ctx

    for cat in ("web", "pwn", "crypto", "reversing", "forensics", "osint"):
        txt = _persona_text(f"specialist_{cat}")
        assert "Escopo" in txt


def test_specialist_context_mentions_sandbox_cwd_file(env):
    _, session, _, ch, storage = env
    p = storage.save(ch.id, "secret.txt", b"abc")
    session.add(Attachment(
        challenge_id=ch.id, name="secret.txt",
        path=str(p.relative_to(storage.storage_dir)),
        mime="text/plain", size=3, type=AttachmentType.download,
    ))
    session.commit()
    ctx = specialist_context(session, ch, TaskOrder(category="crypto", task="decode"))
    assert "cwd_file=secret.txt" in ctx


def test_planner_limits_enforced_by_orchestrator(env):
    _, session, _, ch, storage = env
    runner = ToolRunner(load_tools_config(), storage)
    jury = JuryAgent(_scripted([
        {"action": "DISPATCH", "reason": "step 1",
         "order": {"category": "misc", "task": "t1"}, "findings": []},
        {"action": "DISPATCH", "reason": "step 2",
         "order": {"category": "misc", "task": "t2"}, "findings": []},
    ]), persona_text="jury")
    spec = SpecialistRunner(_scripted([
        {"status": "NEED_HINT", "findings": ["nope"]},
    ]), persona_text="spec")
    orch = Orchestrator(runner=runner, jury=jury, specialists={"misc": spec},
                        agents_limits={"max_attempts": 1, "max_minutes": 60})
    s0 = orch.start(session, ch.id)
    assert s0.state == "awaiting_approval"
    s1 = orch.approve(session, s0.id)
    assert s1.state == "gave_up"
    assert s1.final_report is not None
    assert s1.final_report.status == "gave_up"
    assert s1.dispatch_count == 1

    old_time = datetime.now(timezone.utc) - timedelta(minutes=65)
    assert check_stop_criteria(old_time, 0, {"max_attempts": 12, "max_minutes": 60}).stop is True


def test_specialist_enforces_max_tool_runs(env):
    _, session, _, ch, storage = env
    runner = ToolRunner(load_tools_config(), storage)
    payloads = [
        {"tool": "python", "args": ["-c", "print('one')"]},
        {"tool": "python", "args": ["-c", "print('two')"]},
        {"status": "NEED_HINT", "findings": ["stopped at budget"]},
    ]
    agent = SpecialistRunner(_scripted(payloads), persona_text="spec")
    report = agent.run(session, ch, TaskOrder(category="misc", task="x", max_tool_runs=1), runner=runner)
    assert report.status == "NEED_HINT"
    assert "budget" in report.findings[0]


def test_auto_mode_runs_full_loop_to_solved_and_persists(env):
    engine, session, _, ch, storage = env
    runner = ToolRunner(load_tools_config(), storage)
    jury = JuryAgent(_scripted([
        {"action": "DISPATCH", "reason": "go",
         "order": {"category": "crypto", "task": "solve"}, "findings": []},
        {"action": "SUBMIT", "reason": "found",
         "flag_candidate": "CTF{auto_ok}", "findings": ["solved"]},
    ]), persona_text="jury")
    spec = SpecialistRunner(_scripted([
        {"tool": "python", "args": ["-c", "print('CTF{auto_ok}')"]},
        {"status": "SOLVED", "flag_candidate": "CTF{auto_ok}", "findings": ["got flag"]},
    ]), persona_text="spec")

    orch = Orchestrator(runner=runner, jury=jury, specialists={"crypto": spec},
                        agents_limits={"max_attempts": 5, "max_minutes": 60})
    s = orch.start(session, ch.id, mode="auto")
    assert s.state == "solved"
    assert s.final_report is not None
    assert s.final_report.status == "solved"
    assert s.final_report.flag_candidate == "CTF{auto_ok}"

    # Simulate server restart: brand new Orchestrator hydrates from SQLite
    orch2 = Orchestrator(runner=runner, jury=jury, specialists={"crypto": spec})
    hydrated = orch2.get_session(session, s.id)
    assert hydrated is not None
    assert hydrated.state == "solved"
    assert hydrated.final_report.flag_candidate == "CTF{auto_ok}"
    assert orch2.latest_for_challenge(session, ch.id).id == s.id


def test_reject_on_terminal_session_raises_and_reassess_triggers_recon(env):
    _, session, _, ch, storage = env
    runner = ToolRunner(load_tools_config(), storage)
    jury = JuryAgent(_scripted([
        {"action": "GIVE_UP", "reason": "done", "findings": []},
    ]), persona_text="jury")
    orch = Orchestrator(runner=runner, jury=jury, specialists={})
    s = orch.start(session, ch.id)
    orch.terminate(session, s.id, "stop")
    with pytest.raises(ValueError):
        orch.reject(session, s.id, "late feedback")

    action, _ = reassess_strategy(
        datetime.now(timezone.utc), 2,
        [SimpleNamespace(status="FAILED"), SimpleNamespace(status="FAILED")],
        {"max_attempts": 10, "max_minutes": 60},
    )
    assert action == "RECON"


def test_forensics_multimodal_attaches_image(env):
    _, session, _, ch, storage = env
    png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    p = storage.save(ch.id, "clue.png", png_bytes)
    session.add(Attachment(
        challenge_id=ch.id, name="clue.png",
        path=str(p.relative_to(storage.storage_dir)),
        mime="image/png", size=len(png_bytes), type=AttachmentType.download,
    ))
    session.commit()

    seen_binary = []

    def fn(messages, info):
        for m in messages:
            for part in (m.parts or []):
                content = getattr(part, "content", None)
                if isinstance(content, list):
                    for item in content:
                        if isinstance(item, BinaryContent):
                            seen_binary.append(item.media_type)
        blob = "\n".join(str(part) for m in messages for part in (m.parts or []))
        match = re.search(r"['\"](\w*result\w*)['\"]", blob)
        name = match.group(1) if match else "final_result"
        return ModelResponse(parts=[ToolCallPart(
            tool_name=name,
            args=json.dumps({"status": "NEED_HINT", "findings": ["saw png"]}),
        )])

    runner = ToolRunner(load_tools_config(), storage)
    spec = SpecialistRunner(FunctionModel(fn), persona_text="forensics")
    rep = spec.run(session, ch, TaskOrder(category="forensics", task="inspect image"), runner=runner)
    assert rep.status == "NEED_HINT"
    assert seen_binary == ["image/png"]


def test_run_with_fallback_on_503():
    calls = []

    def fn(model):
        calls.append(model)
        if model == "primary":
            raise RuntimeError("HTTP 503 Service Unavailable")
        return "ok-fallback"

    assert run_with_fallback(["primary", "backup"], fn) == "ok-fallback"
    assert calls == ["primary", "backup"]


def test_run_with_fallback_on_unexpected_model_behavior():
    from pydantic_ai.exceptions import UnexpectedModelBehavior

    calls = []

    def fn(model):
        calls.append(model)
        if model == "primary":
            raise UnexpectedModelBehavior("Exceeded maximum output retries (4)")
        return "ok-recovered"

    assert run_with_fallback(["primary", "backup"], fn) == "ok-recovered"
    assert calls == ["primary", "backup"]


def test_background_start_and_new_endpoints(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'bg.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    storage = StorageService(tmp_path / "storage")

    import app.agents.api as agent_api

    def _fake_orch(storage_dir):
        runner = ToolRunner(load_tools_config(), storage)
        jury = JuryAgent(_scripted([
            {"action": "DISPATCH", "reason": "r",
             "order": {"category": "web", "task": "check"}, "findings": []},
        ]), persona_text="jury")
        return Orchestrator(runner=runner, jury=jury, specialists={})

    monkeypatch.setattr(agent_api, "_make_orchestrator", _fake_orch)
    app = create_app(engine=engine, storage_dir=tmp_path / "storage")
    with TestClient(app) as c:
        with Session(engine) as s:
            ev = Event(name="e")
            s.add(ev)
            s.commit()
            s.refresh(ev)
            c1 = Challenge(event_id=ev.id, title="c1", points=500)
            c2 = Challenge(event_id=ev.id, title="c2", points=100)
            s.add(c1)
            s.add(c2)
            s.commit()
            s.refresh(c1)
            s.refresh(c2)
            ev_id, c1_id, c2_id = ev.id, c1.id, c2.id

        # Rank & inventory endpoints
        r_rank = c.get(f"/api/agent/events/{ev_id}/rank")
        assert r_rank.status_code == 200
        assert r_rank.json()[0]["title"] == "c1"

        r_inv = c.get("/api/agent/tools/inventory")
        assert r_inv.status_code == 200
        assert any(row["name"] == "python" for row in r_inv.json())

        # Background start via API
        r_bg = c.post(f"/api/agent/{c1_id}/start", json={"mode": "supervised", "background": True})
        assert r_bg.status_code == 201
        sid = r_bg.json()["id"]

        for _ in range(40):
            cur = c.get(f"/api/agent/sessions/{sid}").json()
            if cur["state"] != "running":
                break
            time.sleep(0.05)
        assert cur["state"] == "awaiting_approval"

        # Cross-challenge web guard returns 404
        assert c.post(f"/challenges/{c2_id}/agent/sessions/{sid}/approve").status_code == 404

        # REST terminate endpoint
        r_term = c.post(f"/api/agent/sessions/{sid}/terminate", json={"reason": "done"})
        assert r_term.status_code == 200
        assert r_term.json()["state"] == "rejected"


def test_switch_checkpoint_to_full_auto_and_quick_auto_redirect(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'fa.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    storage = StorageService(tmp_path / "storage")

    import app.agents.api as agent_api

    def _fake_orch(storage_dir):
        runner = ToolRunner(load_tools_config(), storage)
        jury = JuryAgent(_scripted([
            {"action": "DISPATCH", "reason": "step 1",
             "order": {"category": "crypto", "task": "crack"}, "findings": []},
            {"action": "SUBMIT", "reason": "flag found",
             "flag_candidate": "CTF{full_auto_win}", "findings": ["flag ok"]},
        ]), persona_text="jury")
        spec = SpecialistRunner(_scripted([
            {"status": "SOLVED", "flag_candidate": "CTF{full_auto_win}", "findings": ["solved"]},
        ]), persona_text="spec")
        return Orchestrator(runner=runner, jury=jury, specialists={"crypto": spec})

    monkeypatch.setattr(agent_api, "_make_orchestrator", _fake_orch)
    app = create_app(engine=engine, storage_dir=tmp_path / "storage")
    with TestClient(app) as c:
        ev = c.post("/api/events", json={"name": "e"}).json()
        ch = c.post(f"/api/events/{ev['id']}/challenges", json={"title": "c1"}).json()

        # Start supervised -> stops at checkpoint
        r_start = c.post(f"/api/agent/{ch['id']}/start")
        sid = r_start.json()["id"]
        assert r_start.json()["state"] == "awaiting_approval"

        # Approve and switch to Full Auto via panel route -> finishes automatically
        r_auto = c.post(
            f"/challenges/{ch['id']}/agent/sessions/{sid}/approve",
            data={"mode": "auto", "background": "false"},
        )
        assert r_auto.status_code == 200
        assert "solved" in r_auto.text
        assert "CTF{full_auto_win}" in r_auto.text

        # Quick create with go_to=agent_auto starts full auto in background automatically
        r_quick = c.post(
            "/challenges/quick",
            data={"event_id": str(ev["id"]), "title": "q1", "category": "crypto", "go_to": "agent_auto"},
            follow_redirects=False,
        )
        assert r_quick.status_code == 303
        q_ch_id = int(r_quick.headers["location"].split("/")[2])

        for _ in range(40):
            side_html = c.get(f"/challenges/{q_ch_id}/agent/side").text
            if "solved" in side_html:
                break
            time.sleep(0.05)
        assert "solved" in side_html


def test_terminate_stops_running_full_auto_loop(env):
    _, session, _, ch, storage = env
    runner = ToolRunner(load_tools_config(), storage)
    orch_holder = {}

    class SlowJury:
        def decide(self, session, challenge, reports, extra_feedback=None):
            active = orch_holder["orch"].latest_for_challenge(session, challenge.id)
            orch_holder["orch"].terminate(session, active.id, "operator stop")
            from app.agents.payloads import JuryDecision
            return JuryDecision(action="RECHECK", reason="late", findings=[])

    orch = Orchestrator(runner=runner, jury=SlowJury(), specialists={})
    orch_holder["orch"] = orch
    sess = orch.start(session, ch.id, mode="auto")
    assert sess.state == "rejected"
    assert sess.pending_decision is None


def test_dispatch_raises_tool_budget_floor_for_heavy_categories(env):
    _, session, _, ch, storage = env
    runner = ToolRunner(load_tools_config(), storage)
    seen_orders = []

    class CaptureSpec:
        def run(self, session, challenge, order, runner=None):
            seen_orders.append(order)
            from app.agents.payloads import SpecialistReport
            return SpecialistReport(status="NEED_HINT", findings=["ok"])

    jury = JuryAgent(_scripted([
        {"action": "DISPATCH", "reason": "rev",
         "order": {"category": "rev", "task": "analyze binary", "max_tool_runs": 5}, "findings": []},
    ]), persona_text="jury")
    orch = Orchestrator(runner=runner, jury=jury, specialists={"reversing": CaptureSpec()})
    s0 = orch.start(session, ch.id, mode="supervised")
    orch.approve(session, s0.id, mode="supervised")
    assert len(seen_orders) == 1
    assert seen_orders[0].max_tool_runs == 8


def test_fast_path_blocks_fabricated_flag_when_tools_ran_without_literal(env):
    _, session, _, ch, storage = env
    runner = ToolRunner(load_tools_config(), storage)
    jury = JuryAgent(_scripted([
        {"action": "DISPATCH", "reason": "r1",
         "order": {"category": "reversing", "task": "derive key"}, "findings": []},
        {"action": "DISPATCH", "reason": "query api with key",
         "order": {"category": "reversing", "task": "post /api/step"}, "findings": []},
        {"action": "SUBMIT", "reason": "server returned flag",
         "flag_candidate": "CTF{real_server_flag}", "findings": ["api returned literal"]},
    ]), persona_text="jury")
    spec = SpecialistRunner(_scripted([
        # Dispatch 1: tool outputs bare hex, specialist wraps it as CTF{...} and says next_step="to confirm the flag"
        {"tool": "python", "args": ["-c", "print('DERIVED KEY: deadbeef')"]},
        {"status": "SOLVED", "flag_candidate": "CTF{deadbeef}",
         "findings": ["derived key deadbeef"],
         "next_step": "Submit derived key to obtain/confirm the flag."},
        # Dispatch 2: tool actually prints the real literal flag
        {"tool": "python", "args": ["-c", "print('CTF{real_server_flag}')"]},
        {"status": "SOLVED", "flag_candidate": "CTF{real_server_flag}",
         "findings": ["api returned CTF{real_server_flag}"]},
    ]), persona_text="spec")

    orch = Orchestrator(runner=runner, jury=jury, specialists={"reversing": spec})
    s = orch.start(session, ch.id, mode="auto")
    assert s.state == "solved"
    assert s.dispatch_count == 2
    assert s.final_report.flag_candidate == "CTF{real_server_flag}"
    assert any("fast-path blocked" in f for f in s.findings)
