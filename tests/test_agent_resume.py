import json
import re
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine

import app.agents.api as agent_api
from app.agents.cli_op import process_cli_turn
from app.agents.jury import JuryAgent
from app.agents.orchestrator import AgentSession, Orchestrator
from app.agents.payloads import JuryDecision, SpecialistReport, TaskOrder
from app.agents.writeup import build_challenge_writeup
from app.agents.specialists import SpecialistRunner
from app.database import configure_sqlite_engine, ensure_schema
from app.main import create_app
from app.models import Challenge, ChallengeStatus, Event, Note
from app.services import tracking
from app.services.storage import StorageService
from app.templates import badge_for, report_status_label, state_label
from app.tooling.config import ToolSettings, ToolSpec
from app.tooling.runner import ToolRunner


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
        f"sqlite:///{tmp_path / 'resume.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    configure_sqlite_engine(engine)
    SQLModel.metadata.create_all(engine)
    storage = StorageService(tmp_path / "storage")
    session = Session(engine)
    ev = Event(name="KOTH-Resume", flag_format=r"CTF\{.*\}")
    session.add(ev)
    session.commit()
    session.refresh(ev)
    ch = Challenge(event_id=ev.id, title="ResumeChal", category="web", description="Find secret flag")
    session.add(ch)
    session.commit()
    session.refresh(ch)
    return engine, session, ev, ch, storage


def test_resume_after_max_attempts_preserves_state_and_solves(tmp_path):
    engine, session, _ev, ch, storage = _env(tmp_path)
    runner = ToolRunner(ToolSettings(tools={"python": ToolSpec(mode="native", guard="none")}), storage)

    seen_prompts: list[str] = []

    class CapturingJury(JuryAgent):
        def decide(self, session, challenge, reports, extra_feedback=None, *, blackboard=None):
            seen_prompts.append(
                json.dumps({"extra": extra_feedback, "blackboard": blackboard, "reports": len(reports)})
            )
            return super().decide(session, challenge, reports, extra_feedback=extra_feedback, blackboard=blackboard)

    jury = CapturingJury(_scripted([
        {"action": "DISPATCH", "reason": "step 1",
         "order": {"category": "web", "task": "probe /"},
         "findings": ["server up"], "dead_ends": ["robots empty"], "hypotheses": ["check /admin"]},
        {"action": "DISPATCH", "reason": "step 2 after hint",
         "order": {"category": "web", "task": "probe /admin"},
         "findings": ["operator hint applied"]},
    ]), persona_text="jury")
    spec = SpecialistRunner(_scripted([
        {"tool_name": "run_tool", "tool_args": {"tool": "python", "args": ["-c", "print('404 on /')"]}},
        {"status": "NEED_HINT", "findings": ["root gave 404"], "dead_ends": ["/ is empty"]},
        {"tool_name": "run_tool", "tool_args": {"tool": "python", "args": ["-c", "print('CTF{resumed_after_limit}')"]}},
        {"status": "SOLVED", "flag_candidate": "CTF{resumed_after_limit}", "findings": ["found flag at /admin"]},
    ]), persona_text="spec", category="web")

    orch = Orchestrator(runner=runner, jury=jury, specialists={"web": spec},
                        agents_limits={"max_attempts": 1, "max_minutes": 60})
    s0 = orch.start(session, ch.id, mode="auto")
    assert s0.state == "gave_up"
    assert s0.dispatch_count == 1
    assert s0.final_report is not None and s0.final_report.status == "gave_up"
    assert "server up" in s0.findings
    assert len(s0.telemetry) == 1

    # Resume with extra_attempts and hint -> solves on dispatch 2
    s1 = orch.resume(session, s0.id, mode="auto", feedback="try /admin", extra_attempts=3)
    assert s1.id == s0.id
    assert s1.state == "solved"
    assert s1.resume_count == 1
    assert s1.extra_attempts == 3
    assert s1.budget_started_at is not None
    assert s1.dispatch_count == 2
    assert len(s1.reports) == 2
    assert len(s1.telemetry) == 2
    assert "server up" in s1.findings
    assert "robots empty" in s1.blackboard.dead_ends
    assert s1.final_report is not None
    assert s1.final_report.flag_candidate == "CTF{resumed_after_limit}"

    # CapturingJury saw the operator hint and preserved blackboard on resume
    resumed_prompt = json.loads(seen_prompts[1])
    assert "try /admin" in resumed_prompt["extra"]
    assert "robots empty" in resumed_prompt["blackboard"]["dead_ends"]
    assert resumed_prompt["reports"] == 1


