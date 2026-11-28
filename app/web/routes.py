from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlmodel import Session, select

from ..agents.cli_op import list_cli_messages, process_cli_turn
from ..agents.config import AGENTS_PATH, MODELS_PATH
from ..agents.planner import planner_rank
from ..agents.writeup import (
    build_arsenal_digest,
    build_challenge_writeup,
    build_event_writeup,
    build_proof_bundle_zip,
    persist_writeup_note_and_attachment,
    save_writeup_to_arsenal,
)
from ..database import get_session
from ..models import (
    Attachment, AttachmentType, Attempt, AttemptResult, AttemptType,
    ChallengeStatus, Difficulty, Event, EventStatus,
)
from ..services import tracking
from ..services.storage import StorageService
from ..templates import templates
from ..tooling.config import DEFAULT_PATH as TOOLS_PATH
from ..tooling.mcp_servers import DEFAULT_MCP_PATH as MCP_PATH, probe_mcp_server
from ..tooling.sandbox import current_sandbox_mode

router = APIRouter()


def nav_context(session: Session, active_nav: str = "") -> dict:
    """Shared sidebar navigation data across all KOTH pages."""
    events = tracking.list_events(session)
    event_rows = []
    for ev in events:
        chs = tracking.list_challenges(session, event_id=ev.id)
        solved = sum(1 for c in chs if c.status is ChallengeStatus.solved)
        event_rows.append((ev, solved, len(chs)))
    open_challenges = tracking.list_challenges(session, status=ChallengeStatus.open)
    return {
        "active_nav": active_nav,
        "nav_events": event_rows,
        "nav_open_challenges": open_challenges,
    }


def _safe_orchestrator(request: Request):
    try:
        from ..agents.api import get_orchestrator

        return get_orchestrator(request)
    except Exception:
        return None


def _cli_feed_ctx(request: Request, session: Session) -> dict:
    messages = list_cli_messages(session)
    orch = _safe_orchestrator(request)
    sessions_by_ch: dict[int, dict] = {}
    has_running = False
    challenge_map: dict[int, object] = {}
    for m in messages:
        if m.challenge_id and m.challenge_id not in challenge_map:
            ch = tracking.get_challenge(session, m.challenge_id)
            if ch:
                challenge_map[m.challenge_id] = ch
        if m.challenge_id and orch is not None and m.challenge_id not in sessions_by_ch:
            sess = (
                orch.latest_for_challenge(session, m.challenge_id)
                if hasattr(orch, "latest_for_challenge")
                else None
            )
            if sess is not None:
                d = sess.to_dict()
                sessions_by_ch[m.challenge_id] = d
                if d["state"] == "running":
                    has_running = True
    return {
        "cli_messages": messages,
        "cli_sessions": sessions_by_ch,
        "cli_challenges": challenge_map,
        "cli_has_running": has_running,
    }


def _parse_difficulty(raw: str) -> Difficulty | None:
    cleaned = raw.strip().lower()
    if not cleaned:
        return None
    try:
        return Difficulty(cleaned)
    except ValueError:
        return None


def _save_uploads(
    session: Session,
    storage: StorageService,
    challenge_id: int,
    uploads: list[UploadFile],
    attachment_type: AttachmentType = AttachmentType.download,
) -> None:
    for upload in uploads:
        if not upload or not upload.filename:
            continue
        data = upload.file.read()
        if not data and not upload.filename:
            continue
        path = storage.save(challenge_id, upload.filename, data)
        session.add(Attachment(
            challenge_id=challenge_id,
            name=upload.filename,
            path=str(path.relative_to(storage.storage_dir)),
            mime=upload.content_type or "application/octet-stream",
            size=len(data),
            type=attachment_type,
        ))
    session.commit()


