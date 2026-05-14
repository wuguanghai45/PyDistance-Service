"""HTTP API routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.schemas import DistanceResponse, StatusResponse
from app.sensor import sensor_service

router = APIRouter(prefix="/api/v1", tags=["distance"])


@router.get(
    "/distance",
    response_model=DistanceResponse,
    summary="Get filtered distance readings for all configured channels",
)
async def get_distance() -> DistanceResponse:
    snapshot = sensor_service.get_latest()
    return DistanceResponse(**snapshot)


@router.get(
    "/status",
    response_model=StatusResponse,
    summary="Get sensor and service health information",
)
async def get_status() -> StatusResponse:
    snapshot = sensor_service.get_status()
    if not snapshot["sensor_online"]:
        # Still return a body, but use 503 to flag degraded state to clients.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"message": "Sensor is offline", "status": snapshot},
        )
    return StatusResponse(**snapshot)


health_router = APIRouter(tags=["health"])


@health_router.get("/health", summary="Lightweight liveness probe")
async def health() -> dict[str, str]:
    return {"status": "ok"}
