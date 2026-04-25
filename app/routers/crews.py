import logging
from datetime import datetime
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from ..core.database import get_db
from ..core.event_bus import bus
from ..core.rate_limiter import location_limiter
from ..core.security import get_current_user, require_roles
from ..models.common import CrewStatus
from ..models.crew import (
    CreateCrewRequest,
    CreateCrewResponse,
    CrewListResponse,
    CrewLocation,
    CrewLocationHistoryItem,
    CrewLocationHistoryResponse,
    CrewOut,
    CrewPositionItem,
    CrewPositionsResponse,
    IngestLocationRequest,
    IngestLocationResponse,
    UpdateCrewRequest,
    UpdateCrewResponse,
    UpdateCrewStatusRequest,
    UpdateCrewStatusResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/crews", tags=["crews"])

DBDep = Annotated[AsyncIOMotorDatabase, Depends(get_db)]
UserDep = Annotated[dict, Depends(get_current_user)]
DispatcherDep = Annotated[dict, Depends(require_roles("ADMIN", "DISPATCHER"))]
AdminDep = Annotated[dict, Depends(require_roles("ADMIN"))]

# Statuses that allow GPS tracking
_GPS_ACTIVE_STATUSES = {
    CrewStatus.ON_DUTY.value,
    CrewStatus.IN_ROUTE.value,
    CrewStatus.AT_STOP.value,
    CrewStatus.ON_BREAK.value,
}

_LOCATION_MIN_INTERVAL_SEC = 1.0


def _new_crew_id() -> str:
    return "crew-" + uuid4().hex[:8]


def _doc_to_out(doc: dict) -> CrewOut:
    loc_raw = doc.get("current_location")
    location = None
    if loc_raw:
        location = CrewLocation(
            lat=loc_raw["lat"],
            lng=loc_raw["lng"],
            accuracy_m=loc_raw.get("accuracy_m"),
            heading_deg=loc_raw.get("heading_deg"),
            speed_mps=loc_raw.get("speed_mps"),
            updated_at=loc_raw["updated_at"],
        )
    return CrewOut(
        crew_id=doc["_id"],
        name=doc.get("name", ""),
        status=doc.get("status", CrewStatus.UNKNOWN),
        members_count=doc.get("members_count", 1),
        vehicle_id=doc.get("vehicle_id"),
        phone=doc.get("phone"),
        current_location=location,
        assigned_route_plan_id=doc.get("assigned_route_plan_id"),
        created_at=doc["created_at"],
        updated_at=doc["updated_at"],
    )


# ── List ──────────────────────────────────────────────────────────────────────

@router.get("", response_model=CrewListResponse)
async def list_crews(
    db: DBDep,
    _user: UserDep,
    crew_status: str | None = Query(None, alias="status"),
):
    query: dict = {}
    if crew_status:
        query["status"] = crew_status
    docs = await db.crews.find(query).sort("name", 1).to_list(None)
    return CrewListResponse(items=[_doc_to_out(d) for d in docs], total=len(docs))


# ── Latest positions (must be before /{crew_id}) ──────────────────────────────

@router.get("/positions", response_model=CrewPositionsResponse)
async def get_crew_positions(db: DBDep, _user: UserDep):
    """Fast-poll endpoint: returns latest position for every non-OFF_DUTY crew."""
    docs = await db.crews.find(
        {"status": {"$in": list(_GPS_ACTIVE_STATUSES)}}
    ).to_list(None)
    items = []
    for d in docs:
        loc_raw = d.get("current_location")
        location = None
        if loc_raw:
            location = CrewLocation(
                lat=loc_raw["lat"],
                lng=loc_raw["lng"],
                accuracy_m=loc_raw.get("accuracy_m"),
                heading_deg=loc_raw.get("heading_deg"),
                speed_mps=loc_raw.get("speed_mps"),
                updated_at=loc_raw["updated_at"],
            )
        items.append(
            CrewPositionItem(
                crew_id=d["_id"],
                name=d.get("name", ""),
                status=d.get("status", CrewStatus.UNKNOWN),
                vehicle_id=d.get("vehicle_id"),
                members_count=d.get("members_count", 1),
                assigned_route_plan_id=d.get("assigned_route_plan_id"),
                location=location,
            )
        )
    return CrewPositionsResponse(items=items)


# ── Create ────────────────────────────────────────────────────────────────────

@router.post("", response_model=CreateCrewResponse, status_code=201)
async def create_crew(body: CreateCrewRequest, db: DBDep, _user: DispatcherDep):
    crew_id = _new_crew_id()
    now = datetime.utcnow()
    await db.crews.insert_one(
        {
            "_id": crew_id,
            "name": body.name,
            "status": CrewStatus.OFF_DUTY.value,
            "members_count": body.members_count,
            "vehicle_id": body.vehicle_id,
            "phone": body.phone,
            "current_location": None,
            "assigned_route_plan_id": None,
            "created_at": now,
            "updated_at": now,
        }
    )
    return CreateCrewResponse(crew_id=crew_id)


# ── Get detail ────────────────────────────────────────────────────────────────

@router.get("/{crew_id}", response_model=CrewOut)
async def get_crew(crew_id: str, db: DBDep, _user: UserDep):
    doc = await db.crews.find_one({"_id": crew_id})
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "CREW_NOT_FOUND", "message": "Crew not found."}},
        )
    return _doc_to_out(doc)


# ── Update crew fields ────────────────────────────────────────────────────────

