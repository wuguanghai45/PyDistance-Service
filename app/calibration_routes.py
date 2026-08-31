"""Calibration HTTP APIs and immediate task streaming for trusted station networks."""

from __future__ import annotations

import asyncio
import csv
import io

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import Response

from app.calibration_models import (
    Confirmation,
    ReleaseRequest,
    StartRequest,
    StationConfig,
    demo_config,
)
from app.calibration_service import TERMINAL, CalibrationService
from app.calibration_store import StationBusy


def service(request: Request) -> CalibrationService:
    """Get the lifespan-owned orchestrator, never a second global instance."""
    return request.app.state.calibration


def check_origin(request: Request) -> None:
    """Reject cross-site browser requests without requiring application credentials."""
    origin = request.headers.get("origin")
    if origin and origin != str(request.base_url).rstrip("/"):
        raise HTTPException(403, "禁止跨站操作标定工位")


router = APIRouter(
    prefix="/api/v1/calibration",
    tags=["calibration"],
    dependencies=[Depends(check_origin)],
)
ws_router = APIRouter()


def translate_error(exc: Exception) -> HTTPException:
    """Map expected validation, busy-station and missing-record errors."""
    if isinstance(exc, KeyError):
        return HTTPException(404, "工位配置或标定任务不存在")
    return HTTPException(409 if isinstance(exc, StationBusy) else 422, str(exc))


@router.get("/system")
async def system_info(svc: CalibrationService = Depends(service)) -> dict:
    """Expose readiness without returning MQTT credentials."""
    s = svc.settings
    return {
        "live_enabled": bool(s.CALIBRATION_LIVE_ENABLED and s.MQTT_HOST),
        "station_owner": svc.store.owner(),
        "demo_config": demo_config().model_dump(),
        "measurement_order": ["ALN", "AHN", "BHN", "BLN", "BHY", "BLY", "ALY", "AHY"],
    }


@router.get("/configs")
async def configs(svc: CalibrationService = Depends(service)) -> list[dict]:
    """List immutable station configuration versions."""
    return svc.store.configs()


@router.post("/configs", status_code=201)
async def save_config(
    config: StationConfig, svc: CalibrationService = Depends(service)
) -> dict:
    """Save a new recipe version; running tasks retain their original snapshot."""
    return svc.store.save_config(config.model_dump())


@router.delete("/configs/{config_id}", status_code=204)
async def delete_config(
    config_id: str, svc: CalibrationService = Depends(service)
) -> None:
    """Delete one saved recipe version while retaining every task snapshot."""
    try:
        svc.store.delete_config(config_id)
    except KeyError as exc:
        raise translate_error(exc) from exc


@router.post("/tasks", status_code=201)
async def start_task(
    body: StartRequest, svc: CalibrationService = Depends(service)
) -> dict:
    """Capture baseline and launch a task; default mode never controls hardware."""
    try:
        return await svc.start(body)
    except (ValueError, KeyError) as exc:
        raise translate_error(exc) from exc


@router.get("/tasks")
async def tasks(
    limit: int = Query(50, ge=1, le=200), svc: CalibrationService = Depends(service)
) -> list[dict]:
    """List recent task summaries without repeating all raw sample arrays."""
    return [
        {
            k: task[k]
            for k in (
                "id",
                "created_at",
                "finished_at",
                "mode",
                "robot_label",
                "status",
                "step_title",
                "verdict",
            )
        }
        for task in svc.store.list_tasks(limit)
    ]


@router.get("/tasks/{task_id}")
async def task(task_id: str, svc: CalibrationService = Depends(service)) -> dict:
    """Return progress, eight-point results and frozen recipe/baseline."""
    try:
        return svc.snapshot(task_id)
    except KeyError as exc:
        raise translate_error(exc) from exc


@router.get("/tasks/{task_id}/result")
async def result(task_id: str, svc: CalibrationService = Depends(service)) -> dict:
    """Return complete or partial measurements without claiming incomplete tasks passed."""
    record = await task(task_id, svc)
    return {
        k: record[k]
        for k in (
            "id",
            "mode",
            "status",
            "verdict",
            "baseline",
            "measurements",
            "error",
        )
    }


