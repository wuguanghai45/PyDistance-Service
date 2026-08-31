"""Hardware-free acceptance, audit, protocol-correlation and sensor-quality tests."""

import asyncio
import json
import time
from collections import deque
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.calibration_models import (
    MEASUREMENT_ORDER,
    StartRequest,
    StationConfig,
    demo_config,
)
from app.calibration_robot import (
    LiveRobot,
    SimRobot,
    build_command,
    pose_from_command_state,
    start_theoretical_pose,
    target_matches,
    theoretical_pose,
)
from app.calibration_routes import router, ws_router
from app.calibration_service import CalibrationService
from app.calibration_store import StationBusy, Store
from app.config import Settings
from app.sensor import SensorService, _ChannelState


class FakeSensor:
    """Provide controllable sensor reads and fail if requested by a test."""

    def __init__(self):
        self.calls = 0
        self.failure = None

    def calibration_reading(self, *args):
        self.calls += 1
        if self.failure:
            raise ValueError(self.failure)
        assert args[0] == 1
        return CalibrationService._sim_reading(args[0], 0 if self.calls == 1 else 250)


@pytest.fixture
def setup(tmp_path):
    """Create an isolated station database and disable real MQTT unconditionally."""
    store = Store(str(tmp_path / "cal.sqlite3"))
    config = demo_config()
    record = store.save_config(config.model_dump())
    settings = Settings(
        _env_file=None,
        CALIBRATION_LIVE_ENABLED=False,
        MQTT_HOST="",
    )
    sensor = FakeSensor()
    svc = CalibrationService(store, sensor, settings)
    yield svc, record, config, sensor
    store.close()


def request(record, **kwargs):
    """Construct a default safe simulation request."""
    return StartRequest(config_id=record["id"], identity="ANT-TEST", **kwargs)


def test_history_deletion_removes_events_and_rejects_station_owner(tmp_path):
    """Keep locked-task evidence until an operator explicitly releases the station."""
    store = Store(str(tmp_path / "history.sqlite3"))
    try:
        for task_id in ("first", "second"):
            store.create_task({"id": task_id})
            store.release(task_id)
            store.event(task_id, "test", {"task": task_id})
        store.delete_task("first")
        assert store.list_tasks() == [{"id": "second"}]
        assert store.events("first") == []
        assert store.clear_tasks() == 1
        assert store.list_tasks() == []

        store.create_task({"id": "locked"})
        with pytest.raises(StationBusy):
            store.delete_task("locked")
        with pytest.raises(StationBusy):
            store.clear_tasks()
    finally:
        store.close()


def test_full_simulation_order_motion_and_precision(setup):
    """Exercise all eight measurements, box legs, return and final station release."""

    async def run():
        svc, record, config, sensor = setup
        started = await svc.start(request(record))
        await svc.worker
        task = svc.snapshot(started["id"])
        assert task["status"] == "COMPLETED", task["error"]
        assert tuple(task["measurements"]) == MEASUREMENT_ORDER
        assert task["baseline"]["channel"] == 1
        assert all(m["reading"]["channel"] == 1 for m in task["measurements"].values())
        assert [m["height_mm"] for m in task["measurements"].values()] == [
            250,
            550,
            551,
            251,
            549,
            249,
            248,
            548,
        ]
        assert task["verdict"] == "NOT_EVALUATED"
        assert task["robot_state"]["coordX"] == config.finish.x
        assert task["robot_state"]["liftHeight"] == config.low_height_mm
        assert not task["station_locked"]
        assert sensor.calls == 0
        audit = svc.store.events(task["id"])
        commands = [
            e["data"]["payload"]["robotCommands"][0]
            for e in audit
            if e["kind"] == "command_sent"
        ]
        moves = [
            c["commandContent"]
            for c in commands
            if c["commandContent"]["robotCommandType"] == "MOVE"
        ]
        assert [(m["coordX"], m["orientation"]) for m in moves] == [
            (1000, 0),
            (2000, 18000),
            (1000, 18000),
            (2000, 0),
            (3000, 0),
        ]
        assert len({c["robotCommandLabel"] for c in commands}) == len(commands)
        assert commands[0]["commandContent"]["robotCommandType"] == "INIT"

    asyncio.run(run())


def test_evaluation_failure_is_not_execution_failure(setup):
    """Out-of-tolerance measurements still permit the normal safe return sequence."""

    async def run():
        svc, _, config, _ = setup
        data = config.model_dump()
        data["limits"] = {
            key: {"target_mm": 0, "tolerance_mm": 1} for key in MEASUREMENT_ORDER
        }
        record = svc.store.save_config(StationConfig.model_validate(data).model_dump())
        task = await svc.start(request(record))
        await svc.worker
        final = svc.snapshot(task["id"])
        assert final["status"] == "COMPLETED"
        assert final["verdict"] == "FAIL"

    asyncio.run(run())


