"""
Fast-poll dashboard summary endpoint.

All counts are fetched in parallel via asyncio.gather so the response
time stays low even as collections grow.
"""

import asyncio
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from motor.motor_asyncio import AsyncIOMotorDatabase

from ..core.database import get_db
from ..core.security import get_current_user
from ..models.common import CrewStatus, EventStatus, FillState, RoutePlanStatus

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

DBDep = Annotated[AsyncIOMotorDatabase, Depends(get_db)]
UserDep = Annotated[dict, Depends(get_current_user)]

_ACTIVE_CREW_STATUSES = [
    CrewStatus.ON_DUTY.value,
    CrewStatus.IN_ROUTE.value,
    CrewStatus.AT_STOP.value,
    CrewStatus.ON_BREAK.value,
]

_ACTIVE_PLAN_STATUSES = [
    RoutePlanStatus.DISPATCHED.value,
    RoutePlanStatus.IN_PROGRESS.value,
]


@router.get("/summary")
async def get_summary(db: DBDep, _user: UserDep):
    """Aggregate counts for the dispatcher operations map."""
    (
        containers_total,
        containers_near_full,
        containers_full,
        containers_critical,
        open_alarms,
        critical_alarms,
        crews_on_duty,
        active_route_plans,
        open_tickets,
    ) = await asyncio.gather(
        db.containers.count_documents({"status": "ACTIVE"}),
        db.containers.count_documents({
            "status": "ACTIVE",
            "latest_state.fill_state": FillState.NEAR_FULL.value,
        }),
        db.containers.count_documents({
            "status": "ACTIVE",
            "latest_state.fill_state": FillState.FULL.value,
        }),
        db.containers.count_documents({
            "status": "ACTIVE",
            "latest_state.fill_state": FillState.CRITICAL.value,
        }),
        db.events.count_documents({"status": EventStatus.OPEN.value}),
        db.events.count_documents({
            "status": EventStatus.OPEN.value,
            "severity": "CRITICAL",
        }),
        db.crews.count_documents({"status": {"$in": _ACTIVE_CREW_STATUSES}}),
        db.route_plans.count_documents({"status": {"$in": _ACTIVE_PLAN_STATUSES}}),
        db.maintenance_tickets.count_documents({"status": "OPEN"}),
    )

    return {
        "containers_total": containers_total,
        "containers_near_full": containers_near_full,
        "containers_full": containers_full,
        "containers_critical": containers_critical,
        "open_alarms": open_alarms,
        "critical_alarms": critical_alarms,
        "crews_on_duty": crews_on_duty,
        "active_route_plans": active_route_plans,
        "open_tickets": open_tickets,
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }
