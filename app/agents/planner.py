from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from sqlmodel import Session, select

from ..models import Challenge, Event


def _difficulty_weight(ch: Challenge) -> float:
    weights = {"easy": 0.0, "medium": 0.5, "hard": 1.0, "insane": 1.5}
    return weights.get((ch.difficulty.value if ch.difficulty else ""), 0.5)


def planner_rank(session: Session, event: Event) -> list[tuple[Challenge, float]]:
    """Deterministic prioritization: points weight + difficulty weight, solved last.
    score = points/100 + difficulty_weight + (1.0 if open else 0) - (10 if solved)."""
    challenges = list(session.exec(select(Challenge).where(Challenge.event_id == event.id)).all())
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


@dataclass
class StopCheckResult:
    stop: bool
    reason: str | None = None


def stop_criteria(limits: dict) -> StopCriteria:
    return StopCriteria(
        max_attempts=int(limits.get("max_attempts", 12)),
        max_minutes=int(limits.get("max_minutes", 60)),
    )


def check_stop_criteria(created_at: datetime, dispatch_count: int, limits: dict,
                        now: datetime | None = None) -> StopCheckResult:
    """Evaluate PL stop criteria against session dispatches and elapsed wall time."""
    if not limits:
        return StopCheckResult(stop=False)
    crit = stop_criteria(limits)
    if crit.max_attempts > 0 and dispatch_count >= crit.max_attempts:
        return StopCheckResult(
            stop=True,
            reason=f"planner stop criteria: max_attempts reached ({dispatch_count}/{crit.max_attempts})",
        )
    cur = now or datetime.now(timezone.utc)
    start = created_at if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc)
    elapsed_min = (cur - start).total_seconds() / 60.0
    if crit.max_minutes > 0 and elapsed_min >= crit.max_minutes:
        return StopCheckResult(
            stop=True,
            reason=f"planner stop criteria: max_minutes reached ({elapsed_min:.1f}/{crit.max_minutes}m)",
        )
    return StopCheckResult(stop=False)


def reassess_strategy(created_at: datetime, dispatch_count: int,
                      reports: list, limits: dict) -> tuple[Literal["CONTINUE", "RECON", "GIVE_UP"], str]:
    """PL strategy reassessment after unresolved specialist dispatches."""
    stop = check_stop_criteria(created_at, dispatch_count, limits)
    if stop.stop:
        return "GIVE_UP", stop.reason or "stop criteria reached"
    if len(reports) >= 2 and all(getattr(r, "status", None) == "FAILED" for r in reports[-2:]):
        return "RECON", "two consecutive specialist failures — refresh recon before next dispatch"
    return "CONTINUE", "within budget"
