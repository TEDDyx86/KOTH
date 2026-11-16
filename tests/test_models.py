from sqlmodel import Session, select

from app.models import (
    Attempt, AttemptResult, AttemptType, Attachment, AttachmentType,
    Challenge, ChallengeStatus, Event, EventStatus, Note,
)


def _engine(tmp_path):
    from sqlmodel import SQLModel, create_engine
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return engine


def test_event_roundtrip(tmp_path):
    with Session(_engine(tmp_path)) as session:
        event = Event(name="picoCTF 2027", status=EventStatus.active)
        session.add(event)
        session.commit()
        session.refresh(event)
        rows = session.exec(select(Event)).all()
        assert len(rows) == 1
        assert rows[0].status is EventStatus.active


def test_challenge_defaults_and_fields(tmp_path):
    with Session(_engine(tmp_path)) as session:
        event = Event(name="e")
        session.add(event)
        session.commit()
        session.refresh(event)
        ch = Challenge(event_id=event.id, title="_rsa_revenge", category="crypto", points=500,
                       difficulty="hard", target="chal.example.com:1337", tags="rsa,lattice")
        session.add(ch)
        session.commit()
        session.refresh(ch)
        assert ch.status is ChallengeStatus.open
        assert ch.first_interaction is None
        assert ch.solved_at is None


def test_attempt_note_attachment_rows(tmp_path):
    with Session(_engine(tmp_path)) as session:
        event = Event(name="e")
        session.add(event)
        session.commit()
        session.refresh(event)
        ch = Challenge(event_id=event.id, title="t")
        session.add(ch)
        session.commit()
        session.refresh(ch)
        session.add(Attempt(challenge_id=ch.id, type=AttemptType.manual, description="tried ROP",
                            result=AttemptResult.failed, learning="no gadgets at 0x40"))
        session.add(Note(challenge_id=ch.id, content="# v1"))
        session.add(Note(challenge_id=ch.id, content="# v2"))
        session.add(Attachment(challenge_id=ch.id, name="shot.png", path="1/shot.png",
                               mime="image/png", size=10, type=AttachmentType.screenshot))
        session.commit()
        assert len(session.exec(select(Attempt)).all()) == 1
        assert len(session.exec(select(Note)).all()) == 2
        assert len(session.exec(select(Attachment)).all()) == 1
