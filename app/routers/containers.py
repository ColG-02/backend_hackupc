from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from ..core.database import get_db
from ..core.security import get_current_user, require_roles
from ..models.container import (
    ContainerDetail,
    ContainerLatestResponse,
    ContainerListResponse,
    ContainerSummary,
    CreateContainerRequest,
    CreateContainerResponse,
    FillSnapshot,
    HealthSnapshot,
    SensorSnapshot,
    TelemetryHistoryResponse,
    UpdateContainerRequest,
    UpdateContainerResponse,
    VisionSnapshot,
)
from ..models.device import DeviceConfig

router = APIRouter(prefix="/containers", tags=["containers"])

DBDep = Annotated[AsyncIOMotorDatabase, Depends(get_db)]
UserDep = Annotated[dict, Depends(get_current_user)]
AdminDep = Annotated[dict, Depends(require_roles("ADMIN"))]

_INTERVAL_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "1h": 60}


def _doc_to_summary(doc: dict) -> ContainerSummary:
    return ContainerSummary(
        container_id=doc["_id"],
        name=doc.get("name", ""),
        site_id=doc.get("site_id"),
        location=doc.get("location"),
        status=doc.get("status"),
        assigned_device_id=doc.get("device_id"),
        latest_state=doc.get("latest_state"),
    )


def _doc_to_detail(doc: dict) -> ContainerDetail:
    return ContainerDetail(
        container_id=doc["_id"],
        name=doc.get("name", ""),
        site_id=doc.get("site_id"),
        location=doc.get("location"),
        address=doc.get("address"),
        status=doc.get("status"),
        container_type=doc.get("container_type"),
        capacity=doc.get("capacity"),
        assigned_device_id=doc.get("device_id"),
        latest_state=doc.get("latest_state"),
        config_revision=doc.get("config_revision"),
        created_at=doc.get("created_at"),
        updated_at=doc.get("updated_at"),
    )


# ── List ──────────────────────────────────────────────────────────────────────

@router.get("", response_model=ContainerListResponse)
async def list_containers(
    db: DBDep,
    _user: UserDep,
    status_filter: str | None = Query(None, alias="status"),
    fill_state: str | None = None,
    camera_state: str | None = None,
    site_id: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    query: dict = {}
    if status_filter:
        query["status"] = status_filter
    if fill_state:
        query["latest_state.fill_state"] = fill_state
    if camera_state:
        query["latest_state.camera_state"] = camera_state
    if site_id:
        query["site_id"] = site_id

    total = await db.containers.count_documents(query)
    docs = await db.containers.find(query).skip(offset).limit(limit).to_list(None)
    return ContainerListResponse(
        items=[_doc_to_summary(d) for d in docs],
        total=total,
        limit=limit,
        offset=offset,
    )


# ── Create ────────────────────────────────────────────────────────────────────

@router.post("", response_model=CreateContainerResponse, status_code=201)
async def create_container(body: CreateContainerRequest, db: DBDep, _admin: AdminDep):
    existing = await db.containers.find_one({"_id": body.container_id})
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": {
                    "code": "CONTAINER_ALREADY_EXISTS",
                    "message": f"Container {body.container_id} already exists.",
                }
            },
        )
    now = datetime.utcnow()
    default_config = DeviceConfig()
    await db.containers.insert_one(
        {
            "_id": body.container_id,
            "name": body.name,
            "site_id": body.site_id,
            "location": body.location.model_dump() if body.location else None,
            "address": body.address,
            "status": "ACTIVE",
            "container_type": body.container_type,
            "capacity": body.capacity.model_dump() if body.capacity else None,
            "device_id": None,
            "config_revision": 1,
            "config": default_config.model_dump(),
            "latest_state": {},
            "last_collected_at": None,
            "created_at": now,
            "updated_at": now,
        }
    )
    return CreateContainerResponse(container_id=body.container_id)


# ── Get detail ────────────────────────────────────────────────────────────────

@router.get("/{container_id}", response_model=ContainerDetail)
async def get_container(container_id: str, db: DBDep, _user: UserDep):
    doc = await db.containers.find_one({"_id": container_id})
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": {
                    "code": "CONTAINER_NOT_FOUND",
                    "message": "Container not found.",
                }
            },
        )
    return _doc_to_detail(doc)


# ── Update ────────────────────────────────────────────────────────────────────