@router.patch("/{crew_id}", response_model=UpdateCrewResponse)
async def update_crew(
    crew_id: str, body: UpdateCrewRequest, db: DBDep, _user: DispatcherDep
):
    updates: dict = {"updated_at": datetime.utcnow()}
    if body.name is not None:
        updates["name"] = body.name
    if body.members_count is not None:
        updates["members_count"] = body.members_count
    if body.vehicle_id is not None:
        updates["vehicle_id"] = body.vehicle_id
    if body.phone is not None:
        updates["phone"] = body.phone

    result = await db.crews.update_one({"_id": crew_id}, {"$set": updates})
    if result.matched_count == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "CREW_NOT_FOUND", "message": "Crew not found."}},
        )
    return UpdateCrewResponse(crew_id=crew_id)


# ── Delete crew ───────────────────────────────────────────────────────────────

@router.delete("/{crew_id}", status_code=204)
async def delete_crew(crew_id: str, db: DBDep, _user: AdminDep):
    result = await db.crews.delete_one({"_id": crew_id})
    if result.deleted_count == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "CREW_NOT_FOUND", "message": "Crew not found."}},
        )
    location_limiter.remove(crew_id)


# ── Update crew status ────────────────────────────────────────────────────────

@router.patch("/{crew_id}/status", response_model=UpdateCrewStatusResponse)
async def update_crew_status(
    crew_id: str, body: UpdateCrewStatusRequest, db: DBDep, _user: DispatcherDep
):
    now = datetime.utcnow()
    updates: dict = {"status": body.status.value, "updated_at": now}

    # Clear assignment when going off-duty
    if body.status == CrewStatus.OFF_DUTY:
        updates["assigned_route_plan_id"] = None

    result = await db.crews.update_one({"_id": crew_id}, {"$set": updates})
    if result.matched_count == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "CREW_NOT_FOUND", "message": "Crew not found."}},
        )
    return UpdateCrewStatusResponse(crew_id=crew_id, status=body.status)


# ── GPS location ingestion ────────────────────────────────────────────────────

@router.post("/{crew_id}/location", response_model=IngestLocationResponse)
async def ingest_location(
    crew_id: str, body: IngestLocationRequest, db: DBDep, _user: UserDep
):
    now = datetime.utcnow()

    crew = await db.crews.find_one({"_id": crew_id})
    if not crew:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "CREW_NOT_FOUND", "message": "Crew not found."}},
        )

    # Only accept GPS while crew is active
    if crew.get("status") not in _GPS_ACTIVE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": {
                    "code": "CREW_NOT_ACTIVE",
                    "message": "GPS updates are only accepted while the crew is on duty.",
                }
            },
        )

    # Validate coordinates
    if not (-90 <= body.lat <= 90 and -180 <= body.lng <= 180):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": {"code": "INVALID_COORDINATES", "message": "Coordinates out of range."}},
        )

    # Rate limit: max 1 update per second per crew
    if not location_limiter.is_allowed(crew_id, _LOCATION_MIN_INTERVAL_SEC):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error": {"code": "RATE_LIMITED", "message": "Too many location updates."}},
        )

    location_doc = {
        "lat": body.lat,
        "lng": body.lng,
        "accuracy_m": body.accuracy_m,
        "heading_deg": body.heading_deg,
        "speed_mps": body.speed_mps,
        "updated_at": now,
    }

    # Update latest position on crew document
    await db.crews.update_one(
        {"_id": crew_id},
        {"$set": {"current_location": location_doc, "updated_at": now}},
    )

    # Append to history
    await db.crew_location_history.insert_one(
        {
            "crew_id": crew_id,
            "route_plan_id": body.route_plan_id or crew.get("assigned_route_plan_id"),
            "lat": body.lat,
            "lng": body.lng,
            "accuracy_m": body.accuracy_m,
            "heading_deg": body.heading_deg,
            "speed_mps": body.speed_mps,
            "battery_level": body.battery_level,
            "recorded_at": body.recorded_at,
            "received_at": now,
        }
    )

    await bus.publish(
        "crew.location.updated",
        {
            "crew_id": crew_id,
            "name": crew.get("name"),
            "status": crew.get("status"),
            "lat": body.lat,
            "lng": body.lng,
            "heading_deg": body.heading_deg,
            "speed_mps": body.speed_mps,
            "updated_at": now.isoformat() + "Z",
        },
    )

    return IngestLocationResponse(crew_id=crew_id, received_at=now.isoformat() + "Z")


# ── Location history ──────────────────────────────────────────────────────────

@router.get("/{crew_id}/locations", response_model=CrewLocationHistoryResponse)
async def get_location_history(
    crew_id: str,
    db: DBDep,
    _user: UserDep,
    from_: datetime = Query(..., alias="from"),
    to: datetime = Query(...),
    limit: int = Query(500, ge=1, le=2000),
):
    if not await db.crews.find_one({"_id": crew_id}):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "CREW_NOT_FOUND", "message": "Crew not found."}},
        )

    query = {"crew_id": crew_id, "recorded_at": {"$gte": from_, "$lte": to}}
    total = await db.crew_location_history.count_documents(query)
    docs = (
        await db.crew_location_history.find(query)
        .sort("recorded_at", 1)
        .limit(limit)
        .to_list(None)
    )
    items = [
        CrewLocationHistoryItem(
            crew_id=d["crew_id"],
            route_plan_id=d.get("route_plan_id"),
            lat=d["lat"],
            lng=d["lng"],
            accuracy_m=d.get("accuracy_m"),
            heading_deg=d.get("heading_deg"),
            speed_mps=d.get("speed_mps"),
            recorded_at=d["recorded_at"],
            received_at=d["received_at"],
        )
        for d in docs
    ]
    return CrewLocationHistoryResponse(items=items, total=total)
