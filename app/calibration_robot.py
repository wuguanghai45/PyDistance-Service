"""ANT MQTT adapter and deterministic simulator; importing never connects a robot."""

from __future__ import annotations

import asyncio
import json
import math
import time
from uuid import uuid4

from app.calibration_models import StationConfig
from app.calibration_store import utc_now


def angle_error(a: float, b: float) -> float:
    """Return the smallest absolute angle difference in degrees."""
    return abs((a - b + 180) % 360 - 180)


def field_value(state: dict, dotted_path: str):
    """Read a configured telemetry path without guessing undocumented fields."""
    value = state
    for key in dotted_path.split("."):
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def theoretical_pose(config: StationConfig, lift_height: float | int) -> dict:
    """Return the configured start pose used to seed the command state chain."""
    return {
        "coordX": config.start.x,
        "coordY": config.start.y,
        "orientation": round(config.start.orientation * 100),
        "liftHeight": lift_height,
    }


def start_theoretical_pose(
    config: StationConfig, lift_height: float | int | None = None
) -> dict:
    """Return the start waypoint pose for the first command's expectedState."""
    height = config.low_height_mm if lift_height is None else lift_height
    return theoretical_pose(config, height)


def pose_from_command_state(state: dict) -> dict:
    """Extract the pose fields carried in expectedState/futureState envelopes."""
    return {
        k: state[k] for k in ("coordX", "coordY", "orientation", "liftHeight")
    }


def build_command(
    label: str,
    action: str,
    expected_state: dict,
    config: StationConfig,
    **target,
) -> dict:
    """Build one reference-compatible command from the previous theoretical pose.

    expectedState is the prior command's futureState; futureState is the new
    target pose. Neither envelope uses live telemetry.
    The firmware uses centidegrees; configuration and navigation use degrees.
    Each independent command gets a UUID and is never automatically reissued.
    """
    set_id = f"{label}-CAL-{uuid4().hex}"
    content = {"robotCommandType": action}
    command = {
        "robotCommandLabel": set_id + "-0",
        "previousCommandLabel": "",
        "commandContent": content,
    }
    if action == "HOME_SET_ORIGIN":
        content.update(
            originOffsetX=config.start.x,
            originOffsetY=config.start.y,
            originOrientation=round(config.start.orientation * 100),
        )
    elif action in ("MOVE", "SPIN", "LIFT"):
        future = pose_from_command_state(expected_state)
        future.update(target)
        content.update(
            future,
            coordZ=0,
            finalTargetX=future["coordX"],
            finalTargetY=future["coordY"],
            finalTargetZ=0,
            maxVelocity=config.velocity if action == "MOVE" else 0,
            maxAcceleration=config.acceleration if action == "MOVE" else 0,
            millisecond=0,
            obstacleAvoidance=config.obstacle_avoidance,
        )
        for key, values in (("expectedState", expected_state), ("futureState", future)):
            command[key] = {
                k: values[k] for k in ("coordX", "coordY", "orientation", "liftHeight")
            }
            command[key].update(
                coordZ=0,
                xTolerance=config.position_tolerance_mm,
                yTolerance=config.position_tolerance_mm,
                zTolerance=30,
                orientationTolerance=round(config.orientation_tolerance_deg * 100),
                liftHeightTolerance=config.lift_tolerance_mm,
                velocity=0,
                velocityTolerance=50,
                acceleration=0,
                accelerationTolerance=100,
                angularVelocity=0,
                angularVelocityTolerance=100,
                angularAcceleration=0,
                angularAccelerationTolerance=100,
            )
    elif action != "INIT":
        raise ValueError(f"未支持的机器人命令 {action}")
    return {
        "robotCommandSetLabel": set_id,
        "timestamp": utc_now(),
        "robotCommands": [command],
    }


