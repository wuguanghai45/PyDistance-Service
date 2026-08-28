"""Persistent, single-station orchestration of empty and loaded ANT calibration."""

from __future__ import annotations

import asyncio
import math
import time
from uuid import uuid4

from app.calibration_models import MEASUREMENT_ORDER, Point, StartRequest, StationConfig
from app.calibration_robot import (
    LiveRobot,
    SimRobot,
    angle_error,
    field_value,
    target_matches,
)
from app.calibration_store import StationBusy, Store, utc_now

TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED"}
CANCEL_WARNING = "已停止后续调度，但当前机器人动作可能仍在执行；此操作不是硬件急停。请现场停止并确认安全。"


class CalibrationService:
    """Own exactly one active job for the physical sensor station."""

    def __init__(self, store: Store, sensor, settings, robot_factory=None):
        self.store, self.sensor, self.settings = store, sensor, settings
        self.robot_factory = robot_factory
        self.lock = asyncio.Lock()
        self.worker: asyncio.Task | None = None
        self.robot = None
        self.current: dict | None = None
        self.confirmed = asyncio.Event()

    def _reading(self, config: StationConfig, since: float) -> dict:
        """Collect a valid precision reading using the recipe's quality limits."""
        return self.sensor.calibration_reading(
            config.sensor_channel,
            since,
            config.sample_window_seconds,
            config.min_samples,
            config.max_sample_age_seconds,
            config.max_spread_mm,
        )

    @staticmethod
    def _sim_reading(channel: int, height: float = 0) -> dict:
        """Return explicitly synthetic data, separate from the physical sensor API."""
        return {
            "channel": channel,
            "timestamp": utc_now(),
            "distance_mm": 2000.0 - height,
            "raw_voltage": None,
            "samples_in_window": 25,
            "spread_mm": 0.0,
            "status": "Normal",
            "simulation": True,
            "filter_method": "synthetic",
            "sample_age_seconds": 0.0,
            "voltages": [],
            "sample_offsets_seconds": [],
        }

    async def start(self, request: StartRequest) -> dict:
        """Validate, freeze baseline and recipe, then schedule the background job."""
        async with self.lock:
            if self.store.owner() or (self.worker and not self.worker.done()):
                raise StationBusy("工位已占用，不能同时标定另一台机器人")
            config = StationConfig.model_validate(
                self.store.config(request.config_id)["config"]
            )
            # StartRequest validates topic-safe characters. SN/Label is metadata;
            # both are passed through unchanged as the actual MQTT identifier.
            label = request.identity
            if request.mode == "live":
                if (
                    not self.settings.CALIBRATION_LIVE_ENABLED
                    or not self.settings.MQTT_HOST
                ):
                    raise ValueError(
                        "实机模式未启用：需配置 CALIBRATION_LIVE_ENABLED 和 MQTT_HOST"
                    )
                if not all(
                    (
                        request.ground_clear_confirmed,
                        request.robot_at_start_confirmed,
                        request.route_safe_confirmed,
                        request.loaded_low_safe_confirmed,
                        request.live_motion_confirmed,
                    )
                ):
                    raise ValueError(
                        "必须确认地面无遮挡、机器人在起点、路线安全、低位仍有负载及允许实机动作"
                    )
                baseline = self._reading(
                    config, time.monotonic() - config.sample_window_seconds
                )
            else:
                baseline = self._sim_reading(config.sensor_channel)
            task = {
                "id": uuid4().hex,
                "created_at": utc_now(),
                "finished_at": None,
                "mode": request.mode,
                "identity_type": request.identity_type,
                "identity": request.identity,
                "robot_label": label,
                "config_id": request.config_id,
                "config": config.model_dump(),
                "confirmations": request.model_dump(),
                "baseline": baseline,
                "status": "RUNNING",
                "step": "PREPARE",
                "step_title": "准备初始化",
                "measurements": {},
                "error": None,
                "pending_confirmation": None,
                "wait_until": None,
                "verdict": "PENDING",
                "robot_state": {},
                "station_released": False,
            }
            self.store.create_task(task)
            self.current = task
            self.robot = None
            self.store.event(task["id"], "baseline", baseline)
            self.worker = asyncio.create_task(
                self._run(task, config), name=f"calibration-{task['id']}"
            )
            return self.snapshot(task["id"])

    def snapshot(self, task_id: str) -> dict:
        """Read a persisted task and attach the active robot's latest telemetry."""
        task = self.store.task(task_id)
        if self.current and self.current["id"] == task_id and self.robot:
            task["robot_state"] = self.robot.state.copy()
            task["mqtt_connected"] = self.robot.connected
        task["station_locked"] = self.store.owner() == task_id
        return task

    def _save(self) -> None:
        """Commit task state and a robot snapshot before proceeding."""
        if self.robot:
            self.current["robot_state"] = self.robot.state.copy()
        self.store.save_task(self.current)

    def _step(self, name: str, title: str) -> None:
        """Persist the current step before any associated physical side effect."""
        self.current.update(step=name, step_title=title, wait_until=None)
        self._save()
        self.store.event(self.current["id"], "step", {"step": name, "title": title})

    async def _run(self, task: dict, config: StationConfig) -> None:
        """Execute the whole recipe once; never resume automatically after interruption."""
        factory = self.robot_factory or (
            SimRobot if task["mode"] == "simulation" else LiveRobot
        )
        try:
            self.robot = factory(
                task["robot_label"],
                config,
                self.settings,
                lambda kind, data: self.store.event(task["id"], kind, data),
            )
            self._step("CONNECT", "连接机器人并等待最新状态")
            await self.robot.start()
            if self.robot.state["mainState"] == "UNKNOWN":
                self._step("INIT", "初始化机器人")
                await self.robot.command("INIT")
            if self.robot.state["mainState"] == "LOCATION_UNKNOWN":
                if not config.allow_set_origin:
                    raise ValueError("机器人定位未知；未启用按已确认起点设置原点")
                self._step("HOME", "使用已人工确认的起始地码设置原点")
                await self.robot.command("HOME_SET_ORIGIN")
            self._assert_idle()
            self._assert_point(config.start, config.start.orientation, config)
            if task["mode"] == "live" and config.load_feedback_field:
                self._assert_load(False, config)
            self._step("START_LOW", "起点下降至低位")
            await self.robot.command("LIFT", liftHeight=config.low_height_mm)
            await self._transit(
                [*config.approach_waypoints, config.calibration], "TO_CAL", config
            )
            await self._measure("ALN", config)
            self._step("EMPTY_HIGH", "空载 A 面举升至高位")
            await self.robot.command("LIFT", liftHeight=config.high_height_mm)
            await self._measure("AHN", config)
            self._step("EMPTY_ROTATE_B", "空载旋转 180°，传感器对准 B 点")
            await self.robot.command(
                "SPIN",
                orientation=round((config.calibration.orientation + 180) % 360 * 100),
            )
            await self._measure("BHN", config)
            self._step("EMPTY_LOW_B", "空载 B 面下降至低位")
            await self.robot.command("LIFT", liftHeight=config.low_height_mm)
            await self._measure("BLN", config)
            await self._box_move(
                config.bin, "TO_BIN", "保持 B 朝向，倒车到料箱点", config
            )
            self._step("PICKUP", "举升取箱")
            await self.robot.command("LIFT", liftHeight=config.high_height_mm)
            await self._verify_load(True, "CONFIRM_PICKUP", config)
            await self._box_move(
                config.calibration,
                "RETURN_LOADED",
                "保持 B 朝向，携箱前进返回标定点",
                config,
            )
            await self._measure("BHY", config)
            self._step("LOADED_LOW_B", "负载 B 面下降至低位")
            await self.robot.command("LIFT", liftHeight=config.low_height_mm)
            await self._measure("BLY", config)
            self._step("LOADED_ROTATE_A", "负载低位旋转 180°，传感器对准 A 点")
            await self.robot.command(
                "SPIN", orientation=round(config.calibration.orientation * 100)
            )
            await self._measure("ALY", config)
            self._step("LOADED_HIGH_A", "负载 A 面举升至高位")
            await self.robot.command("LIFT", liftHeight=config.high_height_mm)
            await self._measure("AHY", config)
            storage = config.storage or config.bin
            await self._box_move(
                storage, "TO_STORAGE", "保持 A 朝向，高位携箱正向到存放点", config
            )
            self._step("DROP", "下降并归还料箱")
            await self.robot.command("LIFT", liftHeight=config.low_height_mm)
            await self._verify_load(False, "CONFIRM_DROP", config)
            await self._transit(
                [*config.exit_waypoints, config.finish], "TO_FINISH", config
            )
            if tuple(task["measurements"]) != MEASUREMENT_ORDER:
                raise RuntimeError("八项数据不完整，禁止标记完成")
            verdicts = [r["verdict"] for r in task["measurements"].values()]
            task["verdict"] = (
                "FAIL"
                if "FAIL" in verdicts
                else ("PASS" if all(v == "PASS" for v in verdicts) else "NOT_EVALUATED")
            )
            task.update(
                status="COMPLETED",
                step="COMPLETED",
                step_title="标定流程完成，机器人已到完成点",
            )
        except asyncio.CancelledError:
            task.update(status="CANCELLED", error=CANCEL_WARNING)
            self.store.event(task["id"], "cancelled", {"message": CANCEL_WARNING})
        except Exception as exc:
            task.update(status="FAILED", error=str(exc))
            self.store.event(task["id"], "failed", {"message": str(exc)})
        finally:
            task.update(
                finished_at=utc_now(), pending_confirmation=None, wait_until=None
            )
            self._save()
            if self.robot:
                try:
                    await self.robot.close()
                except Exception as exc:
                    self.store.event(task["id"], "close_error", {"message": str(exc)})
            if task["status"] == "COMPLETED" or task["mode"] == "simulation":
                self.store.release(task["id"])
                task["station_released"] = True
                self._save()
            # Failed live sessions keep the durable station lock for operator acknowledgement.

    def _assert_idle(self) -> None:
        """Require current, fault-free telemetry and zero motion for measurements."""
        self.robot.guard()
        if self.robot.state.get("mainState") != "IDLE":
            raise RuntimeError("机器人未静止或不处于 IDLE")
        for key in ("velocity", "angularVelocity"):
            if key in self.robot.state and self.robot.state[key] != 0:
                raise RuntimeError("机器人速度非零，禁止采样")

    def _assert_point(
        self, point: Point, orientation: float, config: StationConfig
    ) -> None:
        """Verify position, heading and scan status, optionally the actual code string."""
        target = {
            "coordX": point.x,
            "coordY": point.y,
            "orientation": round(orientation * 100),
        }
        if not target_matches(self.robot.state, target, config):
            raise ValueError(f"未到位或朝向不匹配：{point.code}")
        if self.current["mode"] == "simulation":
            return
        if self.robot.state.get("qrCodeStatus") != config.scan_valid_value:
            raise ValueError("qrCodeStatus 未匹配配置的有效扫码状态")
        if (
            config.scan_code_field
            and str(field_value(self.robot.state, config.scan_code_field)) != point.code
        ):
            raise ValueError(f"实际地码不匹配：{point.code}")

    async def _transit(
        self, points: list[Point], prefix: str, config: StationConfig
    ) -> None:
        """Follow configured waypoints using explicit straight legs and final headings."""
        for index, point in enumerate(points):
            self._step(f"{prefix}_{index}", f"前往地码 {point.code}")
            state = self.robot.state
            dx, dy = point.x - state["coordX"], point.y - state["coordY"]
            if math.hypot(dx, dy) > 0.1:
                heading = math.degrees(math.atan2(dy, dx)) % 360
                if angle_error(state["orientation"] / 100, heading) > 0.01:
                    await self.robot.command("SPIN", orientation=round(heading * 100))
                await self.robot.command("MOVE", coordX=point.x, coordY=point.y)
            if (
                angle_error(self.robot.state["orientation"] / 100, point.orientation)
                > 0.01
            ):
                await self.robot.command(
                    "SPIN", orientation=round(point.orientation * 100)
                )
            self._assert_idle()
            self._assert_point(point, point.orientation, config)

    async def _box_move(
        self, point: Point, step: str, title: str, config: StationConfig
    ) -> None:
        """Move on the prevalidated box line without rotating away from A/B."""
        self._step(step, title)
        orientation = (
            config.calibration.orientation
            if step == "TO_STORAGE"
            else (config.calibration.orientation + 180) % 360
        )
        if (
            angle_error(self.robot.state["orientation"] / 100, orientation)
            > config.orientation_tolerance_deg
        ):
            raise ValueError("取放箱行驶前朝向错误")
        await self.robot.command("MOVE", coordX=point.x, coordY=point.y)
        self._assert_idle()
        self._assert_point(point, orientation, config)

    async def _wait_still(
        self, seconds: float, expected: dict, config: StationConfig
    ) -> None:
        """Wait cancellably while checking pose, fresh telemetry and faults throughout."""
        deadline = time.monotonic() + seconds
        self.current["wait_until"] = time.time() + seconds
        self._save()
        while True:
            self._assert_idle()
            if not target_matches(self.robot.state, expected, config):
                raise ValueError("等待/采样期间机器人位姿或举升高度发生偏移")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(0.05, remaining))
        self.current["wait_until"] = None

    async def _measure(self, key: str, config: StationConfig) -> None:
        """Wait two seconds, then sample only the new stationary window and persist it."""
        self._step("MEASURE_" + key, f"采集 {key}：稳定等待后读取高度")
        expected = {
            "coordX": config.calibration.x,
            "coordY": config.calibration.y,
            "orientation": round(
                ((config.calibration.orientation + (180 if key[0] == "B" else 0)) % 360)
                * 100
            ),
            "liftHeight": config.high_height_mm
            if key[1] == "H"
            else config.low_height_mm,
        }
        self._assert_point(config.calibration, expected["orientation"] / 100, config)
        simulated = self.current["mode"] == "simulation"
        await self._wait_still(
            0.04 if simulated else config.settle_seconds, expected, config
        )
        since = time.monotonic()
        await self._wait_still(
            0.02 if simulated else config.sample_window_seconds, expected, config
        )
        if not simulated and config.load_feedback_field:
            self._assert_load(key[2] == "Y", config)
        if simulated:
            height = (
                250
                + expected["liftHeight"]
                + (1 if key[0] == "B" else 0)
                - (2 if key[2] == "Y" else 0)
            )
            reading = self._sim_reading(config.sensor_channel, height)
        else:
            reading = self._reading(config, since)
        baseline = self.current["baseline"]["distance_mm"]
        height = baseline - reading["distance_mm"]
        if height <= 0:
            raise ValueError("计算高度非正数，请检查地面基准和激光目标")
        limit = config.limits.get(key)
        deviation = height - limit.target_mm if limit else None
        result = {
            "key": key,
            "reading": reading,
            "ground_distance_mm": baseline,
            "height_mm": height,
            "deviation_mm": deviation,
            "verdict": ("PASS" if abs(deviation) <= limit.tolerance_mm else "FAIL")
            if limit
            else "NOT_EVALUATED",
            "robot_state": self.robot.state.copy(),
            "timestamp": utc_now(),
            "load_evidence": "simulation"
            if simulated
            else ("telemetry" if config.load_feedback_field else "operator_confirmed"),
        }
        self.current["measurements"][key] = result
        self._save()
        self.store.event(self.current["id"], "measurement", result)

    def _assert_load(self, loaded: bool, config: StationConfig) -> None:
        """Reject a missing or conflicting explicitly configured load switch."""
        value = field_value(self.robot.state, config.load_feedback_field)
        if (
            not isinstance(value, (bool, int))
            or value not in (0, 1)
            or bool(value) != loaded
        ):
            raise ValueError("载荷反馈与测量工况不匹配，禁止记录为 N/Y")

    async def _verify_load(
        self, loaded: bool, step: str, config: StationConfig
    ) -> None:
        """Confirm pickup/drop using configured feedback or an explicit operator gate."""
        self._step(step, "确认料箱已取到" if loaded else "确认料箱已放回原位")
        if self.current["mode"] == "simulation":
            self.store.event(
                self.current["id"],
                "load_confirmation",
                {"loaded": loaded, "source": "simulation"},
            )
            return
        self.confirmed.clear()
        if not config.load_feedback_field:
            self.current["pending_confirmation"] = step
            self._save()
        deadline = time.monotonic() + config.confirmation_timeout_seconds
        while True:
            self._assert_idle()
            if config.load_feedback_field:
                value = field_value(self.robot.state, config.load_feedback_field)
                valid = (
                    isinstance(value, (bool, int))
                    and value in (0, 1)
                    and bool(value) == loaded
                )
            else:
                valid = self.confirmed.is_set()
            if valid:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("等待取箱/放箱确认超时")
            await asyncio.sleep(0.05)
        self.current["pending_confirmation"] = None
        self._save()
        self.store.event(
            self.current["id"],
            "load_confirmation",
            {
                "loaded": loaded,
                "source": "telemetry" if config.load_feedback_field else "operator",
            },
        )

    def confirm(self, task_id: str, step: str) -> dict:
        """Acknowledge only the current task's pending pickup/drop step."""
        task = self.store.task(task_id)
        if (
            not self.current
            or self.current["id"] != task_id
            or task["status"] != "RUNNING"
            or task["pending_confirmation"] != step
        ):
            raise ValueError("确认已过期或当前步骤不需要确认")
        self.confirmed.set()
        self.store.event(task_id, "operator_confirmation", {"step": step})
        return self.snapshot(task_id)

    async def cancel(self, task_id: str) -> dict:
        """Cancel orchestration, not hardware motion; wait for cleanup before unlock."""
        async with self.lock:
            task = self.store.task(task_id)
            if task["status"] in TERMINAL:
                return self.snapshot(task_id)
            if not self.current or self.current["id"] != task_id or not self.worker:
                raise ValueError("任务不属于当前执行器")
            self.current.update(status="CANCELLING", error=CANCEL_WARNING)
            self._save()
            self.worker.cancel()
            try:
                await self.worker
            except asyncio.CancelledError:
                # Cancellation before the coroutine's first instruction still needs a terminal record.
                self.current.update(
                    status="CANCELLED", error=CANCEL_WARNING, finished_at=utc_now()
                )
                self._save()
                if self.current["mode"] == "simulation":
                    self.store.release(task_id)
            return self.snapshot(task_id)

    def release(self, task_id: str) -> dict:
        """Unlock a terminated live task only after the caller confirms physical safety."""
        task = self.store.task(task_id)
        if task["status"] not in TERMINAL or (
            self.current
            and self.current["id"] == task_id
            and self.worker
            and not self.worker.done()
        ):
            raise ValueError("任务仍在执行/清理，不能解锁")
        self.store.event(task_id, "operator_station_release", {"confirmed_safe": True})
        self.store.release(task_id)
        task["station_released"] = True
        self.store.save_task(task)
        return self.snapshot(task_id)

    async def close(self) -> None:
        """Cancel active orchestration on shutdown, keeping live safety locks durable."""
        if self.worker and not self.worker.done():
            await self.cancel(self.current["id"])
            # A terminal snapshot may precede MQTT cleanup. Do not close SQLite
            # until the worker has finished its final audit writes.
            try:
                await self.worker
            except asyncio.CancelledError:
                pass
