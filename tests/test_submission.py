import re

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.agents.submission import validate_flag
from app.models import Attempt, AttemptType, Challenge, Event


@pytest.fixture
def env(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        event = Event(name="ctf", flag_format=r"CTF\{.*\}")
        session.add(event)
        session.commit()
        session.refresh(event)
        ch = Challenge(event_id=event.id, title="c")
        session.add(ch)
        session.commit()
        session.refresh(ch)
        yield session, ch, event


def test_valid_flag_accepted_and_recorded(env):
    session, ch, event = env
    result = validate_flag(session, ch, "CTF{hello_world}")
    assert result.accepted is True
    rows = list(session.exec(select(Attempt).where(
        Attempt.challenge_id == ch.id,
        Attempt.type == AttemptType.submission,
    )).all())
    assert len(rows) == 1 and rows[0].tool_command == "CTF{hello_world}"
    assert rows[0].result.value == "solved"


def test_bad_regex_rejected(env):
    session, ch, event = env
    result = validate_flag(session, ch, "flag{nope}")
    assert result.accepted is False
    assert "format" in result.reason.lower()


def test_duplicate_rejected(env):
    session, ch, event = env
    assert validate_flag(session, ch, "CTF{dup}").accepted is True
    second = validate_flag(session, ch, "CTF{dup}")
    assert second.accepted is False
    assert "duplic" in second.reason.lower()


def test_custom_event_flag_format(env):
    session, ch, event = env
    event.flag_format = r"HTB\{.*\}"
    session.add(event)
    session.commit()
    result = validate_flag(session, ch, "HTB{custom}")
    assert result.accepted is True


def test_default_flag_format_accepts_generic_prefixes_when_event_unset(env, monkeypatch):
    monkeypatch.delenv("CTF_FLAG_FORMAT", raising=False)
    session, ch, event = env
    event.flag_format = None
    session.add(event)
    session.commit()

    assert validate_flag(session, ch, "IDP{3x3cut4_qu4lqu3R_c01s4}").accepted is True
    assert validate_flag(session, ch, "picoCTF{generic_ok}").accepted is True
    assert validate_flag(session, ch, "not_a_flag").accepted is False
    assert validate_flag(session, ch, "IDP{}").accepted is False


def test_format_rejected_flag_can_be_accepted_after_event_format_update(env):
    session, ch, event = env
    # First rejected due to strict CTF\{.*\} event format
    first = validate_flag(session, ch, "IDP{real_flag_123}")
    assert first.accepted is False
    assert "format" in first.reason.lower()

    # After updating event flag_format, the same flag must NOT be blocked as duplicate
    event.flag_format = r"IDP\{.*\}"
    session.add(event)
    session.commit()

    second = validate_flag(session, ch, "IDP{real_flag_123}")
    assert second.accepted is True
