import os

from sqlmodel import Session, select

from ..models import Attempt, AttemptType, Attachment, AttachmentType, Challenge, Event
from ..services import tracking

CONTEXT_BUDGETS: dict[str, int] = {
    "op": 16_000,
    "jury": 48_000,
    "specialist": 32_000,
    "submission": 16_000,
}

DEFAULT_DISPLAY_FLAG_FORMAT = r"[A-Za-z0-9_-]{2,32}\{[^}\r\n]+\}"


def _truncate(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    # drop from the oldest end, keep a marker
    return "…(older context truncated)\n" + text[-max_chars:]


def _join_with_pinned_header(header: str, body: str, max_chars: int) -> str:
    """Keep `header` intact at the top and only truncate oldest lines of `body`.
    For tiny micro-budgets (< header + 220), fall back to tail-truncating the combined
    text so recent items remain visible."""
    if not body:
        return header[:max_chars] if max_chars > 0 and len(header) > max_chars else header
    full = f"{header}\n{body}"
    if max_chars <= 0 or len(full) <= max_chars:
        return full
    if max_chars < len(header) + 220:
        return _truncate(full, max_chars)
    remaining = max_chars - len(header) - 2
    trimmed_body = _truncate(body, remaining)
    return f"{header}\n{trimmed_body}"


def effective_flag_format(session: Session, challenge: Challenge) -> str:
    if getattr(challenge, "flag_format", None):
        return challenge.flag_format
    event = session.get(Event, challenge.event_id) if session is not None else None
    if event and event.flag_format:
        return event.flag_format
    return os.environ.get("CTF_FLAG_FORMAT") or DEFAULT_DISPLAY_FLAG_FORMAT


def challenge_card(session: Session, challenge: Challenge) -> str:
    event = session.get(Event, challenge.event_id)
    fmt = effective_flag_format(session, challenge)
    lines = [
        f"# Challenge: {challenge.title}",
        f"event: {event.name if event else challenge.event_id}",
        f"category: {challenge.category} | points: {challenge.points} | "
        f"difficulty: {challenge.difficulty} | status: {challenge.status}",
        f"target: {challenge.target or '(none)'}",
        f"flag_format: {fmt}",
        f"description: {challenge.description or '(none)'}",
    ]
    return "\n".join(lines)


def _artifact_lines(
    session: Session,
    challenge: Challenge,
    *,
    in_sandbox: bool = False,
    include_outputs: bool = False,
) -> list[str]:
    rows = list(session.exec(select(Attachment).where(Attachment.challenge_id == challenge.id)).all())
    lines = []
    output_count = 0
    for a in rows:
        t_val = a.type.value if hasattr(a.type, "value") else str(a.type)
        if not include_outputs and t_val == AttachmentType.output.value:
            output_count += 1
            continue
        hint = f", cwd_file={a.name}" if in_sandbox and t_val == "download" else ""
        lines.append(f"- {a.name} ({t_val}, {a.size}b, path={a.path}{hint})")
    if output_count:
        lines.append(f"- (+{output_count} tool output file(s) saved in storage; use attempt logs for summaries)")
    return lines


def _note_lines(session: Session, challenge: Challenge, *, max_notes: int = 8, clip: int = 1200) -> list[str]:
    notes = tracking.note_history(session, challenge.id)
    kept: list[str] = []
    for n in notes:
        txt = (n.content or "").strip()
        if not txt or txt.startswith("# Writeup —"):
            continue
        if len(txt) > clip:
            txt = txt[:clip] + "…(truncated)"
        kept.append(f"- [{n.id}] {txt}")
        if len(kept) >= max_notes:
            break
    kept.reverse()
    return kept


def _attempt_lines(
    session: Session,
    challenge: Challenge,
    learning_clip: int = 450,
    *,
    limit: int | None = None,
) -> list[str]:
    stmt = select(Attempt).where(Attempt.challenge_id == challenge.id).order_by(Attempt.id.desc())
    if limit is not None:
        stmt = stmt.limit(limit)
    rows = list(session.exec(stmt).all())
    lines = []
    for a in reversed(rows):
        learning = (a.learning or "").replace("\n", " ")[:learning_clip]
        t_val = a.type.value if hasattr(a.type, "value") else str(a.type)
        r_val = a.result.value if hasattr(a.result, "value") else str(a.result)
        lines.append(f"- [{a.id}] {t_val}|{r_val}|{a.description}"
                     + (f" | {learning}" if learning else "")
                     + (f" | cmd={a.tool_command}" if a.tool_command else ""))
    return lines


def _blackboard_lines(blackboard: dict | None) -> list[str]:
    if not blackboard:
        return []
    lines: list[str] = []
    facts = blackboard.get("confirmed_facts") or []
    dead_ends = blackboard.get("dead_ends") or []
    hyps = blackboard.get("active_hypotheses") or []
    targets = blackboard.get("discovered_targets") or []
    if not any((facts, dead_ends, hyps, targets)):
        return []
    lines.append("## Blackboard (Structured State)")
    if targets:
        lines.append("discovered_targets: " + ", ".join(str(t) for t in targets[-6:]))
    if facts:
        lines.append("confirmed_facts:")
        lines.extend(f"  - {f}" for f in facts[-15:])
    if dead_ends:
        lines.append("dead_ends (DO NOT REPEAT):")
        lines.extend(f"  - {d}" for d in dead_ends[-15:])
    if hyps:
        lines.append("active_hypotheses:")
        lines.extend(f"  - {h}" for h in hyps[-8:])
    return lines


def _dedupe_command_summary(session: Session, challenge: Challenge, limit: int = 24) -> list[str]:
    rows = list(session.exec(
        select(Attempt)
        .where(Attempt.challenge_id == challenge.id, Attempt.type == AttemptType.tool)
        .order_by(Attempt.id.desc())
        .limit(limit)
    ).all())
    if not rows:
        return []
    seen: dict[str, int] = {}
    ordered: list[str] = []
    for a in rows:
        cmd = (a.tool_command or a.description or "").strip()
        if not cmd:
            continue
        if cmd not in seen:
            ordered.append(cmd[:140])
        seen[cmd] = seen.get(cmd, 0) + 1
    out = []
    for cmd in ordered[:14]:
        cnt = seen[cmd]
        suffix = f" (x{cnt})" if cnt > 1 else ""
        out.append(f"- {cmd}{suffix}")
    return out


def jury_context(
    session: Session,
    challenge: Challenge,
    budget: int | None = None,
    *,
    blackboard: dict | None = None,
) -> str:
    budget = budget or CONTEXT_BUDGETS["jury"]
    pinned_parts = [challenge_card(session, challenge), "## Artifacts"]
    pinned_parts += (_artifact_lines(session, challenge) or ["(none)"])
    bb = _blackboard_lines(blackboard)
    if bb:
        pinned_parts += bb
    notes = _note_lines(session, challenge)
    if notes:
        pinned_parts.append("## Operator notes")
        pinned_parts += notes

    body_parts = ["## Recent attempts (newest last)"]
    body_parts += (_attempt_lines(session, challenge, limit=40) or ["(none)"])
    return _join_with_pinned_header("\n".join(pinned_parts), "\n".join(body_parts), budget)


def specialist_context(
    session: Session,
    challenge: Challenge,
    order,
    budget: int | None = None,
    *,
    blackboard: dict | None = None,
) -> str:
    budget = budget or CONTEXT_BUDGETS["specialist"]
    # imported at call time so tests can monkeypatch DEFAULT_KNOWLEDGE_ROOT
    from ..knowledge.index import DEFAULT_KNOWLEDGE_ROOT, KnowledgeIndex

    index = KnowledgeIndex(session, knowledge_root=DEFAULT_KNOWLEDGE_ROOT)
    query = f"{order.category} {order.task}".strip()
    hits = index.search(query, limit=3, category=order.category) or \
        index.search(query, limit=3)

    pinned_parts = [
        challenge_card(session, challenge),
        "## Task from Júri",
        f"category: {order.category}",
        f"task: {order.task}",
    ]
    if order.hints:
        pinned_parts.append("hints: " + "; ".join(order.hints))
    pinned_parts.append(f"max tool runs this dispatch: {order.max_tool_runs}")

    bb = _blackboard_lines(blackboard)
    if bb:
        pinned_parts += bb

    if hits:
        pinned_parts.append("## Arsenal references (use these techniques)")
        for h in hits:
            pinned_parts.append(f"- {h['title']} ({h['path']}): {h['snippet']}")

    pinned_parts.append("## Artifacts (download files are staged in sandbox cwd by filename)")
    pinned_parts += (_artifact_lines(session, challenge, in_sandbox=True) or ["(none)"])

    body_parts: list[str] = []
    if session is not None:
        recent = _attempt_lines(session, challenge, learning_clip=240, limit=18)
        if recent:
            body_parts.append("## Recent attempts & outputs (do not repeat identical commands)")
            body_parts += recent
        tried = _dedupe_command_summary(session, challenge)
        if tried:
            body_parts.append("## Commands already executed (avoid duplicating)")
            body_parts += tried

    return _join_with_pinned_header("\n".join(pinned_parts), "\n".join(body_parts), budget)


def submission_context(session: Session, challenge: Challenge, flag_candidate: str,
                       budget: int | None = None) -> str:
    budget = budget or CONTEXT_BUDGETS["submission"]
    fmt = effective_flag_format(session, challenge)
    submitted = [a.tool_command for a in
                 session.exec(select(Attempt).where(
                     Attempt.challenge_id == challenge.id,
                     Attempt.type == AttemptType.submission)).all()
                 if a.tool_command]
    parts = [
        challenge_card(session, challenge),
        f"effective flag_format: {fmt}",
        f"flag_candidate: {flag_candidate}",
        "## Previously submitted (do not resubmit)",
        *(submitted or ["(none)"]),
    ]
    return _truncate("\n".join(parts), budget)
