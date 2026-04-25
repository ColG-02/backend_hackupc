from datetime import date, datetime

from pydantic import BaseModel

from .common import RoutePlanStatus, RouteStopStatus


class Depot(BaseModel):
    lat: float
    lng: float
    name: str | None = None


class RouteConstraints(BaseModel):
    max_route_duration_min: int = 480
    include_threshold_pct: float = 70
    force_include_event_types: list[str] = ["GARBAGE_DETECTED", "CRITICAL_FULL", "FULL_THRESHOLD"]
    allow_drop_low_priority: bool = True


class CreateRoutePlanRequest(BaseModel):
    date: date
    depot: Depot
    vehicle_ids: list[str]
    constraints: RouteConstraints = RouteConstraints()


class RouteStop(BaseModel):
    stop_id: str
    order: int
    container_id: str
    eta: datetime | None = None
    service_time_min: int = 8
    priority_score: float
    reason: list[str] = []
    status: RouteStopStatus = RouteStopStatus.PENDING
    completed_at: datetime | None = None
    collected_weight_kg: float | None = None
    notes: str | None = None


class VehicleRoute(BaseModel):
    vehicle_id: str
    estimated_distance_km: float | None = None
    estimated_duration_min: float | None = None
    stops: list[RouteStop] = []


class RoutePlanSummary(BaseModel):
    vehicles_used: int
    stops: int
    estimated_distance_km: float | None = None
    estimated_duration_min: float | None = None
    dropped_low_priority_stops: int = 0


class RoutePlanOut(BaseModel):
    route_plan_id: str
    date: str
    status: RoutePlanStatus
    summary: RoutePlanSummary | None = None
    routes: list[VehicleRoute] = []
    created_at: datetime | None = None
    dispatched_at: datetime | None = None
    dispatched_by: str | None = None


class RoutePlanListItem(BaseModel):
    route_plan_id: str
    date: str
    status: RoutePlanStatus
    vehicles_used: int
    stops: int
    estimated_distance_km: float | None = None
    estimated_duration_min: float | None = None


class RoutePlanListResponse(BaseModel):
    items: list[RoutePlanListItem]
    total: int


class DispatchRequest(BaseModel):
    dispatched_by: str


class DispatchResponse(BaseModel):
    route_plan_id: str
    status: RoutePlanStatus
    dispatched_at: str


class UpdateStopRequest(BaseModel):
    status: RouteStopStatus
    completed_at: datetime | None = None
    collected_weight_kg: float | None = None
    notes: str | None = None


class UpdateStopResponse(BaseModel):
    updated: bool = True
    route_plan_id: str
    stop_id: str
    status: RouteStopStatus
