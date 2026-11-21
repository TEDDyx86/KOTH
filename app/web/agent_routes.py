"""Painel do Agente — rotas HTMX (feed, checkpoint, flag manual)."""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlmodel import Session

from ..agents.api import get_orchestrator
from ..agents.orchestrator import AgentSession
from ..agents.submission import validate_flag
from ..database import get_session
from ..models import ChallengeStatus
from ..services import tracking
from ..templates import templates

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


def _side(request: Request, challenge_id: int, sess: AgentSession | None):
    return templates.TemplateResponse(request, "_agent_side.html", {
        "challenge_id": challenge_id,
        "sess": sess.to_dict() if sess else None,
    })


@router.get("/challenges/{challenge_id}/agent")
def agent_panel(request: Request, challenge_id: int,
                session: Session = Depends(get_session)):
    from .routes import nav_context

    challenge = _challenge_or_404(session, challenge_id)
    ctx = nav_context(session, active_nav=f"challenge-{challenge_id}")
    ctx["challenge"] = challenge
    return templates.TemplateResponse(request, "agent_panel.html", ctx)


@router.get("/challenges/{challenge_id}/agent/feed")
def agent_feed(request: Request, challenge_id: int,
               session: Session = Depends(get_session)):
    _challenge_or_404(session, challenge_id)
    orch = _sessions_of(request)
    sess = latest_session(orch, challenge_id, session=session) if orch else None
    return templates.TemplateResponse(request, "_agent_feed.html", {
        "challenge_id": challenge_id,
        "sess": sess.to_dict() if sess else None,
    })


@router.get("/challenges/{challenge_id}/agent/side")
def agent_side(request: Request, challenge_id: int,
               session: Session = Depends(get_session)):
    _challenge_or_404(session, challenge_id)
    orch = _sessions_of(request)
    sess = latest_session(orch, challenge_id, session=session) if orch else None
    return _side(request, challenge_id, sess)


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
    return _side(request, challenge_id, sess)


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
    return _side(request, challenge_id, sess)


@router.post("/challenges/{challenge_id}/agent/sessions/{session_id}/terminate")
def agent_terminate(request: Request, challenge_id: int, session_id: str,
                    session: Session = Depends(get_session)):
    orch = get_orchestrator(request)
    _session_or_404(orch, session_id, challenge_id=challenge_id, session=session)
    sess = orch.terminate(session, session_id, "rejected via panel")
    return _side(request, challenge_id, sess)


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
    return _side(request, challenge_id, sess)


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
