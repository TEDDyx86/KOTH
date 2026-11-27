import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.agents.orchestrator import AgentSession, Orchestrator
from app.models import Challenge, Event, Note


@pytest.fixture
def env(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}",
                           connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        event = Event(name="ctf")
        session.add(event)
        session.commit()
        session.refresh(event)
        ch = Challenge(event_id=event.id, title="c")
        session.add(ch)
        session.commit()
        session.refresh(ch)
        yield session, ch


def test_terminate_discards_decision_and_notes(env):
    session, ch = env
    orch = Orchestrator(runner=None, jury=None, specialists={})
    s = AgentSession(id="abc123", challenge_id=ch.id, state="awaiting_approval")
    s.pending_decision = None
    orch.sessions["abc123"] = s

    out = orch.terminate(session, "abc123", "operator rejected the plan")

    assert out.state == "rejected"
    assert out.pending_decision is None
    assert any("terminated by operator" in f for f in out.findings)
    notes = list(session.exec(select(Note).where(Note.challenge_id == ch.id)).all())
    assert any("[agent terminated]" in n.content for n in notes)


def test_approve_after_terminate_raises(env):
    session, ch = env
    orch = Orchestrator(runner=None, jury=None, specialists={})
    s = AgentSession(id="abc123", challenge_id=ch.id, state="awaiting_approval")
    orch.sessions["abc123"] = s
    orch.terminate(session, "abc123")
    with pytest.raises(ValueError):
        orch.approve(session, "abc123")
