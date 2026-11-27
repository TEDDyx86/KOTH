from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import Session

from ..database import get_session
from ..models import Event
from ..tooling.config import load_tools_config
from ..tooling.doctor import inventory
from ..tooling.runner import ToolRunner
from .config import load_agents, load_models
from .jury import JuryAgent
from .models import resolve_candidates_for_role
from .orchestrator import Orchestrator
from .planner import planner_rank
from .specialists import SpecialistRunner

router = APIRouter()

SPECIALIST_CATEGORIES = ("web", "pwn", "crypto", "reversing", "forensics", "osint", "misc")


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
    jury_role = agents_cfg.roles["jury"].model if "jury" in agents_cfg.roles else "strong"
    jury_models = resolve_candidates_for_role("jury", jury_role, models_cfg)
    jury = JuryAgent(jury_models[0], persona_text=_persona_text("jury"),
                     fallback_models=jury_models[1:])
    max_dispatch_tokens = int(agents_cfg.limits.get("max_dispatch_tokens", 350_000))
    specialists = {}
    for cat in SPECIALIST_CATEGORIES:
        role_key = f"specialist_{cat}"
        model_role = agents_cfg.roles[role_key].model if role_key in agents_cfg.roles else "fast"
        cands = resolve_candidates_for_role(role_key, model_role, models_cfg)
        specialists[cat] = SpecialistRunner(
            cands[0],
            persona_text=_persona_text(role_key),
            fallback_models=cands[1:],
            category=cat,
            max_dispatch_tokens=max_dispatch_tokens,
        )
    return Orchestrator(runner=runner, jury=jury, specialists=specialists,
                        agents_limits=agents_cfg.limits)


def get_orchestrator(request: Request) -> Orchestrator:
    """Shared accessor — the web panel routes reuse the same instance."""
    orch = getattr(request.app.state, "_orchestrator", None)
    if orch is None:
        orch = _make_orchestrator(request.app.state.storage)
        request.app.state._orchestrator = orch
    return orch


def reload_orchestrator(app) -> None:
    """Hot-reload runner/models/limits into the existing Orchestrator without losing active workers."""
    orch = getattr(app.state, "_orchestrator", None)
    if orch is None:
        return
    try:
        fresh = _make_orchestrator(app.state.storage)
    except Exception:
        with orch._lock:
            if not orch._active_workers:
                app.state._orchestrator = None
        return
    with orch._lock:
        orch.runner = fresh.runner
        orch.jury = fresh.jury
        orch.specialists = fresh.specialists
        orch.limits = fresh.limits


class StartIn(BaseModel):
    mode: Literal["supervised", "auto"] = "supervised"
    background: bool = False


class ApproveIn(BaseModel):
    note: str | None = None
    mode: Literal["supervised", "auto"] | None = None
    background: bool = False


class RejectIn(BaseModel):
    feedback: str


class TerminateIn(BaseModel):
    reason: str = ""


class ResumeIn(BaseModel):
    mode: Literal["supervised", "auto"] | None = None
    feedback: str | None = None
    target: str | None = None
    extra_attempts: int | None = None
    clear_dead_ends: bool = False
    rerun_recon: bool = False
    background: bool = False


class HintIn(BaseModel):
    feedback: str


@router.post("/{challenge_id}/start", status_code=201)
def start_session(challenge_id: int, request: Request,
                  body: StartIn | None = None,
                  session: Session = Depends(get_session)):
    orch = get_orchestrator(request)
    mode = body.mode if body else "supervised"
    background = body.background if body else False
    try:
        if background:
            agent_session = orch.start_background(
                session.get_bind(), session, challenge_id, mode=mode)
        else:
            agent_session = orch.start(session, challenge_id, mode=mode)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return agent_session.to_dict()


@router.get("/sessions/{session_id}")
def get_session_state(session_id: str, request: Request,
                      session: Session = Depends(get_session)):
    orch = get_orchestrator(request)
    agent_session = (orch.get_session(session, session_id)
                     if hasattr(orch, "get_session") else orch.sessions.get(session_id))
    if agent_session is None:
        raise HTTPException(status_code=404, detail="unknown session")
    return agent_session.to_dict()


