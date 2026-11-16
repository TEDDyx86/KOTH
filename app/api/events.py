from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from ..database import get_session
from ..models import Event, EventStatus
from ..services import tracking

router = APIRouter()


class EventCreate(BaseModel):
    name: str
    url: str | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None


class StatusUpdate(BaseModel):
    status: EventStatus


@router.post("", response_model=Event)
def create_event(body: EventCreate, session: Session = Depends(get_session)):
    return tracking.create_event(session, body.name, url=body.url,
                                 starts_at=body.starts_at, ends_at=body.ends_at)


@router.get("", response_model=list[Event])
def list_events(session: Session = Depends(get_session)):
    return tracking.list_events(session)


@router.patch("/{event_id}/status", response_model=Event)
def set_status(event_id: int, body: StatusUpdate, session: Session = Depends(get_session)):
    try:
        return tracking.set_event_status(session, event_id, body.status)
    except LookupError:
        raise HTTPException(status_code=404, detail="event not found")
