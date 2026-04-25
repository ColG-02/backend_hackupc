"""
Route planning service.

Candidate selection and priority scoring are fully implemented per spec §13.
Actual route optimisation (OR-Tools VRP) is deferred — stops are assigned to
vehicles via a simple round-robin sort by priority score.
"""

from datetime import datetime, timedelta
from uuid import uuid4

from motor.motor_asyncio import AsyncIOMotorDatabase

from ..models.common import EventStatus, EventType, RoutePlanStatus, RouteStopStatus
from ..models.route import CreateRoutePlanRequest
from .event_service import create_system_event
from ..models.common import EventSeverity


def _new_plan_id(plan_date: str) -> str:
    return f"rp-{plan_date}-" + uuid4().hex[:6]


def _new_stop_id() -> str:
    return "stop-" + uuid4().hex[:6]


def _compute_priority(container: dict, open_event_types: set[str]) -> float:
    """Priority score formula from spec §13.1."""
    fused = container.get("latest_state", {}).get("fused_fill_pct") or 0
    garbage_open = 1 if EventType.GARBAGE_DETECTED.value in open_event_types else 0
    critical_open = 1 if EventType.CRITICAL_FULL.value in open_event_types else 0
    full_open = 1 if EventType.FULL_THRESHOLD.value in open_event_types else 0

    last_collected = container.get("last_collected_at")
    if last_collected:
        days_since = (datetime.utcnow() - last_collected).days
        days_score = min(days_since, 10)
    else:
        days_score = 10

    return (
        0.50 * fused
        + 20 * garbage_open
        + 20 * critical_open
        + 10 * full_open
        + 5 * days_score
    )


async def create_route_plan(
    db: AsyncIOMotorDatabase, request: CreateRoutePlanRequest, created_by: str
) -> dict:
    threshold = request.constraints.include_threshold_pct
    forced_types = set(request.constraints.force_include_event_types)
    plan_date = request.date.isoformat()

    # Fetch all active containers
    all_containers = await db.containers.find({"status": "ACTIVE"}).to_list(None)

    # For each container, gather open events to determine inclusion and score
    candidates: list[tuple[dict, set, float]] = []
    for container in all_containers:
        cid = container["_id"]
        open_events = await db.events.find(
            {"container_id": cid, "status": EventStatus.OPEN.value}
        ).to_list(None)
        open_types = {e["type"] for e in open_events}

        fused = container.get("latest_state", {}).get("fused_fill_pct") or 0
        tamper = container.get("latest_state", {}).get("tamper_open", False)

        include = (
            fused >= threshold
            or bool(open_types & forced_types)
            or tamper
        )
        if include:
            score = _compute_priority(container, open_types)
            candidates.append((container, open_types, score))

    # Sort by priority score descending
    candidates.sort(key=lambda x: x[2], reverse=True)

    n_vehicles = len(request.vehicle_ids)
    vehicle_stops: dict[str, list[dict]] = {vid: [] for vid in request.vehicle_ids}
    dropped = 0

    for i, (container, open_types, score) in enumerate(candidates):
        vid = request.vehicle_ids[i % n_vehicles]
        stop_order = len(vehicle_stops[vid]) + 1

        # Simple ETA: base time 06:00 + (order - 1) * (service_time + travel_time)
        base = datetime.combine(request.date, datetime.min.time()).replace(hour=6)
        eta = base + timedelta(minutes=(stop_order - 1) * 23)  # 8 min service + 15 min travel

        reasons = []
        fused = container.get("latest_state", {}).get("fused_fill_pct") or 0
        fill_state = container.get("latest_state", {}).get("fill_state")
        if fill_state:
            reasons.append(fill_state)
        for et in open_types & forced_types:
            reasons.append(et)

        vehicle_stops[vid].append(
            {
                "stop_id": _new_stop_id(),
                "order": stop_order,
                "container_id": container["_id"],
                "eta": eta,
                "service_time_min": 8,
                "priority_score": round(score, 2),
                "reason": reasons,
                "status": RouteStopStatus.PENDING.value,
                "completed_at": None,
                "collected_weight_kg": None,
                "notes": None,
            }
        )

    total_stops = sum(len(s) for s in vehicle_stops.values())
    plan_id = _new_plan_id(plan_date.replace("-", ""))
    now = datetime.utcnow()

    routes = []
    for vid in request.vehicle_ids:
        stops = vehicle_stops[vid]
        est_duration = len(stops) * 23
        routes.append(
            {
                "vehicle_id": vid,
                "estimated_distance_km": None,
                "estimated_duration_min": est_duration,
                "stops": stops,
            }
        )

    plan_doc = {
        "_id": plan_id,
        "date": plan_date,
        "status": RoutePlanStatus.PLANNED.value,
        "depot": request.depot.model_dump(),
        "vehicle_ids": request.vehicle_ids,
        "summary": {
            "vehicles_used": n_vehicles,
            "stops": total_stops,
            "estimated_distance_km": None,
            "estimated_duration_min": sum(r["estimated_duration_min"] for r in routes),
            "dropped_low_priority_stops": dropped,
        },
        "routes": routes,
        "created_by": created_by,
        "created_at": now,
        "updated_at": now,
        "dispatched_at": None,
        "dispatched_by": None,
    }
    await db.route_plans.insert_one(plan_doc)
    return plan_doc