@router.post("/sessions/{session_id}/approve")
def approve(session_id: str, body: ApproveIn, request: Request,
            session: Session = Depends(get_session)):
    orch = get_orchestrator(request)
    agent_session = (orch.get_session(session, session_id)
                     if hasattr(orch, "get_session") else orch.sessions.get(session_id))
    if agent_session is None:
        raise HTTPException(status_code=404, detail="unknown session")
    try:
        if body.background:
            updated = orch.approve_background(session.get_bind(), session, session_id, mode=body.mode)
        elif body.mode is not None:
            updated = orch.approve(session, session_id, mode=body.mode)
        else:
            updated = orch.approve(session, session_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return updated.to_dict()


@router.post("/sessions/{session_id}/hint")
def queue_session_hint(session_id: str, body: HintIn, request: Request,
                       session: Session = Depends(get_session)):
    orch = get_orchestrator(request)
    agent_session = (orch.get_session(session, session_id)
                     if hasattr(orch, "get_session") else orch.sessions.get(session_id))
    if agent_session is None:
        raise HTTPException(status_code=404, detail="unknown session")
    try:
        updated = orch.queue_feedback(session, session_id, body.feedback)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return updated.to_dict()


@router.post("/sessions/{session_id}/reject")
def reject(session_id: str, body: RejectIn, request: Request,
           session: Session = Depends(get_session)):
    orch = get_orchestrator(request)
    agent_session = (orch.get_session(session, session_id)
                     if hasattr(orch, "get_session") else orch.sessions.get(session_id))
    if agent_session is None:
        raise HTTPException(status_code=404, detail="unknown session")
    try:
        return orch.reject(session, session_id, body.feedback).to_dict()
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/sessions/{session_id}/terminate")
def terminate(session_id: str, request: Request,
              body: TerminateIn | None = None,
              session: Session = Depends(get_session)):
    orch = get_orchestrator(request)
    agent_session = (orch.get_session(session, session_id)
                     if hasattr(orch, "get_session") else orch.sessions.get(session_id))
    if agent_session is None:
        raise HTTPException(status_code=404, detail="unknown session")
    reason = body.reason if body else ""
    return orch.terminate(session, session_id, reason).to_dict()


@router.post("/sessions/{session_id}/resume")
def resume_session(session_id: str, request: Request,
                   body: ResumeIn | None = None,
                   session: Session = Depends(get_session)):
    orch = get_orchestrator(request)
    agent_session = (orch.get_session(session, session_id)
                     if hasattr(orch, "get_session") else orch.sessions.get(session_id))
    if agent_session is None:
        raise HTTPException(status_code=404, detail="unknown session")
    payload = body or ResumeIn()
    try:
        if payload.background and hasattr(orch, "resume_background"):
            updated = orch.resume_background(
                session.get_bind(),
                session,
                session_id,
                mode=payload.mode,
                feedback=payload.feedback,
                new_target=payload.target,
                extra_attempts=payload.extra_attempts,
                clear_dead_ends=payload.clear_dead_ends,
                rerun_recon=payload.rerun_recon,
            )
        else:
            updated = orch.resume(
                session,
                session_id,
                mode=payload.mode,
                feedback=payload.feedback,
                new_target=payload.target,
                extra_attempts=payload.extra_attempts,
                clear_dead_ends=payload.clear_dead_ends,
                rerun_recon=payload.rerun_recon,
            )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return updated.to_dict()


@router.post("/sessions/{session_id}/hint")
def queue_session_hint(session_id: str, body: HintIn, request: Request,
                       session: Session = Depends(get_session)):
    orch = get_orchestrator(request)
    agent_session = (orch.get_session(session, session_id)
                     if hasattr(orch, "get_session") else orch.sessions.get(session_id))
    if agent_session is None:
        raise HTTPException(status_code=404, detail="unknown session")
    try:
        updated = orch.queue_feedback(session, session_id, body.feedback)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return updated.to_dict()


@router.get("/events/{event_id}/rank")
def event_planner_rank(event_id: int, session: Session = Depends(get_session)):
    event = session.get(Event, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="event not found")
    ranked = planner_rank(session, event)
    return [
        {"challenge_id": ch.id, "title": ch.title, "category": ch.category,
         "points": ch.points, "difficulty": ch.difficulty, "status": ch.status,
         "score": round(score, 2)}
        for ch, score in ranked
    ]


@router.get("/tools/inventory")
def tools_inventory():
    return inventory(load_tools_config())