def test_resume_after_max_minutes_resets_time_window(tmp_path):
    engine, session, _ev, ch, storage = _env(tmp_path)
    runner = ToolRunner(ToolSettings(tools={"python": ToolSpec(mode="native", guard="none")}), storage)
    jury = JuryAgent(_scripted([
        {"action": "DISPATCH", "reason": "go",
         "order": {"category": "web", "task": "solve now"}},
    ]), persona_text="jury")
    spec = SpecialistRunner(_scripted([
        {"tool_name": "run_tool", "tool_args": {"tool": "python", "args": ["-c", "print('CTF{time_window_ok}')"]}},
        {"status": "SOLVED", "flag_candidate": "CTF{time_window_ok}", "findings": ["solved"]},
    ]), persona_text="spec", category="web")
    orch = Orchestrator(runner=runner, jury=jury, specialists={"web": spec},
                        agents_limits={"max_attempts": 10, "max_minutes": 30})

    # Simulate an old session that expired due to max_minutes
    s = AgentSession(
        id="expired1",
        challenge_id=ch.id,
        state="gave_up",
        mode="auto",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=120),
    )
    orch._save(session, s)

    resumed = orch.resume(session, "expired1", mode="auto", feedback="fresh window")
    assert resumed.state == "solved"
    assert resumed.budget_started_at is not None
    assert resumed.final_report.flag_candidate == "CTF{time_window_ok}"


def test_resume_after_jury_give_up_clears_dead_ends_reopens_challenge_and_writeup(tmp_path):
    engine, session, _ev, ch, storage = _env(tmp_path)
    tracking.update_challenge(session, ch.id, status=ChallengeStatus.gave_up)
    runner = ToolRunner(ToolSettings(tools={"python": ToolSpec(mode="native", guard="none")}), storage)

    jury = JuryAgent(_scripted([
        {"action": "GIVE_UP", "reason": "stuck", "findings": ["dead"], "dead_ends": ["old path"]},
        {"action": "DISPATCH", "reason": "retry new path",
         "order": {"category": "web", "task": "hit /v2"}},
    ]), persona_text="jury")
    spec = SpecialistRunner(_scripted([
        {"tool_name": "run_tool", "tool_args": {"tool": "python", "args": ["-c", "print('CTF{reopened_ok}')"]}},
        {"status": "SOLVED", "flag_candidate": "CTF{reopened_ok}", "findings": ["v2 worked"]},
    ]), persona_text="spec", category="web")
    orch = Orchestrator(runner=runner, jury=jury, specialists={"web": spec},
                        agents_limits={"max_attempts": 5, "max_minutes": 60})

    s0 = orch.start(session, ch.id, mode="auto")
    assert s0.state == "gave_up"
    assert "old path" in s0.blackboard.dead_ends

    s1 = orch.resume(
        session, s0.id,
        mode="auto",
        feedback="endpoint moved to /v2",
        new_target="http://127.0.0.1:9001",
        clear_dead_ends=True,
    )
    session.refresh(ch)
    assert ch.status == ChallengeStatus.solved
    assert ch.target == "http://127.0.0.1:9001"
    assert s1.state == "solved"
    assert s1.resume_count == 1
    assert "old path" not in s1.blackboard.dead_ends
    assert "http://127.0.0.1:9001" in s1.blackboard.discovered_targets

    notes = tracking.note_history(session, ch.id)
    assert any("[agent resumed #1]" in n.content for n in notes)

    wu = build_challenge_writeup(session, ch, agent_session=s1, storage=storage)
    assert "retomadas=1" in wu


