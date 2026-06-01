"""FastAPI application entry point."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.config import settings
from app.logger import get_logger, setup_logging
from app.routes import health_router, router as api_v1_router
from app.sensor import sensor_service
from app.ws_routes import router as ws_router

setup_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start/stop the background sampling thread alongside the HTTP server."""
    logger.info("Starting PyDistance-Service v%s", __version__)
    sensor_service.start()
    try:
        yield
    finally:
        logger.info("Shutting down PyDistance-Service")
        sensor_service.stop()


app = FastAPI(
    title="PyDistance-Service",
    description=(
        "RESTful service for ADS1115 + CHG laser displacement sensor. "
        "Samples multiple channels at high frequency, applies a "
        "configurable 1-second window filter, and exposes the result via JSON."
    ),
    version=__version__,
    lifespan=lifespan,
)

app.include_router(api_v1_router)
app.include_router(health_router)
app.include_router(ws_router)

_STATIC_DIR = Path(__file__).resolve().parent / "static"
if _STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="static")


def main() -> None:
    """CLI helper: `python -m app.main`."""
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        log_config=None,
        reload=False,
    )


if __name__ == "__main__":
    main()