def test_station_exclusion_and_cancel_before_worker_starts(setup):
    """Starting or cancelling in adjacent requests cannot leave an unowned motion job."""

    async def run():
        svc, record, _, _ = setup
        task = await svc.start(request(record))
        with pytest.raises(StationBusy):
            await svc.start(request(record))
        final = await svc.cancel(task["id"])
        assert final["status"] == "CANCELLED"
        assert svc.store.owner() is None

    asyncio.run(run())


def test_mid_run_cancel_preserves_partial_records(setup):
    """Cancellation never issues pickup/return commands after an interrupted sample."""

    async def run():
        svc, record, _, _ = setup
        task = await svc.start(request(record))
        while not svc.current["measurements"]:
            await asyncio.sleep(0.01)
        final = await svc.cancel(task["id"])
        assert final["status"] == "CANCELLED"
        assert 1 <= len(final["measurements"]) < 8
        before = len(svc.store.events(task["id"]))
        await asyncio.sleep(0.1)
        assert len(svc.store.events(task["id"])) == before

    asyncio.run(run())


def test_live_gates_still_require_enablement_and_safety_confirmations(setup):
    """Removing access credentials does not enable live motion or skip safety checks."""

    async def run():
        svc, record, _, sensor = setup
        with pytest.raises(ValueError, match="未启用"):
            await svc.start(request(record, mode="live"))
        svc.settings.CALIBRATION_LIVE_ENABLED = True
        with pytest.raises(ValueError, match="MQTT_HOST"):
            await svc.start(request(record, mode="live"))
        svc.settings.MQTT_HOST = "not-used.invalid"
        with pytest.raises(ValueError, match="必须确认"):
            await svc.start(request(record, mode="live"))
        assert sensor.calls == 0
        assert svc.store.owner() is None

    asyncio.run(run())


@pytest.mark.parametrize("identity_type", ["robotSN", "robotLabel"])
def test_robot_identifiers_pass_through_without_mapping(setup, identity_type):
    """Both input types use the unchanged identifier for MQTT command labels."""

    async def run():
        svc, record, _, sensor = setup
        identifier = "ANT-SN.001"
        task = await svc.start(
            StartRequest(
                config_id=record["id"], identity=identifier, identity_type=identity_type
            )
        )
        await svc.worker
        final = svc.snapshot(task["id"])
        assert final["status"] == "COMPLETED", final["error"]
        assert final["robot_label"] == final["identity"] == identifier
        assert final["identity_type"] == identity_type
        commands = [
            event["data"]["payload"]
            for event in svc.store.events(task["id"])
            if event["kind"] == "command_sent"
        ]
        assert commands and all(
            c["robotCommandSetLabel"].startswith(identifier + "-CAL-") for c in commands
        )
        assert sensor.calls == 0

    asyncio.run(run())


@pytest.mark.parametrize("identity", ["robot/#", "robot+", "robot/other", "robot\n"])
def test_identifier_still_rejects_mqtt_topic_injection(identity):
    """Direct identifiers cannot introduce wildcards or another topic level."""
    with pytest.raises(ValidationError):
        StartRequest(config_id="config", identity=identity, identity_type="robotSN")


def test_baseline_failure_never_claims_or_moves_station(setup):
    """Invalid ground references abort before MQTT connection or station ownership."""

    async def run():
        svc, record, _, sensor = setup
        svc.settings.CALIBRATION_LIVE_ENABLED = True
        svc.settings.MQTT_HOST = "not-used.invalid"
        sensor.failure = "基准过期"
        with pytest.raises(ValueError, match="基准过期"):
            await svc.start(
                request(
                    record,
                    mode="live",
                    ground_clear_confirmed=True,
                    robot_at_start_confirmed=True,
                    route_safe_confirmed=True,
                    loaded_low_safe_confirmed=True,
                    live_motion_confirmed=True,
                )
            )
        assert svc.store.owner() is None
        assert not svc.store.list_tasks()

    asyncio.run(run())


def test_failed_live_task_requires_explicit_release(setup):
    """Injected live fault remains locked and cannot falsely report task completion."""

    class BrokenRobot(SimRobot):
        async def command(self, action, **target):
            raise RuntimeError("injected failure")

    async def run():
        svc, record, _, _ = setup
        svc.settings.CALIBRATION_LIVE_ENABLED = True
        svc.settings.MQTT_HOST = "not-used.invalid"
        svc.robot_factory = BrokenRobot
        task = await svc.start(
            request(
                record,
                mode="live",
                ground_clear_confirmed=True,
                robot_at_start_confirmed=True,
                route_safe_confirmed=True,
                loaded_low_safe_confirmed=True,
                live_motion_confirmed=True,
            )
        )
        await svc.worker
        final = svc.snapshot(task["id"])
        assert final["status"] == "FAILED"
        assert final["station_locked"]
        with pytest.raises(StationBusy):
            await svc.start(request(record))
        assert not svc.release(task["id"])["station_locked"]

    asyncio.run(run())


