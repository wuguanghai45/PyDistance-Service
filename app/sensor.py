"""Hardware Abstraction Layer for ADS1115 + CHG laser displacement sensor.

The :class:`SensorService` is a singleton that owns the I2C bus, runs a
background sampling thread, and serves filtered readings to the API layer.
A background daemon thread is used (instead of an asyncio task) so the tight
sampling loop is not affected by event-loop latency, while FastAPI handlers
only ever touch in-memory state.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from app.config import settings
from app.logger import get_logger

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Pure helpers (no hardware deps) — easy to unit-test.
# ---------------------------------------------------------------------------

def voltage_to_distance(real_v: float) -> tuple[float | None, str]:
    """Map real sensor voltage (V) to distance (mm) via linear interpolation.

    Returns ``(distance_mm, status)``. ``distance_mm`` is ``None`` when the
    target is invalid / out of range.
    """
    if real_v > settings.V_ERROR:
        return None, "Out of Range"
    distance = settings.D_MIN + (real_v / settings.V_MAX) * (settings.D_MAX - settings.D_MIN)
    distance = max(settings.D_MIN, min(settings.D_MAX, distance))
    return distance, "Normal"


def apply_filter(values: list[float]) -> float | None:
    """Apply the configured filter algorithm to a list of voltage samples."""
    n = len(values)
    if n == 0:
        return None

    method = settings.FILTER_METHOD
    if method == "mean":
        return sum(values) / n

    sorted_v = sorted(values)
    if method == "median":
        mid = n // 2
        if n % 2:
            return sorted_v[mid]
        return (sorted_v[mid - 1] + sorted_v[mid]) / 2

    # trimmed_mean
    k = int(n * settings.TRIM_RATIO)
    trimmed = sorted_v[k : n - k] if n > 2 * k else sorted_v
    return sum(trimmed) / len(trimmed)


# ---------------------------------------------------------------------------
# Per-channel state container.
# ---------------------------------------------------------------------------


class _ChannelState:
    """In-memory rolling state for a single ADS1115 channel."""

    def __init__(self, channel: int, deque_capacity: int) -> None:
        self.channel = channel
        # Each entry: (monotonic_ts, raw_voltage_after_divider)
        self.samples: deque[tuple[float, float]] = deque(maxlen=deque_capacity)
        self.last_distance: float | None = None
        self.last_status: str = "Error"
        self.last_voltage: float = 0.0
        self.last_timestamp: str = ""
        self.consecutive_failures: int = 0


# ---------------------------------------------------------------------------
# Sensor service singleton.
# ---------------------------------------------------------------------------


class SensorService:
    """Singleton that owns the ADS1115 and a background sampling thread."""

    _instance: "SensorService | None" = None
    _instance_lock = threading.Lock()

    def __new__(cls) -> "SensorService":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialised = False
            return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialised", False):
            return
        self._initialised = True

        # Deque is sized to comfortably hold > 1 window of samples.
        capacity = max(
            32,
            int((settings.WINDOW_SECONDS / max(settings.SAMPLE_INTERVAL, 1e-3)) * 4),
        )
        self._states: dict[int, _ChannelState] = {
            ch: _ChannelState(ch, capacity) for ch in settings.ADS_CHANNELS
        }

        self._state_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        # Hardware handles (populated in `start`).
        self._i2c = None
        self._ads = None
        self._analog_ins: dict[int, object] = {}
        self._ads_online = False

        self._start_ts: float = 0.0
        self._total_rounds: int = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Initialise ADS1115 and launch the sampling thread."""
        if self._thread is not None and self._thread.is_alive():
            logger.warning("SensorService.start() called while already running")
            return

        self._init_hardware()
        self._stop_event.clear()
        self._start_ts = time.monotonic()
        self._thread = threading.Thread(
            target=self._sampling_loop,
            name="ads1115-sampler",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "SensorService started: channels=%s, interval=%.3fs, filter=%s",
            settings.ADS_CHANNELS,
            settings.SAMPLE_INTERVAL,
            settings.FILTER_METHOD,
        )

    def stop(self, timeout: float = 2.0) -> None:
        """Signal the sampling thread to exit and wait for it."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        logger.info("SensorService stopped")

    def _init_hardware(self) -> None:
        """Initialise I2C / ADS1115 / AnalogIn handles.

        Imports are local so the module can be imported on a dev machine
        without adafruit-blinka being able to bind to real hardware.
        """
        try:
            import board
            import busio
            import adafruit_ads1x15.ads1115 as ADS
            from adafruit_ads1x15.analog_in import AnalogIn
        except Exception as exc:
            logger.error("Failed to import hardware libraries: %s", exc)
            self._ads_online = False
            return

        try:
            self._i2c = busio.I2C(board.SCL, board.SDA)
            self._ads = ADS.ADS1115(self._i2c, address=settings.I2C_ADDRESS)
            self._ads.gain = settings.ADS_GAIN
            self._ads.data_rate = settings.ADS_DATA_RATE
            self._analog_ins = {
                ch: AnalogIn(self._ads, ch) for ch in settings.ADS_CHANNELS
            }
            self._ads_online = True
            logger.info(
                "ADS1115 initialised at 0x%02X gain=%.4f data_rate=%d",
                settings.I2C_ADDRESS,
                settings.ADS_GAIN,
                settings.ADS_DATA_RATE,
            )
        except Exception as exc:
            self._ads_online = False
            logger.exception("ADS1115 initialisation failed: %s", exc)

    # ------------------------------------------------------------------
    # Sampling loop (runs in background thread)
    # ------------------------------------------------------------------

    def _sampling_loop(self) -> None:
        interval = max(settings.SAMPLE_INTERVAL, 0.001)
        while not self._stop_event.is_set():
            round_start = time.monotonic()
            for ch in settings.ADS_CHANNELS:
                if self._stop_event.is_set():
                    break
                self._sample_channel(ch)
            self._total_rounds += 1

            # Sleep for the remainder of the period (busy rounds don't oversleep).
            elapsed = time.monotonic() - round_start
            remaining = interval - elapsed
            if remaining > 0:
                self._stop_event.wait(remaining)

    def _sample_channel(self, channel: int) -> None:
        state = self._states[channel]
        if not self._ads_online:
            state.consecutive_failures += 1
            return

        try:
            measured_v = self._analog_ins[channel].voltage
        except Exception as exc:
            state.consecutive_failures += 1
            if state.consecutive_failures in (1, 10, 100) or state.consecutive_failures % 1000 == 0:
                logger.error(
                    "I2C read failed on channel %d (consecutive=%d): %s",
                    channel,
                    state.consecutive_failures,
                    exc,
                )
            return

        real_v = measured_v * settings.DIVIDER_RATIO
        now = time.monotonic()
        with self._state_lock:
            state.samples.append((now, real_v))
            state.consecutive_failures = 0

    # ------------------------------------------------------------------
    # Query API (called from FastAPI handlers)
    # ------------------------------------------------------------------

    def _window_samples(self, state: _ChannelState) -> list[float]:
        """Return voltages within the configured time window."""
        cutoff = time.monotonic() - settings.WINDOW_SECONDS
        with self._state_lock:
            return [v for ts, v in state.samples if ts >= cutoff]

    def get_latest(self) -> dict:
        """Return a snapshot dict suitable for the ``/distance`` endpoint."""
        now_iso = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        channels_out: list[dict] = []

        for ch in settings.ADS_CHANNELS:
            state = self._states[ch]
            window = self._window_samples(state)
            samples_in_window = len(window)

            filtered_v = apply_filter(window)
            if filtered_v is None:
                channels_out.append(
                    {
                        "channel": ch,
                        "distance_mm": None,
                        "raw_voltage": 0.0,
                        "samples_in_window": 0,
                        "status": "Error",
                        "unit": "mm",
                    }
                )
                state.last_status = "Error"
                state.last_distance = None
                state.last_voltage = 0.0
                state.last_timestamp = now_iso
                continue

            distance, status = voltage_to_distance(filtered_v)
            self._check_anomaly(ch, state, distance, filtered_v)

            state.last_voltage = filtered_v
            state.last_distance = distance
            state.last_status = status
            state.last_timestamp = now_iso

            channels_out.append(
                {
                    "channel": ch,
                    "distance_mm": round(distance, 2) if distance is not None else None,
                    "raw_voltage": round(filtered_v, 4),
                    "samples_in_window": samples_in_window,
                    "status": status,
                    "unit": "mm",
                }
            )

        return {"timestamp": now_iso, "channels": channels_out}

    def _check_anomaly(
        self,
        channel: int,
        state: _ChannelState,
        new_distance: float | None,
        new_voltage: float,
    ) -> None:
        """Log a WARNING when the distance jumps abnormally or readings drift to error."""
        prev = state.last_distance
        if new_distance is None and state.last_status == "Normal":
            logger.warning(
                "Channel %d transitioned to Out of Range (last_distance=%.2f mm, v=%.3f V)",
                channel,
                prev or 0.0,
                new_voltage,
            )
            return
        if (
            new_distance is not None
            and prev is not None
            and abs(new_distance - prev) > settings.ANOMALY_JUMP_MM
        ):
            logger.warning(
                "Channel %d distance jump: %.2f mm -> %.2f mm (>%.1f mm threshold)",
                channel,
                prev,
                new_distance,
                settings.ANOMALY_JUMP_MM,
            )

    # ------------------------------------------------------------------
    # Status / health
    # ------------------------------------------------------------------

    def get_status(self) -> dict:
        """Return a snapshot dict suitable for the ``/status`` endpoint."""
        channel_blocks: list[dict] = []
        total_in_window = 0
        for ch in settings.ADS_CHANNELS:
            state = self._states[ch]
            window = self._window_samples(state)
            samples_in_window = len(window)
            total_in_window += samples_in_window
            channel_blocks.append(
                {
                    "channel": ch,
                    "consecutive_failures": state.consecutive_failures,
                    "last_status": state.last_status,
                    "samples_in_window": samples_in_window,
                }
            )

        # Average per-channel rate over the configured window.
        rate = (
            total_in_window / len(settings.ADS_CHANNELS) / settings.WINDOW_SECONDS
            if settings.ADS_CHANNELS
            else 0.0
        )

        return {
            "sensor_online": self._ads_online,
            "i2c_address": f"0x{settings.I2C_ADDRESS:02X}",
            "channels": channel_blocks,
            "total_rounds": self._total_rounds,
            "actual_sample_rate_hz": round(rate, 2),
            "filter_method": settings.FILTER_METHOD,
            "uptime_seconds": round(time.monotonic() - self._start_ts, 2) if self._start_ts else 0.0,
        }


# Module-level singleton accessor for the rest of the app to import.
sensor_service = SensorService()
