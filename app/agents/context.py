from sqlmodel import Session, select

from ..models import Attempt, AttemptType, Attachment, Challenge, Event, Note
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


def _artifact_lines(session: Session, challenge: Challenge, *, in_sandbox: bool = False) -> list[str]:
    rows = list(session.exec(select(Attachment).where(Attachment.challenge_id == challenge.id)).all())
    lines = []
    for a in rows:
        t_val = a.type.value if hasattr(a.type, "value") else str(a.type)
        hint = f", cwd_file={a.name}" if in_sandbox and t_val == "download" else ""
        lines.append(f"- {a.name} ({t_val}, {a.size}b, path={a.path}{hint})")
    return lines


def _note_lines(session: Session, challenge: Challenge) -> list[str]:
    notes = tracking.note_history(session, challenge.id)
    return [f"- [{n.id}] {n.content}" for n in reversed(notes)]


def _attempt_lines(session: Session, challenge: Challenge, learning_clip: int = 450) -> list[str]:
    rows = list(session.exec(
        select(Attempt).where(Attempt.challenge_id == challenge.id).order_by(Attempt.id.desc())
    ).all())
    lines = []
    for a in reversed(rows):
        learning = (a.learning or "").replace("\n", " ")[:learning_clip]
        t_val = a.type.value if hasattr(a.type, "value") else str(a.type)
        r_val = a.result.value if hasattr(a.result, "value") else str(a.result)
        lines.append(f"- [{a.id}] {t_val}|{r_val}|{a.description}"
                     + (f" | {learning}" if learning else "")
                     + (f" | cmd={a.tool_command}" if a.tool_command else ""))
    return lines


def jury_context(session: Session, challenge: Challenge, budget: int | None = None) -> str:
    budget = budget or CONTEXT_BUDGETS["jury"]
    parts = [challenge_card(session, challenge), "## Artifacts"]
    parts += (_artifact_lines(session, challenge) or ["(none)"])
    notes = _note_lines(session, challenge)
    if notes:
        parts.append("## Operator notes")
        parts += notes
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
    parts.append("## Artifacts (download files are staged in sandbox cwd by filename)")
    parts += (_artifact_lines(session, challenge, in_sandbox=True) or ["(none)"])
    if session is not None:
        recent = _attempt_lines(session, challenge, learning_clip=320)[-6:]
        if recent:
            parts.append("## Recent attempts & outputs (do not repeat identical commands)")
            parts += recent
    return _truncate("\n".join(parts), budget)


def submission_context(session: Session, challenge: Challenge, flag_candidate: str,
                       budget: int | None = None) -> str:
    budget = budget or CONTEXT_BUDGETS["submission"]
    event = session.get(Event, challenge.event_id)
    submitted = [a.tool_command for a in
                 session.exec(select(Attempt).where(
                     Attempt.challenge_id == challenge.id,
                     Attempt.type == AttemptType.submission)).all()
                 if a.tool_command]
    parts = [
        challenge_card(session, challenge),
        f"event flag_format: {event.flag_format if event else 'CTF{{.*}}'}",
        f"flag_candidate: {flag_candidate}",
        "## Previously submitted (do not resubmit)",
        *(submitted or ["(none)"]),
    ]
    return _truncate("\n".join(parts), budget)
