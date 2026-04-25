from datetime import datetime

from pydantic import BaseModel, Field

from .common import CrewStatus


class CrewLocation(BaseModel):
    lat: float
    lng: float
    accuracy_m: float | None = None
    heading_deg: float | None = None
    speed_mps: float | None = None
    updated_at: datetime


class CrewOut(BaseModel):
    crew_id: str
    name: str
    status: CrewStatus
    members_count: int
    vehicle_id: str | None = None
    phone: str | None = None
    current_location: CrewLocation | None = None
    assigned_route_plan_id: str | None = None
    created_at: datetime
    updated_at: datetime


class CrewListResponse(BaseModel):
    items: list[CrewOut]
    total: int


class CreateCrewRequest(BaseModel):
    name: str
    members_count: int = 1
    vehicle_id: str | None = None
    phone: str | None = None


class CreateCrewResponse(BaseModel):
    crew_id: str
    created: bool = True


class UpdateCrewRequest(BaseModel):
    name: str | None = None
    members_count: int | None = None
    vehicle_id: str | None = None
    phone: str | None = None


class UpdateCrewResponse(BaseModel):
    crew_id: str
    updated: bool = True


class UpdateCrewStatusRequest(BaseModel):
    status: CrewStatus


class UpdateCrewStatusResponse(BaseModel):
    crew_id: str
    status: CrewStatus


class IngestLocationRequest(BaseModel):
    route_plan_id: str | None = None
    lat: float
    lng: float
    accuracy_m: float | None = None
    heading_deg: float | None = None
    speed_mps: float | None = None
    recorded_at: datetime
    battery_level: float | None = None


class IngestLocationResponse(BaseModel):
    accepted: bool = True
    crew_id: str
    received_at: str


class CrewPositionItem(BaseModel):
    crew_id: str
    name: str
    status: CrewStatus
    vehicle_id: str | None = None
    members_count: int
    assigned_route_plan_id: str | None = None
    location: CrewLocation | None = None


class CrewPositionsResponse(BaseModel):
    items: list[CrewPositionItem]


class CrewLocationHistoryItem(BaseModel):
    crew_id: str
    route_plan_id: str | None = None
    lat: float
    lng: float
    accuracy_m: float | None = None
    heading_deg: float | None = None
    speed_mps: float | None = None
    recorded_at: datetime
    received_at: datetime


class CrewLocationHistoryResponse(BaseModel):
    items: list[CrewLocationHistoryItem]
    total: int
