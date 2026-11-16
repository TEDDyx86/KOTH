import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.models import ChallengeStatus, EventStatus
from app.services import tracking


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_create_and_list_events(session):
    tracking.create_event(session, "picoCTF")
    tracking.create_event(session, "HTB Business", url="https://ctf.htb.com")
    events = tracking.list_events(session)
    assert [e.name for e in events] == ["picoCTF", "HTB Business"]
    assert events[1].url == "https://ctf.htb.com"


def test_set_event_status(session):
    event = tracking.create_event(session, "picoCTF")
    updated = tracking.set_event_status(session, event.id, EventStatus.active)
    assert updated.status is EventStatus.active


def test_set_event_status_missing_raises(session):
    with pytest.raises(LookupError):
        tracking.set_event_status(session, 999, EventStatus.active)


def test_create_and_filter_challenges(session):
    event = tracking.create_event(session, "e")
    tracking.create_challenge(session, event.id, "web1", category="web", points=100, tags="ssti,xss")
    tracking.create_challenge(session, event.id, "pwn1", category="pwn", points=300, tags="rop")
    web_only = tracking.list_challenges(session, event_id=event.id, category="web")
    assert [c.title for c in web_only] == ["web1"]
    rop = tracking.list_challenges(session, tag="rop")
    assert [c.title for c in rop] == ["pwn1"]
    solved = tracking.list_challenges(session, status=ChallengeStatus.open)
    assert len(solved) == 2


def test_create_challenge_missing_event_raises(session):
    with pytest.raises(LookupError):
        tracking.create_challenge(session, 999, "x")


def test_tag_filter_matches_any_tag(session):
    event = tracking.create_event(session, "e")
    tracking.create_challenge(session, event.id, "a", tags="rop,heap")
    tracking.create_challenge(session, event.id, "b", tags="ssti")
    matches = tracking.list_challenges(session, tag="heap")
    assert [c.title for c in matches] == ["a"]