def enable_fake_live(svc):
    """Enable live orchestration with an injected robot, never the real MQTT adapter."""
    svc.settings.CALIBRATION_LIVE_ENABLED = True
    svc.settings.MQTT_HOST = "not-used.invalid"
    svc.robot_factory = SimRobot


def live_request(record):
    """Supply explicit physical confirmations for a hardware-free live-path test."""
    return request(
        record,
        mode="live",
        ground_clear_confirmed=True,
        robot_at_start_confirmed=True,
        route_safe_confirmed=True,
        loaded_low_safe_confirmed=True,
        live_motion_confirmed=True,
    )


def test_live_manual_pickup_drop_gates(setup):
    """Unverified box operations pause motion until the matching operator gate is acknowledged."""

    async def run():
        svc, record, _, sensor = setup
        enable_fake_live(svc)

        async def fast_wait(seconds, expected, config):
            svc._assert_idle()
            assert target_matches(svc.robot.state, expected, config)

        svc._wait_still = fast_wait
        task = await svc.start(live_request(record))
        for gate in ("CONFIRM_PICKUP", "CONFIRM_DROP"):

            async def wait_gate():
                while svc.current["pending_confirmation"] != gate:
                    assert svc.current["status"] == "RUNNING", svc.current["error"]
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(wait_gate(), 3)
            before = len(svc.store.events(task["id"]))
            await asyncio.sleep(0.1)
            assert len(svc.store.events(task["id"])) == before
            with pytest.raises(ValueError):
                svc.confirm(task["id"], "stale-step")
            svc.confirm(task["id"], gate)
        await svc.worker
        assert svc.current["status"] == "COMPLETED"
        assert sensor.calls == 9
        assert [
            e["data"]["step"]
            for e in svc.store.events(task["id"])
            if e["kind"] == "operator_confirmation"
        ] == ["CONFIRM_PICKUP", "CONFIRM_DROP"]

    asyncio.run(run())


def test_sensor_failure_after_partial_live_run_keeps_lock(setup):
    """A stale measurement cannot reuse prior data or silently continue to take the box."""

    async def run():
        svc, record, _, sensor = setup
        enable_fake_live(svc)

        async def fail_at_second_sample(seconds, expected, config):
            if svc.current["measurements"]:
                sensor.failure = "采样过期"

        svc._wait_still = fail_at_second_sample
        task = await svc.start(live_request(record))
        await svc.worker
        assert svc.current["status"] == "FAILED"
        assert tuple(svc.current["measurements"]) == ("ALN",)
        assert svc.store.owner() == task["id"]
        assert all(
            e["data"].get("step") != "TO_BIN" for e in svc.store.events(task["id"])
        )

    asyncio.run(run())


def test_missing_load_feedback_rejects_empty_phase(setup):
    """Configured load evidence is mandatory instead of silently falling back to manual."""

    async def run():
        svc, _, config, _ = setup
        enable_fake_live(svc)
        data = config.model_dump()
        data["load_feedback_field"] = "sensorStatus.loadSensor"
        record = svc.store.save_config(data)
        await svc.start(live_request(record))
        await svc.worker
        assert svc.current["status"] == "FAILED"
        assert "载荷反馈" in svc.current["error"]
        assert not svc.current["measurements"]

    asyncio.run(run())


def test_cancel_at_confirmation_keeps_live_station_locked(setup):
    """Cancelling a live manual gate cannot dispatch the return motion."""

    async def run():
        svc, record, _, _ = setup
        enable_fake_live(svc)

        async def fast_wait(*args):
            return None

        svc._wait_still = fast_wait
        task = await svc.start(live_request(record))

        async def wait_pickup():
            while svc.current["pending_confirmation"] != "CONFIRM_PICKUP":
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait_pickup(), 3)
        final = await svc.cancel(task["id"])
        assert final["status"] == "CANCELLED"
        assert final["station_locked"]
        assert tuple(final["measurements"]) == ("ALN", "AHN", "BHN", "BLN")

    asyncio.run(run())


def test_confirmation_timeout_keeps_live_station_locked(setup):
    """An unattended pickup gate times out without treating the load as confirmed."""

    async def run():
        svc, _, config, _ = setup
        enable_fake_live(svc)
        config.confirmation_timeout_seconds = 5
        record = svc.store.save_config(config.model_dump())

        async def fast_wait(*args):
            return None

        svc._wait_still = fast_wait
        task = await svc.start(live_request(record))
        await asyncio.wait_for(svc.worker, 8)
        final = svc.snapshot(task["id"])
        assert final["status"] == "FAILED"
        assert "确认超时" in final["error"]
        assert final["station_locked"]
        assert tuple(final["measurements"]) == ("ALN", "AHN", "BHN", "BLN")

    asyncio.run(run())


