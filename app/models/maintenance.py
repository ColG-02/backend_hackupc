from datetime import datetime

from pydantic import BaseModel

from .common import TicketPriority, TicketStatus


class CreateTicketRequest(BaseModel):
    container_id: str
    device_id: str | None = None
    type: str
    priority: TicketPriority
    description: str


class CreateTicketResponse(BaseModel):
    ticket_id: str
    created: bool = True


class UpdateTicketRequest(BaseModel):
    status: TicketStatus | None = None
    resolved_by: str | None = None
    resolution: str | None = None


class UpdateTicketResponse(BaseModel):
    updated: bool = True
    ticket_id: str
    status: TicketStatus


class TicketOut(BaseModel):
    ticket_id: str
    container_id: str
    device_id: str | None = None
    type: str
    priority: TicketPriority
    status: TicketStatus
    description: str
    created_at: datetime | None = None
    resolved_by: str | None = None
    resolution: str | None = None


class TicketListResponse(BaseModel):
    items: list[TicketOut]
    total: int
