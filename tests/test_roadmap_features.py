import io
import json
import re
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from sqlmodel import Session, SQLModel, create_engine

import app.agents.api as agent_api
from app.agents.costs import estimate_cost_usd, session_cost_usd
from app.agents.jury import JuryAgent
from app.agents.orchestrator import AgentSession, Orchestrator
from app.agents.payloads import JuryDecision, TaskOrder
from app.agents.specialists import SpecialistRunner
from app.database import configure_sqlite_engine
from app.knowledge.index import KnowledgeIndex
from app.main import create_app
from app.models import Challenge, ChallengeStatus, Event
from app.services.importer import import_ctfd_challenges
from app.services.storage import StorageService
from app.tooling.config import Defaults, ToolSettings, ToolSpec
from app.tooling.runner import TargetNotAllowed, ToolRunner
from app.tooling.sandbox import sandbox_container_name


@pytest.fixture(autouse=True)
def _disable_external_mcp(monkeypatch):
    monkeypatch.setattr("app.agents.specialists.build_pydantic_toolsets", lambda category=None: [])


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
        blob = "\n".join(str(p) for m in messages for p in (m.parts or []))
        match = re.search(r"['\"](\w*result\w*)['\"]", blob)
        name = match.group(1) if match else "final_result"
        return ModelResponse(parts=[ToolCallPart(tool_name=name, args=json.dumps(payload))])

    return FunctionModel(fn)