@router.patch("/{container_id}", response_model=UpdateContainerResponse)
async def update_container(
    container_id: str, body: UpdateContainerRequest, db: DBDep, _admin: AdminDep
):
    updates: dict = {"updated_at": datetime.utcnow()}
    if body.name is not None:
        updates["name"] = body.name
    if body.site_id is not None:
        updates["site_id"] = body.site_id
    if body.address is not None:
        updates["address"] = body.address
    if body.capacity is not None:
        updates["capacity"] = body.capacity.model_dump()

    result = await db.containers.update_one({"_id": container_id}, {"$set": updates})
    if result.matched_count == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": {
                    "code": "CONTAINER_NOT_FOUND",
                    "message": "Container not found.",
                }
            },
        )
    return UpdateContainerResponse(container_id=container_id)


# ── Latest state ──────────────────────────────────────────────────────────────

@router.get("/{container_id}/latest", response_model=ContainerLatestResponse)
async def get_latest(container_id: str, db: DBDep, _user: UserDep):
    doc = await db.containers.find_one({"_id": container_id})
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": {
                    "code": "CONTAINER_NOT_FOUND",
                    "message": "Container not found.",
                }
            },
        )
    state = doc.get("latest_state") or {}

    # Latest telemetry reading for fill details
    latest_ts = (
        await db.telemetry_timeseries.find({"meta.container_id": container_id})
        .sort("ts", -1)
        .limit(1)
        .to_list(1)
    )
    ts_doc = latest_ts[0] if latest_ts else {}

    return ContainerLatestResponse(
        container_id=container_id,
        last_seen_at=state.get("last_seen_at"),
        fill=FillSnapshot(
            height_pct=ts_doc.get("fill_height_pct"),
            weight_pct=ts_doc.get("fill_weight_pct"),
            fused_pct=ts_doc.get("fused_fill_pct"),
            state=ts_doc.get("fill_state"),
            confidence=ts_doc.get("fill_confidence"),
        ),
        vision=VisionSnapshot(
            camera_state=state.get("camera_state"),
            confidence=state.get("camera_confidence"),
            model_id=ts_doc.get("model_id"),
            last_inference_at=ts_doc.get("ts"),
        ),
        sensors=SensorSnapshot(
            temperature_c=state.get("temperature_c"),
            humidity_pct=state.get("humidity_pct"),
            light_lux=state.get("light_lux"),
            ultrasonic_distance_cm=state.get("ultrasonic_distance_cm"),
            weight_kg=state.get("weight_kg"),
            tamper_open=state.get("tamper_open"),
        ),
        health=HealthSnapshot(
            device_status=state.get("device_status"),
            rssi_dbm=ts_doc.get("rssi_dbm"),
            uptime_sec=ts_doc.get("uptime_sec"),
            offline_queue_count=0,
        ),
    )


# ── Telemetry history ─────────────────────────────────────────────────────────

@router.get("/{container_id}/telemetry", response_model=TelemetryHistoryResponse)
async def get_telemetry_history(
    container_id: str,
    db: DBDep,
    _user: UserDep,
    from_: datetime = Query(..., alias="from"),
    to: datetime = Query(...),
    interval: str = Query("raw"),
):
    if not await db.containers.find_one({"_id": container_id}):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": {
                    "code": "CONTAINER_NOT_FOUND",
                    "message": "Container not found.",
                }
            },
        )

    match = {"meta.container_id": container_id, "ts": {"$gte": from_, "$lte": to}}
    projection = {
        "_id": 0,
        "ts": 1,
        "temperature_c": 1,
        "humidity_pct": 1,
        "fused_fill_pct": 1,
        "weight_kg": 1,
        "camera_state": 1,
    }

    if interval == "raw":
        docs = (
            await db.telemetry_timeseries.find(match, projection)
            .sort("ts", 1)
            .to_list(None)
        )
    else:
        minutes = _INTERVAL_MINUTES.get(interval, 5)
        pipeline = [
            {"$match": match},
            {
                "$group": {
                    "_id": {
                        "$dateTrunc": {
                            "date": "$ts",
                            "unit": "minute",
                            "binSize": minutes,
                        }
                    },
                    "temperature_c": {"$avg": "$temperature_c"},
                    "humidity_pct": {"$avg": "$humidity_pct"},
                    "fused_fill_pct": {"$avg": "$fused_fill_pct"},
                    "weight_kg": {"$avg": "$weight_kg"},
                    "camera_state": {"$last": "$camera_state"},
                }
            },
            {"$sort": {"_id": 1}},
            {
                "$project": {
                    "_id": 0,
                    "ts": "$_id",
                    "temperature_c": 1,
                    "humidity_pct": 1,
                    "fused_fill_pct": 1,
                    "weight_kg": 1,
                    "camera_state": 1,
                }
            },
        ]
        docs = await db.telemetry_timeseries.aggregate(pipeline).to_list(None)

    return TelemetryHistoryResponse(
        container_id=container_id,
        from_=from_,
        to=to,
        interval=interval,
        items=docs,
    )
