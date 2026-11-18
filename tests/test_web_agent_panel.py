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


def test_start_posts_and_renders_side(client):
    c, ch_id, fake = client
    r = c.post(f"/challenges/{ch_id}/agent/start")
    assert r.status_code == 200
    assert fake.calls[0] == ("start", ch_id)
    assert "awaiting_approval" in r.text


def test_start_409_when_session_active(client):
    c, ch_id, fake = client
    fake.sessions["s1"] = AgentSession(id="s1", challenge_id=ch_id,
                                       state="running")
    r = c.post(f"/challenges/{ch_id}/agent/start")
    assert r.status_code == 409
    assert fake.calls == []


def test_approve_terminate_adjust(client):
    c, ch_id, fake = client
    s = AgentSession(id="s1", challenge_id=ch_id, state="awaiting_approval")
    fake.sessions["s1"] = s

    r = c.post(f"/challenges/{ch_id}/agent/sessions/s1/approve")
    assert r.status_code == 200
    assert ("approve", "s1") in fake.calls

    r = c.post(f"/challenges/{ch_id}/agent/sessions/s1/adjust",
               data={"instruction": "focus on the csv file"})
    assert r.status_code == 200
    assert ("adjust", "s1", "focus on the csv file") in fake.calls

    r = c.post(f"/challenges/{ch_id}/agent/sessions/s1/terminate")
    assert r.status_code == 200
    assert "rejected" in r.text
    assert ("terminate", "s1", "rejected via panel") in fake.calls


def test_unknown_session_404(client):
    c, ch_id, _ = client
    r = c.post(f"/challenges/{ch_id}/agent/sessions/nope/approve")
    assert r.status_code == 404
    r = c.post(f"/challenges/{ch_id}/agent/sessions/nope/terminate")
    assert r.status_code == 404
    r = c.post(f"/challenges/{ch_id}/agent/sessions/nope/adjust",
               data={"instruction": "x"})
    assert r.status_code == 404


def test_panel_end_to_end(tmp_path, monkeypatch):
    """Wiring real: painel + orchestrator com modelos scriptados."""
    import app.agents.api as agent_api
    from app.agents.jury import JuryAgent
    from app.agents.orchestrator import Orchestrator
    from app.agents.specialists import SpecialistRunner
    from app.services.storage import StorageService
    from app.tooling.config import load_tools_config
    from app.tooling.runner import ToolRunner
    from tests.test_api_agent import _scripted

    engine = create_engine(f"sqlite:///{tmp_path / 'e.db'}",
                           connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    storage = StorageService(tmp_path / "storage")

    def _fake_orchestrator(storage_dir):
        runner = ToolRunner(load_tools_config(), storage)
        jury = JuryAgent(_scripted([
            {"action": "DISPATCH", "reason": "recon first",
             "order": {"category": "misc", "task": "look"}, "findings": []},
            {"action": "RECHECK", "reason": "nothing yet", "findings": []},
        ]), persona_text="jury test")
        specialist = SpecialistRunner(_scripted([
            {"status": "NEED_HINT", "findings": ["nothing found"]},
        ]), persona_text="spec test")
        return Orchestrator(runner=runner, jury=jury,
                            specialists={"misc": specialist})

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

        r = c.post(f"/challenges/{ch.id}/agent/start")
        assert r.status_code == 200
        assert "awaiting_approval" in r.text

        r = c.get(f"/challenges/{ch.id}/agent/feed")
        assert "recon first" in r.text

        r_side = c.get(f"/challenges/{ch.id}/agent/side")
        m = re.search(r"sessions/([0-9a-f]+)/approve", r_side.text)
        sid = m.group(1)

        r = c.post(f"/challenges/{ch.id}/agent/sessions/{sid}/approve")
        assert r.status_code == 200
        assert "RECHECK" in r.text

        r = c.post(f"/challenges/{ch.id}/agent/sessions/{sid}/terminate")
        assert r.status_code == 200
        assert "rejected" in r.text