def _challenge_redirect(
    request: Request, session: Session, challenge_id: int, go_to: str
) -> str:
    if go_to == "agent_auto":
        try:
            from ..agents.api import get_orchestrator

            orch = get_orchestrator(request)
            if hasattr(orch, "start_background"):
                orch.start_background(session.get_bind(), session, challenge_id, mode="auto")
            else:
                orch.start(session, challenge_id, mode="auto")
        except Exception:
            pass
        return f"/challenges/{challenge_id}/agent"
    if go_to == "agent":
        return f"/challenges/{challenge_id}/agent"
    return f"/challenges/{challenge_id}"


@router.get("/")
def cli_home(request: Request, session: Session = Depends(get_session)):
    ctx = nav_context(session, active_nav="cli")
    ctx.update(_cli_feed_ctx(request, session))
    events = tracking.list_events(session)
    ctx["event_list"] = events
    ctx["active_event"] = events[-1] if events else None
    return templates.TemplateResponse(request, "cli.html", ctx)


@router.get("/cli/feed")
def cli_feed(request: Request, session: Session = Depends(get_session)):
    return templates.TemplateResponse(request, "_cli_feed.html", _cli_feed_ctx(request, session))


@router.post("/cli/message")
def cli_message(
    request: Request,
    message: str = Form(""),
    event_id: str = Form(""),
    mode: str = Form("auto"),
    force_launch: bool = Form(False),
    files: list[UploadFile] = File(default=[]),
    session: Session = Depends(get_session),
):
    eid = int(event_id) if event_id.strip().isdigit() else None
    orch = _safe_orchestrator(request)
    process_cli_turn(
        session,
        request.app.state.storage,
        orch,
        message,
        event_id=eid,
        mode=mode if mode in ("auto", "supervised") else "auto",
        force_launch=force_launch,
        uploads=files,
    )
    return templates.TemplateResponse(request, "_cli_feed.html", _cli_feed_ctx(request, session))


@router.get("/dashboard")
def dashboard(request: Request, session: Session = Depends(get_session)):
    ctx = nav_context(session, active_nav="dashboard")
    events = tracking.list_events(session)
    all_challenges = tracking.list_challenges(session)
    challenge_map = {c.id: c for c in all_challenges}
    recent = list(session.exec(
        select(Attempt).order_by(Attempt.timestamp.desc()).limit(15)
    ).all())

    orch = _safe_orchestrator(request)
    challenge_states: dict[int, dict] = {}
    running_count = 0
    awaiting_count = 0
    total_cost_usd = 0.0
    for ch in ctx["nav_open_challenges"]:
        if orch is not None and hasattr(orch, "latest_for_challenge"):
            sess = orch.latest_for_challenge(session, ch.id)
            if sess is not None:
                st = sess.state
                sdict = sess.to_dict() if hasattr(sess, "to_dict") else {}
                cost = float(sdict.get("estimated_cost_usd", 0.0) or 0.0)
                total_cost_usd += cost
                challenge_states[ch.id] = {
                    "state": st,
                    "dispatch_count": sess.dispatch_count,
                    "cost_usd": cost,
                }
                if st == "running":
                    running_count += 1
                elif st == "awaiting_approval":
                    awaiting_count += 1

    event_cards = []
    for ev in events:
        chs = [c for c in all_challenges if c.event_id == ev.id]
        solved_chs = [c for c in chs if c.status is ChallengeStatus.solved]
        total_pts = sum(int(c.points or 0) for c in chs)
        earned_pts = sum(int(c.points or 0) for c in solved_chs)
        pct = round(100 * len(solved_chs) / len(chs)) if chs else 0
        event_cards.append({
            "event": ev,
            "solved": len(solved_chs),
            "total": len(chs),
            "open_count": len(chs) - len(solved_chs),
            "earned_pts": earned_pts,
            "total_pts": total_pts,
            "pct": pct,
        })

    solved_total = sum(1 for c in all_challenges if c.status is ChallengeStatus.solved)
    earned_total_pts = sum(
        int(c.points or 0) for c in all_challenges if c.status is ChallengeStatus.solved
    )
    total_pts_all = sum(int(c.points or 0) for c in all_challenges)
    open_categories = sorted({
        (c.category or "misc").strip().lower() for c in ctx["nav_open_challenges"]
    })

    ctx.update({
        "events": ctx["nav_events"],
        "event_cards": event_cards,
        "event_list": events,
        "open_challenges": ctx["nav_open_challenges"],
        "challenge_states": challenge_states,
        "challenge_map": challenge_map,
        "open_categories": open_categories,
        "recent_attempts": recent,
        "sandbox_mode": current_sandbox_mode(TOOLS_PATH),
        "dash_stats": {
            "open_count": len(ctx["nav_open_challenges"]),
            "solved_count": solved_total,
            "total_count": len(all_challenges),
            "earned_pts": earned_total_pts,
            "total_pts": total_pts_all,
            "running_count": running_count,
            "awaiting_count": awaiting_count,
            "events_count": len(events),
            "total_cost_usd": round(total_cost_usd, 4),
        },
    })
    return templates.TemplateResponse(request, "dashboard.html", ctx)