def test_unknown_pose_can_initialize_without_fabricating_coordinates():
    robot, _ = live_adapter()
    robot.ingest("robot/state/ANT", {"mainState": "UNKNOWN", "liftHeight": ""})
    assert robot.error is None
    assert robot.state["mainState"] == "UNKNOWN"
    body = build_command("ANT", "INIT", theoretical_pose(robot.config, 0), robot.config)
    assert body["robotCommands"][0]["commandContent"] == {"robotCommandType": "INIT"}
    robot.ingest("robot/state/ANT", {"mainState": "IDLE", "liftHeight": ""})
    assert robot.error is not None


def test_config_validation_and_snapshot_immutability(setup):
    svc, record, config, _ = setup
    assert config.velocity == 100
    assert config.acceleration == 500
    for changes in (
        {"high_height_mm": 0},
        {"settle_seconds": 0},
        {"velocity": 1001},
        {"acceleration": 501},
        {"max_spread_mm": float("nan")},
    ):
        with pytest.raises(ValidationError):
            StationConfig.model_validate({**config.model_dump(), **changes})
    StationConfig.model_validate({**config.model_dump(), "velocity": 1000, "acceleration": 500})
    data = config.model_dump()
    data["bin"]["y"] = 100
    with pytest.raises(ValidationError, match="同一直线"):
        StationConfig.model_validate(data)
    data = config.model_dump()
    data["name"] = "new version"
    svc.store.save_config(data)
    assert svc.store.config(record["id"])["config"]["name"] != "new version"


def test_store_restart_and_process_lock(tmp_path):
    """A second process cannot take ownership; interrupted live runs stay locked."""
    path = str(tmp_path / "restart.sqlite3")
    first = Store(path)
    task = {"id": "job", "status": "RUNNING", "mode": "live"}
    first.create_task(task)
    with pytest.raises(RuntimeError, match="一个进程"):
        Store(path)
    first.close()
    second = Store(path)
    assert second.task("job")["status"] == "INTERRUPTED"
    assert second.owner() == "job"
    second.close()


def live_adapter(config=None):
    """Prepare the production correlation logic with no socket or broker."""
    config = config or demo_config()
    robot = LiveRobot("ANT", config, None, lambda *args: None)
    robot.connected = True
    state = {
        "mainState": "IDLE",
        "coordX": config.start.x,
        "coordY": config.start.y,
        "orientation": round(config.start.orientation * 100),
        "liftHeight": config.low_height_mm,
        "qrCodeStatus": True,
    }
    robot.ingest("robot/state/ANT", state)
    robot.seed_theoretical_from_start()
    return robot, state


def test_wire_units_and_obstacle_avoidance():
    config = demo_config()
    robot, state = live_adapter(config)
    expected = start_theoretical_pose(config, state["liftHeight"])
    body = build_command("ANT", "SPIN", expected, config, orientation=18000)
    command = body["robotCommands"][0]
    assert command["commandContent"]["orientation"] == 18000
    assert command["commandContent"]["obstacleAvoidance"] is True
    assert command["expectedState"]["coordX"] == expected["coordX"]
    assert command["futureState"]["orientation"] == 18000
    assert target_matches({"orientation": 35990}, {"orientation": 0}, config)


def test_build_command_chains_theoretical_states():
    config = demo_config()
    start = start_theoretical_pose(config, 0)
    spin = build_command("ANT", "SPIN", start, config, orientation=9000)
    after_spin = pose_from_command_state(spin["robotCommands"][0]["futureState"])
    assert after_spin["orientation"] == 9000
    assert after_spin["coordX"] == start["coordX"]
    move = build_command(
        "ANT",
        "MOVE",
        after_spin,
        config,
        coordX=147250,
        coordY=145015,
        orientation=9000,
    )
    command = move["robotCommands"][0]
    assert pose_from_command_state(command["expectedState"]) == after_spin
    assert pose_from_command_state(command["futureState"]) == {
        "coordX": 147250,
        "coordY": 145015,
        "orientation": 9000,
        "liftHeight": 0,
    }
    assert command["commandContent"]["orientation"] == 9000


