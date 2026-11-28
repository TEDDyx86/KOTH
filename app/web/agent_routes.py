"""Painel do Agente — rotas HTMX (feed, checkpoint, flag manual, sandbox /work, lote)."""
import asyncio
import json
from pathlib import Path
import shutil

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from sqlmodel import Session

from ..agents.api import get_orchestrator
from ..agents.orchestrator import AgentSession
from ..agents.planner import planner_rank
from ..agents.submission import validate_flag
from ..database import get_session
from ..models import Attachment, AttachmentType, ChallengeStatus, Event
from ..services import tracking
from ..templates import templates
from ..tooling.koth_mcp import safe_sandbox_file

router = APIRouter()


def _sessions_of(request: Request):
    """Orchestrator or None — model/provider config may be unavailable; the
    panel must keep rendering (idle) instead of 500ing the polling."""
    try:
        return get_orchestrator(request)
    except Exception as exc:  # ProviderUnavailable etc.
        request.state.agent_error = str(exc)
        return None


def latest_session(orch, challenge_id: int, session: Session | None = None) -> AgentSession | None:
    if hasattr(orch, "latest_for_challenge"):
        return orch.latest_for_challenge(session, challenge_id)
    matches = [s for s in orch.sessions.values() if s.challenge_id == challenge_id]
    return max(matches, key=lambda s: s.created_at) if matches else None


def _history_for(orch, challenge_id: int, session: Session | None = None) -> list[AgentSession]:
    if orch is None:
        return []
    if hasattr(orch, "list_for_challenge"):
        return orch.list_for_challenge(session, challenge_id)
    matches = [s for s in orch.sessions.values() if s.challenge_id == challenge_id]
    matches.sort(key=lambda s: s.created_at, reverse=True)
    return matches


def _selected_session(
    orch,
    challenge_id: int,
    session: Session | None = None,
    session_id: str | None = None,
) -> AgentSession | None:
    if orch is None:
        return None
    if session_id:
        s = (orch.get_session(session, session_id)
             if hasattr(orch, "get_session") else orch.sessions.get(session_id))
        if s is not None and s.challenge_id == challenge_id:
            return s
    return latest_session(orch, challenge_id, session=session)


def _challenge_or_404(session: Session, challenge_id: int):
    challenge = tracking.get_challenge(session, challenge_id)
    if challenge is None:
        raise HTTPException(status_code=404, detail="challenge not found")
    return challenge


def _session_or_404(orch, session_id: str, challenge_id: int | None = None,
                    session: Session | None = None) -> AgentSession:
    sess = (orch.get_session(session, session_id)
            if hasattr(orch, "get_session") else orch.sessions.get(session_id))
    if sess is None or (challenge_id is not None and sess.challenge_id != challenge_id):
        raise HTTPException(status_code=404, detail="unknown session")
    return sess


def _budget_max(orch, sess: AgentSession | None) -> int:
    limits = getattr(orch, "limits", None) if orch is not None else None
    base = int((limits or {}).get("max_attempts", 20) or 20)
    extra = int(getattr(sess, "extra_attempts", 0) or 0) if sess is not None else 0
    return max(base + extra, 1)


def _side(request: Request, challenge_id: int, sess: AgentSession | None, *, max_attempts: int = 20):
    return templates.TemplateResponse(request, "_agent_side.html", {
        "challenge_id": challenge_id,
        "sess": sess.to_dict() if sess else None,
        "max_attempts": max_attempts,
    })


def _sandbox_workdir(request: Request, session: Session, challenge, orch=None) -> Path:
    storage = getattr(request.app.state, "storage", None)
    if orch is not None and getattr(orch, "runner", None) is not None:
        try:
            orch.runner.stage_downloads(session, challenge)
        except Exception:
            pass
    base = storage.storage_dir if storage is not None else Path("storage")
    wd = base / "sandbox" / str(challenge.event_id) / str(challenge.id)
    wd.mkdir(parents=True, exist_ok=True)
    return wd