def _parse_analytics_filters(event_id: str | None, category: str | None, days: str | None):
    eid = int(event_id) if (event_id or "").strip().isdigit() else None
    cat = (category or "").strip() or None
    d_val = int(days) if (days or "").strip().isdigit() else None
    return eid, cat, d_val


@router.get("/analytics")
def analytics_page(
    request: Request,
    event_id: str = "",
    category: str = "",
    days: str = "",
    session: Session = Depends(get_session),
):
    from ..services.analytics import compute_analytics

    eid, cat, d_val = _parse_analytics_filters(event_id, category, days)
    ctx = nav_context(session, active_nav="analytics")
    data = compute_analytics(session, event_id=eid, category=cat, days=d_val)
    all_cats = sorted({
        (c.category or "misc").strip().lower() for c in tracking.list_challenges(session)
    })
    ctx.update({
        "analytics": data,
        "filter_event_id": eid or "",
        "filter_category": cat or "",
        "filter_days": d_val or "",
        "filter_categories": all_cats,
    })
    return templates.TemplateResponse(request, "analytics.html", ctx)


@router.get("/api/analytics")
def analytics_api(
    event_id: str = "",
    category: str = "",
    days: str = "",
    format: str = "json",
    session: Session = Depends(get_session),
):
    from ..services.analytics import analytics_to_csv, compute_analytics

    eid, cat, d_val = _parse_analytics_filters(event_id, category, days)
    data = compute_analytics(session, event_id=eid, category=cat, days=d_val)
    if (format or "").strip().lower() == "csv":
        return Response(
            content=analytics_to_csv(data),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="koth_benchmark.csv"'},
        )
    return JSONResponse(content=data)


@router.post("/events")
def create_event_form(
    request: Request,
    name: str = Form(...),
    flag_format: str = Form(""),
    session: Session = Depends(get_session),
):
    event = tracking.create_event(
        session, name.strip(), flag_format=flag_format.strip() or None
    )
    return RedirectResponse(f"/events/{event.id}", status_code=303)


def _safe_next(next_url: str | None, fallback: str) -> str:
    if next_url and next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return fallback


@router.post("/events/{event_id}/edit")
def edit_event_form(
    event_id: int,
    name: str = Form(...),
    url: str = Form(""),
    flag_format: str = Form(""),
    status: str = Form("upcoming"),
    next: str = Form(""),
    session: Session = Depends(get_session),
):
    try:
        ev_status = EventStatus(status)
    except ValueError:
        ev_status = EventStatus.upcoming
    try:
        tracking.update_event(
            session, event_id,
            name=name.strip(),
            url=url,
            flag_format=flag_format,
            status=ev_status,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="event not found")
    return RedirectResponse(_safe_next(next, f"/events/{event_id}"), status_code=303)


@router.post("/events/{event_id}/delete")
def delete_event_form(
    request: Request,
    event_id: int,
    next: str = Form(""),
    session: Session = Depends(get_session),
):
    storage = getattr(request.app.state, "storage", None)
    try:
        tracking.delete_event(session, event_id, storage=storage)
    except LookupError:
        raise HTTPException(status_code=404, detail="event not found")
    return RedirectResponse(_safe_next(next, "/dashboard"), status_code=303)


