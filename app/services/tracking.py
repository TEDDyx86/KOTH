from datetime import datetime, timezone
import shutil

from sqlmodel import Session, select

from ..models import (
    AgentSessionRecord, Attachment, Attempt, AttemptResult, AttemptType,
    Challenge, ChallengeStatus, CliMessageRecord, Difficulty, Event,
    EventStatus, Note,
)


def create_event(session: Session, name: str, url: str | None = None,
                 starts_at=None, ends_at=None, flag_format: str | None = None) -> Event:
    event = Event(name=name, url=url, starts_at=starts_at, ends_at=ends_at,
                  flag_format=flag_format or None)
    session.add(event)
    session.commit()
    session.refresh(event)
    return event


def list_events(session: Session) -> list[Event]:
    return list(session.exec(select(Event).order_by(Event.created_at)).all())


def set_event_status(session: Session, event_id: int, status: EventStatus) -> Event:
    event = session.get(Event, event_id)
    if event is None:
        raise LookupError(f"event {event_id} not found")
    event.status = status
    session.add(event)
    session.commit()
    session.refresh(event)
    return event


def update_event(session: Session, event_id: int, *,
                 name: str | None = None,
                 url: str | None = None,
                 flag_format: str | None = None,
                 status: EventStatus | None = None) -> Event:
    event = session.get(Event, event_id)
    if event is None:
        raise LookupError(f"event {event_id} not found")
    if name is not None and name.strip():
        event.name = name.strip()
    if url is not None:
        event.url = url.strip() or None
    if flag_format is not None:
        event.flag_format = flag_format.strip() or None
    if status is not None:
        event.status = status
    session.add(event)
    session.commit()
    session.refresh(event)
    return event


def delete_challenge(session: Session, challenge_id: int, *, storage=None) -> None:
    challenge = session.get(Challenge, challenge_id)
    if challenge is None:
        raise LookupError(f"challenge {challenge_id} not found")
    event_id = challenge.event_id

    for cli_msg in session.exec(
        select(CliMessageRecord).where(CliMessageRecord.challenge_id == challenge_id)
    ).all():
        cli_msg.challenge_id = None
        cli_msg.session_id = None
        session.add(cli_msg)

    for model in (Attempt, Note, AgentSessionRecord):
        for row in session.exec(select(model).where(model.challenge_id == challenge_id)).all():
            session.delete(row)

    attachments = list(session.exec(
        select(Attachment).where(Attachment.challenge_id == challenge_id)
    ).all())
    for att in attachments:
        if storage is not None:
            p = storage.storage_dir / att.path
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                pass
        session.delete(att)

    session.delete(challenge)
    session.commit()

    if storage is not None:
        for folder in (
            storage.storage_dir / "sandbox" / str(event_id) / str(challenge_id),
            storage.storage_dir / str(challenge_id),
        ):
            if folder.exists():
                shutil.rmtree(folder, ignore_errors=True)


def delete_event(session: Session, event_id: int, *, storage=None) -> None:
    event = session.get(Event, event_id)
    if event is None:
        raise LookupError(f"event {event_id} not found")
    ch_ids = [
        c.id for c in session.exec(select(Challenge).where(Challenge.event_id == event_id)).all()
        if c.id is not None
    ]
    for cid in ch_ids:
        delete_challenge(session, cid, storage=storage)
    session.delete(event)
    session.commit()


def create_challenge(session: Session, event_id: int, title: str, category: str = "misc",
                     points: int = 0, difficulty: Difficulty | None = None,
                     description: str = "", target: str | None = None,
                     tags: str = "") -> Challenge:
    if session.get(Event, event_id) is None:
        raise LookupError(f"event {event_id} not found")
    challenge = Challenge(event_id=event_id, title=title, category=category, points=points,
                          difficulty=difficulty, description=description, target=target, tags=tags)
    session.add(challenge)
    session.commit()
    session.refresh(challenge)
    return challenge


def get_challenge(session: Session, challenge_id: int) -> Challenge | None:
    return session.get(Challenge, challenge_id)


