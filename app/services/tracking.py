from datetime import datetime

from datetime import datetime, timezone

from sqlmodel import Session, select

from ..models import (
    Attempt, AttemptResult, AttemptType, Challenge, ChallengeStatus, Difficulty,
    Event, EventStatus, Note,
)


def create_event(session: Session, name: str, url: str | None = None,
                 starts_at=None, ends_at=None) -> Event:
    event = Event(name=name, url=url, starts_at=starts_at, ends_at=ends_at)
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
                     flag: str | None = None) -> Challenge:
    challenge = session.get(Challenge, challenge_id)
    if challenge is None:
        raise LookupError(f"challenge {challenge_id} not found")
    if status is not None:
        challenge.status = status
        if status is ChallengeStatus.solved and challenge.solved_at is None:
            challenge.solved_at = datetime.now(timezone.utc)
    if flag is not None:
        challenge.flag = flag
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