@router.post("/challenges/quick")
def create_challenge_quick(
    request: Request,
    event_id: str = Form(""),
    new_event_name: str = Form(""),
    title: str = Form(...),
    category: str = Form("misc"),
    points: int = Form(100),
    difficulty: str = Form(""),
    target: str | None = Form(None),
    tags: str = Form(""),
    description: str = Form(""),
    go_to: str = Form("agent"),
    files: list[UploadFile] = File(default=[]),
    session: Session = Depends(get_session),
):
    eid: int | None = int(event_id) if event_id.strip().isdigit() else None
    if eid is None:
        events = tracking.list_events(session)
        if new_event_name.strip():
            ev = tracking.create_event(session, new_event_name.strip())
            eid = ev.id
        elif events:
            eid = events[-1].id
        else:
            ev = tracking.create_event(session, "CTF Local")
            eid = ev.id

    ch = tracking.create_challenge(
        session, eid, title.strip(), category=category, points=points,
        difficulty=_parse_difficulty(difficulty), description=description,
        target=(target or "").strip() or None, tags=tags,
    )
    _save_uploads(session, request.app.state.storage, ch.id, files, AttachmentType.download)
    return RedirectResponse(_challenge_redirect(request, session, ch.id, go_to), status_code=303)


@router.get("/events/{event_id}")
def event_page(
    request: Request,
    event_id: int,
    category: str | None = None,
    status: str | None = None,
    sort: str | None = None,
    imported: str | None = None,
    skipped: str | None = None,
    session: Session = Depends(get_session),
):
    event = session.get(Event, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="event not found")
    challenge_status = ChallengeStatus(status) if status else None
    challenges = tracking.list_challenges(
        session, event_id=event_id, category=category, status=challenge_status
    )
    scores = {ch.id: round(score, 2) for ch, score in planner_rank(session, event)}
    if sort == "planner":
        challenges = sorted(challenges, key=lambda c: scores.get(c.id, 0.0), reverse=True)
    ctx = nav_context(session, active_nav=f"event-{event_id}")
    ctx.update({
        "event": event,
        "challenges": challenges,
        "scores": scores,
        "category": category,
        "status": status,
        "sort": sort,
        "imported": imported,
        "skipped": skipped,
    })
    return templates.TemplateResponse(request, "event.html", ctx)