def target_matches(state: dict, target: dict, config: StationConfig) -> bool:
    """Check pose against target using configured XY (default ±50 mm) and heading (±5°) tolerances."""
    for key, value in target.items():
        actual = state.get(key)
        if (
            not isinstance(actual, (int, float))
            or isinstance(actual, bool)
            or not math.isfinite(actual)
        ):
            return False
        if key == "orientation":
            if (
                angle_error(actual / 100, value / 100)
                > config.orientation_tolerance_deg
            ):
                return False
        elif key in ("coordX", "coordY"):
            if abs(actual - value) > config.position_tolerance_mm:
                return False
        elif abs(actual - value) > (
            config.lift_tolerance_mm if key == "liftHeight" else config.position_tolerance_mm
        ):
            return False
    return True


class LiveRobot:
    """Serialize motion and require matching success plus subsequent fresh telemetry."""

    def __init__(self, label: str, config: StationConfig, settings, audit):
        self.label, self.config, self.settings, self.audit = (
            label,
            config,
            settings,
            audit,
        )
        self.state: dict = {}
        self.client = None
        self.connected = False
        self.closed = False
        self.error: str | None = None
        self.received_at = 0.0
        self.sequence = 0
        self.pending: str | None = None
        self.result: str | None = None
        self.success_sequence: int | None = None
        self._theoretical_state = theoretical_pose(config, config.low_height_mm)

    def _commit_theoretical(self, future_state: dict) -> None:
        """Advance the command chain to the just-completed theoretical pose."""
        self._theoretical_state = pose_from_command_state(future_state)

    def seed_theoretical_from_start(self, lift_height: float | int | None = None) -> None:
        """Anchor the command chain at the configured start pose."""
        if lift_height is None:
            lift_height = self.state.get("liftHeight", self.config.low_height_mm)
        self._theoretical_state = start_theoretical_pose(self.config, lift_height)

    async def start(self) -> None:
        """Connect and subscribe without automatic reconnect or command replay."""
        import paho.mqtt.client as mqtt

        loop = asyncio.get_running_loop()
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id="height-cal-" + uuid4().hex,
            clean_session=True,
            reconnect_on_failure=False,
        )
        self.client.connect_timeout = min(5, self.config.command_timeout_seconds)
        self.client.username_pw_set(
            self.settings.MQTT_USERNAME, self.settings.MQTT_PASSWORD
        )
        if self.settings.MQTT_TLS:
            self.client.tls_set(ca_certs=self.settings.MQTT_CA_FILE)

        def connected(client, userdata, flags, reason_code, properties):
            """Subscribe on successful CONNACK; readiness follows SUBACK."""
            if reason_code.is_failure:
                loop.call_soon_threadsafe(self._network_error, "MQTT 连接被拒绝")
                return
            result, _ = client.subscribe(
                [
                    (f"robot/state/{self.label}", 1),
                    (f"robot/command/status/{self.label}", 2),
                ]
            )
            if result != mqtt.MQTT_ERR_SUCCESS:
                loop.call_soon_threadsafe(self._network_error, "MQTT 订阅失败")

        def subscribed(client, userdata, mid, reasons, properties):
            """Reject denied subscriptions instead of waiting for phantom completions."""
            if any(code.is_failure for code in reasons):
                loop.call_soon_threadsafe(self._network_error, "MQTT 订阅权限不足")
            else:
                loop.call_soon_threadsafe(setattr, self, "connected", True)

        def received(client, userdata, message):
            """Pass bounded, non-retained JSON into the asyncio-owned robot state."""
            if message.retain or len(message.payload) > 262144:
                return
            try:
                payload = json.loads(message.payload)
            except (ValueError, UnicodeDecodeError):
                return
            if isinstance(payload, dict):
                loop.call_soon_threadsafe(self.ingest, message.topic, payload)

        def disconnected(client, userdata, flags, reason, properties):
            """Fail closed on link loss; do not reconnect and replay queued motion."""
            loop.call_soon_threadsafe(
                self._network_error, "MQTT 已断开；不会自动重发命令"
            )

        self.client.on_connect, self.client.on_subscribe = connected, subscribed
        self.client.on_message, self.client.on_disconnect = received, disconnected
        self.client.on_connect_fail = lambda *_: loop.call_soon_threadsafe(
            self._network_error, "MQTT 连接失败"
        )
        # Keep connection establishment inside Paho's owned thread so close() can
        # join it even if the task is cancelled while DNS/TCP setup is in progress.
        self.client.connect_async(self.settings.MQTT_HOST, self.settings.MQTT_PORT, 15)
        self.client.loop_start()
        deadline = time.monotonic() + self.config.command_timeout_seconds
        while not self.connected or not self.state:
            self._check_error()
            if time.monotonic() >= deadline:
                raise TimeoutError("等待 MQTT 连接/首次非保留状态超时")
            await asyncio.sleep(0.05)
        self.guard()
        self.seed_theoretical_from_start()

    def _network_error(self, message: str) -> None:
        """Record asynchronous network failure unless shutdown is intentional."""
        if not self.closed:
            self.connected = False
            self.error = message

    def _check_error(self) -> None:
        """Surface a network error to the waiting state machine."""
        if self.error:
            raise RuntimeError(self.error)

    def ingest(self, topic: str, payload: dict) -> None:
        """Accept only exact robot topics and correlate outcomes by command UUID."""
        if self.closed:
            return
        if topic == f"robot/state/{self.label}":
            numeric = ("coordX", "coordY", "orientation", "liftHeight")
            requires_pose = payload.get("mainState") not in (
                "UNKNOWN",
                "LOCATION_UNKNOWN",
            )
            if not isinstance(payload.get("mainState"), str) or (
                requires_pose
                and any(
                    not isinstance(payload.get(k), (int, float))
                    or isinstance(payload[k], bool)
                    or not math.isfinite(payload[k])
                    for k in numeric
                )
            ):
                self.error = "机器人状态缺少有效位姿/举升高度，不能安全执行标定"
                return
            self.state = payload.copy()
            self.received_at = time.monotonic()
            self.sequence += 1
        elif (
            topic == f"robot/command/status/{self.label}"
            and self.pending
            and payload.get("robotCommandLabel") == self.pending
        ):
            status = payload.get("status")
            self.audit("command_status", payload)
            if status == "COMPLETE_FAILURE":
                self.result = status
            elif status == "COMPLETE_SUCCESS" and self.result is None:
                self.result = status
                self.success_sequence = self.sequence

    def guard(self) -> None:
        """Reject disconnects, stale telemetry and reported robot faults."""
        self._check_error()
        if (
            not self.connected
            or time.monotonic() - self.received_at
            > self.config.telemetry_timeout_seconds
        ):
            raise RuntimeError("机器人状态已过期或 MQTT 离线")
        if self.state.get("mainState") in ("ERROR", "FAULT", "ESTOP", "EMERGENCY_STOP"):
            raise RuntimeError("机器人报告故障/急停")

    async def command(self, action: str, **target) -> None:
        """Publish once and await success plus a later matching stationary state."""
        self.guard()
        if (
            action not in ("INIT", "HOME_SET_ORIGIN")
            and self.state["mainState"] != "IDLE"
        ):
            raise RuntimeError("机器人不是 IDLE，拒绝下发动作")
        payload = build_command(
            self.label, action, self._theoretical_state, self.config, **target
        )
        command = payload["robotCommands"][0]
        self.pending, self.result, self.success_sequence = (
            command["robotCommandLabel"],
            None,
            None,
        )
        topic = f"robot/commandSet/create/{self.label}"
        self.audit("command_sent", {"topic": topic, "payload": payload})
        result = self.client.publish(topic, json.dumps(payload), qos=2, retain=False)
        if result.rc != 0:
            raise RuntimeError("MQTT 命令发布失败")
        deadline = time.monotonic() + self.config.command_timeout_seconds
        allowed = ("IDLE", "LOCATION_UNKNOWN") if action == "INIT" else ("IDLE",)
        expected = command.get("futureState", {})
        expected = {
            k: expected[k]
            for k in ("coordX", "coordY", "orientation", "liftHeight")
            if k in expected
        }
        if action == "HOME_SET_ORIGIN":
            expected = {
                "coordX": self.config.start.x,
                "coordY": self.config.start.y,
                "orientation": round(self.config.start.orientation * 100),
            }
        try:
            while True:
                self.guard()
                if self.result == "COMPLETE_FAILURE":
                    raise RuntimeError(f"机器人命令失败：{action}")
                if (
                    self.result == "COMPLETE_SUCCESS"
                    and self.sequence > self.success_sequence
                ):
                    if self.state["mainState"] in allowed and target_matches(
                        self.state, expected, self.config
                    ):
                        if action in ("MOVE", "SPIN", "LIFT"):
                            self._commit_theoretical(command["futureState"])
                        elif action == "HOME_SET_ORIGIN":
                            self.seed_theoretical_from_start()
                        self.audit(
                            "command_complete",
                            {"label": self.pending, "state": self.state.copy()},
                        )
                        return
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"命令或到位确认超时：{action}")
                await asyncio.sleep(0.05)
        finally:
            self.pending = None

    async def close(self) -> None:
        """Disconnect without asserting that the physical robot has stopped."""
        self.closed = True
        self.connected = False
        if self.client is not None:
            self.client.disconnect()
            await asyncio.to_thread(self.client.loop_stop)