def _sandbox_files(workdir: Path) -> list[dict]:
    items: list[dict] = []
    for p in sorted(workdir.rglob("*")):
        if p.is_file():
            rel = str(p.relative_to(workdir)).replace("\\", "/")
            items.append({"rel": rel, "size": p.stat().st_size})
    return items[:120]


@router.get("/challenges/{challenge_id}/agent")
def agent_panel(request: Request, challenge_id: int,
                session_id: str | None = None,
                session: Session = Depends(get_session)):
    from .routes import nav_context

    challenge = _challenge_or_404(session, challenge_id)
    orch = _sessions_of(request)
    history = _history_for(orch, challenge_id, session=session)
    sess = _selected_session(orch, challenge_id, session=session, session_id=session_id)
    workdir = _sandbox_workdir(request, session, challenge, orch=orch)
    ctx = nav_context(session, active_nav=f"challenge-{challenge_id}")
    ctx["challenge"] = challenge
    ctx["sess"] = sess.to_dict() if sess else None
    ctx["max_attempts"] = _budget_max(orch, sess)
    ctx["selected_session_id"] = sess.id if (session_id and sess) else ""
    ctx["session_history"] = [s.to_dict() for s in history]
    ctx["sandbox_files"] = _sandbox_files(workdir)
    return templates.TemplateResponse(request, "agent_panel.html", ctx)


@router.get("/challenges/{challenge_id}/agent/feed")
def agent_feed(request: Request, challenge_id: int,
               session_id: str | None = None,
               session: Session = Depends(get_session)):
    _challenge_or_404(session, challenge_id)
    orch = _sessions_of(request)
    sess = _selected_session(orch, challenge_id, session=session, session_id=session_id)
    return templates.TemplateResponse(request, "_agent_feed.html", {
        "challenge_id": challenge_id,
        "sess": sess.to_dict() if sess else None,
        "max_attempts": _budget_max(orch, sess),
    })


@router.get("/challenges/{challenge_id}/agent/side")
def agent_side(request: Request, challenge_id: int,
               session_id: str | None = None,
               session: Session = Depends(get_session)):
    _challenge_or_404(session, challenge_id)
    orch = _sessions_of(request)
    sess = _selected_session(orch, challenge_id, session=session, session_id=session_id)
    return _side(request, challenge_id, sess, max_attempts=_budget_max(orch, sess))


def _format_sse(ev: dict) -> str:
    payload = json.dumps({"ts": ev.get("ts"), **(ev.get("data") or {})}, ensure_ascii=False)
    return f"id: {ev['id']}\nevent: {ev['type']}\ndata: {payload}\n\n"


