from pathlib import Path

from sqlmodel import Session

from ..models import Attempt, AttemptType, Attachment, Challenge, Event
from ..services import tracking

CONTEXT_BUDGETS: dict[str, int] = {
    "op": 16_000,
    "jury": 48_000,
    "specialist": 32_000,
    "submission": 16_000,
}


def _truncate(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    # drop from the oldest end, keep a marker
    return "…(older context truncated)\n" + text[-max_chars:]


def challenge_card(session: Session, challenge: Challenge) -> str:
    event = session.get(Event, challenge.event_id)
    lines = [
        f"# Challenge: {challenge.title}",
        f"event: {event.name if event else challenge.event_id}",
        f"category: {challenge.category} | points: {challenge.points} | "
        f"difficulty: {challenge.difficulty} | status: {challenge.status}",
        f"target: {challenge.target or '(none)'}",
        f"flag_format: {event.flag_format if event else 'CTF{{.*}}'}",
        f"description: {challenge.description or '(none)'}",
    ]
    return "\n".join(lines)


def _artifact_lines(session: Session, challenge: Challenge) -> list[str]:
    rows = session.query(Attachment).filter(Attachment.challenge_id == challenge.id).all()
    return [f"- {a.name} ({a.type.value}, {a.size}b, path={a.path})" for a in rows]


def _attempt_lines(session: Session, challenge: Challenge, learning_clip: int = 200) -> list[str]:
    rows = session.query(Attempt).filter(Attempt.challenge_id == challenge.id)\
        .order_by(Attempt.id.desc()).all()
    lines = []
    for a in reversed(rows):
        learning = (a.learning or "").replace("\n", " ")[:learning_clip]
        lines.append(f"- [{a.id}] {a.type.value}|{a.result.value}|{a.description}"
                     + (f" | {learning}" if learning else "")
                     + (f" | cmd={a.tool_command}" if a.tool_command else ""))
    return lines


def jury_context(session: Session, challenge: Challenge, budget: int | None = None) -> str:
    budget = budget or CONTEXT_BUDGETS["jury"]
    parts = [challenge_card(session, challenge), "## Artifacts"]
    parts += (_artifact_lines(session, challenge) or ["(none)"])
    parts.append("## Recent attempts (newest last)")
    parts += (_attempt_lines(session, challenge) or ["(none)"])
    return _truncate("\n".join(parts), budget)


def specialist_context(session: Session, challenge: Challenge, order,
                       budget: int | None = None) -> str:
    budget = budget or CONTEXT_BUDGETS["specialist"]
    # imported at call time so tests can monkeypatch DEFAULT_KNOWLEDGE_ROOT
    from ..knowledge.index import DEFAULT_KNOWLEDGE_ROOT, KnowledgeIndex

    index = KnowledgeIndex(session, knowledge_root=DEFAULT_KNOWLEDGE_ROOT)
    query = f"{order.category} {order.task}".strip()
    hits = index.search(query, limit=3, category=order.category) or \
        index.search(query, limit=3)
    parts = [challenge_card(session, challenge),
             "## Task from Júri",
             f"category: {order.category}",
             f"task: {order.task}"]
    if order.hints:
        parts.append("hints: " + "; ".join(order.hints))
    parts.append(f"max tool runs this dispatch: {order.max_tool_runs}")
    if hits:
        parts.append("## Arsenal references (use these techniques)")
        for h in hits:
            parts.append(f"- {h['title']} ({h['path']}): {h['snippet']}")
    parts.append("## Artifacts")
    parts += (_artifact_lines(session, challenge) or ["(none)"])
    return _truncate("\n".join(parts), budget)


def submission_context(session: Session, challenge: Challenge, flag_candidate: str,
                       budget: int | None = None) -> str:
    budget = budget or CONTEXT_BUDGETS["submission"]
    event = session.get(Event, challenge.event_id)
    submitted = [a.tool_command for a in
                 session.query(Attempt).filter(Attempt.challenge_id == challenge.id,
                                               Attempt.type == AttemptType.submission).all()
                 if a.tool_command]
    parts = [
        challenge_card(session, challenge),
        f"event flag_format: {event.flag_format if event else 'CTF{{.*}}'}",
        f"flag_candidate: {flag_candidate}",
        "## Previously submitted (do not resubmit)",
        *(submitted or ["(none)"]),
    ]
    return _truncate("\n".join(parts), budget)
