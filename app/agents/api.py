from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import Session

from ..database import get_session
from ..tooling.config import load_tools_config
from ..tooling.runner import ToolRunner
from .config import load_agents, load_models
from .jury import JuryAgent
from .models import resolve_for_role
from .orchestrator import Orchestrator
from .specialists import SpecialistRunner

router = APIRouter()

SPECIALIST_CATEGORIES = ("web", "pwn", "crypto", "reversing", "forensics", "osint")


def _persona_text(name: str) -> str:
    p = Path(__file__).resolve().parent.parent.parent / "agents" / "personas" / f"{name}.md"
    if p.exists():
        return p.read_text(encoding="utf-8")
    return f"Você é o agente '{name}' deste framework de CTF. Terse. Sem disclaimers."


def _make_orchestrator(storage_dir) -> Orchestrator:
    """Builds the real orchestrator from configs (overridable in tests)."""
    models_cfg = load_models()
    agents_cfg = load_agents()
    runner = ToolRunner(load_tools_config(), storage_dir)
    jury = JuryAgent(resolve_for_role("jury", agents_cfg.roles["jury"].model, models_cfg),
                     persona_text=_persona_text("jury"))
    specialists = {
        cat: SpecialistRunner(
            resolve_for_role(f"specialist_{cat}", "fast", models_cfg),
            persona_text=_persona_text(f"specialist_{cat}"))
        for cat in SPECIALIST_CATEGORIES
    }
    return Orchestrator(runner=runner, jury=jury, specialists=specialists,
                        agents_limits=agents_cfg.limits)


def get_orchestrator(request: Request) -> Orchestrator:
    """Shared accessor — the web panel routes reuse the same instance."""
    orch = getattr(request.app.state, "_orchestrator", None)
    if orch is None:
        orch = _make_orchestrator(request.app.state.storage)
        request.app.state._orchestrator = orch
    return orch


class ApproveIn(BaseModel):
    note: str | None = None


class RejectIn(BaseModel):
    feedback: str


@router.post("/{challenge_id}/start", status_code=201)
def start_session(challenge_id: int, request: Request,
                  session: Session = Depends(get_session)):
    orch = get_orchestrator(request)
    try:
        agent_session = orch.start(session, challenge_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return agent_session.to_dict()


@router.get("/sessions/{session_id}")
def get_session_state(session_id: str, request: Request):
    orch = get_orchestrator(request)
    agent_session = orch.sessions.get(session_id)
    if agent_session is None:
        raise HTTPException(status_code=404, detail="unknown session")
    return agent_session.to_dict()


@router.post("/sessions/{session_id}/approve")
def approve(session_id: str, body: ApproveIn, request: Request,
            session: Session = Depends(get_session)):
    orch = get_orchestrator(request)
    agent_session = orch.sessions.get(session_id)
    if agent_session is None:
        raise HTTPException(status_code=404, detail="unknown session")
    try:
        updated = orch.approve(session, session_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return updated.to_dict()


@router.post("/sessions/{session_id}/reject")
def reject(session_id: str, body: RejectIn, request: Request,
           session: Session = Depends(get_session)):
    orch = get_orchestrator(request)
    agent_session = orch.sessions.get(session_id)
    if agent_session is None:
        raise HTTPException(status_code=404, detail="unknown session")
    return orch.reject(session, session_id, body.feedback).to_dict()
