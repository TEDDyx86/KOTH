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


def latest_session(orch, challenge_id: int) -> AgentSession | None:
    matches = [s for s in orch.sessions.values() if s.challenge_id == challenge_id]
    return max(matches, key=lambda s: s.created_at) if matches else None


def _challenge_or_404(session: Session, challenge_id: int):
    challenge = tracking.get_challenge(session, challenge_id)
    if challenge is None:
        raise HTTPException(status_code=404, detail="challenge not found")
    return challenge


def _session_or_404(orch, session_id: str) -> AgentSession:
    sess = orch.sessions.get(session_id)
    if sess is None:
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
    challenge = _challenge_or_404(session, challenge_id)
    return templates.TemplateResponse(request, "agent_panel.html",
                                      {"challenge": challenge})


@router.get("/challenges/{challenge_id}/agent/feed")
def agent_feed(request: Request, challenge_id: int,
               session: Session = Depends(get_session)):
    _challenge_or_404(session, challenge_id)
    sess = latest_session(get_orchestrator(request), challenge_id)
    return templates.TemplateResponse(request, "_agent_feed.html", {
        "challenge_id": challenge_id,
        "sess": sess.to_dict() if sess else None,
    })


@router.get("/challenges/{challenge_id}/agent/side")
def agent_side(request: Request, challenge_id: int,
               session: Session = Depends(get_session)):
    _challenge_or_404(session, challenge_id)
    sess = latest_session(get_orchestrator(request), challenge_id)
    return _side(request, challenge_id, sess)


@router.post("/challenges/{challenge_id}/agent/start")
def agent_start(request: Request, challenge_id: int,
                session: Session = Depends(get_session)):
    _challenge_or_404(session, challenge_id)
    orch = get_orchestrator(request)
    existing = latest_session(orch, challenge_id)
    if existing and existing.state in ("running", "awaiting_approval"):
        raise HTTPException(status_code=409, detail="session already active")
    try:
        sess = orch.start(session, challenge_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return _side(request, challenge_id, sess)


@router.post("/challenges/{challenge_id}/agent/sessions/{session_id}/approve")
def agent_approve(request: Request, challenge_id: int, session_id: str,
                  session: Session = Depends(get_session)):
    orch = get_orchestrator(request)
    _session_or_404(orch, session_id)
    try:
        sess = orch.approve(session, session_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return _side(request, challenge_id, sess)


@router.post("/challenges/{challenge_id}/agent/sessions/{session_id}/terminate")
def agent_terminate(request: Request, challenge_id: int, session_id: str,
                    session: Session = Depends(get_session)):
    orch = get_orchestrator(request)
    _session_or_404(orch, session_id)
    sess = orch.terminate(session, session_id, "rejected via panel")
    return _side(request, challenge_id, sess)


@router.post("/challenges/{challenge_id}/agent/sessions/{session_id}/adjust")
def agent_adjust(request: Request, challenge_id: int, session_id: str,
                 instruction: str = Form(...),
                 session: Session = Depends(get_session)):
    orch = get_orchestrator(request)
    _session_or_404(orch, session_id)
    sess = orch.reject(session, session_id, instruction)
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