def list_challenges(session: Session, event_id: int | None = None,
                    category: str | None = None, status: ChallengeStatus | None = None,
                    tag: str | None = None) -> list[Challenge]:
    query = select(Challenge)
    if event_id is not None:
        query = query.where(Challenge.event_id == event_id)
    if category is not None:
        query = query.where(Challenge.category == category)
    if status is not None:
        query = query.where(Challenge.status == status)
    challenges = list(session.exec(query).all())
    if tag is not None:
        challenges = [c for c in challenges if tag in [t.strip() for t in c.tags.split(",") if t.strip()]]
    return challenges


def record_attempt(session: Session, challenge_id: int, type: AttemptType,
                   description: str, result: AttemptResult,
                   tool_command: str | None = None, learning: str = "",
                   flag: str | None = None) -> Attempt:
    challenge = session.get(Challenge, challenge_id)
    if challenge is None:
        raise LookupError(f"challenge {challenge_id} not found")
    now = datetime.now(timezone.utc)
    if challenge.first_interaction is None:
        challenge.first_interaction = now
    if result is AttemptResult.solved:
        if challenge.solved_at is None:
            challenge.status = ChallengeStatus.solved
            challenge.solved_at = now
        if flag is not None:
            challenge.flag = flag
    session.add(challenge)
    attempt = Attempt(challenge_id=challenge_id, type=type, description=description,
                      tool_command=tool_command, result=result, learning=learning)
    session.add(attempt)
    session.commit()
    session.refresh(attempt)
    return attempt


def update_challenge(session: Session, challenge_id: int,
                     status: ChallengeStatus | None = None,
                     flag: str | None = None,
                     title: str | None = None,
                     category: str | None = None,
                     points: int | None = None,
                     difficulty: Difficulty | None = None,
                     clear_difficulty: bool = False,
                     description: str | None = None,
                     target: str | None = None,
                     tags: str | None = None) -> Challenge:
    challenge = session.get(Challenge, challenge_id)
    if challenge is None:
        raise LookupError(f"challenge {challenge_id} not found")
    if status is not None:
        challenge.status = status
        if status is ChallengeStatus.solved and challenge.solved_at is None:
            challenge.solved_at = datetime.now(timezone.utc)
        elif status is not ChallengeStatus.solved:
            challenge.solved_at = None
    if flag is not None:
        challenge.flag = flag.strip() or None
    if title is not None and title.strip():
        challenge.title = title.strip()
    if category is not None:
        challenge.category = category
    if points is not None:
        challenge.points = points
    if clear_difficulty:
        challenge.difficulty = None
    elif difficulty is not None:
        challenge.difficulty = difficulty
    if description is not None:
        challenge.description = description
    if target is not None:
        challenge.target = target or None
    if tags is not None:
        challenge.tags = tags
    session.add(challenge)
    session.commit()
    session.refresh(challenge)
    return challenge


def list_attempts(session: Session, challenge_id: int) -> list[Attempt]:
    query = select(Attempt).where(Attempt.challenge_id == challenge_id).order_by(Attempt.timestamp)
    return list(session.exec(query).all())


def challenge_summary(session: Session, challenge: Challenge) -> dict:
    from sqlmodel import func
    count = session.exec(
        select(func.count()).select_from(Attempt).where(Attempt.challenge_id == challenge.id)
    ).one()
    active_seconds: float | None = None
    if challenge.first_interaction is not None:
        end = challenge.solved_at or datetime.now(timezone.utc)
        active_seconds = (end - challenge.first_interaction).total_seconds()
    return {"attempt_count": count, "active_seconds": active_seconds}


def add_note(session: Session, challenge_id: int, content: str) -> Note:
    challenge = session.get(Challenge, challenge_id)
    if challenge is None:
        raise LookupError(f"challenge {challenge_id} not found")
    if challenge.first_interaction is None:
        challenge.first_interaction = datetime.now(timezone.utc)
        session.add(challenge)
    note = Note(challenge_id=challenge_id, content=content)
    session.add(note)
    session.commit()
    session.refresh(note)
    return note


def note_history(session: Session, challenge_id: int) -> list[Note]:
    query = select(Note).where(Note.challenge_id == challenge_id).order_by(Note.timestamp.desc(), Note.id.desc())
    return list(session.exec(query).all())


def current_note(session: Session, challenge_id: int) -> Note | None:
    history = note_history(session, challenge_id)
    return history[0] if history else None