def test_resume_after_terminate_and_crash_sync_and_background(tmp_path):
    engine, session, _ev, ch, storage = _env(tmp_path)
    runner = ToolRunner(ToolSettings(tools={"python": ToolSpec(mode="native", guard="none")}), storage)
    jury = JuryAgent(_scripted([
        {"action": "DISPATCH", "reason": "initial", "order": {"category": "web", "task": "init"}},
        {"action": "DISPATCH", "reason": "after terminate", "order": {"category": "web", "task": "solve"}},
        {"action": "DISPATCH", "reason": "after crash", "order": {"category": "web", "task": "solve2"}},
    ]), persona_text="jury")
    spec = SpecialistRunner(_scripted([
        {"tool_name": "run_tool", "tool_args": {"tool": "python", "args": ["-c", "print('CTF{bg_resume_ok}')"]}},
        {"status": "SOLVED", "flag_candidate": "CTF{bg_resume_ok}", "findings": ["ok"]},
    ]), persona_text="spec", category="web")
    orch = Orchestrator(runner=runner, jury=jury, specialists={"web": spec})

    s0 = orch.start(session, ch.id, mode="supervised")
    orch.terminate(session, s0.id, "wrong direction")
    assert s0.state == "rejected"
    assert s0.stop_requested is True

    # approve() must still fail on terminated sessions
    with pytest.raises(ValueError):
        orch.approve(session, s0.id)

    # Sync resume clears stop_requested and continues in supervised mode
    s1 = orch.resume(session, s0.id, mode="supervised", feedback="go ahead")
    assert s1.state == "awaiting_approval"
    assert s1.stop_requested is False
    assert s1.resume_count == 1

    # Simulate crashed background session (rejected + error) and resume in background
    s1.state = "rejected"
    s1.error = "RuntimeError: transient crash"
    s1.pending_decision = None
    orch._save(session, s1)

    s2 = orch.resume_background(engine, session, s1.id, mode="auto", feedback="retry in bg")
    assert s2.resume_count == 2
    assert s2.error is None

    for _ in range(60):
        cur = orch.get_session(session, s1.id)
        if cur.state != "running":
            break
        time.sleep(0.05)
    assert cur.state == "solved"
    assert cur.final_report.flag_candidate == "CTF{bg_resume_ok}"


def test_resume_rejects_invalid_states_and_unknown_session(tmp_path):
    engine, session, _ev, ch, storage = _env(tmp_path)
    orch = Orchestrator(runner=None, jury=None, specialists={})

    with pytest.raises(KeyError):
        orch.resume(session, "does-not-exist")

    for bad_state in ("solved", "running", "awaiting_approval"):
        sid = f"sess-{bad_state}"
        orch.sessions[sid] = AgentSession(id=sid, challenge_id=ch.id, state=bad_state)
        with pytest.raises(ValueError):
            orch.resume(session, sid)


