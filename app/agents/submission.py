import os
import re
from dataclasses import dataclass

from sqlmodel import Session, select

from ..models import Attempt, AttemptResult, AttemptType, Challenge, Event
from ..services import tracking
from .context import submission_context

DEFAULT_FLAG_FORMAT = r"[A-Za-z0-9_-]{2,32}\{[^}\r\n]+\}"
_PLACEHOLDER_BODIES = {
    "...",
    "flag",
    "flag_falsa",
    "fake",
    "example",
    "placeholder",
    "test",
    "todo",
    "none",
    "null",
    "redacted",
}


@dataclass
class SubmissionResult:
    accepted: bool
    reason: str


def is_placeholder_flag(candidate: str | None) -> bool:
    cand = (candidate or "").strip()
    if not cand or cand.lower() in ("none", "null", "n/a", "-", "unknown"):
        return True
    if "{" in cand and cand.endswith("}"):
        inner = cand.split("{", 1)[1][:-1].strip().lower()
        if not inner or inner in _PLACEHOLDER_BODIES or "..." in inner:
            return True
    return False


def validate_flag(session: Session, challenge: Challenge, flag_candidate: str) -> SubmissionResult:
    """Deterministic flag gate: regex against challenge/event format + duplicate check.
    Records the outcome as an Attempt(type=submission). Manual submission:
    the framework never calls a platform API."""
    event = session.get(Event, challenge.event_id)
    pattern = (
        getattr(challenge, "flag_format", None)
        or (event.flag_format if event and event.flag_format else None)
        or os.environ.get("CTF_FLAG_FORMAT")
        or DEFAULT_FLAG_FORMAT
    )

    if is_placeholder_flag(flag_candidate):
        tracking.record_attempt(
            session, challenge.id, AttemptType.submission,
            "flag rejected (placeholder)", AttemptResult.failed,
            tool_command=flag_candidate, learning="placeholder or empty flag candidate")
        return SubmissionResult(accepted=False, reason="flag candidate is a placeholder")

    if not re.fullmatch(pattern, flag_candidate or ""):
        tracking.record_attempt(
            session, challenge.id, AttemptType.submission,
            "flag rejected (format)", AttemptResult.failed,
            tool_command=flag_candidate, learning="does not match challenge/event flag_format")
        return SubmissionResult(accepted=False,
                                reason=f"flag does not match format {pattern!r}")

    duplicates = list(session.exec(select(Attempt).where(
        Attempt.challenge_id == challenge.id,
        Attempt.type == AttemptType.submission,
        Attempt.result == AttemptResult.solved,
        Attempt.tool_command == flag_candidate)).all())
    if duplicates:
        tracking.record_attempt(
            session, challenge.id, AttemptType.submission,
            "flag rejected (duplicate)", AttemptResult.failed,
            tool_command=flag_candidate, learning="already submitted before")
        return SubmissionResult(accepted=False, reason="duplicate submission")

    tracking.record_attempt(
        session, challenge.id, AttemptType.submission,
        "flag validated — ready for manual submission", AttemptResult.solved,
        tool_command=flag_candidate, flag=flag_candidate,
        learning=submission_context(session, challenge, flag_candidate)[:1500])
    return SubmissionResult(accepted=True, reason="valid — submit manually on the platform")
