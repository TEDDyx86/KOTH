from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlmodel import Session, select

from ..database import get_session
from ..models import Attempt, ChallengeStatus, Event
from ..services import tracking
from ..templates import templates

router = APIRouter()


@router.get("/")
def dashboard(request: Request, session: Session = Depends(get_session)):
    events = tracking.list_events(session)
    event_rows = []
    for event in events:
        chs = tracking.list_challenges(session, event_id=event.id)
        solved = sum(1 for c in chs if c.status is ChallengeStatus.solved)
        event_rows.append((event, solved, len(chs)))
    open_challenges = tracking.list_challenges(session, status=ChallengeStatus.open)
    recent = list(session.exec(
        select(Attempt).order_by(Attempt.timestamp.desc()).limit(15)
    ).all())
    return templates.TemplateResponse(request, "dashboard.html", {
        "events": event_rows,
        "open_challenges": open_challenges,
        "recent_attempts": recent,
    })


@router.post("/events")
def create_event_form(request: Request, name: str = Form(...),
                      session: Session = Depends(get_session)):
    tracking.create_event(session, name)
    return RedirectResponse("/", status_code=303)
