import json
import re

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from sqlmodel import Session, SQLModel, create_engine

from app.agents.jury import JuryAgent
from app.agents.orchestrator import Orchestrator
from app.agents.specialists import SpecialistRunner
from app.models import Challenge, Event, Note
from app.services.storage import StorageService
from app.tooling.config import load_tools_config
from app.tooling.runner import ToolRunner


@pytest.fixture
def env(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        event = Event(name="ctf")
        session.add(event)
        session.commit()
        session.refresh(event)
        ch = Challenge(event_id=event.id, title="c", target=None)
        session.add(ch)
        session.commit()
        session.refresh(ch)
        yield session, ch, tmp_path


def _scripted(model_name, payloads: list[dict]):
    state = {"n": 0}

    def fn(messages, info):
        idx = state["n"]
        state["n"] += 1
        if idx < len(payloads):
            payload = payloads[idx]
            if "tool" in payload:  # a run_tool call
                return ModelResponse(parts=[ToolCallPart(
                    tool_name="run_tool",
                    args=json.dumps({"tool": payload["tool"], "args": payload["args"]}))])
        blob = "\n".join(str(p) for m in messages for p in (m.parts or []))
        match = re.search(r"['\"](\w*result\w*)['\"]", blob)
        name = match.group(1) if match else "final_result"
        payload = payloads[min(idx, len(payloads) - 1)]
        payload = {k: v for k, v in payload.items() if k not in ("tool", "args")}
        return ModelResponse(parts=[ToolCallPart(tool_name=name, args=json.dumps(payload))])

    return FunctionModel(fn)


def _orchestrator(env):
    session, ch, tmp_path = env
    runner = ToolRunner(load_tools_config(), StorageService(tmp_path / "storage"))
    jury = JuryAgent(_scripted("jury", [
        {"action": "DISPATCH", "reason": "start with misc",
         "order": {"category": "misc", "task": "inspect attachment"}, "findings": []},
        {"action": "SUBMIT", "reason": "specialist found flag",
         "flag_candidate": "CTF{orch_marker}", "findings": ["flag found"]},
    ]), persona_text="jury test")
    specialist = SpecialistRunner(_scripted("spec", [
        {"tool": "python", "args": ["-c", "print('spec-run')"]},
        {"status": "SOLVED", "flag_candidate": "CTF{orch_marker}",
         "findings": ["flag printed by tool"]},
    ]), persona_text="spec test")
    return Orchestrator(runner=runner, jury=jury,
                        specialists={"misc": specialist}), session, ch


def test_start_stops_at_approval(env):
    orch, session, ch = _orchestrator(env)
    s = orch.start(session, ch.id)
    assert s.state == "awaiting_approval"
    assert s.pending_decision.action == "DISPATCH"


def test_approve_dispatch_then_submit(env):
    orch, session, ch = _orchestrator(env)
    s0 = orch.start(session, ch.id)
    s1 = orch.approve(session, s0.id)
    # one cycle executed; jury decided SUBMIT; stopped again
    assert s1.state == "awaiting_approval"
    assert s1.pending_decision.action == "SUBMIT"
    assert any("spec-run" in f or "flag printed" in f for f in s1.findings)

    s2 = orch.approve(session, s1.id)
    assert s2.state == "solved"


def test_reject_records_feedback_and_redispatches(env):
    orch, session, ch = _orchestrator(env)
    s0 = orch.start(session, ch.id)
    s1 = orch.reject(session, s0.id, "try harder on the pcap")
    assert s1.state == "awaiting_approval"
    notes = session.query(Note).filter(Note.challenge_id == ch.id).all()
    assert any("try harder on the pcap" in n.content for n in notes)


def test_give_up(env, tmp_path):
    session, ch, tmp_path = env
    runner = ToolRunner(load_tools_config(), StorageService(tmp_path / "storage"))
    jury = JuryAgent(_scripted("jury2", [
        {"action": "GIVE_UP", "reason": "criteria hit", "findings": []},
    ]), persona_text="jury test")
    orch = Orchestrator(runner=runner, jury=jury, specialists={})
    s0 = orch.start(session, ch.id)
    assert s0.pending_decision.action == "GIVE_UP"
    s1 = orch.approve(session, s0.id)
    assert s1.state == "gave_up"
