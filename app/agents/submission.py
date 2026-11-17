import re
from dataclasses import dataclass

from sqlmodel import Session

from ..models import Attempt, AttemptResult, AttemptType, Challenge, Event
from ..services import tracking
from .context import submission_context

DEFAULT_FLAG_FORMAT = r"CTF\{.*\}"


@dataclass
class SubmissionResult:
    accepted: bool
    reason: str


def validate_flag(session: Session, challenge: Challenge, flag_candidate: str) -> SubmissionResult:
    """Deterministic flag gate: regex against the event format + duplicate check in
    the PE. Records the outcome as an Attempt(type=submission). Manual submission:
    the framework never calls a platform API."""
    event = session.get(Event, challenge.event_id)
    pattern = (event.flag_format if event and event.flag_format
               else DEFAULT_FLAG_FORMAT)

    if not re.fullmatch(pattern, flag_candidate or ""):
        result = tracking.record_attempt(
            session, challenge.id, AttemptType.submission,
            "flag rejected (format)", AttemptResult.failed,
            tool_command=flag_candidate, learning="does not match event flag_format")
        return SubmissionResult(accepted=False,
                                reason=f"flag does not match format {pattern!r}")

    duplicates = session.query(Attempt).filter(
        Attempt.challenge_id == challenge.id,
        Attempt.type == AttemptType.submission,
        Attempt.tool_command == flag_candidate).all()
    if duplicates:
        result = tracking.record_attempt(
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
