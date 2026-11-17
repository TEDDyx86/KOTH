import json
import re

import pytest
from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from sqlmodel import Session, SQLModel, create_engine

from app.agents.jury import JuryAgent
from app.agents.orchestrator import Orchestrator
from app.agents.specialists import SpecialistRunner
from app.main import create_app
from app.models import Challenge, Event
from app.services.storage import StorageService
from app.tooling.config import load_tools_config
from app.tooling.runner import ToolRunner


def _scripted(payloads: list[dict]):
    state = {"n": 0}

    def fn(messages, info):
        idx = state["n"]
        state["n"] += 1
        if idx < len(payloads) and "tool" in payloads[idx]:
            return ModelResponse(parts=[ToolCallPart(
                tool_name="run_tool",
                args=json.dumps({"tool": payloads[idx]["tool"],
                                 "args": payloads[idx]["args"]}))])
        blob = "\n".join(str(p) for m in messages for p in (m.parts or []))
        match = re.search(r"['\"](\w*result\w*)['\"]", blob)
        name = match.group(1) if match else "final_result"
        payload = payloads[min(idx, len(payloads) - 1)]
        payload = {k: v for k, v in payload.items() if k not in ("tool", "args")}
        return ModelResponse(parts=[ToolCallPart(tool_name=name, args=json.dumps(payload))])

    return FunctionModel(fn)


@pytest.fixture
def client(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    storage = StorageService(tmp_path / "storage")

    import app.agents.api as agent_api

    def _fake_orchestrator(storage_dir):
        runner = ToolRunner(load_tools_config(), storage)
        jury = JuryAgent(_scripted([
            {"action": "DISPATCH", "reason": "recon first",
             "order": {"category": "misc", "task": "look"}, "findings": []},
        ]), persona_text="jury test")
        specialists = {"misc": SpecialistRunner(_scripted([
            {"status": "NEED_HINT", "findings": ["nothing yet"]},
        ]), persona_text="spec test")}
        return Orchestrator(runner=runner, jury=jury, specialists=specialists)

    monkeypatch.setattr(agent_api, "_make_orchestrator", _fake_orchestrator)

    app = create_app(engine=engine, storage_dir=tmp_path / "storage")
    with TestClient(app) as c:
        with Session(engine) as session:
            event = Event(name="ctf")
            session.add(event)
            session.commit()
            session.refresh(event)
            ch = Challenge(event_id=event.id, title="c")
            session.add(ch)
            session.commit()
            session.refresh(ch)
        yield c, ch.id, session


def test_agent_session_flow(client):
    c, challenge_id, session = client
    r = c.post(f"/api/agent/{challenge_id}/start")
    assert r.status_code == 201
    body = r.json()
    assert body["state"] == "awaiting_approval"
    assert body["pending_decision"]["action"] == "DISPATCH"
    sid = body["id"]

    r = c.get(f"/api/agent/sessions/{sid}")
    assert r.status_code == 200 and r.json()["state"] == "awaiting_approval"

    r = c.post(f"/api/agent/sessions/{sid}/approve", json={"note": None})
    assert r.status_code == 200
    assert r.json()["state"] == "awaiting_approval"
    assert r.json()["pending_decision"]["action"] == "RECHECK" or "findings" in r.json()

    r = c.post(f"/api/agent/sessions/{sid}/reject", json={"feedback": "adjust"})
    assert r.status_code == 200


def test_unknown_session_404(client):
    c, _, _ = client
    r = c.get("/api/agent/sessions/nope")
    assert r.status_code == 404
