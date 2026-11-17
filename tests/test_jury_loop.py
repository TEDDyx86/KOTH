import json
import re
from dataclasses import dataclass

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from sqlmodel import Session, SQLModel, create_engine

from app.agents.jury import JuryAgent
from app.agents.payloads import JuryDecision, SpecialistReport, TaskOrder
from app.agents.specialists import SpecialistRunner
from app.models import Challenge, Event
from app.services import tracking
from app.services.storage import StorageService
from app.tooling.config import load_tools_config
from app.tooling.runner import ToolRunner

PERSONA = "Você é um especialista de teste. Retorne o payload estruturado."


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
        yield session, ch


def _runner(tmp_path):
    return ToolRunner(load_tools_config(), StorageService(tmp_path / "storage"))


def _scripted_jury_model(decision: dict):
    def fn(messages, info):
        blob = "\n".join(str(p) for m in messages for p in (m.parts or []))
        match = re.search(r"['\"](\w*result\w*)['\"]", blob)
        name = match.group(1) if match else "final_result"
        return ModelResponse(parts=[ToolCallPart(tool_name=name, args=json.dumps(decision))])

    return FunctionModel(fn)


def test_jury_decision_with_test_model(env):
    session, ch = env
    agent = JuryAgent(_scripted_jury_model({"action": "RECHECK", "reason": "need recon",
                                            "findings": ["no artifacts yet"]}),
                      persona_text="Você é o Júri de teste.")
    decision = agent.decide(session, ch, reports=[])
    assert isinstance(decision, JuryDecision)
    assert decision.action == "RECHECK"
    assert "no artifacts yet" in decision.findings


def _scripted_specialist_model(tool_call: dict | None):
    state = {"n": 0}

    def fn(messages, info):
        state["n"] += 1
        if state["n"] == 1 and tool_call is not None:
            return ModelResponse(parts=[ToolCallPart(tool_name="run_tool",
                                                     args=json.dumps(tool_call))])
        # final structured output via the output tool (discover its name)
        blob = "\n".join(str(p) for m in messages for p in (m.parts or []))
        match = re.search(r"['\"](\w*result\w*)['\"]", blob)
        name = match.group(1) if match else "final_result"
        report = {"status": "NEED_HINT", "findings": ["checked strings"],
                  "next_step": "run strings on attachment"}
        return ModelResponse(parts=[ToolCallPart(tool_name=name, args=json.dumps(report))])

    return FunctionModel(fn)


def test_specialist_runs_tool_and_returns_report(env, tmp_path):
    session, ch = env
    runner = _runner(tmp_path)
    agent = SpecialistRunner(_scripted_specialist_model(
        {"tool": "python", "args": ["-c", "print('recon-marker')"]}),
        persona_text=PERSONA)
    order = TaskOrder(category="misc", task="inspect attachment")
    report = agent.run(session, ch, order, runner=runner)
    assert isinstance(report, SpecialistReport)
    assert report.status == "NEED_HINT"
    summary = tracking.challenge_summary(session, ch)
    assert summary["attempt_count"] >= 1  # tool run persisted by the runner


def test_specialist_rejects_bad_tool_gracefully(env, tmp_path):
    session, ch = env
    runner = _runner(tmp_path)
    agent = SpecialistRunner(_scripted_specialist_model(
        {"tool": "definitely-not-a-tool", "args": []}),
        persona_text=PERSONA)
    order = TaskOrder(category="misc", task="x")
    report = agent.run(session, ch, order, runner=runner)
    assert report.status == "NEED_HINT"  # run completes; guard error fed back to model