@router.get("/tasks/{task_id}/events")
async def events(
    task_id: str,
    after: int = Query(0, ge=0),
    svc: CalibrationService = Depends(service),
) -> list[dict]:
    """Read incremental command and operator audit events (500 per page)."""
    await task(task_id, svc)
    return svc.store.events(task_id, after)


@router.post("/tasks/{task_id}/cancel")
async def cancel(task_id: str, svc: CalibrationService = Depends(service)) -> dict:
    """Stop future scheduling; this endpoint is not a hardware emergency stop."""
    try:
        return await svc.cancel(task_id)
    except (ValueError, KeyError) as exc:
        raise translate_error(exc) from exc


@router.post("/tasks/{task_id}/confirm")
async def confirm(
    task_id: str, body: Confirmation, svc: CalibrationService = Depends(service)
) -> dict:
    """Confirm a current pickup/drop gate, rejecting stale or negative acknowledgements."""
    if not body.confirmed:
        raise HTTPException(422, "未确认；请检查现场或取消任务")
    try:
        return svc.confirm(task_id, body.step)
    except (ValueError, KeyError) as exc:
        raise translate_error(exc) from exc


@router.post("/tasks/{task_id}/release")
async def release(
    task_id: str, body: ReleaseRequest, svc: CalibrationService = Depends(service)
) -> dict:
    """Release a failed/interrupted station after a physical safety acknowledgement."""
    if not body.robot_stopped_and_station_safe:
        raise HTTPException(422, "必须确认机器人已经停止、料箱及工位安全")
    try:
        return svc.release(task_id)
    except (ValueError, KeyError) as exc:
        raise translate_error(exc) from exc


def csv_safe(value):
    """Prevent spreadsheet formula evaluation of operator-supplied identifiers."""
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


@router.get("/tasks/{task_id}/export")
async def export(task_id: str, svc: CalibrationService = Depends(service)) -> Response:
    """Export all eight slots with explicit simulation, task status and missing values."""
    record = await task(task_id, svc)
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(
        [
            "task_id",
            "robot_label",
            "mode",
            "task_status",
            "verdict",
            "key",
            "channel",
            "ground_mm",
            "distance_mm",
            "height_mm",
            "deviation_mm",
            "measurement_verdict",
            "timestamp",
        ]
    )
    for key in ("ALN", "BLN", "AHN", "BHN", "ALY", "BLY", "AHY", "BHY"):
        measurement = record["measurements"].get(key, {})
        writer.writerow(
            [
                csv_safe(v)
                for v in [
                    record["id"],
                    record["robot_label"],
                    record["mode"],
                    record["status"],
                    record["verdict"],
                    key,
                    record["baseline"]["channel"],
                    record["baseline"]["distance_mm"],
                    measurement.get("reading", {}).get("distance_mm", ""),
                    measurement.get("height_mm", ""),
                    measurement.get("deviation_mm", ""),
                    measurement.get("verdict", "MISSING"),
                    measurement.get("timestamp", ""),
                ]
            ]
        )
    return Response(
        "\ufeff" + stream.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="calibration-{record["id"]}.csv"'
        },
    )


@ws_router.websocket("/ws/calibration/{task_id}")
async def stream_task(websocket: WebSocket, task_id: str) -> None:
    """Immediately stream task snapshots and audit events; no client handshake needed."""
    svc = websocket.app.state.calibration
    origin = websocket.headers.get("origin")
    if origin and origin.removeprefix("https://").removeprefix(
        "http://"
    ) != websocket.headers.get("host"):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    try:
        cursor = 0
        while True:
            snapshot = svc.snapshot(task_id)
            audit = svc.store.events(task_id, cursor)
            if audit:
                cursor = audit[-1]["seq"]
            await websocket.send_json({"task": snapshot, "events": audit})
            if snapshot["status"] in TERMINAL and len(audit) < 500:
                await websocket.close(code=1000)
                return
            await asyncio.sleep(0.2)
    except WebSocketDisconnect:
        return
    except (ValueError, KeyError):
        await websocket.close(code=1008)