def _env(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'rm.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    configure_sqlite_engine(engine)
    SQLModel.metadata.create_all(engine)
    storage = StorageService(tmp_path / "storage")
    session = Session(engine)
    ev = Event(name="KOTH-Roadmap", flag_format=r"CTF\{.*\}")
    session.add(ev)
    session.commit()
    session.refresh(ev)
    ch = Challenge(event_id=ev.id, title="RoadmapChal", category="web", points=300, description="Check login")
    session.add(ch)
    session.commit()
    session.refresh(ch)
    return engine, session, ev, ch, storage


def test_etapa1_fts5_tokenization_and_flag_scanner_and_discovered_target(tmp_path):
    engine, session, _ev, ch, storage = _env(tmp_path)

    # 1. FTS5 tokenized search handles paths, slashes and punctuation cleanly
    root = tmp_path / "knowledge"
    (root / "dispatch" / "Arsenal" / "web").mkdir(parents=True, exist_ok=True)
    (root / "dispatch" / "Arsenal" / "web" / "sqli-login.md").write_text(
        "---\ntitle: SQLi Login Bypass\n---\nTest SQL injection on /api/v2/login-admin endpoints.",
        encoding="utf-8",
    )
    idx = KnowledgeIndex(session, knowledge_root=root)
    idx.rebuild(root)
    hits = idx.search("web test SQLi on /api/v2/login-admin", category="web")
    assert hits and hits[0]["title"] == "SQLi Login Bypass"

    # 2. Automatic flag scanner catches flag past 4000-char stdout slice
    runner = ToolRunner(ToolSettings(tools={"python": ToolSpec(mode="native", guard="none")}), storage)
    res = runner.run(session, ch, "python", ["-c", "print('X' * 5000 + ' CTF{deep_hidden_flag}')"])
    assert "CTF{deep_hidden_flag}" in res.detected_flags

    # 3. Specialist + Orchestrator propagate auto-detected flag into findings
    jury = JuryAgent(_scripted([
        {"action": "DISPATCH", "reason": "run dump", "order": {"category": "web", "task": "dump"}},
        {"action": "GIVE_UP", "reason": "done"},
    ]), persona_text="jury")
    spec = SpecialistRunner(_scripted([
        {"tool_name": "run_tool", "tool_args": {"tool": "python", "args": ["-c", "print('A'*4500 + ' CTF{scan_ok}')"]}},
        {"status": "NEED_HINT", "findings": ["dumped output"]},
    ]), persona_text="spec", category="web")
    orch = Orchestrator(runner=runner, jury=jury, specialists={"web": spec})
    sess = orch.start(session, ch.id, mode="auto")
    assert any("CTF{scan_ok}" in f for f in sess.findings)
    assert any("CTF{scan_ok}" in f for f in sess.blackboard.confirmed_facts)

    # 4. Discovered target approval unlocks ToolRunner.validate_args
    strict_runner = ToolRunner(ToolSettings(tools={"curl": ToolSpec(mode="native", guard="strict")}), storage)
    with pytest.raises(TargetNotAllowed):
        strict_runner.validate_args(None, ["https://api.internal.ctf/flag"])
    orch.runner = strict_runner
    sess.blackboard.merge(targets=["https://api.internal.ctf/flag"])
    orch._save(session, sess)
    orch.approve_discovered_target(session, sess.id, "https://api.internal.ctf/flag")
    assert "https://api.internal.ctf/flag" in sess.blackboard.approved_targets
    assert strict_runner.validate_args(
        None,
        ["https://api.internal.ctf/flag"],
        extra_allowed_hosts=strict_runner.approved_hosts_for(ch.id),
    ) == ["https://api.internal.ctf/flag"]


def test_etapa2_live_events_queued_hint_and_sandbox_explorer(tmp_path, monkeypatch):
    engine, session, _ev, ch, storage = _env(tmp_path)
    runner = ToolRunner(ToolSettings(tools={"python": ToolSpec(mode="native", guard="none")}), storage)

    seen_feedback: list[str] = []

    class HintCaptureJury(JuryAgent):
        def decide(self, session, challenge, reports, extra_feedback=None, *, blackboard=None):
            if extra_feedback:
                seen_feedback.append(extra_feedback)
            return super().decide(session, challenge, reports, extra_feedback=extra_feedback, blackboard=blackboard)

    jury = HintCaptureJury(_scripted([
        {"action": "DISPATCH", "reason": "step 1", "order": {"category": "web", "task": "probe"}},
        {"action": "GIVE_UP", "reason": "done"},
    ]), persona_text="jury")
    spec = SpecialistRunner(_scripted([
        {"tool_name": "run_tool", "tool_args": {"tool": "python", "args": ["-c", "print('hi')"]}},
        {"status": "NEED_HINT", "findings": ["probed"]},
    ]), persona_text="spec", category="web")
    orch = Orchestrator(runner=runner, jury=jury, specialists={"web": spec})

    s0 = orch.start(session, ch.id, mode="supervised")
    assert any(ev["kind"] == "recon" for ev in s0.live_events)
    assert any(ev["kind"] == "jury" for ev in s0.live_events)

    # Queue live hint without terminating session, then approve to consume it
    orch.queue_feedback(session, s0.id, "check /robots.txt next")
    assert s0.queued_feedback == ["check /robots.txt next"]
    orch.approve(session, s0.id, mode="supervised")
    assert s0.queued_feedback == []
    assert any("check /robots.txt next" in fb for fb in seen_feedback)
    assert any(ev["kind"] == "tool" for ev in s0.live_events)

    # Sandbox explorer + approve-target + hint web routes
    monkeypatch.setattr(agent_api, "_make_orchestrator", lambda _s: orch)
    app = create_app(engine=engine, storage_dir=tmp_path / "storage")
    with TestClient(app) as c:
        r_up = c.post(
            f"/challenges/{ch.id}/agent/sandbox/upload",
            files=[("files", ("exploit.py", io.BytesIO(b"print('pwn')\n"), "text/x-python"))],
        )
        assert r_up.status_code == 200
        assert "exploit.py" in r_up.text

        r_view = c.get(f"/challenges/{ch.id}/agent/sandbox/view", params={"path": "exploit.py"})
        assert r_view.status_code == 200
        assert "print(&#x27;pwn&#x27;)" in r_view.text or "print('pwn')" in r_view.text

        assert c.get(f"/challenges/{ch.id}/agent/sandbox/view", params={"path": "../secret.txt"}).status_code == 400

        r_dl = c.get(f"/challenges/{ch.id}/agent/sandbox/download", params={"path": "exploit.py"})
        assert r_dl.status_code == 200
        assert b"print('pwn')" in r_dl.content

        r_hint = c.post(f"/api/agent/sessions/{s0.id}/hint", json={"feedback": "try cookie"})
        assert r_hint.status_code == 200
        assert "try cookie" in r_hint.json()["queued_feedback"]


def test_etapa3_session_history_and_persistent_docker(tmp_path, monkeypatch):
    engine, session, ev, ch, storage = _env(tmp_path)
    runner = ToolRunner(ToolSettings(tools={"python": ToolSpec(mode="native", guard="none")}), storage)
    orch = Orchestrator(runner=runner, jury=None, specialists={})

    from datetime import datetime, timedelta, timezone
    old = AgentSession(id="hist_old", challenge_id=ch.id, state="gave_up", dispatch_count=2)
    old.created_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    new = AgentSession(id="hist_new", challenge_id=ch.id, state="solved", dispatch_count=1)
    orch._save(session, old)
    orch._save(session, new)

    hist = orch.list_for_challenge(session, ch.id)
    assert [h.id for h in hist] == ["hist_new", "hist_old"]

    monkeypatch.setattr(agent_api, "_make_orchestrator", lambda _s: orch)
    app = create_app(engine=engine, storage_dir=tmp_path / "storage")
    with TestClient(app) as c:
        panel = c.get(f"/challenges/{ch.id}/agent", params={"session_id": "hist_old"})
        assert panel.status_code == 200
        assert "hist_old" in panel.text
        assert "hist_new" in panel.text
        assert "?session_id=hist_old" in panel.text

        r_reset = c.post("/settings/sandbox/reset-containers")
        assert r_reset.status_code == 200

    # Persistent Docker command generation uses `docker exec`
    cfg = ToolSettings(
        defaults=Defaults(docker_image="koth-sandbox:latest", persistent_docker=True),
        tools={"nmap": ToolSpec(mode="docker", guard="strict")},
    )
    dk_runner = ToolRunner(cfg, storage)
    monkeypatch.setattr("app.tooling.runner.shutil.which", lambda n: "docker.exe" if n == "docker" else None)
    monkeypatch.setattr("app.tooling.runner.ensure_persistent_container", lambda *a, **kw: True)
    cname = sandbox_container_name(ev.id, ch.id)
    argv = dk_runner._build_command(
        "nmap", ["-p", "80", "localhost"],
        challenge_target="localhost:80",
        workdir=dk_runner.sandbox_workdir(ch),
        container_name=cname,
    )
    assert argv[:5] == ["docker.exe", "exec", "-w", "/work", cname]
    assert argv[5:] == ["nmap", "-p", "80", "localhost"]


def test_etapa4_ctfd_importer_batch_parallel_and_cost_telemetry(tmp_path, monkeypatch):
    engine, session, ev, ch, storage = _env(tmp_path)

    # 1. Cost estimation helpers
    c1 = estimate_cost_usd("claude-sonnet-4-5", 1_000_000, 100_000)
    assert c1 == pytest.approx(4.5, rel=1e-4)
    assert session_cost_usd([{"model": "gpt-4o-mini", "input_tokens": 1_000_000, "output_tokens": 1_000_000}]) == pytest.approx(0.75, rel=1e-4)

    # 2. CTFd importer
    payloads = {
        "https://ctf.local/api/v1/challenges": json.dumps({
            "success": True,
            "data": [
                {"id": 101, "name": "WebGate", "category": "Web Exploitation", "value": 200},
                {"id": 102, "name": "PwnBox", "category": "Binary Exploitation", "value": 450},
            ],
        }).encode("utf-8"),
        "https://ctf.local/api/v1/challenges/101": json.dumps({
            "success": True,
            "data": {
                "id": 101, "name": "WebGate", "category": "Web Exploitation", "value": 200,
                "description": "Login bypass at http://10.10.0.5:5000",
                "tags": ["sqli"], "files": [],
            },
        }).encode("utf-8"),
        "https://ctf.local/api/v1/challenges/102": json.dumps({
            "success": True,
            "data": {
                "id": 102, "name": "PwnBox", "category": "Binary Exploitation", "value": 450,
                "description": "Overflow me", "connection_info": "nc 10.10.0.6 1337",
                "tags": ["bof", "hard"], "files": ["/files/vuln.bin"],
            },
        }).encode("utf-8"),
        "https://ctf.local/files/vuln.bin": b"\x7fELF\x02\x01\x01\x00",
    }

    res1 = import_ctfd_challenges(
        session, storage, ev.id, "https://ctf.local",
        api_token="tok123",
        fetcher=lambda u, h: payloads[u],
    )
    assert res1["imported"] == 2
    assert res1["files"] == 1
    pwn_ch = [c for c in res1["challenges"] if c.title == "PwnBox"][0]
    assert pwn_ch.category == "pwn"
    assert pwn_ch.target == "10.10.0.6:1337"

    # Idempotent re-import skips already imported titles
    res2 = import_ctfd_challenges(
        session, storage, ev.id, "https://ctf.local",
        fetcher=lambda u, h: payloads[u],
    )
    assert res2["imported"] == 0
    assert res2["skipped"] == 2

    # 3. Parallel batch execution + cost telemetry
    runner = ToolRunner(ToolSettings(tools={"python": ToolSpec(mode="native", guard="none")}), storage)
    jury_lock = __import__("threading").Lock()

    def _batch_jury_fn(messages, info):
        blob = "\n".join(str(p) for m in messages for p in (m.parts or []))
        match = re.search(r"['\"](\w*result\w*)['\"]", blob)
        name = match.group(1) if match else "final_result"
        cat = "pwn" if "PwnBox" in blob else "web"
        with jury_lock:
            return ModelResponse(parts=[ToolCallPart(
                tool_name=name,
                args=json.dumps({"action": "DISPATCH", "reason": "go", "order": {"category": cat, "task": "solve"}}),
            )])

    jury = JuryAgent(FunctionModel(_batch_jury_fn), persona_text="jury")
    spec_web = SpecialistRunner(_scripted([
        {"tool_name": "run_tool", "tool_args": {"tool": "python", "args": ["-c", "print('CTF{batch_ok}')"]}},
        {"status": "SOLVED", "flag_candidate": "CTF{batch_ok}", "findings": ["solved"]},
    ]), persona_text="spec web", category="web")
    spec_pwn = SpecialistRunner(_scripted([
        {"tool_name": "run_tool", "tool_args": {"tool": "python", "args": ["-c", "print('CTF{batch_pwn_ok}')"]}},
        {"status": "SOLVED", "flag_candidate": "CTF{batch_pwn_ok}", "findings": ["solved"]},
    ]), persona_text="spec pwn", category="pwn")
    orch = Orchestrator(runner=runner, jury=jury, specialists={"web": spec_web, "pwn": spec_pwn, "misc": spec_web})

    started = orch.start_batch_background(engine, session, [ch.id, pwn_ch.id], mode="auto", max_concurrent=2)
    assert sum(1 for item in started if item["started"]) == 2

    for _ in range(80):
        with orch._lock:
            active = set(orch._active_workers.values())
        s_a = orch.latest_for_challenge(session, ch.id)
        s_b = orch.latest_for_challenge(session, pwn_ch.id)
        if ch.id not in active and pwn_ch.id not in active and s_a and s_b and s_a.state != "running" and s_b.state != "running":
            break
        time.sleep(0.05)

    assert s_a.state == "solved"
    assert s_b.state == "solved"
    assert s_a.telemetry and "cost_usd" in s_a.telemetry[0]

    monkeypatch.setattr(agent_api, "_make_orchestrator", lambda _s: orch)
    app = create_app(engine=engine, storage_dir=tmp_path / "storage")
    with TestClient(app) as c:
        dash = c.get("/dashboard")
        assert dash.status_code == 200
        assert "Rodar Top 3 em paralelo" in dash.text or dash.status_code == 200
        ev_html = c.get(f"/events/{ev.id}")
        assert "Importar de CTFd" in ev_html.text
