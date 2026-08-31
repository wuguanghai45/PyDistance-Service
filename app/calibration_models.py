"""Validated station recipes and operator requests for eight-point calibration."""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MeasurementKey = Literal["ALN", "AHN", "BHN", "BLN", "BHY", "BLY", "ALY", "AHY"]
MEASUREMENT_ORDER = ("ALN", "AHN", "BHN", "BLN", "BHY", "BLY", "ALY", "AHY")


class StrictModel(BaseModel):
    """Reject misspelled fields and non-finite numeric values."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Point(StrictModel):
    """An explicitly mapped floor-code point; coordinates are millimetres."""

    code: str = Field(min_length=1, max_length=128)
    x: int
    y: int
    orientation: float = Field(
        ge=0, lt=360, description="Degrees, converted to 0.01° on MQTT"
    )


class Limit(StrictModel):
    """Inclusive acceptance interval, independent of commanded lift travel."""

    target_mm: float
    tolerance_mm: float = Field(ge=0)


class StationConfig(StrictModel):
    """A versioned recipe; A/B are physical ends, never ADC channel names."""

    name: str = Field(min_length=1, max_length=80)
    start: Point
    calibration: Point
    bin: Point
    storage: Point | None = None
    finish: Point
    approach_waypoints: list[Point] = Field(default_factory=list, max_length=50)
    exit_waypoints: list[Point] = Field(default_factory=list, max_length=50)
    sensor_channel: Literal[1] = Field(
        default=1, description="固定使用实际接线 ADS1115 A1（channel=1）；A0 暂不启用"
    )
    low_height_mm: int = Field(ge=0, le=1000)
    high_height_mm: int = Field(gt=0, le=1000)
    settle_seconds: float = Field(default=2, ge=2, le=30)
    sample_window_seconds: float = Field(default=0.5, ge=0.1, le=5)
    min_samples: int = Field(default=5, ge=2, le=1000)
    max_sample_age_seconds: float = Field(default=0.2, gt=0, le=2)
    max_spread_mm: float = Field(
        default=5, gt=0, description="保留字段：窗口极差仅记录，不再用于拒绝读数"
    )
    command_timeout_seconds: float = Field(default=60, ge=1, le=600)
    confirmation_timeout_seconds: float = Field(
        default=300, ge=5, le=1800, description="取放箱载荷反馈等待超时"
    )
    telemetry_timeout_seconds: float = Field(default=3, ge=0.2, le=30)
    position_tolerance_mm: float = Field(default=50, gt=0, le=50)
    orientation_tolerance_deg: float = Field(default=5, gt=0, le=10)
    lift_tolerance_mm: float = Field(default=3, gt=0, le=20)
    velocity: int = Field(default=100, gt=0, le=1000)
    acceleration: int = Field(default=500, gt=0, le=500)
    obstacle_avoidance: bool = True
    allow_set_origin: bool = False
    load_feedback_field: str | None = Field(default=None, max_length=100)
    scan_code_field: str | None = Field(default=None, max_length=100)
    scan_valid_value: bool | int | str = True
    limits: dict[MeasurementKey, Limit] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_geometry(self) -> "StationConfig":
        """Reject recipes that cannot preserve A/B orientation on the box legs."""
        if self.high_height_mm <= self.low_height_mm + 2 * self.lift_tolerance_mm:
            raise ValueError("高低位差必须大于两倍举升到位公差")
        angle = math.radians(self.calibration.orientation)
        for point in (self.bin, self.storage or self.bin):
            dx, dy = point.x - self.calibration.x, point.y - self.calibration.y
            along = dx * math.cos(angle) + dy * math.sin(angle)
            lateral = abs(-dx * math.sin(angle) + dy * math.cos(angle))
            if along <= 2 * self.position_tolerance_mm or lateral > 1:
                raise ValueError(
                    "料箱/存放点必须在标定点 A 朝向的正前方同一直线上；不支持自动绕障规划"
                )
        if self.start.x == self.calibration.x and self.start.y == self.calibration.y:
            raise ValueError(
                "起始点不能与标定点重合，采集地面基准时光路必须无机器人遮挡"
            )
        b_angle = (self.calibration.orientation + 180) % 360
        if (
            abs((self.bin.orientation - b_angle + 180) % 360 - 180)
            > self.orientation_tolerance_deg
        ):
            raise ValueError("取箱点朝向必须与 A 测量朝向相差 180°，保持 B 面测量姿态")
        if (
            self.storage
            and abs(
                (self.storage.orientation - self.calibration.orientation + 180) % 360
                - 180
            )
            > self.orientation_tolerance_deg
        ):
            raise ValueError("独立存放点朝向必须与 A 测量朝向一致")
        return self


class StartRequest(StrictModel):
    """Explicit identity and confirmations required before any hardware motion."""

    config_id: str
    identity_type: Literal["robotSN"] = Field(
        default="robotSN", description="固定使用设备服务提供的 robotSN"
    )
    identity: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_.:-]+$",
        description="设备服务提供的 robotSN，直接用作 MQTT 主题后缀",
    )
    mode: Literal["simulation", "live"] = Field(
        default="live", description="HTTP 标定任务固定为实机模式"
    )
    ground_clear_confirmed: bool = False
    robot_at_start_confirmed: bool = False
    route_safe_confirmed: bool = False
    loaded_low_safe_confirmed: bool = False
    live_motion_confirmed: bool = False


class ReleaseRequest(StrictModel):
    """Acknowledge physical safety before releasing a failed live station."""

    robot_stopped_and_station_safe: bool


def demo_config() -> StationConfig:
    """Return a clearly labelled straight-line simulation recipe, not plant coordinates."""
    return StationConfig(
        name="演示工位（坐标仅供模拟）",
        start=Point(code="DEMO-START", x=0, y=0, orientation=0),
        calibration=Point(code="DEMO-CAL", x=1000, y=0, orientation=0),
        bin=Point(code="DEMO-BIN", x=2000, y=0, orientation=180),
        finish=Point(code="DEMO-END", x=3000, y=0, orientation=0),
        low_height_mm=0,
        high_height_mm=300,
    )