def test_first_motion_command_uses_start_theoretical_expected_state():
    config = demo_config()
    robot, state = live_adapter(config)
    # Live telemetry differs from theoretical start; envelopes must ignore it.
    robot.ingest(
        "robot/state/ANT",
        {
            **state,
            "coordX": state["coordX"] + 27,
            "coordY": state["coordY"] - 19,
            "orientation": state["orientation"] + 54,
            "liftHeight": state["liftHeight"] + 12,
        },
    )
    robot.seed_theoretical_from_start()
    start = start_theoretical_pose(config)
    assert robot.theoretical_state == start
    lift = build_command(
        "ANT",
        "LIFT",
        robot.theoretical_state,
        config,
        liftHeight=config.high_height_mm,
    )["robotCommands"][0]
    assert pose_from_command_state(lift["expectedState"]) == start
    assert pose_from_command_state(lift["futureState"]) == {
        **start,
        "liftHeight": config.high_height_mm,
    }
    assert lift["commandContent"]["coordX"] == start["coordX"]
    assert lift["commandContent"]["orientation"] == start["orientation"]
    spin = build_command(
        "ANT",
        "SPIN",
        pose_from_command_state(lift["futureState"]),
        config,
        orientation=18000,
    )["robotCommands"][0]
    assert pose_from_command_state(spin["expectedState"]) == pose_from_command_state(
        lift["futureState"]
    )
    assert spin["commandContent"]["coordX"] == start["coordX"]
    assert spin["commandContent"]["liftHeight"] == config.high_height_mm
    assert spin["futureState"]["orientation"] == 18000


def test_simulation_commands_chain_theoretical_envelopes(setup):
    """Every MOVE/SPIN/LIFT expectedState is the prior futureState, never live pose."""

    async def run():
        svc, record, config, _ = setup
        started = await svc.start(request(record))
        await svc.worker
        task = svc.snapshot(started["id"])
        assert task["status"] == "COMPLETED", task["error"]
        commands = [
            e["data"]["payload"]["robotCommands"][0]
            for e in svc.store.events(task["id"])
            if e["kind"] == "command_sent"
        ]
        motion = [
            c
            for c in commands
            if c["commandContent"]["robotCommandType"] in ("MOVE", "SPIN", "LIFT")
        ]
        assert pose_from_command_state(motion[0]["expectedState"]) == start_theoretical_pose(
            config
        )
        previous = pose_from_command_state(motion[0]["futureState"])
        for command in motion[1:]:
            assert pose_from_command_state(command["expectedState"]) == previous
            previous = pose_from_command_state(command["futureState"])
            content = command["commandContent"]
            assert content["coordX"] == previous["coordX"]
            assert content["coordY"] == previous["coordY"]
            assert content["orientation"] == previous["orientation"]
            assert content["liftHeight"] == previous["liftHeight"]

    asyncio.run(run())


def test_robot_diagnostics_matches_idle_gate():
    idle = {
        "mainState": "IDLE",
        "velocity": 0,
        "angularVelocity": 12,
    }
    moving = {**idle, "velocity": 12}
    string_zero = {**idle, "velocity": "0"}
    missing_velocity = {"mainState": "IDLE"}
    assert CalibrationService.is_still(idle) is True
    assert CalibrationService.is_still(string_zero) is True
    assert CalibrationService.is_still(missing_velocity) is True
    assert CalibrationService.robot_diagnostics(idle)["stationary"] is True
    diag = CalibrationService.robot_diagnostics(moving)
    assert diag["stationary"] is False
    assert diag["blockers"][0]["field"] == "velocity"


def test_target_matches_position_and_orientation_tolerance():
    config = demo_config()
    assert config.position_tolerance_mm == 50
    assert config.orientation_tolerance_deg == 5
    assert target_matches(
        {"coordX": 1050, "coordY": -40, "orientation": 9040},
        {"coordX": 1000, "coordY": 0, "orientation": 9000},
        config,
    )
    # Live telemetry keeps millimetre floats and centidegree heading.
    assert target_matches(
        {"coordX": 147250.000, "coordY": 141015.000, "orientation": 9191},
        {"coordX": 147250, "coordY": 141015, "orientation": 9000},
        config,
    )
    assert not target_matches(
        {"coordX": 1050.001, "coordY": 0, "orientation": 9000},
        {"coordX": 1000, "coordY": 0, "orientation": 9000},
        config,
    )
    assert not target_matches(
        {"coordX": 1000, "coordY": 0, "orientation": 8490},
        {"coordX": 1000, "coordY": 0, "orientation": 9000},
        config,
    )


def test_live_command_requires_correlated_success_and_post_success_state():
    """Stale IDLE, foreign results and duplicate success cannot complete the command."""

    async def run():
        robot, state = live_adapter()
        published = []
        robot.client = SimpleNamespace(
            publish=lambda *args, **kwargs: published.append(args)
            or SimpleNamespace(rc=0)
        )
        pending = asyncio.create_task(robot.command("LIFT", liftHeight=100))
        await asyncio.sleep(0.01)
        label = json.loads(published[0][1])["robotCommands"][0]["robotCommandLabel"]
        robot.ingest(
            "robot/command/status/ANT",
            {"robotCommandLabel": "other", "status": "COMPLETE_SUCCESS"},
        )
        robot.ingest("robot/state/ANT", {**state, "liftHeight": 100})
        await asyncio.sleep(0.06)
        assert not pending.done()
        robot.ingest(
            "robot/command/status/ANT",
            {"robotCommandLabel": label, "status": "COMPLETE_SUCCESS"},
        )
        await asyncio.sleep(0.06)
        assert not pending.done()
        robot.ingest("robot/state/ANT", {**state, "liftHeight": 100})
        await asyncio.wait_for(pending, 1)

    asyncio.run(run())


