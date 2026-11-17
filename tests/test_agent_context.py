import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.agents.context import (
    CONTEXT_BUDGETS,
    challenge_card,
    jury_context,
    specialist_context,
    submission_context,
)
from app.models import Challenge, Event


@pytest.fixture
def env(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        event = Event(name="ctf", flag_format="CTF{.*}")
        session.add(event)
        session.commit()
        session.refresh(event)
        ch = Challenge(event_id=event.id, title="ping me", category="forensics",
                       points=200, target=None,
                       description="ICMP pcap challenge")
        session.add(ch)
        session.commit()
        session.refresh(ch)
        yield session, ch, event


def test_challenge_card(env):
    session, ch, event = env
    card = challenge_card(session, ch)
    assert "ping me" in card
    assert "forensics" in card
    assert "CTF{.*}" in card


def test_jury_context_contains_attempts_and_budget(env, tmp_path):
    from app.services import tracking

    session, ch, event = env
    for i in range(15):
        tracking.record_attempt(session, ch.id, "manual", f"attempt {i}", "failed",
                                learning=f"learning-{i}")
    ctx = jury_context(session, ch, budget=250)
    assert "attempt 14" in ctx  # newest kept
    assert "attempt 10" in ctx
    assert "attempt 3" not in ctx  # oldest dropped (budget truncation)


def test_specialist_context_includes_order_and_knowledge(env, tmp_path, monkeypatch):
    import app.knowledge.index as idx_mod
    from app.agents.payloads import TaskOrder

    root = tmp_path / "knowledge"
    (root / "dispatch" / "Arsenal" / "FORENSICS").mkdir(parents=True, exist_ok=True)
    (root / "dispatch" / "Arsenal" / "FORENSICS" / "icmp-tunnel.md").write_text(
        "---\ntitle: ICMP tunnel\n---\nExtract ICMP payload from pcap.", encoding="utf-8")
    monkeypatch.setattr(idx_mod, "DEFAULT_KNOWLEDGE_ROOT", root)

    session, ch, event = env
    order = TaskOrder(category="forensics", task="extract ICMP payload")
    ctx = specialist_context(session, ch, order)
    assert "extract ICMP payload" in ctx
    assert "ICMP" in ctx


def test_submission_context(env):
    from app.services import tracking

    session, ch, event = env
    tracking.record_attempt(session, ch.id, "submission", "first flag", "failed",
                            tool_command="CTF{old}")
    ctx = submission_context(session, ch, "CTF{new}")
    assert "CTF{new}" in ctx
    assert "CTF{old}" in ctx
