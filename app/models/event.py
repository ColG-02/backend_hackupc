from datetime import datetime

from pydantic import BaseModel

from .common import EventSeverity, EventStatus, EventType


class EventOut(BaseModel):
    event_id: str
    container_id: str
    device_id: str
    type: EventType
    severity: EventSeverity
    status: EventStatus
    started_at: datetime
    ended_at: datetime | None = None
    confidence: float | None = None
    summary: str
    state: dict | None = None
    evidence: dict | None = None
    media_ids: list[str] = []
    created_at: datetime | None = None
    updated_at: datetime | None = None


class EventListResponse(BaseModel):
    items: list[EventOut]
    total: int
    limit: int
    offset: int


class AcknowledgeRequest(BaseModel):
    acknowledged_by: str
    note: str | None = None


class ResolveRequest(BaseModel):
    resolved_by: str
    resolution: str | None = None
    resolved_at: datetime | None = None


class IgnoreRequest(BaseModel):
    ignored_by: str
    reason: str | None = None


class EventUpdateResponse(BaseModel):
    updated: bool = True
    event_id: str
    status: EventStatus