@router.post("/events/{event_id}/import/ctfd")
def import_ctfd_form(
    request: Request,
    event_id: int,
    base_url: str = Form(...),
    api_token: str = Form(""),
    session_cookie: str = Form(""),
    session: Session = Depends(get_session),
):
    from ..services.importer import import_ctfd_challenges

    event = session.get(Event, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="event not found")
    try:
        res = import_ctfd_challenges(
            session,
            request.app.state.storage,
            event_id,
            base_url,
            api_token=api_token,
            session_cookie=session_cookie,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Falha ao importar CTFd: {exc}")
    return RedirectResponse(
        f"/events/{event_id}?imported={res['imported']}&skipped={res['skipped']}",
        status_code=303,
    )


@router.post("/events/{event_id}/challenges")
def create_challenge_form(
    request: Request,
    event_id: int,
    title: str = Form(...),
    category: str = Form("misc"),
    points: int = Form(100),
    difficulty: str = Form(""),
    target: str | None = Form(None),
    tags: str = Form(""),
    description: str = Form(""),
    go_to: str = Form("challenge"),
    files: list[UploadFile] = File(default=[]),
    session: Session = Depends(get_session),
):
    ch = tracking.create_challenge(
        session, event_id, title.strip(), category=category,
        points=points, difficulty=_parse_difficulty(difficulty),
        description=description, target=(target or "").strip() or None, tags=tags,
    )
    _save_uploads(session, request.app.state.storage, ch.id, files, AttachmentType.download)
    return RedirectResponse(_challenge_redirect(request, session, ch.id, go_to), status_code=303)


@router.get("/challenges/{challenge_id}")
def challenge_page(
    request: Request, challenge_id: int, session: Session = Depends(get_session)
):
    challenge = tracking.get_challenge(session, challenge_id)
    if challenge is None:
        raise HTTPException(status_code=404, detail="challenge not found")
    attachments = list(session.exec(
        select(Attachment).where(Attachment.challenge_id == challenge_id)
    ).all())
    ctx = nav_context(session, active_nav=f"challenge-{challenge_id}")
    ctx.update({
        "challenge": challenge,
        "summary": tracking.challenge_summary(session, challenge),
        "attempts": tracking.list_attempts(session, challenge_id),
        "notes": tracking.note_history(session, challenge_id),
        "attachments": attachments,
    })
    return templates.TemplateResponse(request, "challenge.html", ctx)


@router.post("/challenges/{challenge_id}/edit")
def edit_challenge_form(
    challenge_id: int,
    title: str = Form(...),
    category: str = Form("misc"),
    points: int = Form(100),
    difficulty: str = Form(""),
    status: str = Form(""),
    flag: str | None = Form(None),
    target: str = Form(""),
    tags: str = Form(""),
    description: str = Form(""),
    session: Session = Depends(get_session),
):
    parsed_diff = _parse_difficulty(difficulty)
    parsed_status = None
    if status.strip():
        try:
            parsed_status = ChallengeStatus(status.strip())
        except ValueError:
            parsed_status = None
    try:
        tracking.update_challenge(
            session, challenge_id,
            title=title.strip(),
            category=category,
            points=points,
            difficulty=parsed_diff,
            clear_difficulty=(parsed_diff is None),
            status=parsed_status,
            flag=flag,
            target=target.strip(),
            tags=tags,
            description=description,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="challenge not found")
    return RedirectResponse(f"/challenges/{challenge_id}", status_code=303)


@router.post("/challenges/{challenge_id}/delete")
def delete_challenge_form(
    request: Request,
    challenge_id: int,
    next: str = Form(""),
    session: Session = Depends(get_session),
):
    ch = tracking.get_challenge(session, challenge_id)
    if ch is None:
        raise HTTPException(status_code=404, detail="challenge not found")
    fallback = f"/events/{ch.event_id}"
    storage = getattr(request.app.state, "storage", None)
    tracking.delete_challenge(session, challenge_id, storage=storage)
    return RedirectResponse(_safe_next(next, fallback), status_code=303)


@router.post("/challenges/{challenge_id}/attempts")
def record_attempt_form(
    request: Request,
    challenge_id: int,
    description: str = Form(...),
    tool_command: str = Form(""),
    result: str = Form("failed"),
    learning: str = Form(""),
    session: Session = Depends(get_session),
):
    tracking.record_attempt(
        session, challenge_id, AttemptType.manual, description,
        AttemptResult(result), tool_command=tool_command or None, learning=learning,
    )
    return RedirectResponse(f"/challenges/{challenge_id}", status_code=303)


@router.post("/challenges/{challenge_id}/notes")
def add_note_form(
    request: Request,
    challenge_id: int,
    content: str = Form(...),
    session: Session = Depends(get_session),
):
    tracking.add_note(session, challenge_id, content)
    return RedirectResponse(f"/challenges/{challenge_id}", status_code=303)


@router.post("/challenges/{challenge_id}/attachments")
def upload_attachment_form(
    request: Request,
    challenge_id: int,
    file: UploadFile | None = File(default=None),
    files: list[UploadFile] = File(default=[]),
    type: str = Form("download"),
    session: Session = Depends(get_session),
):
    challenge = tracking.get_challenge(session, challenge_id)
    if challenge is None:
        raise HTTPException(status_code=404, detail="challenge not found")
    uploads = [f for f in [file, *files] if f is not None and f.filename]
    _save_uploads(session, request.app.state.storage, challenge_id, uploads, AttachmentType(type))
    return RedirectResponse(f"/challenges/{challenge_id}", status_code=303)


def _latest_session_for(request: Request, session: Session, challenge_id: int):
    orch = _safe_orchestrator(request)
    if orch is not None and hasattr(orch, "latest_for_challenge"):
        return orch.latest_for_challenge(session, challenge_id)
    return None


@router.get("/challenges/{challenge_id}/writeup.md")
def download_challenge_writeup(
    request: Request, challenge_id: int, session: Session = Depends(get_session)
):
    ch = tracking.get_challenge(session, challenge_id)
    if ch is None:
        raise HTTPException(status_code=404, detail="challenge not found")
    storage = getattr(request.app.state, "storage", None)
    sess = _latest_session_for(request, session, challenge_id)
    md = build_challenge_writeup(session, ch, agent_session=sess, storage=storage)
    slug = "".join(c if c.isalnum() or c in "-_" else "-" for c in ch.title.lower()).strip("-") or f"challenge-{challenge_id}"
    return Response(
        content=md,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{slug}-writeup.md"'},
    )


@router.get("/challenges/{challenge_id}/proof-bundle.zip")
def download_proof_bundle(
    request: Request, challenge_id: int, session: Session = Depends(get_session)
):
    ch = tracking.get_challenge(session, challenge_id)
    if ch is None:
        raise HTTPException(status_code=404, detail="challenge not found")
    storage = getattr(request.app.state, "storage", None)
    sess = _latest_session_for(request, session, challenge_id)
    zip_bytes = build_proof_bundle_zip(session, ch, agent_session=sess, storage=storage)
    slug = "".join(c if c.isalnum() or c in "-_" else "-" for c in ch.title.lower()).strip("-") or f"challenge-{challenge_id}"
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{slug}-proofs.zip"'},
    )