def test_resume_persistence_and_schema_migration(tmp_path):
    # Verify ensure_schema adds new AgentSessionRecord columns on a legacy table
    legacy_engine = create_engine(
        f"sqlite:///{tmp_path / 'legacy.db'}",
        connect_args={"check_same_thread": False},
    )
    with legacy_engine.connect() as conn:
        conn.execute(text(
            "CREATE TABLE agentsessionrecord ("
            " id VARCHAR PRIMARY KEY,"
            " challenge_id INTEGER NOT NULL,"
            " state VARCHAR NOT NULL,"
            " mode VARCHAR NOT NULL,"
            " dispatch_count INTEGER NOT NULL,"
            " pending_decision_json VARCHAR,"
            " findings_json VARCHAR NOT NULL,"
            " reports_json VARCHAR NOT NULL,"
            " final_report_json VARCHAR,"
            " blackboard_json VARCHAR NOT NULL,"
            " telemetry_json VARCHAR NOT NULL,"
            " error VARCHAR,"
            " total_tokens INTEGER NOT NULL,"
            " created_at DATETIME NOT NULL,"
            " updated_at DATETIME NOT NULL)"
        ))
        conn.execute(text(
            "INSERT INTO agentsessionrecord VALUES "
            "('leg1', 1, 'gave_up', 'auto', 2, NULL, '[]', '[]', NULL, '{}', '[]', NULL, 10, "
            "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
        ))
        conn.commit()

    ensure_schema(legacy_engine)
    orch = Orchestrator(runner=None, jury=None, specialists={})
    with Session(legacy_engine) as s:
        hydrated = orch.get_session(s, "leg1")
        assert hydrated is not None
        assert hydrated.resume_count == 0
        assert hydrated.extra_attempts == 0
        assert hydrated.budget_started_at is None

    # Verify fresh Orchestrator hydrates resume_count, extra_attempts, budget_started_at after resume
    engine, session, _ev, ch, storage = _env(tmp_path)
    runner = ToolRunner(ToolSettings(tools={"python": ToolSpec(mode="native", guard="none")}), storage)
    jury = JuryAgent(_scripted([
        {"action": "DISPATCH", "reason": "go", "order": {"category": "web", "task": "check"}},
    ]), persona_text="jury")
    orch1 = Orchestrator(runner=runner, jury=jury, specialists={},
                         agents_limits={"max_attempts": 5, "max_minutes": 60})
    sess = AgentSession(id="pers1", challenge_id=ch.id, state="gave_up", dispatch_count=5)
    orch1._save(session, sess)
    orch1.resume(session, "pers1", mode="supervised", extra_attempts=7)

    orch2 = Orchestrator(runner=runner, jury=jury, specialists={},
                         agents_limits={"max_attempts": 5, "max_minutes": 60})
    hydrated2 = orch2.get_session(session, "pers1")
    assert hydrated2.resume_count == 1
    assert hydrated2.extra_attempts == 7
    assert hydrated2.budget_started_at is not None


def test_update_target_and_resume_delegates_on_closed_session(tmp_path):
    engine, session, _ev, ch, storage = _env(tmp_path)
    runner = ToolRunner(ToolSettings(tools={"python": ToolSpec(mode="native", guard="none")}), storage)
    jury = JuryAgent(_scripted([
        {"action": "DISPATCH", "reason": "probe new", "order": {"category": "web", "task": "curl"}},
    ]), persona_text="jury")
    spec = SpecialistRunner(_scripted([
        {"tool_name": "run_tool", "tool_args": {"tool": "python", "args": ["-c", "print('CTF{new_target_win}')"]}},
        {"status": "SOLVED", "flag_candidate": "CTF{new_target_win}", "findings": ["solved"]},
    ]), persona_text="spec", category="web")

    orch = Orchestrator(runner=runner, jury=jury, specialists={"web": spec},
                        agents_limits={"max_attempts": 2, "max_minutes": 60})
    s = AgentSession(id="closed_t", challenge_id=ch.id, state="gave_up", dispatch_count=2)
    s.blackboard.merge(dead_ends=["unreachable target: http://old:1234"])
    orch._save(session, s)

    out = orch.update_target_and_resume(session, "closed_t", "http://127.0.0.1:45555", mode="auto")
    session.refresh(ch)
    assert ch.target == "http://127.0.0.1:45555"
    assert out.state == "solved"
    assert out.resume_count == 1
    assert not any("unreachable target" in d for d in out.blackboard.dead_ends)


