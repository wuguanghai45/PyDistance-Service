"""Application configuration loaded from environment / .env file."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings loaded from .env (or environment variables)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # --- Sensor physical parameters ---
    D_MIN: float = 50.0
    D_MAX: float = 2500.0
    V_MAX: float = 10.0
    V_ERROR: float = 10.1
    DIVIDER_RATIO: float = 1.682

    # --- ADS1115 hardware configuration ---
    # Hex string (e.g. "0x48") or integer are both accepted via validator.
    I2C_ADDRESS: int = 0x48
    ADS_CHANNELS: list[int] = Field(default_factory=lambda: [0, 1])
    ADS_GAIN: float = 2 / 3
    ADS_DATA_RATE: int = 250

    # --- Sampling & filtering ---
    SAMPLE_INTERVAL: float = 0.02
    WINDOW_SECONDS: float = 1.0
    FILTER_METHOD: Literal["mean", "median", "trimmed_mean"] = "trimmed_mean"
    TRIM_RATIO: float = 0.2
    ANOMALY_JUMP_MM: float = 50.0

    # --- Service ---
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    WS_PUSH_INTERVAL: float = 0.1
    LOG_LEVEL: str = "INFO"
    LOG_FILE: str = "logs/service.log"

    # Allow I2C_ADDRESS to be supplied as "0x48" string in .env.
    @field_validator("I2C_ADDRESS", mode="before")
    @classmethod
    def _parse_hex_address(cls, v: object) -> int:
        if isinstance(v, str):
            return int(v, 0)
        if isinstance(v, int):
            return v
        raise TypeError(f"I2C_ADDRESS must be int or str, got {type(v)!r}")

    @field_validator("ADS_CHANNELS")
    @classmethod
    def _validate_channels(cls, v: list[int]) -> list[int]:
        if not v:
            raise ValueError("ADS_CHANNELS must contain at least one channel")
        for ch in v:
            if ch not in (0, 1, 2, 3):
                raise ValueError(f"ADS1115 channel must be 0..3, got {ch}")
        if len(set(v)) != len(v):
            raise ValueError("ADS_CHANNELS contains duplicates")
        return v

    @field_validator("TRIM_RATIO")
    @classmethod
    def _validate_trim(cls, v: float) -> float:
        if not 0.0 <= v < 0.5:
            raise ValueError("TRIM_RATIO must be in [0, 0.5)")
        return v


# Module-level singleton; import this instead of constructing Settings yourself.
settings = Settings()