@pytest.mark.parametrize("failure", [None, "connect", "subscribe"])
def test_production_mqtt_callbacks_without_network(monkeypatch, failure):
    """Drive real connect/SUBACK/message callbacks through an injected Paho transport."""
    import paho.mqtt.client as mqtt

    clients = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.options = kwargs
            self.sent = []
            clients.append(self)

        def username_pw_set(self, username, password):
            self.credentials = (username, password)

        def tls_set(self, **kwargs):
            self.tls = kwargs

        def connect_async(self, host, port, keepalive):
            self.destination = (host, port)

        def loop_start(self):
            self.on_connect(
                self, None, None, SimpleNamespace(is_failure=failure == "connect"), None
            )
            self.deliver("robot/state/ANT", {"mainState": "UNKNOWN", "liftHeight": ""})

        def subscribe(self, topics):
            self.topics = topics
            self.on_subscribe(
                self,
                None,
                1,
                [SimpleNamespace(is_failure=failure == "subscribe")],
                None,
            )
            return 0, 1

        def deliver(self, topic, payload, retain=False):
            self.on_message(
                self,
                None,
                SimpleNamespace(
                    topic=topic, payload=json.dumps(payload).encode(), retain=retain
                ),
            )

        def publish(self, topic, text, **kwargs):
            self.sent.append((topic, json.loads(text), kwargs))
            command = json.loads(text)["robotCommands"][0]
            label = command["robotCommandLabel"]
            content = command["commandContent"]
            if content["robotCommandType"] == "INIT":
                state = {"mainState": "LOCATION_UNKNOWN"}
            else:
                state = {
                    "mainState": "IDLE",
                    "coordX": 0,
                    "coordY": 0,
                    "orientation": 0,
                    "liftHeight": 0,
                }
            self.deliver(
                "robot/command/status/ANT",
                {"robotCommandLabel": label, "status": "COMPLETE_SUCCESS"},
            )
            asyncio.get_running_loop().call_later(
                0.01, self.deliver, "robot/state/ANT", state
            )
            return SimpleNamespace(rc=0)

        def disconnect(self):
            self.on_disconnect(self, None, None, None, None)

        def loop_stop(self):
            return None

    monkeypatch.setattr(mqtt, "Client", FakeClient)

    async def run():
        settings = Settings(
            _env_file=None,
            MQTT_HOST="fake.invalid",
            MQTT_PORT=8883,
            MQTT_TLS=True,
            MQTT_CA_FILE=None,
            MQTT_USERNAME="fake-user",
            MQTT_PASSWORD="fake-pass",
        )
        robot = LiveRobot("ANT", demo_config(), settings, lambda *args: None)
        if failure:
            with pytest.raises(RuntimeError):
                await robot.start()
            await robot.close()
            return
        await robot.start()
        client = clients[0]
        assert client.destination == ("fake.invalid", 8883)
        assert client.options["reconnect_on_failure"] is False
        assert client.topics == [
            ("robot/state/ANT", 1),
            ("robot/command/status/ANT", 2),
        ]
        assert client.tls == {"ca_certs": None}
        client.deliver("robot/state/ANT", {"mainState": "FAULT"}, retain=True)
        await asyncio.sleep(0)
        assert robot.state["mainState"] == "UNKNOWN"
        await robot.command("INIT")
        assert robot.state["mainState"] == "LOCATION_UNKNOWN"
        await robot.command("HOME_SET_ORIGIN")
        assert robot.state["mainState"] == "IDLE"
        assert client.sent[0][2] == {"qos": 2, "retain": False}
        await robot.close()
        assert not robot.connected
        assert robot.error is None

    asyncio.run(run())


@pytest.mark.parametrize(
    "failure", ["command", "disconnect", "stale", "timeout", "wrong_target"]
)
def test_live_failures_do_not_complete(failure):
    async def run():
        config = demo_config().model_copy(update={"command_timeout_seconds": 0.15})
        robot, state = live_adapter(config)
        robot.client = SimpleNamespace(
            publish=lambda *args, **kwargs: SimpleNamespace(rc=0)
        )
        pending = asyncio.create_task(robot.command("LIFT", liftHeight=100))
        await asyncio.sleep(0.01)
        if failure == "command":
            robot.ingest(
                "robot/command/status/ANT",
                {"robotCommandLabel": robot.pending, "status": "COMPLETE_FAILURE"},
            )
        elif failure == "disconnect":
            robot._network_error("断线")
        elif failure == "stale":
            robot.received_at -= 100
        elif failure == "wrong_target":
            robot.ingest(
                "robot/command/status/ANT",
                {"robotCommandLabel": robot.pending, "status": "COMPLETE_SUCCESS"},
            )
            robot.ingest("robot/state/ANT", state)
        with pytest.raises((RuntimeError, TimeoutError)):
            await pending

    asyncio.run(run())


