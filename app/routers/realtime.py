"""
Server-Sent Events endpoint for real-time dispatcher operations.

Clients connect once and receive a stream of typed events:
  crew.location.updated, container.latest_state.updated,
  alarm.created, alarm.updated,
  route.stop.updated, route.plan.updated

A keepalive comment is sent every 25 seconds so proxies and load
balancers do not close the idle connection.
"""

import asyncio
import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from ..core.event_bus import bus
from ..core.security import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/realtime", tags=["realtime"])

UserDep = Annotated[dict, Depends(get_current_user)]

_KEEPALIVE_INTERVAL_SEC = 25


@router.get("/operations", summary="SSE stream for live operations dashboard")
async def operations_stream(request: Request, _user: UserDep):
    """
    Returns a text/event-stream. Each event has the form:

        event: <event_type>
        data: <json payload>

    The stream sends a keepalive comment (`: ping`) every 25 seconds.
    """
    q = bus.subscribe()

    async def generator():
        try:
            while True:
                # Use a timeout so we can emit keepalives and check disconnection
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=_KEEPALIVE_INTERVAL_SEC)
                except asyncio.TimeoutError:
                    if await request.is_disconnected():
                        break
                    yield ": ping\n\n"
                    continue

                if await request.is_disconnected():
                    break

                event_type = msg.get("event", "message")
                payload = {k: v for k, v in msg.items() if k != "event"}
                data = json.dumps(payload, default=str)
                yield f"event: {event_type}\ndata: {data}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            bus.unsubscribe(q)
            logger.debug("SSE client disconnected.")

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