async def complete_route_stop(
    db: AsyncIOMotorDatabase,
    plan_id: str,
    stop_id: str,
    update: dict,
    completed_by: str,
) -> dict | None:
    """Mark a stop completed and apply all side effects per spec §13.5."""
    now = datetime.utcnow()

    plan = await db.route_plans.find_one({"_id": plan_id})
    if not plan:
        return None

    target_stop = None
    container_id = None
    for route in plan.get("routes", []):
        for stop in route.get("stops", []):
            if stop["stop_id"] == stop_id:
                target_stop = stop
                container_id = stop["container_id"]
                break

    if not target_stop:
        return None

    # Update the stop in-place
    await db.route_plans.update_one(
        {"_id": plan_id, "routes.stops.stop_id": stop_id},
        {
            "$set": {
                "routes.$[].stops.$[s].status": update.get("status", RouteStopStatus.COMPLETED.value),
                "routes.$[].stops.$[s].completed_at": update.get("completed_at", now),
                "routes.$[].stops.$[s].collected_weight_kg": update.get("collected_weight_kg"),
                "routes.$[].stops.$[s].notes": update.get("notes"),
                "updated_at": now,
            }
        },
        array_filters=[{"s.stop_id": stop_id}],
    )

    if update.get("status") == RouteStopStatus.COMPLETED.value and container_id:
        # Update container last_collected_at
        await db.containers.update_one(
            {"_id": container_id},
            {"$set": {"last_collected_at": now, "updated_at": now}},
        )

        # Resolve open fill/garbage events
        resolve_types = [
            EventType.GARBAGE_DETECTED.value,
            EventType.FULL_THRESHOLD.value,
            EventType.CRITICAL_FULL.value,
        ]
        await db.events.update_many(
            {
                "container_id": container_id,
                "type": {"$in": resolve_types},
                "status": EventStatus.OPEN.value,
            },
            {
                "$set": {
                    "status": EventStatus.RESOLVED.value,
                    "ended_at": now,
                    "updated_at": now,
                }
            },
        )

        # Get device_id for the container
        container = await db.containers.find_one({"_id": container_id})
        device_id = container.get("device_id", "") if container else ""

        await create_system_event(
            db,
            container_id,
            device_id,
            EventType.COLLECTION_CONFIRMED,
            EventSeverity.INFO,
            "Container serviced — collection confirmed by route stop completion.",
        )

    return {"plan_id": plan_id, "stop_id": stop_id, "status": update.get("status")}