class SimRobot:
    """In-memory robot that emits the same command audit without opening a socket."""

    def __init__(self, label: str, config: StationConfig, settings, audit):
        self.label, self.config, self.audit = label, config, audit
        self._theoretical_state = theoretical_pose(config, config.low_height_mm)
        self.state = {
            "mainState": "UNKNOWN",
            **self._theoretical_state,
            "qrCodeStatus": True,
        }
        self.connected = False
        self.received_at = 0.0

    async def start(self) -> None:
        """Start only the local simulator."""
        self.connected = True
        self.received_at = time.monotonic()

    def guard(self) -> None:
        """Reject use after the simulation session closed."""
        if not self.connected:
            raise RuntimeError("模拟机器人已关闭")

    def _commit_theoretical(self, future_state: dict) -> None:
        """Advance the command chain to the just-completed theoretical pose."""
        self._theoretical_state = pose_from_command_state(future_state)

    def seed_theoretical_from_start(self, lift_height: float | int | None = None) -> None:
        """Anchor the command chain at the configured start pose."""
        if lift_height is None:
            lift_height = self.state.get("liftHeight", self.config.low_height_mm)
        self._theoretical_state = start_theoretical_pose(self.config, lift_height)

    async def command(self, action: str, **target) -> None:
        """Simulate asynchronous motion and preserve the reference wire payload."""
        self.guard()
        payload = build_command(
            self.label, action, self._theoretical_state, self.config, **target
        )
        command = payload["robotCommands"][0]
        self.audit("command_sent", {"simulation": True, "payload": payload})
        self.state["mainState"] = "WORKING"
        await asyncio.sleep(0.03)
        if action in ("MOVE", "SPIN", "LIFT"):
            self._commit_theoretical(command["futureState"])
            self.state.update(self._theoretical_state)
        elif action == "HOME_SET_ORIGIN":
            self.seed_theoretical_from_start()
            self.state.update(self._theoretical_state)
        self.state["mainState"] = "IDLE"
        self.received_at = time.monotonic()
        self.audit("command_complete", {"simulation": True, "state": self.state.copy()})

    async def close(self) -> None:
        """Close the simulator; no hardware is affected."""
        self.connected = False
