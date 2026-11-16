from datetime import datetime

from sqlmodel import Session, select

from ..models import (
    Challenge, ChallengeStatus, Difficulty, Event, EventStatus,
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
