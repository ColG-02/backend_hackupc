from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from ..core.database import get_db
from ..core.security import get_current_user, require_roles
from ..models.common import RoutePlanStatus, RouteStopStatus
from ..core.event_bus import bus
from ..models.route import (
    AssignRouteRequest,
    AssignRouteResponse,
    CreateRoutePlanRequest,
    DispatchRequest,
    DispatchResponse,
    RoutePlanListItem,
    RoutePlanListResponse,
    RoutePlanOut,
    RoutePlanSummary,
    UpdateStopRequest,
    UpdateStopResponse,
    VehicleRoute,
    RouteStop,
)
from ..services.route_service import assign_route_to_crew, complete_route_stop, create_route_plan

router = APIRouter(prefix="/routes", tags=["routes"])

DBDep = Annotated[AsyncIOMotorDatabase, Depends(get_db)]
UserDep = Annotated[dict, Depends(get_current_user)]
DispatcherDep = Annotated[dict, Depends(require_roles("ADMIN", "DISPATCHER"))]
# Crew members and dispatchers can both update stop statuses
StopUpdateDep = Annotated[dict, Depends(require_roles("ADMIN", "DISPATCHER", "CREW"))]


def _doc_to_out(doc: dict) -> RoutePlanOut:
    routes = []
    for r in doc.get("routes", []):
        stops = [RouteStop(**s) for s in r.get("stops", [])]
        routes.append(
            VehicleRoute(
                vehicle_id=r["vehicle_id"],
                estimated_distance_km=r.get("estimated_distance_km"),
                estimated_duration_min=r.get("estimated_duration_min"),
                stops=stops,
            )
        )
    summary_raw = doc.get("summary") or {}
    summary = RoutePlanSummary(
        vehicles_used=summary_raw.get("vehicles_used", 0),
        stops=summary_raw.get("stops", 0),
        estimated_distance_km=summary_raw.get("estimated_distance_km"),
        estimated_duration_min=summary_raw.get("estimated_duration_min"),
        dropped_low_priority_stops=summary_raw.get("dropped_low_priority_stops", 0),
    )
    return RoutePlanOut(
        route_plan_id=doc["_id"],
        date=doc.get("date", ""),
        status=doc.get("status", RoutePlanStatus.PLANNED),
        summary=summary,
        routes=routes,
        created_at=doc.get("created_at"),
        dispatched_at=doc.get("dispatched_at"),
        dispatched_by=doc.get("dispatched_by"),
    )


@router.post("/plan", response_model=RoutePlanOut, status_code=201)
async def plan_route(body: CreateRoutePlanRequest, db: DBDep, user: DispatcherDep):
    if not body.vehicle_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": "At least one vehicle_id is required.",
                }
            },
        )
    plan_doc = await create_route_plan(db, body, created_by=user["_id"])
    return _doc_to_out(plan_doc)


@router.get("/plans", response_model=RoutePlanListResponse)
async def list_plans(
    db: DBDep,
    _user: UserDep,
    date: str | None = None,
    plan_status: str | None = Query(None, alias="status"),
):
    query: dict = {}
    if date:
        query["date"] = date
    if plan_status:
        query["status"] = plan_status

    total = await db.route_plans.count_documents(query)
    docs = await db.route_plans.find(query).sort("created_at", -1).to_list(None)

    items = [
        RoutePlanListItem(
            route_plan_id=d["_id"],
            date=d.get("date", ""),
            status=d.get("status", RoutePlanStatus.PLANNED),
            vehicles_used=d.get("summary", {}).get("vehicles_used", 0),
            stops=d.get("summary", {}).get("stops", 0),
            estimated_distance_km=d.get("summary", {}).get("estimated_distance_km"),
            estimated_duration_min=d.get("summary", {}).get("estimated_duration_min"),
        )
        for d in docs
    ]
    return RoutePlanListResponse(items=items, total=total)


@router.get("/plans/{route_plan_id}", response_model=RoutePlanOut)
async def get_plan(route_plan_id: str, db: DBDep, _user: UserDep):
    doc = await db.route_plans.find_one({"_id": route_plan_id})
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": {
                    "code": "ROUTE_PLAN_NOT_FOUND",
                    "message": "Route plan not found.",
                }
            },
        )
    return _doc_to_out(doc)


@router.post("/plans/{route_plan_id}/dispatch", response_model=DispatchResponse)
async def dispatch_plan(
    route_plan_id: str, body: DispatchRequest, db: DBDep, _user: DispatcherDep
):
    now = datetime.utcnow()
    result = await db.route_plans.update_one(
        {"_id": route_plan_id, "status": RoutePlanStatus.PLANNED.value},
        {
            "$set": {
                "status": RoutePlanStatus.DISPATCHED.value,
                "dispatched_by": body.dispatched_by,
                "dispatched_at": now,
                "updated_at": now,
            }
        },
    )
    if result.matched_count == 0:
        doc = await db.route_plans.find_one({"_id": route_plan_id})
        if not doc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": {
                        "code": "ROUTE_PLAN_NOT_FOUND",
                        "message": "Route plan not found.",
                    }
                },
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": {
                    "code": "INVALID_STATE",
                    "message": f"Route plan is in state '{doc['status']}' and cannot be dispatched.",
                }
            },
        )
    await bus.publish(
        "route.plan.updated",
        {"route_plan_id": route_plan_id, "status": RoutePlanStatus.DISPATCHED.value,
         "dispatched_at": now.isoformat() + "Z"},
    )
    return DispatchResponse(
        route_plan_id=route_plan_id,
        status=RoutePlanStatus.DISPATCHED,
        dispatched_at=now.isoformat() + "Z",
    )


@router.post("/plans/{route_plan_id}/assign", response_model=AssignRouteResponse)
async def assign_plan(
    route_plan_id: str,
    body: AssignRouteRequest,
    db: DBDep,
    _user: DispatcherDep,
):
    try:
        result = await assign_route_to_crew(db, route_plan_id, body.crew_id, body.vehicle_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "CREW_NOT_FOUND", "message": str(exc)}},
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": {"code": "INVALID_STATE", "message": str(exc)}},
        )
    if not result:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "ROUTE_PLAN_NOT_FOUND", "message": "Route plan not found."}},
        )
    return AssignRouteResponse(
        route_plan_id=route_plan_id,
        crew_id=body.crew_id,
        vehicle_id=body.vehicle_id,
    )


@router.patch(
    "/plans/{route_plan_id}/stops/{stop_id}", response_model=UpdateStopResponse
)
async def update_stop(
    route_plan_id: str,
    stop_id: str,
    body: UpdateStopRequest,
    db: DBDep,
    _user: StopUpdateDep,
):
    result = await complete_route_stop(
        db,
        route_plan_id,
        stop_id,
        body.model_dump(exclude_none=False),
        completed_by=_user["_id"],
    )
    if not result:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": {
                    "code": "STOP_NOT_FOUND",
                    "message": "Route plan or stop not found.",
                }
            },
        )
    return UpdateStopResponse(
        route_plan_id=route_plan_id,
        stop_id=stop_id,
        status=body.status,
    )
