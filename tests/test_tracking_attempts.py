import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.models import AttemptResult, AttemptType, ChallengeStatus
from app.services import tracking


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def challenge(session):
    event = tracking.create_event(session, "e")
    return tracking.create_challenge(session, event.id, "rop1", category="pwn")


def test_record_attempt_sets_first_interaction(session, challenge):
    assert challenge.first_interaction is None
    tracking.record_attempt(session, challenge.id, AttemptType.manual,
                            "tried ret2libc", AttemptResult.failed)
    refreshed = tracking.get_challenge(session, challenge.id)
    assert refreshed.first_interaction is not None


def test_solve_attempt_finalizes(session, challenge):
    tracking.record_attempt(session, challenge.id, AttemptType.agent,
                            "leaked canary then ROP", AttemptResult.solved,
                            flag="flag{rop_w1n}")
    refreshed = tracking.get_challenge(session, challenge.id)
    assert refreshed.status is ChallengeStatus.solved
    assert refreshed.solved_at is not None
    assert refreshed.flag == "flag{rop_w1n}"


def test_second_attempt_does_not_reset_solved_at(session, challenge):
    tracking.record_attempt(session, challenge.id, AttemptType.manual, "x", AttemptResult.failed)
    tracking.record_attempt(session, challenge.id, AttemptType.manual, "y", AttemptResult.solved, flag="flag{a}")
    first_solved_at = tracking.get_challenge(session, challenge.id).solved_at
    tracking.record_attempt(session, challenge.id, AttemptType.manual, "z", AttemptResult.solved, flag="flag{b}")
    assert tracking.get_challenge(session, challenge.id).solved_at == first_solved_at
    assert tracking.get_challenge(session, challenge.id).flag == "flag{b}"


def test_update_challenge_marks_solved(session, challenge):
    updated = tracking.update_challenge(session, challenge.id, status=ChallengeStatus.solved, flag="flag{u}")
    assert updated.solved_at is not None
    assert updated.flag == "flag{u}"


def test_challenge_summary(session, challenge):
    tracking.record_attempt(session, challenge.id, AttemptType.manual, "a", AttemptResult.failed)
    tracking.record_attempt(session, challenge.id, AttemptType.tool, "b", AttemptResult.breakthrough, tool_command="nmap -p 1337 chal")
    summary = tracking.challenge_summary(session, tracking.get_challenge(session, challenge.id))
    assert summary["attempt_count"] == 2
    assert summary["active_seconds"] is not None and summary["active_seconds"] >= 0


def test_summary_before_interaction(session, challenge):
    summary = tracking.challenge_summary(session, challenge)
    assert summary["attempt_count"] == 0
    assert summary["active_seconds"] is None


def test_record_attempt_missing_challenge_raises(session):
    with pytest.raises(LookupError):
        tracking.record_attempt(session, 999, AttemptType.manual, "x", AttemptResult.failed)
