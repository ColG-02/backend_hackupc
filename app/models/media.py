from datetime import datetime

from pydantic import BaseModel


class MediaOut(BaseModel):
    media_id: str
    event_id: str
    container_id: str
    device_id: str
    content_type: str
    captured_at: datetime | None = None
    width: int | None = None
    height: int | None = None
    model_id: str | None = None
    camera_state: str | None = None
    confidence: float | None = None
    url: str
