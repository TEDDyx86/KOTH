from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from ..database import get_session
from ..models import Challenge, ChallengeStatus, Difficulty
from ..services import tracking

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
