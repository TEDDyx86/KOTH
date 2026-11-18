import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

from app.agents.orchestrator import AgentSession
from app.agents.payloads import JuryDecision, TaskOrder
from app.main import create_app
from app.models import Challenge, Event


class FakeOrch:
    """Same shape as Orchestrator for the web-layer tests."""

    def __init__(self):
        self.sessions: dict[str, AgentSession] = {}
        self.calls: list[tuple] = []

    def start(self, session, challenge_id):
        self.calls.append(("start", challenge_id))
        s = AgentSession(id="s1", challenge_id=challenge_id,
                         state="awaiting_approval")
        s.pending_decision = JuryDecision(
            action="DISPATCH", reason="start with web recon",
            order=TaskOrder(category="web", task="dump the source"))
        self.sessions[s.id] = s
        return s

    def approve(self, session, session_id):
        self.calls.append(("approve", session_id))
        s = self.sessions[session_id]
        s.pending_decision = JuryDecision(action="RECHECK", reason="verify",
                                          findings=[])
        return s

    def terminate(self, session, session_id, reason=""):
        self.calls.append(("terminate", session_id, reason))
        s = self.sessions[session_id]
        s.state = "rejected"
        s.pending_decision = None
        return s

    def reject(self, session, session_id, feedback):
        self.calls.append(("adjust", session_id, feedback))
        s = self.sessions[session_id]
        s.pending_decision = JuryDecision(
            action="DISPATCH", reason=f"adjusted: {feedback}",
            order=TaskOrder(category="web", task="try harder"))
        return s


@pytest.fixture
def client(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}",
                           connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    fake = FakeOrch()
    monkeypatch.setattr("app.web.agent_routes.get_orchestrator",
                        lambda request: fake)
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
        yield c, ch.id, fake


def test_panel_page_renders(client):
    c, ch_id, _ = client
    r = c.get(f"/challenges/{ch_id}/agent")
    assert r.status_code == 200
    assert "Painel do Agente" in r.text
    assert "Registrar flag" in r.text


def test_feed_empty_then_session(client):
    c, ch_id, fake = client
    r = c.get(f"/challenges/{ch_id}/agent/feed")
    assert r.status_code == 200
    assert "Nenhuma sessão" in r.text

    fake.sessions["s1"] = AgentSession(id="s1", challenge_id=ch_id,
                                       state="awaiting_approval")
    r = c.get(f"/challenges/{ch_id}/agent/feed")
    assert r.status_code == 200
    assert "s1" in r.text


def test_side_start_button_then_checkpoint(client):
    c, ch_id, fake = client
    r = c.get(f"/challenges/{ch_id}/agent/side")
    assert "Iniciar agente" in r.text

    s = AgentSession(id="s1", challenge_id=ch_id, state="awaiting_approval")
    s.pending_decision = JuryDecision(action="DISPATCH", reason="go",
                                      order=TaskOrder(category="web", task="t"))
    fake.sessions["s1"] = s
    r = c.get(f"/challenges/{ch_id}/agent/side")
    assert "Aprovar" in r.text
    assert "Ajustar" in r.text
    assert "Rejeitar" in r.text
    assert "web: t" in r.text


def test_latest_session_wins(client):
    c, ch_id, fake = client
    old = AgentSession(id="old", challenge_id=ch_id, state="rejected")
    old.created_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    fake.sessions["old"] = old
    fake.sessions["new"] = AgentSession(id="new", challenge_id=ch_id,
                                        state="running")
    r = c.get(f"/challenges/{ch_id}/agent/feed")
    assert "new" in r.text
    assert "old" not in r.text
