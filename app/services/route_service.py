"""
Route planning service.

Candidate selection and priority scoring follow spec §13.
Stop ordering uses a nearest-neighbor heuristic (greedy TSP approximation)
with haversine distances — no external libraries required.
True VRP (OR-Tools) can replace _nn_order() later without touching the rest.
"""

import math
from datetime import datetime, timedelta
from uuid import uuid4

from motor.motor_asyncio import AsyncIOMotorDatabase

from ..core.event_bus import bus
from ..models.common import (
    CrewStatus,
    EventStatus,
    EventType,
    RoutePlanStatus,
    RouteStopStatus,
)
from ..models.route import CreateRoutePlanRequest
from .event_service import create_system_event
from ..models.common import EventSeverity

_AVG_SPEED_KMH = 30.0   # average urban truck speed
_SERVICE_MIN = 8         # minutes spent at each stop

# Stop statuses that count as "done" when checking plan completion
_TERMINAL_STOP_STATUSES = {
    RouteStopStatus.COMPLETED.value,
    RouteStopStatus.SKIPPED.value,
    RouteStopStatus.FAILED.value,
}


def _new_plan_id(plan_date: str) -> str:
    return f"rp-{plan_date}-" + uuid4().hex[:6]


def _new_stop_id() -> str:
    return "stop-" + uuid4().hex[:6]


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in km between two (lat, lng) points."""
    r = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _container_coords(container: dict) -> tuple[float, float] | None:
    """Return (lat, lng) from a container document, or None if absent."""
    loc = container.get("location")
    if not loc:
        return None
    coords = loc.get("coordinates")  # GeoJSON: [lng, lat]
    if not coords or len(coords) < 2:
        return None
    return coords[1], coords[0]  # (lat, lng)


def _nn_order(
    depot_lat: float,
    depot_lng: float,
    items: list[tuple[dict, set, float]],
) -> list[tuple[dict, set, float]]:
    """
    Nearest-neighbor TSP heuristic starting from depot.
    Containers without location data are appended at the end in priority order.
    """
    with_coords = [(c, ot, sc) for c, ot, sc in items if _container_coords(c)]
    without_coords = [(c, ot, sc) for c, ot, sc in items if not _container_coords(c)]

    ordered: list[tuple[dict, set, float]] = []
    cur_lat, cur_lng = depot_lat, depot_lng

    remaining = list(with_coords)
    while remaining:
        best_idx = min(
            range(len(remaining)),
            key=lambda i: _haversine_km(cur_lat, cur_lng, *_container_coords(remaining[i][0])),
        )
        chosen = remaining.pop(best_idx)
        ordered.append(chosen)
        cur_lat, cur_lng = _container_coords(chosen[0])

    without_coords.sort(key=lambda x: x[2], reverse=True)
    return ordered + without_coords


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
    max_duration = request.constraints.max_route_duration_min
    plan_date = request.date.isoformat()
    depot_lat = request.depot.lat
    depot_lng = request.depot.lng

    all_containers = await db.containers.find({"status": "ACTIVE"}).to_list(None)

    candidates: list[tuple[dict, set, float]] = []
    for container in all_containers:
        cid = container["_id"]
        open_events = await db.events.find(
            {"container_id": cid, "status": EventStatus.OPEN.value}
        ).to_list(None)
        open_types = {e["type"] for e in open_events}

        fused = container.get("latest_state", {}).get("fused_fill_pct") or 0
        tamper = container.get("latest_state", {}).get("tamper_open", False)

        include = fused >= threshold or bool(open_types & forced_types) or tamper
        if include:
            score = _compute_priority(container, open_types)
            candidates.append((container, open_types, score))

    candidates.sort(key=lambda x: x[2], reverse=True)

    n_vehicles = len(request.vehicle_ids)
    vehicle_candidates: dict[str, list[tuple[dict, set, float]]] = {
        vid: [] for vid in request.vehicle_ids
    }
    for i, item in enumerate(candidates):
        vehicle_candidates[request.vehicle_ids[i % n_vehicles]].append(item)

    dropped = 0
    base_time = datetime.combine(request.date, datetime.min.time()).replace(hour=6)

    routes = []
    total_stops = 0
    total_distance_km = 0.0
    total_duration_min = 0.0

    for vid in request.vehicle_ids:
        ordered = _nn_order(depot_lat, depot_lng, vehicle_candidates[vid])

        stops: list[dict] = []
        cur_lat, cur_lng = depot_lat, depot_lng
        cumulative_min = 0.0
        route_distance_km = 0.0

        for container, open_types, score in ordered:
            coords = _container_coords(container)
            if coords:
                travel_km = _haversine_km(cur_lat, cur_lng, *coords)
                travel_min = (travel_km / _AVG_SPEED_KMH) * 60
                cur_lat, cur_lng = coords
            else:
                travel_km = 0.0
                travel_min = 15.0

            stop_duration = travel_min + _SERVICE_MIN
            if request.constraints.allow_drop_low_priority and (
                cumulative_min + stop_duration > max_duration
            ):
                dropped += 1
                continue

            cumulative_min += stop_duration
            route_distance_km += travel_km
            eta = base_time + timedelta(minutes=cumulative_min - _SERVICE_MIN)

            reasons: list[str] = []
            fill_state = container.get("latest_state", {}).get("fill_state")
            if fill_state:
                reasons.append(fill_state)
            for et in open_types & forced_types:
                reasons.append(et)

            stops.append(
                {
                    "stop_id": _new_stop_id(),
                    "order": len(stops) + 1,
                    "container_id": container["_id"],
                    "eta": eta,
                    "service_time_min": _SERVICE_MIN,
                    "priority_score": round(score, 2),
                    "reason": reasons,
                    "status": RouteStopStatus.PENDING.value,
                    "arrived_at": None,
                    "started_at": None,
                    "completed_at": None,
                    "skipped_at": None,
                    "skip_reason": None,
                    "collected_weight_kg": None,
                    "notes": None,
                    "issue_reported": False,
                }
            )

        total_stops += len(stops)
        total_distance_km += route_distance_km
        total_duration_min += cumulative_min
        routes.append(
            {
                "vehicle_id": vid,
                "estimated_distance_km": round(route_distance_km, 2),
                "estimated_duration_min": round(cumulative_min, 1),
                "stops": stops,
            }
        )

    plan_id = _new_plan_id(plan_date.replace("-", ""))
    now = datetime.utcnow()

    plan_doc = {
        "_id": plan_id,
        "date": plan_date,
        "status": RoutePlanStatus.PLANNED.value,
        "depot": request.depot.model_dump(),
        "vehicle_ids": request.vehicle_ids,
        "assigned_crew_id": None,
        "assigned_vehicle_id": None,
        "summary": {
            "vehicles_used": n_vehicles,
            "stops": total_stops,
            "estimated_distance_km": round(total_distance_km, 2),
            "estimated_duration_min": round(total_duration_min, 1),
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


async def assign_route_to_crew(
    db: AsyncIOMotorDatabase,
    plan_id: str,
    crew_id: str,
    vehicle_id: str | None,
) -> dict | None:
    """Assign a dispatched plan to a crew and transition plan → IN_PROGRESS."""
    now = datetime.utcnow()

    plan = await db.route_plans.find_one({"_id": plan_id})
    if not plan:
        return None

    if plan.get("status") not in (RoutePlanStatus.PLANNED.value, RoutePlanStatus.DISPATCHED.value):
        raise ValueError(f"Plan is in state '{plan['status']}' and cannot be assigned.")

    crew = await db.crews.find_one({"_id": crew_id})
    if not crew:
        raise KeyError(f"Crew '{crew_id}' not found.")

    crew_updates: dict = {
        "assigned_route_plan_id": plan_id,
        "status": CrewStatus.IN_ROUTE.value,
        "updated_at": now,
    }
    if vehicle_id:
        crew_updates["vehicle_id"] = vehicle_id

    await db.crews.update_one({"_id": crew_id}, {"$set": crew_updates})
    await db.route_plans.update_one(
        {"_id": plan_id},
        {
            "$set": {
                "assigned_crew_id": crew_id,
                "assigned_vehicle_id": vehicle_id,
                "status": RoutePlanStatus.IN_PROGRESS.value,
                "updated_at": now,
            }
        },
    )

    await bus.publish(
        "route.plan.updated",
        {"route_plan_id": plan_id, "status": RoutePlanStatus.IN_PROGRESS.value,
         "assigned_crew_id": crew_id, "updated_at": now.isoformat() + "Z"},
    )
    return {"plan_id": plan_id, "crew_id": crew_id, "vehicle_id": vehicle_id}


async def complete_route_stop(
    db: AsyncIOMotorDatabase,
    plan_id: str,
    stop_id: str,
    update: dict,
    completed_by: str,
) -> dict | None:
    """Update a stop's status and apply side-effects per the stop's new state."""
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
        if target_stop:
            break

    if not target_stop:
        return None

    new_status = update.get("status", target_stop["status"])

    # Build the field set for the stop document
    stop_fields: dict = {
        "routes.$[].stops.$[s].status": new_status,
        "updated_at": now,
    }
    for field in ("arrived_at", "started_at", "completed_at", "skipped_at",
                  "skip_reason", "collected_weight_kg", "notes", "issue_reported"):
        val = update.get(field)
        if val is not None:
            stop_fields[f"routes.$[].stops.$[s].{field}"] = val

    await db.route_plans.update_one(
        {"_id": plan_id, "routes.stops.stop_id": stop_id},
        {"$set": stop_fields},
        array_filters=[{"s.stop_id": stop_id}],
    )

    # Transition plan from DISPATCHED → IN_PROGRESS on first active stop
    if new_status in (RouteStopStatus.ARRIVED.value, RouteStopStatus.IN_PROGRESS.value):
        if plan.get("status") == RoutePlanStatus.DISPATCHED.value:
            await db.route_plans.update_one(
                {"_id": plan_id},
                {"$set": {"status": RoutePlanStatus.IN_PROGRESS.value, "updated_at": now}},
            )
            await bus.publish(
                "route.plan.updated",
                {"route_plan_id": plan_id, "status": RoutePlanStatus.IN_PROGRESS.value,
                 "updated_at": now.isoformat() + "Z"},
            )

    # Side-effects on COMPLETED stop
    if new_status == RouteStopStatus.COMPLETED.value and container_id:
        await db.containers.update_one(
            {"_id": container_id},
            {"$set": {"last_collected_at": now, "updated_at": now}},
        )

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
            {"$set": {"status": EventStatus.RESOLVED.value, "ended_at": now, "updated_at": now}},
        )

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

        await bus.publish(
            "container.latest_state.updated",
            {"container_id": container_id, "last_collected_at": now.isoformat() + "Z"},
        )

    # Publish stop update event
    await bus.publish(
        "route.stop.updated",
        {
            "route_plan_id": plan_id,
            "stop_id": stop_id,
            "container_id": container_id,
            "status": new_status,
            "updated_at": now.isoformat() + "Z",
        },
    )

    # Reload plan and auto-complete if all stops are terminal
    updated_plan = await db.route_plans.find_one({"_id": plan_id})
    if updated_plan:
        all_done = all(
            stop.get("status") in _TERMINAL_STOP_STATUSES
            for route in updated_plan.get("routes", [])
            for stop in route.get("stops", [])
        )
        if all_done and updated_plan.get("status") not in (
            RoutePlanStatus.COMPLETED.value, RoutePlanStatus.CANCELLED.value
        ):
            await db.route_plans.update_one(
                {"_id": plan_id},
                {"$set": {"status": RoutePlanStatus.COMPLETED.value, "updated_at": now}},
            )
            # Clear crew assignment when plan finishes
            assigned_crew = updated_plan.get("assigned_crew_id")
            if assigned_crew:
                await db.crews.update_one(
                    {"_id": assigned_crew},
                    {"$set": {
                        "status": CrewStatus.ON_DUTY.value,
                        "assigned_route_plan_id": None,
                        "updated_at": now,
                    }},
                )
            await bus.publish(
                "route.plan.updated",
                {"route_plan_id": plan_id, "status": RoutePlanStatus.COMPLETED.value,
                 "updated_at": now.isoformat() + "Z"},
            )

    return {"plan_id": plan_id, "stop_id": stop_id, "status": new_status}
