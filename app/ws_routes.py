"""WebSocket routes for real-time distance streaming."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.config import settings
from app.logger import get_logger
from app.sensor import sensor_service

logger = get_logger(__name__)

router = APIRouter(tags=["websocket"])


@router.websocket("/ws/distance")
async def ws_distance(websocket: WebSocket) -> None:
    """Stream filtered distance readings at WS_PUSH_INTERVAL."""
    await websocket.accept()
    client = websocket.client
    logger.debug("WebSocket connected: %s", client)
    try:
        while True:
            payload = sensor_service.get_latest()
            await websocket.send_json(payload)
            await asyncio.sleep(settings.WS_PUSH_INTERVAL)
    except WebSocketDisconnect:
        logger.debug("WebSocket disconnected: %s", client)
    except Exception:
        logger.exception("WebSocket error for %s", client)
        raise