@router.get("/challenges/{challenge_id}/agent/stream")
async def agent_stream(
    request: Request,
    challenge_id: int,
    session_id: str | None = None,
    max_events: int | None = None,
    session: Session = Depends(get_session),
):
    from ..agents.events import event_bus as default_bus

    _challenge_or_404(session, challenge_id)
    orch = _sessions_of(request)
    sess = _selected_session(orch, challenge_id, session=session, session_id=session_id)
    bus = getattr(orch, "event_bus", None) or default_bus

    raw_last = request.headers.get("last-event-id") or ""
    last_id = int(raw_last) if raw_last.strip().isdigit() else None
    target_sid = sess.id if sess is not None else f"challenge:{challenge_id}"

    async def _gen():
        loop = asyncio.get_running_loop()
        replay, q, unsub = bus.subscribe(target_sid, loop, last_event_id=last_id)
        sent = 0
        try:
            for item in replay:
                yield _format_sse(item)
                sent += 1
                if max_events is not None and sent >= max_events:
                    return

            if sess is not None and sess.state in ("solved", "gave_up", "rejected"):
                is_active = bool(orch and sess.id in getattr(orch, "_active_workers", {}))
                if not is_active:
                    yield f"event: done\ndata: {json.dumps({'state': sess.state})}\n\n"
                    return

            while True:
                if await request.is_disconnected():
                    break
                try:
                    item = await asyncio.wait_for(q.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield "event: heartbeat\ndata: {}\n\n"
                    continue
                yield _format_sse(item)
                sent += 1
                if max_events is not None and sent >= max_events:
                    break
        finally:
            unsub()

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/challenges/{challenge_id}/agent/start")
def agent_start(request: Request, challenge_id: int,
                mode: str = Form("supervised"),
                background: bool = Form(False),
                session: Session = Depends(get_session)):
    _challenge_or_404(session, challenge_id)
    orch = _sessions_of(request)
    if orch is None:
        raise HTTPException(
            status_code=503,
            detail=f"agent runtime unavailable: "
                   f"{getattr(request.state, 'agent_error', 'unknown')}")
    existing = latest_session(orch, challenge_id, session=session)
    if existing and existing.state in ("running", "awaiting_approval"):
        raise HTTPException(status_code=409, detail="session already active")
    try:
        if background and hasattr(orch, "start_background"):
            sess = orch.start_background(session.get_bind(), session, challenge_id, mode=mode)
        elif mode != "supervised":
            sess = orch.start(session, challenge_id, mode=mode)
        else:
            sess = orch.start(session, challenge_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return _side(request, challenge_id, sess, max_attempts=_budget_max(orch, sess))


@router.post("/challenges/{challenge_id}/agent/sessions/{session_id}/approve")
def agent_approve(request: Request, challenge_id: int, session_id: str,
                  mode: str | None = Form(None),
                  background: bool = Form(False),
                  session: Session = Depends(get_session)):
    orch = get_orchestrator(request)
    _session_or_404(orch, session_id, challenge_id=challenge_id, session=session)
    try:
        if background and hasattr(orch, "approve_background"):
            sess = orch.approve_background(session.get_bind(), session, session_id, mode=mode)
        elif mode is not None:
            sess = orch.approve(session, session_id, mode=mode)
        else:
            sess = orch.approve(session, session_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return _side(request, challenge_id, sess, max_attempts=_budget_max(orch, sess))


@router.post("/challenges/{challenge_id}/agent/sessions/{session_id}/terminate")
def agent_terminate(request: Request, challenge_id: int, session_id: str,
                    session: Session = Depends(get_session)):
    orch = get_orchestrator(request)
    _session_or_404(orch, session_id, challenge_id=challenge_id, session=session)
    sess = orch.terminate(session, session_id, "rejected via panel")
    return _side(request, challenge_id, sess, max_attempts=_budget_max(orch, sess))


@router.post("/challenges/{challenge_id}/agent/sessions/{session_id}/adjust")
def agent_adjust(request: Request, challenge_id: int, session_id: str,
                 instruction: str = Form(...),
                 session: Session = Depends(get_session)):
    orch = get_orchestrator(request)
    _session_or_404(orch, session_id, challenge_id=challenge_id, session=session)
    try:
        sess = orch.reject(session, session_id, instruction)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return _side(request, challenge_id, sess, max_attempts=_budget_max(orch, sess))


@router.post("/challenges/{challenge_id}/agent/sessions/{session_id}/update-target")
def agent_update_target(request: Request, challenge_id: int, session_id: str,
                        target: str = Form(...),
                        mode: str | None = Form(None),
                        background: bool = Form(True),
                        session: Session = Depends(get_session)):
    orch = get_orchestrator(request)
    _session_or_404(orch, session_id, challenge_id=challenge_id, session=session)
    try:
        sess = orch.update_target_and_resume(
            session,
            session_id,
            target,
            engine=session.get_bind(),
            mode=mode,
            background=background,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return _side(request, challenge_id, sess, max_attempts=_budget_max(orch, sess))


@router.post("/challenges/{challenge_id}/agent/sessions/{session_id}/resume")
def agent_resume(request: Request, challenge_id: int, session_id: str,
                 mode: str | None = Form(None),
                 instruction: str = Form(""),
                 target: str = Form(""),
                 extra_attempts: str = Form(""),
                 clear_dead_ends: bool = Form(False),
                 rerun_recon: bool = Form(False),
                 background: bool = Form(True),
                 session: Session = Depends(get_session)):
    orch = get_orchestrator(request)
    _session_or_404(orch, session_id, challenge_id=challenge_id, session=session)
    extra_val: int | None = None
    raw_extra = (extra_attempts or "").strip()
    if raw_extra:
        if not raw_extra.isdigit():
            raise HTTPException(status_code=400, detail="extra_attempts must be a positive integer")
        extra_val = int(raw_extra)
    chosen_mode = mode if mode in ("supervised", "auto") else None
    try:
        if background and hasattr(orch, "resume_background"):
            sess = orch.resume_background(
                session.get_bind(),
                session,
                session_id,
                mode=chosen_mode,
                feedback=instruction.strip() or None,
                new_target=target.strip() or None,
                extra_attempts=extra_val,
                clear_dead_ends=clear_dead_ends,
                rerun_recon=rerun_recon,
            )
        else:
            sess = orch.resume(
                session,
                session_id,
                mode=chosen_mode,
                feedback=instruction.strip() or None,
                new_target=target.strip() or None,
                extra_attempts=extra_val,
                clear_dead_ends=clear_dead_ends,
                rerun_recon=rerun_recon,
            )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return _side(request, challenge_id, sess, max_attempts=_budget_max(orch, sess))


@router.post("/challenges/{challenge_id}/agent/sessions/{session_id}/approve-target")
def agent_approve_target(request: Request, challenge_id: int, session_id: str,
                         target: str = Form(...),
                         session: Session = Depends(get_session)):
    orch = get_orchestrator(request)
    _session_or_404(orch, session_id, challenge_id=challenge_id, session=session)
    try:
        sess = orch.approve_discovered_target(session, session_id, target)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return templates.TemplateResponse(request, "_agent_feed.html", {
        "challenge_id": challenge_id,
        "sess": sess.to_dict(),
        "max_attempts": _budget_max(orch, sess),
    })


@router.post("/challenges/{challenge_id}/agent/sessions/{session_id}/hint")
def agent_queue_hint(request: Request, challenge_id: int, session_id: str,
                     feedback: str = Form(...),
                     session: Session = Depends(get_session)):
    orch = get_orchestrator(request)
    _session_or_404(orch, session_id, challenge_id=challenge_id, session=session)
    try:
        sess = orch.queue_feedback(session, session_id, feedback)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _side(request, challenge_id, sess, max_attempts=_budget_max(orch, sess))


@router.get("/challenges/{challenge_id}/agent/sandbox")
def agent_sandbox_list(request: Request, challenge_id: int,
                       session: Session = Depends(get_session)):
    challenge = _challenge_or_404(session, challenge_id)
    orch = _sessions_of(request)
    workdir = _sandbox_workdir(request, session, challenge, orch=orch)
    return templates.TemplateResponse(request, "_agent_sandbox.html", {
        "challenge_id": challenge_id,
        "files": _sandbox_files(workdir),
    })


@router.get("/challenges/{challenge_id}/agent/sandbox/view")
def agent_sandbox_view(request: Request, challenge_id: int, path: str,
                       session: Session = Depends(get_session)):
    challenge = _challenge_or_404(session, challenge_id)
    orch = _sessions_of(request)
    workdir = _sandbox_workdir(request, session, challenge, orch=orch)
    try:
        target = safe_sandbox_file(workdir, path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    raw = target.read_bytes()[:16384]
    try:
        preview = raw.decode("utf-8")
        mode_lbl = "texto"
    except UnicodeDecodeError:
        preview = raw[:1024].hex(" ")
        mode_lbl = "hex"
    import html as html_lib
    body = (
        f'<div class="koth-card-soft">'
        f'<div class="mono" style="font-size:0.78rem; color:var(--muted); margin-bottom:0.35rem;">'
        f'{html_lib.escape(path)} ({mode_lbl}, {target.stat().st_size} B)</div>'
        f'<pre class="mono" style="margin:0; max-height:280px; overflow:auto; white-space:pre-wrap; font-size:0.78rem;">'
        f'{html_lib.escape(preview)}</pre></div>'
    )
    return HTMLResponse(content=body)


@router.get("/challenges/{challenge_id}/agent/sandbox/download")
def agent_sandbox_download(request: Request, challenge_id: int, path: str,
                           session: Session = Depends(get_session)):
    challenge = _challenge_or_404(session, challenge_id)
    orch = _sessions_of(request)
    workdir = _sandbox_workdir(request, session, challenge, orch=orch)
    try:
        target = safe_sandbox_file(workdir, path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return Response(
        content=target.read_bytes(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{target.name}"'},
    )


@router.post("/challenges/{challenge_id}/agent/sandbox/upload")
def agent_sandbox_upload(request: Request, challenge_id: int,
                         files: list[UploadFile] = File(...),
                         session: Session = Depends(get_session)):
    challenge = _challenge_or_404(session, challenge_id)
    storage = request.app.state.storage
    orch = _sessions_of(request)
    workdir = _sandbox_workdir(request, session, challenge, orch=orch)
    for up in files:
        if not up or not up.filename:
            continue
        data = up.file.read()
        dest = storage.save(challenge_id, up.filename, data)
        session.add(Attachment(
            challenge_id=challenge_id,
            name=up.filename,
            path=str(dest.relative_to(storage.storage_dir)),
            mime=up.content_type or "application/octet-stream",
            size=len(data),
            type=AttachmentType.download,
        ))
        safe_name = Path(up.filename).name
        if safe_name:
            shutil.copy2(dest, workdir / safe_name)
    session.commit()
    return templates.TemplateResponse(request, "_agent_sandbox.html", {
        "challenge_id": challenge_id,
        "files": _sandbox_files(workdir),
    })


@router.post("/dashboard/run-batch")
def dashboard_run_batch(request: Request,
                        count: int = Form(3),
                        event_id: str = Form(""),
                        session: Session = Depends(get_session)):
    orch = _sessions_of(request)
    if orch is None:
        raise HTTPException(status_code=503, detail="agent runtime unavailable")
    cap = max(1, min(int(count or 3), 5))
    eid = int(event_id) if event_id.strip().isdigit() else None
    ranked_pairs: list[tuple] = []
    events = [session.get(Event, eid)] if eid is not None else tracking.list_events(session)
    for ev in events:
        if ev is not None:
            ranked_pairs.extend(planner_rank(session, ev))
    ranked_pairs.sort(key=lambda t: t[1], reverse=True)
    open_ids = [
        ch.id for ch, _ in ranked_pairs
        if ch.id is not None and ch.status == ChallengeStatus.open
    ][:cap]
    if open_ids and hasattr(orch, "start_batch_background"):
        orch.start_batch_background(
            session.get_bind(),
            session,
            open_ids,
            mode="auto",
            max_concurrent=min(3, len(open_ids)),
        )
    return RedirectResponse("/dashboard?batch=started", status_code=303)


@router.post("/challenges/{challenge_id}/flag")
def submit_flag(request: Request, challenge_id: int, flag: str = Form(...),
                session: Session = Depends(get_session)):
    challenge = _challenge_or_404(session, challenge_id)
    candidate = flag.strip()
    result = validate_flag(session, challenge, candidate)
    if result.accepted:
        tracking.update_challenge(session, challenge_id,
                                  status=ChallengeStatus.solved, flag=candidate)
    return templates.TemplateResponse(request, "_flag_result.html",
                                      {"result": result, "flag": candidate})
