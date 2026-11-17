from dataclasses import dataclass, field

from sqlmodel import Session

from ..models import Challenge, Event


def _difficulty_weight(ch: Challenge) -> float:
    weights = {"easy": 0.0, "medium": 0.5, "hard": 1.0, "insane": 1.5}
    return weights.get((ch.difficulty.value if ch.difficulty else ""), 0.5)


def planner_rank(session: Session, event: Event) -> list[tuple[Challenge, float]]:
    """Deterministic prioritization: points weight + difficulty weight, solved last.
    score = points/100 + difficulty_weight + (1.0 if open else 0) - (10 if solved)."""
    challenges = session.query(Challenge).filter(Challenge.event_id == event.id).all()
    scored = []
    for ch in challenges:
        score = (ch.points / 100) + _difficulty_weight(ch)
        if str(ch.status) == "open" or (ch.status and ch.status.value == "open"):
            score += 1.0
        if ch.status and ch.status.value == "solved":
            score -= 10.0
        scored.append((ch, score))
    scored.sort(key=lambda t: t[1], reverse=True)
    return scored


@dataclass
class StopCriteria:
    max_attempts: int = 12
    max_minutes: int = 60


def stop_criteria(limits: dict) -> StopCriteria:
    return StopCriteria(
        max_attempts=int(limits.get("max_attempts", 12)),
        max_minutes=int(limits.get("max_minutes", 60)),
    )