def test_api_web_panel_dashboard_and_cli_resume(tmp_path, monkeypatch):
    engine, session, ev, ch, storage = _env(tmp_path)
    runner = ToolRunner(ToolSettings(tools={"python": ToolSpec(mode="native", guard="none")}), storage)

    jury = JuryAgent(_scripted([
        {"action": "GIVE_UP", "reason": "initial give up", "findings": ["nothing yet"]},
        {"action": "DISPATCH", "reason": "resumed dispatch", "order": {"category": "web", "task": "solve"}},
    ]), persona_text="jury")
    spec = SpecialistRunner(_scripted([
        {"tool_name": "run_tool", "tool_args": {"tool": "python", "args": ["-c", "print('CTF{ui_api_cli_ok}')"]}},
        {"status": "SOLVED", "flag_candidate": "CTF{ui_api_cli_ok}", "findings": ["solved"]},
    ]), persona_text="spec", category="web")

    real_orch = Orchestrator(runner=runner, jury=jury, specialists={"web": spec},
                             agents_limits={"max_attempts": 5, "max_minutes": 60})
    monkeypatch.setattr(agent_api, "_make_orchestrator", lambda _s: real_orch)

    app = create_app(engine=engine, storage_dir=tmp_path / "storage")
    with TestClient(app) as c:
        # 1. Start and hit GIVE_UP
        r_start = c.post(f"/api/agent/{ch.id}/start", json={"mode": "auto"})
        assert r_start.status_code == 201
        sid = r_start.json()["id"]
        assert r_start.json()["state"] == "gave_up"

        # 2. Panel HTML renders resume-panel + dashboard shows Retomar ->
        panel_html = c.get(f"/challenges/{ch.id}/agent").text
        assert ".resume-panel" in panel_html
        assert ".checkpoint" in panel_html

        side_html = c.get(f"/challenges/{ch.id}/agent/side").text
        assert "Retomar desafio" in side_html
        assert "Nova sessão do zero" in side_html

        dash_html = c.get("/dashboard").text
        assert "Retomar →" in dash_html

        # 3. Unknown session -> 404
        assert c.post("/api/agent/sessions/missing/resume", json={}).status_code == 404

        # 4. Web panel POST resume (sync) solves challenge
        r_web = c.post(
            f"/challenges/{ch.id}/agent/sessions/{sid}/resume",
            data={
                "mode": "auto",
                "instruction": "check /flag",
                "extra_attempts": "6",
                "clear_dead_ends": "true",
                "background": "false",
            },
        )
        assert r_web.status_code == 200
        assert "solved" in r_web.text
        assert "CTF{ui_api_cli_ok}" in r_web.text

        # 5. Re-resuming a solved session via REST API -> 409
        r_conflict = c.post(f"/api/agent/sessions/{sid}/resume", json={})
        assert r_conflict.status_code == 409

        # 6. Create another closed session for REST API + CLI /resume tests
        ch2 = Challenge(event_id=ev.id, title="CliResumeChal", category="web")
        with Session(engine) as s2:
            s2.add(ch2)
            s2.commit()
            s2.refresh(ch2)
            ch2_id = ch2.id

        closed_api = AgentSession(id="api_closed", challenge_id=ch2_id, state="rejected")
        real_orch._save(session, closed_api)
        r_api = c.post(
            "/api/agent/sessions/api_closed/resume",
            json={"mode": "supervised", "feedback": "api hint", "extra_attempts": 4},
        )
        assert r_api.status_code == 200
        assert r_api.json()["resume_count"] == 1
        assert r_api.json()["state"] == "awaiting_approval"

        # 7. CLI /help and /resume
        help_rec = process_cli_turn(session, storage, real_orch, "/help")
        assert "/resume" in help_rec.content

        closed_cli = AgentSession(id="cli_closed", challenge_id=ch2_id, state="gave_up")
        closed_cli.created_at = datetime.now(timezone.utc) + timedelta(seconds=5)
        real_orch._save(session, closed_cli)

        cli_rec = process_cli_turn(
            session, storage, real_orch,
            f"/resume {ch2_id} | target=127.0.0.1:8080 | extra=5 | clear | recon | check /admin",
            mode="supervised",
        )
        assert "Missão retomada (#1)" in cli_rec.content
        assert cli_rec.challenge_id == ch2_id
        assert cli_rec.session_id == "cli_closed"


