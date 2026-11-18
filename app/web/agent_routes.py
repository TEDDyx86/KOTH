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
