from datetime import datetime
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from ..core.database import get_db
from ..core.security import get_current_user, require_roles
from ..models.maintenance import (
    CreateTicketRequest,
    CreateTicketResponse,
    TicketListResponse,
    TicketOut,
    UpdateTicketRequest,
    UpdateTicketResponse,
)

router = APIRouter(prefix="/maintenance/tickets", tags=["maintenance"])

DBDep = Annotated[AsyncIOMotorDatabase, Depends(get_db)]
UserDep = Annotated[dict, Depends(get_current_user)]
AdminDep = Annotated[dict, Depends(require_roles("ADMIN"))]


def _new_ticket_id() -> str:
    return "mt-" + uuid4().hex[:6]


def _doc_to_out(doc: dict) -> TicketOut:
    return TicketOut(
        ticket_id=doc["_id"],
        container_id=doc.get("container_id", ""),
        device_id=doc.get("device_id"),
        type=doc.get("type", ""),
        priority=doc.get("priority", "MEDIUM"),
        status=doc.get("status", "OPEN"),
        description=doc.get("description", ""),
        created_at=doc.get("created_at"),
        resolved_by=doc.get("resolved_by"),
        resolution=doc.get("resolution"),
    )


@router.get("", response_model=TicketListResponse)
async def list_tickets(
    db: DBDep,
    _user: UserDep,
    ticket_status: str | None = Query(None, alias="status"),
    priority: str | None = None,
    container_id: str | None = None,
    device_id: str | None = None,
):
    query: dict = {}
    if ticket_status:
        query["status"] = ticket_status
    if priority:
        query["priority"] = priority
    if container_id:
        query["container_id"] = container_id
    if device_id:
        query["device_id"] = device_id

    total = await db.maintenance_tickets.count_documents(query)
    docs = await db.maintenance_tickets.find(query).sort("created_at", -1).to_list(None)
    return TicketListResponse(items=[_doc_to_out(d) for d in docs], total=total)


@router.post("", response_model=CreateTicketResponse, status_code=201)
async def create_ticket(body: CreateTicketRequest, db: DBDep, _admin: AdminDep):
    ticket_id = _new_ticket_id()
    now = datetime.utcnow()
    await db.maintenance_tickets.insert_one(
        {
            "_id": ticket_id,
            "container_id": body.container_id,
            "device_id": body.device_id,
            "type": body.type,
            "priority": body.priority.value,
            "status": "OPEN",
            "description": body.description,
            "created_at": now,
            "updated_at": now,
        }
    )
    return CreateTicketResponse(ticket_id=ticket_id)


@router.patch("/{ticket_id}", response_model=UpdateTicketResponse)
async def update_ticket(
    ticket_id: str, body: UpdateTicketRequest, db: DBDep, _admin: AdminDep
):
    updates: dict = {"updated_at": datetime.utcnow()}
    if body.status is not None:
        updates["status"] = body.status.value
    if body.resolved_by is not None:
        updates["resolved_by"] = body.resolved_by
    if body.resolution is not None:
        updates["resolution"] = body.resolution

    result = await db.maintenance_tickets.update_one(
        {"_id": ticket_id}, {"$set": updates}
    )
    if result.matched_count == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": {
                    "code": "TICKET_NOT_FOUND",
                    "message": "Maintenance ticket not found.",
                }
            },
        )
    doc = await db.maintenance_tickets.find_one({"_id": ticket_id})
    return UpdateTicketResponse(ticket_id=ticket_id, status=doc["status"])
