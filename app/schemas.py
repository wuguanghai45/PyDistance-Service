"""Pydantic response models for the public API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ChannelStatus = Literal["Normal", "Out of Range", "Error"]


class ChannelReading(BaseModel):
    """Filtered reading for a single ADS1115 channel."""

    channel: int = Field(..., ge=0, le=3, description="ADS1115 channel index")
    distance_mm: float | None = Field(
        None,
        description="Distance in millimetres (truncated to whole mm), null when status != Normal",
    )
    raw_voltage: float = Field(
        ..., description="Filtered sensor voltage in volts (after divider compensation)"
    )
    samples_in_window: int = Field(
        ..., ge=0, description="Number of samples used in the filter window"
    )
    status: ChannelStatus
    unit: str = "mm"


class DistanceResponse(BaseModel):
    """Aggregated reading across all configured channels."""

    timestamp: str = Field(..., description="ISO 8601 UTC timestamp of the response")
    channels: list[ChannelReading]


class ChannelHealth(BaseModel):
    """Per-channel health snapshot."""

    channel: int
    consecutive_failures: int
    last_status: ChannelStatus
    samples_in_window: int


class StatusResponse(BaseModel):
    """Service-wide health snapshot."""

    sensor_online: bool
    i2c_address: str
    channels: list[ChannelHealth]
    total_rounds: int
    actual_sample_rate_hz: float
    filter_method: str
    uptime_seconds: float
