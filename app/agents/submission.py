import os
import re
from dataclasses import dataclass

from sqlmodel import Session, select

from ..models import Attachment, AttachmentType, Attempt, AttemptResult, AttemptType, Challenge, Event
from ..services import tracking
from .context import submission_context
from .payloads import SpecialistReport

DEFAULT_FLAG_FORMAT = r"[A-Za-z0-9_-]{2,32}\{[^}\r\n]+\}"
_UNRESOLVED_NEXT_HINTS = (
    "to obtain",
    "to confirm",
    "obtain/confirm",
    "confirm the flag",
    "retrieve the flag",
    "get the flag",
    "submit derived",
    "para obter",
    "confirmar a flag",
    "ainda falta",
)
_CONN_REFUSED_MARKERS = (
    "curl: (7)",
    "rc=7",
    "winerror 10061",
    "connectionrefusederror",
    "connection refused",
    "err_connection_refused",
    "econnrefused",
    "actively refused",
    "recusou ativamente",
    "nenhuma conexão pôde ser feita",
    "nenhuma conexao pode ser feita",
    "could not connect to server",
)
_CONN_SUCCESS_MARKERS = (
    "http/1.",
    "http/2",
    "200 ok",
    "302 found",
    "401 unauthorized",
    "404 not found",
    "banner",
)
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


def _has_download_attachments(session: Session | None, challenge: Challenge) -> bool:
    if session is None or challenge.id is None:
        return False
    try:
        row = session.exec(
            select(Attachment).where(
                Attachment.challenge_id == challenge.id,
                Attachment.type == AttachmentType.download,
            )
        ).first()
        return row is not None
    except Exception:
        return False


def _has_tool_attempts(session: Session | None, challenge: Challenge) -> bool:
    if session is None or challenge.id is None:
        return False
    try:
        row = session.exec(
            select(Attempt).where(
                Attempt.challenge_id == challenge.id,
                Attempt.type == AttemptType.tool,
            )
        ).first()
        return row is not None
    except Exception:
        return False


def has_verified_flag_evidence(
    session: Session | None,
    challenge: Challenge,
    report: SpecialistReport | None,
    candidate: str,
    tool_outputs: list[str],
    *,
    is_submit: bool = False,
) -> tuple[bool, str | None]:
    """Guard SOLVED/SUBMIT against fabricated or placeholder flags."""
    prefix = "submit blocked" if is_submit else "fast-path blocked"
    if is_placeholder_flag(candidate):
        return False, f"{prefix} for {candidate!r}: placeholder/empty value is not a real flag"

    if report is not None:
        next_low = (report.next_step or "").strip().lower()
        if next_low and any(hint in next_low for hint in _UNRESOLVED_NEXT_HINTS):
            return (
                False,
                f"{prefix} for {candidate!r}: report.next_step still indicates obtaining/confirming the flag",
            )

    has_downloads = _has_download_attachments(session, challenge)
    has_tool_runs = bool(tool_outputs) or _has_tool_attempts(session, challenge)
    requires_evidence = (
        (has_tool_runs and (bool(challenge.target) or has_downloads))
        if is_submit
        else (bool(tool_outputs) or bool(challenge.target) or has_downloads)
    )
    if requires_evidence:
        haystack_parts = list(tool_outputs)
        if session is not None and challenge.id is not None:
            try:
                for att in tracking.list_attempts(session, challenge.id):
                    if att.type != AttemptType.submission and att.learning:
                        haystack_parts.append(att.learning)
            except Exception:
                pass
        if candidate not in "\n".join(haystack_parts):
            return (
                False,
                f"{prefix} for {candidate!r}: flag literal was not observed in any tool/API output "
                "(do not wrap derived keys or hashes in flag format; execute/verify until the server or tool prints the literal flag)",
            )
    return True, None


def detect_target_unreachable(challenge: Challenge, outputs: list[str]) -> str | None:
    if not challenge.target or not outputs:
        return None
    refused = 0
    succeeded = 0
    for out in outputs:
        low = (out or "").lower()
        if any(m in low for m in _CONN_REFUSED_MARKERS):
            refused += 1
        elif any(m in low for m in _CONN_SUCCESS_MARKERS):
            succeeded += 1
    if refused >= 3 and succeeded == 0:
        return (
            f"Target {challenge.target} appears unreachable ({refused} connection-refused errors, "
            "0 successful responses). Update the target port/URL and resume when the instance is back."
        )
    return None