@router.post("/challenges/{challenge_id}/writeup/generate")
def generate_challenge_writeup(
    request: Request,
    challenge_id: int,
    next: str = Form(""),
    session: Session = Depends(get_session),
):
    ch = tracking.get_challenge(session, challenge_id)
    if ch is None:
        raise HTTPException(status_code=404, detail="challenge not found")
    storage = getattr(request.app.state, "storage", None)
    sess = _latest_session_for(request, session, challenge_id)
    md = build_challenge_writeup(session, ch, agent_session=sess, storage=storage)
    persist_writeup_note_and_attachment(session, ch, md, storage=storage)
    return RedirectResponse(_safe_next(next, f"/challenges/{challenge_id}"), status_code=303)


@router.post("/challenges/{challenge_id}/arsenal/save")
def save_challenge_to_arsenal(
    request: Request, challenge_id: int, session: Session = Depends(get_session)
):
    from ..knowledge.index import DEFAULT_KNOWLEDGE_ROOT

    ch = tracking.get_challenge(session, challenge_id)
    if ch is None:
        raise HTTPException(status_code=404, detail="challenge not found")
    storage = getattr(request.app.state, "storage", None)
    sess = _latest_session_for(request, session, challenge_id)
    md = build_arsenal_digest(session, ch, agent_session=sess, storage=storage)
    dest, count = save_writeup_to_arsenal(session, ch, md, knowledge_root=DEFAULT_KNOWLEDGE_ROOT)
    return templates.TemplateResponse(
        request,
        "_provider_test.html",
        {"ok": True, "msg": f"Técnica salva em {dest.name} · FTS5 reindexado ({count} docs)"},
    )


@router.get("/events/{event_id}/writeup.md")
def download_event_writeup(
    request: Request,
    event_id: int,
    only_solved: bool = False,
    session: Session = Depends(get_session),
):
    event = session.get(Event, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="event not found")
    storage = getattr(request.app.state, "storage", None)
    md = build_event_writeup(session, event, storage=storage, only_solved=only_solved)
    slug = "".join(c if c.isalnum() or c in "-_" else "-" for c in event.name.lower()).strip("-") or f"event-{event_id}"
    return Response(
        content=md,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{slug}-writeups.md"'},
    )