def test_visual_refinements_rendered_across_ui(tmp_path, monkeypatch):
    assert badge_for("gave_up") == "neutral"
    assert badge_for("rejected") == "err"
    assert badge_for("solved") == "flag-ok"
    assert state_label("running") == "Executando"
    assert state_label("awaiting_approval") == "Aguardando aprovação"
    assert state_label("gave_up") == "Desistiu"
    assert state_label("rejected") == "Interrompido"
    assert report_status_label("NEED_HINT") == "Precisa de pista"

    engine, session, _ev, ch, storage = _env(tmp_path)
    runner = ToolRunner(ToolSettings(tools={"python": ToolSpec(mode="native", guard="none")}), storage)
    fake_orch = Orchestrator(runner=runner, jury=None, specialists={})
    s = AgentSession(
        id="vis1",
        challenge_id=ch.id,
        state="awaiting_approval",
        pending_decision=JuryDecision(
            action="DISPATCH",
            reason="check /login",
            order=TaskOrder(category="web", task="test sqli"),
        ),
        reports=[SpecialistReport(status="NEED_HINT", findings=["waf active"], tools_used=["curl"])],
    )
    fake_orch._save(session, s)
    monkeypatch.setattr(agent_api, "_make_orchestrator", lambda _s: fake_orch)

    app = create_app(engine=engine, storage_dir=tmp_path / "storage")
    with TestClient(app) as c:
        panel = c.get(f"/challenges/{ch.id}/agent").text
        assert "koth-toast-container" in panel
        assert "koth-confirm-modal" in panel
        assert "kothParseError" in panel
        assert "kothCopy" in panel
        assert "agent-header-bar" in panel
        assert "koth-timeline" in panel
        assert "Precisa de pista" in panel
        assert "Aguardando aprovação" in panel

        cli_home = c.get("/").text
        assert "/solve" in cli_home
        assert "/resume" in cli_home

        from app.templates import templates
        err_badge = templates.get_template("_provider_test.html").render({"ok": False, "msg": "falha"})
        assert "badge err" in err_badge


def test_approve_and_reject_clear_stale_recovery_error_and_side_hides_while_running(tmp_path):
    engine, session, _ev, ch, storage = _env(tmp_path)
    runner = ToolRunner(ToolSettings(tools={"python": ToolSpec(mode="native", guard="none")}), storage)
    jury = JuryAgent(_scripted([
        {"action": "DISPATCH", "reason": "go", "order": {"category": "web", "task": "check"}},
        {"action": "DISPATCH", "reason": "adjusted", "order": {"category": "web", "task": "check2"}},
    ]), persona_text="jury")
    spec = SpecialistRunner(_scripted([
        {"status": "NEED_HINT", "findings": ["ok"]},
    ]), persona_text="spec", category="web")
    orch = Orchestrator(runner=runner, jury=jury, specialists={"web": spec})

    # Simulate orphaned session recovered after restart
    orphaned = AgentSession(
        id="orph1",
        challenge_id=ch.id,
        state="running",
        pending_decision=JuryDecision(
            action="DISPATCH", reason="before restart",
            order=TaskOrder(category="web", task="probe"),
        ),
    )
    orch._save(session, orphaned)
    orch.sessions.clear()

    recovered = orch.get_session(session, "orph1")
    assert recovered.state == "awaiting_approval"
    assert recovered.error and "recuperada" in recovered.error

    # Side renders recovery notice as warn (not err) while awaiting_approval, and hides it when running
    from app.templates import templates
    side_wait = templates.get_template("_agent_side.html").render({
        "challenge_id": ch.id, "sess": recovered.to_dict(), "max_attempts": 20,
    })
    assert "koth-alert warn" in side_wait
    assert "recuperada" in side_wait

    running_dict = recovered.to_dict()
    running_dict["state"] = "running"
    side_run = templates.get_template("_agent_side.html").render({
        "challenge_id": ch.id, "sess": running_dict, "max_attempts": 20,
    })
    assert "recuperada" not in side_run

    # reject() clears stale recovery error
    orch.reject(session, "orph1", "try /v2")
    assert recovered.error is None

    # approve() clears stale recovery error
    recovered.error = "sessão recuperada após reinício (aguardando aprovação)"
    orch.approve(session, "orph1", mode="supervised")
    assert recovered.error is None

    # approve_background() clears stale recovery error immediately
    recovered.error = "sessão recuperada após reinício (aguardando aprovação)"
    recovered.state = "awaiting_approval"
    orch._save(session, recovered)
    bg = orch.approve_background(engine, session, "orph1", mode="supervised")
    assert bg.error is None
    for _ in range(40):
        cur = orch.get_session(session, "orph1")
        if cur.state != "running":
            break
        time.sleep(0.05)
