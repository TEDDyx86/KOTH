import pytest
from sqlmodel import Session, SQLModel, create_engine

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
    return tracking.create_challenge(session, event.id, "n1")


def test_notes_are_append_only(session, challenge):
    first = tracking.add_note(session, challenge.id, "# version 1")
    second = tracking.add_note(session, challenge.id, "# version 2\nwith more detail")
    assert first.id != second.id
    history = tracking.note_history(session, challenge.id)
    assert [n.content for n in history] == ["# version 2\nwith more detail", "# version 1"]
    assert history[0].timestamp >= history[1].timestamp


def test_current_note_returns_latest(session, challenge):
    tracking.add_note(session, challenge.id, "old")
    latest = tracking.add_note(session, challenge.id, "newest")
    assert tracking.current_note(session, challenge.id).id == latest.id


def test_current_note_none_when_empty(session, challenge):
    assert tracking.current_note(session, challenge.id) is None


def test_add_note_sets_first_interaction(session, challenge):
    tracking.add_note(session, challenge.id, "hello")
    assert tracking.get_challenge(session, challenge.id).first_interaction is not None


def test_add_note_missing_challenge_raises(session):
    with pytest.raises(LookupError):
        tracking.add_note(session, 999, "x")