def make_sensor(samples, failures=0):
    """Build an isolated sensor HAL without initializing I2C or its global singleton."""
    sensor = object.__new__(SensorService)
    sensor.__init__()
    sensor._ads_online = True
    state = _ChannelState(1, 100)
    state.samples = deque(samples, maxlen=100)
    state.consecutive_failures = failures
    sensor._states = {1: state}
    return sensor


def test_default_sampling_and_calibration_use_a1(monkeypatch):
    """Actual wiring and API index both identify A1; A0 is unused by default."""
    monkeypatch.delenv("ADS_CHANNELS", raising=False)
    settings = Settings(_env_file=None)
    assert settings.ADS_CHANNELS == [1]
    assert demo_config().sensor_channel == 1
    assert demo_config().max_spread_mm == 5
    monkeypatch.setattr("app.sensor.settings", settings)
    sensor = SensorService()
    assert set(sensor._states) == {1}


@pytest.mark.parametrize("channel", [0, 2, 3])
def test_legacy_other_channel_recipe_rejected_before_start(setup, channel):
    """Preserve old recipes but reject wrong-channel starts before reading or motion."""
    async def run():
        svc, _, config, sensor = setup
        legacy = config.model_dump()
        legacy["sensor_channel"] = channel
        record = svc.store.save_config(legacy)
        with pytest.raises(ValidationError, match="sensor_channel"):
            await svc.start(request(record))
        assert sensor.calls == 0
        assert svc.worker is None
        assert svc.store.owner() is None
        assert svc.store.config(record["id"])["config"]["sensor_channel"] == channel

    asyncio.run(run())


def test_reading_uses_a1_when_a0_is_unstable():
    """Unused A0 noise must never enter the A1 baseline or measurement window."""
    now = time.monotonic()
    samples = [(now - 0.01 * i, 1.001) for i in range(5, 0, -1)]
    sensor = make_sensor(samples)
    unused = _ChannelState(0, 100)
    unused.samples = deque([(now - 0.01 * i, float(i)) for i in range(5, 0, -1)])
    sensor._states[0] = unused
    svc = CalibrationService(None, sensor, Settings(_env_file=None))
    reading = svc._reading(demo_config(), now - 0.1)
    assert reading["channel"] == 1
    assert reading["spread_mm"] == 0


def test_missing_a1_does_not_fall_back_to_a0():
    """A healthy A0 must not substitute for a missing wired A1 sensor."""
    now = time.monotonic()
    sensor = make_sensor([(now - 0.01 * i, 1.001) for i in range(5, 0, -1)])
    state = sensor._states.pop(1)
    sensor._states[0] = state
    svc = CalibrationService(None, sensor, Settings(_env_file=None))
    with pytest.raises(ValueError, match="通道未配置"):
        svc._reading(demo_config(), now - 0.1)


@pytest.mark.parametrize("spread", [3.632, 5.0, 5.001, 5.409])
def test_window_spread_is_reported_but_not_rejected(monkeypatch, spread):
    """Window spread is recorded for diagnostics and does not fail the reading."""
    now = time.monotonic()
    monkeypatch.setattr("app.sensor.voltage_to_distance", lambda v: (v, "Normal"))
    samples = [(now - 0.01 * i, 1.0) for i in range(5, 0, -1)]
    samples[-1] = (now - 0.01, 1.0 + spread)
    svc = CalibrationService(None, make_sensor(samples), Settings(_env_file=None))
    reading = svc._reading(demo_config(), now - 0.1)
    assert reading["spread_mm"] == pytest.approx(spread)
    assert reading["voltages"][-1] == 1.0 + spread


def test_precision_sensor_filters_out_pre_settle_samples():
    now = time.monotonic()
    samples = [(now - 0.4, 9)] + [(now - 0.01 * i, 1.001) for i in range(5, 0, -1)]
    reading = make_sensor(samples).calibration_reading(1, now - 0.1, 0.5, 5, 0.2, 3)
    assert reading["distance_mm"] == pytest.approx(295.245)
    assert reading["samples_in_window"] == 5
    assert reading["voltages"] == [1.001] * 5


