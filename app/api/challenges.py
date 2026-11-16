from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel
from sqlmodel import Session, select

from ..database import get_session
from ..models import (
    Attachment, AttachmentType, Attempt, AttemptResult, AttemptType,
    Challenge, ChallengeStatus, Difficulty, Note,
)
from ..services import tracking
from ..services.storage import StorageService

router = APIRouter()


class ChallengeCreate(BaseModel):
    title: str
    category: str = "misc"
    points: int = 0
    difficulty: Difficulty | None = None
    description: str = ""
    target: str | None = None
    tags: str = ""


class ChallengeUpdate(BaseModel):
    status: ChallengeStatus | None = None
    flag: str | None = None


class AttemptCreate(BaseModel):
    type: AttemptType
    description: str
    result: AttemptResult
    tool_command: str | None = None
    learning: str = ""
    flag: str | None = None


class NoteCreate(BaseModel):
    content: str


@router.post("/events/{event_id}/challenges", response_model=Challenge)
def create_challenge(event_id: int, body: ChallengeCreate,
                     session: Session = Depends(get_session)):
    try:
        return tracking.create_challenge(session, event_id, body.title,
                                         category=body.category, points=body.points,
                                         difficulty=body.difficulty,
                                         description=body.description,
                                         target=body.target, tags=body.tags)
    except LookupError:
        raise HTTPException(status_code=404, detail="event not found")


@router.get("/events/{event_id}/challenges", response_model=list[Challenge])
def list_challenges(event_id: int, category: str | None = None,
                    status: ChallengeStatus | None = None, tag: str | None = None,
                    session: Session = Depends(get_session)):
    return tracking.list_challenges(session, event_id=event_id, category=category,
                                    status=status, tag=tag)


@router.get("/challenges/{challenge_id}")
def challenge_detail(challenge_id: int, session: Session = Depends(get_session)):
    challenge = tracking.get_challenge(session, challenge_id)
    if challenge is None:
        raise HTTPException(status_code=404, detail="challenge not found")
    return {
        "challenge": challenge,
        "summary": tracking.challenge_summary(session, challenge),
        "attempts": tracking.list_attempts(session, challenge_id),
        "notes": tracking.note_history(session, challenge_id),
    }


@router.patch("/challenges/{challenge_id}", response_model=Challenge)
def update_challenge(challenge_id: int, body: ChallengeUpdate,
                     session: Session = Depends(get_session)):
    try:
        return tracking.update_challenge(session, challenge_id,
                                         status=body.status, flag=body.flag)
    except LookupError:
        raise HTTPException(status_code=404, detail="challenge not found")


@router.post("/challenges/{challenge_id}/attempts", response_model=Attempt)
def post_attempt(challenge_id: int, body: AttemptCreate,
                 session: Session = Depends(get_session)):
    try:
        return tracking.record_attempt(session, challenge_id, body.type,
                                       body.description, body.result,
                                       tool_command=body.tool_command,
                                       learning=body.learning, flag=body.flag)
    except LookupError:
        raise HTTPException(status_code=404, detail="challenge not found")


@router.post("/challenges/{challenge_id}/notes", response_model=Note)
def post_note(challenge_id: int, body: NoteCreate,
              session: Session = Depends(get_session)):
    try:
        return tracking.add_note(session, challenge_id, body.content)
    except LookupError:
        raise HTTPException(status_code=404, detail="challenge not found")


@router.get("/challenges/{challenge_id}/notes", response_model=list[Note])
def get_notes(challenge_id: int, session: Session = Depends(get_session)):
    return tracking.note_history(session, challenge_id)


@router.post("/challenges/{challenge_id}/attachments", response_model=Attachment)
def upload_attachment(challenge_id: int, request: Request,
                      file: UploadFile = File(...),
                      type: AttachmentType = Form(AttachmentType.download),
                      session: Session = Depends(get_session)):
    challenge = tracking.get_challenge(session, challenge_id)
    if challenge is None:
        raise HTTPException(status_code=404, detail="challenge not found")
    data = file.file.read()
    storage: StorageService = request.app.state.storage
    path = storage.save(challenge_id, file.filename, data)
    attachment = Attachment(challenge_id=challenge_id, name=file.filename,
                            path=str(path.relative_to(storage.storage_dir)),
                            mime=file.content_type or "application/octet-stream",
                            size=len(data), type=type)
    session.add(attachment)
    session.commit()
    session.refresh(attachment)
    return attachment


@router.get("/challenges/{challenge_id}/attachments", response_model=list[Attachment])
def list_attachments(challenge_id: int, session: Session = Depends(get_session)):
    return list(session.exec(
        select(Attachment).where(Attachment.challenge_id == challenge_id)
    ).all())
