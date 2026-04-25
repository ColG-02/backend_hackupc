from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from ..core.database import get_db
from ..core.security import get_current_user, require_roles
from ..models.event import (
    AcknowledgeRequest,
    EventListResponse,
    EventOut,
    EventUpdateResponse,
    IgnoreRequest,
    ResolveRequest,
)
from ..models.common import EventStatus

router = APIRouter(prefix="/events", tags=["events"])

DBDep = Annotated[AsyncIOMotorDatabase, Depends(get_db)]
UserDep = Annotated[dict, Depends(get_current_user)]
DispatcherDep = Annotated[dict, Depends(require_roles("ADMIN", "DISPATCHER"))]


def _doc_to_out(doc: dict) -> EventOut:
    return EventOut(
        event_id=doc["_id"],
        container_id=doc.get("container_id", ""),
        device_id=doc.get("device_id", ""),
        type=doc.get("type"),
        severity=doc.get("severity"),
        status=doc.get("status"),
        started_at=doc.get("started_at"),
        ended_at=doc.get("ended_at"),
        confidence=doc.get("confidence"),
        summary=doc.get("summary", ""),
        state=doc.get("state"),
        evidence=doc.get("evidence"),
        media_ids=(doc.get("evidence") or {}).get("media_ids", []),
        created_at=doc.get("created_at"),
        updated_at=doc.get("updated_at"),
    )


@router.get("", response_model=EventListResponse)
async def list_events(
    db: DBDep,
    _user: UserDep,
    event_status: str | None = Query(None, alias="status"),
    event_type: str | None = Query(None, alias="type"),
    severity: str | None = None,
    container_id: str | None = None,
    site_id: str | None = None,
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    query: dict = {}
    if event_status:
        query["status"] = event_status
    if event_type:
        query["type"] = event_type
    if severity:
        query["severity"] = severity
    if container_id:
        query["container_id"] = container_id
    if from_ or to:
        query["started_at"] = {}
        if from_:
            query["started_at"]["$gte"] = from_
        if to:
            query["started_at"]["$lte"] = to

    if site_id:
        container_ids = [
            d["_id"]
            async for d in db.containers.find({"site_id": site_id}, {"_id": 1})
        ]
        query["container_id"] = {"$in": container_ids}

    total = await db.events.count_documents(query)
    docs = (
        await db.events.find(query)
        .sort("started_at", -1)
        .skip(offset)
        .limit(limit)
        .to_list(None)
    )
    return EventListResponse(
        items=[_doc_to_out(d) for d in docs],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{event_id}", response_model=EventOut)
async def get_event(event_id: str, db: DBDep, _user: UserDep):
    doc = await db.events.find_one({"_id": event_id})
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "EVENT_NOT_FOUND", "message": "Event not found."}},
        )
    return _doc_to_out(doc)


@router.post("/{event_id}/acknowledge", response_model=EventUpdateResponse)
async def acknowledge_event(
    event_id: str, body: AcknowledgeRequest, db: DBDep, _user: DispatcherDep
):
    now = datetime.utcnow()
    result = await db.events.update_one(
        {"_id": event_id, "status": EventStatus.OPEN.value},
        {
            "$set": {
                "status": EventStatus.ACKNOWLEDGED.value,
                "acknowledged_by": body.acknowledged_by,
                "acknowledged_note": body.note,
                "updated_at": now,
            }
        },
    )
    if result.matched_count == 0:
        doc = await db.events.find_one({"_id": event_id})
        if not doc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": {"code": "EVENT_NOT_FOUND", "message": "Event not found."}},
            )
    return EventUpdateResponse(event_id=event_id, status=EventStatus.ACKNOWLEDGED)


@router.post("/{event_id}/resolve", response_model=EventUpdateResponse)
async def resolve_event(
    event_id: str, body: ResolveRequest, db: DBDep, _user: DispatcherDep
):
    now = body.resolved_at or datetime.utcnow()
    result = await db.events.update_one(
        {"_id": event_id, "status": {"$in": [EventStatus.OPEN.value, EventStatus.ACKNOWLEDGED.value]}},
        {
            "$set": {
                "status": EventStatus.RESOLVED.value,
                "resolved_by": body.resolved_by,
                "resolution": body.resolution,
                "ended_at": now,
                "updated_at": datetime.utcnow(),
            }
        },
    )
    if result.matched_count == 0:
        doc = await db.events.find_one({"_id": event_id})
        if not doc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": {"code": "EVENT_NOT_FOUND", "message": "Event not found."}},
            )
    return EventUpdateResponse(event_id=event_id, status=EventStatus.RESOLVED)


@router.post("/{event_id}/ignore", response_model=EventUpdateResponse)
async def ignore_event(
    event_id: str, body: IgnoreRequest, db: DBDep, _user: DispatcherDep
):
    result = await db.events.update_one(
        {"_id": event_id},
        {
            "$set": {
                "status": EventStatus.IGNORED.value,
                "ignored_by": body.ignored_by,
                "ignore_reason": body.reason,
                "updated_at": datetime.utcnow(),
            }
        },
    )
    if result.matched_count == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "EVENT_NOT_FOUND", "message": "Event not found."}},
        )
    return EventUpdateResponse(event_id=event_id, status=EventStatus.IGNORED)