def test_large_spread_does_not_log_unstable_diagnostics(monkeypatch, caplog):
    """A large window spread must still return a reading without a failure dump."""
    now = time.monotonic()
    values = [1.0 + (i % 3) * 0.1 for i in range(24)] + [4.632]
    selected = [(now - (25 - i) * 0.01, v) for i, v in enumerate(values)]
    sensor = make_sensor([(now - 0.8, 999.0)] + selected)
    monkeypatch.setattr("app.sensor.voltage_to_distance", lambda v: (v, "Normal"))
    with caplog.at_level("WARNING", logger="app.sensor"):
        reading = sensor.calibration_reading(1, now - 0.5, 0.5, 5, 0.2, 3)
    assert reading["spread_mm"] == pytest.approx(3.632)
    assert not any("CALIBRATION_UNSTABLE_WINDOW" in r.message for r in caplog.records)


def test_stable_window_does_not_log_unstable_diagnostics(caplog):
    """A passing calibration window must not produce a failure dump."""
    now = time.monotonic()
    sensor = make_sensor([(now - 0.01 * i, 1.001) for i in range(5, 0, -1)])
    with caplog.at_level("WARNING", logger="app.sensor"):
        sensor.calibration_reading(1, now - 0.1, 0.5, 5, 0.2, 3)
    assert not any("CALIBRATION_UNSTABLE_WINDOW" in r.message for r in caplog.records)


@pytest.mark.parametrize(
    "kind", ["stale", "insufficient", "out_of_range", "failure"]
)
def test_sensor_rejects_bad_windows(kind):
    now = time.monotonic()
    samples = [(now - 0.01 * i, 1.0) for i in range(5, 0, -1)]
    if kind == "stale":
        samples = [(now - 0.4, 1.0)] * 5
    if kind == "insufficient":
        samples = samples[:2]
    if kind == "out_of_range":
        samples[-1] = (now, 11)
    sensor = make_sensor(samples, failures=1 if kind == "failure" else 0)
    with pytest.raises(ValueError):
        sensor.calibration_reading(1, now - 1, 0.5, 5, 0.2, 3)


def test_api_without_token_and_websocket_without_handshake(tmp_path):
    @asynccontextmanager
    async def lifespan(app):
        store = Store(str(tmp_path / "api.sqlite3"))
        settings = Settings(
            _env_file=None,
            CALIBRATION_LIVE_ENABLED=False,
            MQTT_HOST="",
        )
        app.state.calibration = CalibrationService(store, FakeSensor(), settings)
        app.state.record = store.save_config(demo_config().model_dump())
        try:
            yield
        finally:
            await app.state.calibration.close()
            store.close()

    app = FastAPI(lifespan=lifespan)
    app.include_router(router)
    app.include_router(ws_router)
    with TestClient(app) as client:
        record = app.state.record
        assert client.get("/api/v1/calibration/configs").status_code == 200
        assert not client.get("/api/v1/calibration/system").json()["live_enabled"]
        app.state.calibration.settings.CALIBRATION_LIVE_ENABLED = True
        app.state.calibration.settings.MQTT_HOST = "not-used.invalid"
        assert client.get("/api/v1/calibration/system").json()["live_enabled"]
        assert (
            client.post(
                "/api/v1/calibration/tasks",
                headers={"Origin": "http://evil.invalid"},
                json=request(record).model_dump(),
            ).status_code
            == 403
        )
        response = client.post(
            "/api/v1/calibration/tasks",
            json=request(record).model_dump(),
        )
        assert response.status_code == 201, response.text
        task_id = response.json()["id"]
        with client.websocket_connect(f"/ws/calibration/{task_id}") as socket:
            while True:
                item = socket.receive_json()
                if item["task"]["status"] == "COMPLETED":
                    break
        result = client.get(f"/api/v1/calibration/tasks/{task_id}/result").json()
        assert len(result["measurements"]) == 8
        csv = client.get(f"/api/v1/calibration/tasks/{task_id}/export")
        assert "simulation,COMPLETED" in csv.text
        deleted = client.delete(f"/api/v1/calibration/configs/{record['id']}")
        assert deleted.status_code == 204
        assert client.get("/api/v1/calibration/configs").json() == []
        assert client.get(f"/api/v1/calibration/tasks/{task_id}").json()["config"] == record["config"]
        assert client.delete(f"/api/v1/calibration/configs/{record['id']}").status_code == 404
        assert "ALN" in csv.text and "BHY" in csv.text
        assert client.get("/api/v1/calibration/tasks/missing").status_code == 404
        assert (
            client.post(
                f"/api/v1/calibration/tasks/{task_id}/confirm",
                json={"step": "CONFIRM_PICKUP", "confirmed": True},
            ).status_code
            == 422
        )
        assert (
            client.post(
                f"/api/v1/calibration/tasks/{task_id}/release",
                json={"robot_stopped_and_station_safe": False},
            ).status_code
            == 422
        )
        assert client.delete(f"/api/v1/calibration/tasks/{task_id}").status_code == 204
        assert client.get(f"/api/v1/calibration/tasks/{task_id}").status_code == 404
        assert client.delete("/api/v1/calibration/tasks").json() == {"deleted": 0}
