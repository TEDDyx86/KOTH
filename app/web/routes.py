from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlmodel import Session, select

from ..database import get_session
from ..models import (
    Attachment, AttachmentType, Attempt, AttemptResult, AttemptType,
    ChallengeStatus, Event,
)
from ..services import tracking
from ..services.storage import StorageService
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


@router.get("/events/{event_id}")
def event_page(request: Request, event_id: int, category: str | None = None,
               status: str | None = None, session: Session = Depends(get_session)):
    event = session.get(Event, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="event not found")
    challenge_status = ChallengeStatus(status) if status else None
    challenges = tracking.list_challenges(session, event_id=event_id,
                                          category=category, status=challenge_status)
    return templates.TemplateResponse(request, "event.html", {
        "event": event,
        "challenges": challenges,
        "category": category,
        "status": status,
    })


@router.post("/events/{event_id}/challenges")
def create_challenge_form(request: Request, event_id: int, title: str = Form(...),
                          category: str = Form("misc"), points: int = Form(100),
                          difficulty: str = Form(""),
                          target: str | None = Form(None),
                          tags: str = Form(""), description: str = Form(""),
                          session: Session = Depends(get_session)):
    diff = difficulty.strip() or None
    tracking.create_challenge(session, event_id, title, category=category,
                              points=points, difficulty=diff, description=description,
                              target=target, tags=tags)
    return RedirectResponse(f"/events/{event_id}", status_code=303)


@router.get("/challenges/{challenge_id}")
def challenge_page(request: Request, challenge_id: int,
                   session: Session = Depends(get_session)):
    challenge = tracking.get_challenge(session, challenge_id)
    if challenge is None:
        raise HTTPException(status_code=404, detail="challenge not found")
    attachments = list(session.exec(
        select(Attachment).where(Attachment.challenge_id == challenge_id)
    ).all())
    return templates.TemplateResponse(request, "challenge.html", {
        "challenge": challenge,
        "summary": tracking.challenge_summary(session, challenge),
        "attempts": tracking.list_attempts(session, challenge_id),
        "notes": tracking.note_history(session, challenge_id),
        "attachments": attachments,
    })


@router.post("/challenges/{challenge_id}/attempts")
def record_attempt_form(request: Request, challenge_id: int,
                        description: str = Form(...), tool_command: str = Form(""),
                        result: str = Form("failed"), learning: str = Form(""),
                        session: Session = Depends(get_session)):
    tracking.record_attempt(session, challenge_id, AttemptType.manual, description,
                            AttemptResult(result), tool_command=tool_command or None,
                            learning=learning)
    return RedirectResponse(f"/challenges/{challenge_id}", status_code=303)


@router.post("/challenges/{challenge_id}/notes")
def add_note_form(request: Request, challenge_id: int, content: str = Form(...),
                  session: Session = Depends(get_session)):
    tracking.add_note(session, challenge_id, content)
    return RedirectResponse(f"/challenges/{challenge_id}", status_code=303)


@router.post("/challenges/{challenge_id}/attachments")
def upload_attachment_form(request: Request, challenge_id: int,
                           file: UploadFile = File(...),
                           type: str = Form("download"),
                           session: Session = Depends(get_session)):
    challenge = tracking.get_challenge(session, challenge_id)
    if challenge is None:
        raise HTTPException(status_code=404, detail="challenge not found")
    data = file.file.read()
    storage: StorageService = request.app.state.storage
    path = storage.save(challenge_id, file.filename, data)
    session.add(Attachment(challenge_id=challenge_id, name=file.filename,
                           path=str(path.relative_to(storage.storage_dir)),
                           mime=file.content_type or "application/octet-stream",
                           size=len(data), type=AttachmentType(type)))
    session.commit()
    return RedirectResponse(f"/challenges/{challenge_id}", status_code=303)
