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

from ..models.common import EventStatus, EventType, RoutePlanStatus, RouteStopStatus
from ..models.route import CreateRoutePlanRequest
from .event_service import create_system_event
from ..models.common import EventSeverity

_AVG_SPEED_KMH = 30.0   # average urban truck speed
_SERVICE_MIN = 8         # minutes spent at each stop


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
    items: list[tuple[dict, set, float]],  # (container, open_types, score)
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

    # Containers without coordinates go last, sorted by priority
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

    # Sort by priority score descending for round-robin vehicle assignment
    candidates.sort(key=lambda x: x[2], reverse=True)

    n_vehicles = len(request.vehicle_ids)
    # Assign candidates to vehicles via round-robin (highest priority first)
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
        # Reorder each vehicle's stops with nearest-neighbor heuristic
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
                travel_min = 15.0  # default when no coords

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
                    "completed_at": None,
                    "collected_weight_kg": None,
                    "notes": None,
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

    # Reload plan to check whether all stops are now completed
    updated_plan = await db.route_plans.find_one({"_id": plan_id})
    if updated_plan:
        all_stops_done = all(
            stop.get("status") in (RouteStopStatus.COMPLETED.value, RouteStopStatus.SKIPPED.value)
            for route in updated_plan.get("routes", [])
            for stop in route.get("stops", [])
        )
        if all_stops_done:
            await db.route_plans.update_one(
                {"_id": plan_id},
                {"$set": {"status": RoutePlanStatus.COMPLETED.value, "updated_at": now}},
            )

    return {"plan_id": plan_id, "stop_id": stop_id, "status": update.get("status")}
