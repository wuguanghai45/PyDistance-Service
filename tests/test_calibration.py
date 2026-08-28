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
from app.calibration_robot import LiveRobot, SimRobot, build_command, target_matches
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
        return CalibrationService._sim_reading(0, 0 if self.calls == 1 else 250)


@pytest.fixture
def setup(tmp_path):
    """Create an isolated station database and disable real MQTT unconditionally."""
    store = Store(str(tmp_path / "cal.sqlite3"))
    config = demo_config()
    record = store.save_config(config.model_dump())
    settings = Settings(
        _env_file=None,
        CALIBRATION_LIVE_ENABLED=False,
        CALIBRATION_API_TOKEN="",
        MQTT_HOST="",
        ROBOT_SN_MAP={},
    )
    sensor = FakeSensor()
    svc = CalibrationService(store, sensor, settings)
    yield svc, record, config, sensor
    store.close()


def request(record, **kwargs):
    """Construct a default safe simulation request."""
    return StartRequest(config_id=record["id"], identity="ANT-TEST", **kwargs)


def test_full_simulation_order_motion_and_precision(setup):
    """Exercise all eight measurements, box legs, return and final station release."""

    async def run():
        svc, record, config, sensor = setup
        started = await svc.start(request(record))
        await svc.worker
        task = svc.snapshot(started["id"])
        assert task["status"] == "COMPLETED", task["error"]
        assert tuple(task["measurements"]) == MEASUREMENT_ORDER
        assert [m["height_mm"] for m in task["measurements"].values()] == [
            250,
            350,
            351,
            251,
            349,
            249,
            248,
            348,
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


def test_live_gates_and_unknown_sn(setup):
    """No baseline or connection occurs with incomplete authorization or guessed SN."""

    async def run():
        svc, record, _, sensor = setup
        with pytest.raises(ValueError, match="未启用"):
            await svc.start(request(record, mode="live"))
        with pytest.raises(ValueError, match="映射"):
            await svc.start(request(record, identity_type="robotSN"))
        assert sensor.calls == 0
        assert svc.store.owner() is None

    asyncio.run(run())


def test_baseline_failure_never_claims_or_moves_station(setup):
    """Invalid ground references abort before MQTT connection or station ownership."""

    async def run():
        svc, record, _, sensor = setup
        svc.settings.CALIBRATION_LIVE_ENABLED = True
        svc.settings.CALIBRATION_API_TOKEN = "test-only"
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
        svc.settings.CALIBRATION_API_TOKEN = "test-only"
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
    svc.settings.CALIBRATION_API_TOKEN = "test-only"
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
    body = build_command("ANT", "INIT", robot.state, robot.config)
    assert body["robotCommands"][0]["commandContent"] == {"robotCommandType": "INIT"}
    robot.ingest("robot/state/ANT", {"mainState": "IDLE", "liftHeight": ""})
    assert robot.error is not None


def test_config_validation_and_snapshot_immutability(setup):
    svc, record, config, _ = setup
    for changes in (
        {"high_height_mm": 0},
        {"settle_seconds": 0},
        {"velocity": 10000},
        {"max_spread_mm": float("nan")},
    ):
        with pytest.raises(ValidationError):
            StationConfig.model_validate({**config.model_dump(), **changes})
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
        "coordX": 1000,
        "coordY": 0,
        "orientation": 0,
        "liftHeight": 0,
        "qrCodeStatus": True,
    }
    robot.ingest("robot/state/ANT", state)
    return robot, state


def test_wire_units_and_obstacle_avoidance():
    config = demo_config()
    robot, state = live_adapter(config)
    body = build_command("ANT", "SPIN", state, config, orientation=18000)
    command = body["robotCommands"][0]
    assert command["commandContent"]["orientation"] == 18000
    assert command["commandContent"]["obstacleAvoidance"] is True
    assert command["expectedState"]["coordX"] == state["coordX"]
    assert target_matches({"orientation": 35990}, {"orientation": 0}, config)


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
    state = _ChannelState(0, 100)
    state.samples = deque(samples, maxlen=100)
    state.consecutive_failures = failures
    sensor._states = {0: state}
    return sensor


def test_precision_sensor_filters_out_pre_settle_samples():
    now = time.monotonic()
    samples = [(now - 0.4, 9)] + [(now - 0.01 * i, 1.001) for i in range(5, 0, -1)]
    reading = make_sensor(samples).calibration_reading(0, now - 0.1, 0.5, 5, 0.2, 3)
    assert reading["distance_mm"] == pytest.approx(295.245)
    assert reading["samples_in_window"] == 5
    assert reading["voltages"] == [1.001] * 5


@pytest.mark.parametrize(
    "kind", ["stale", "insufficient", "out_of_range", "unstable", "failure"]
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
    if kind == "unstable":
        samples[-1] = (now, 2)
    sensor = make_sensor(samples, failures=1 if kind == "failure" else 0)
    with pytest.raises(ValueError):
        sensor.calibration_reading(0, now - 1, 0.5, 5, 0.2, 3)


def test_api_auth_websocket_result_export_and_missing_task(tmp_path):
    @asynccontextmanager
    async def lifespan(app):
        store = Store(str(tmp_path / "api.sqlite3"))
        settings = Settings(
            _env_file=None,
            CALIBRATION_LIVE_ENABLED=False,
            CALIBRATION_API_TOKEN="test-token",
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
        assert client.get("/api/v1/calibration/configs").status_code == 401
        headers = {"Authorization": "Bearer test-token"}
        assert (
            client.get("/api/v1/calibration/configs", headers=headers).status_code
            == 200
        )
        assert (
            client.post(
                "/api/v1/calibration/tasks",
                headers={**headers, "Origin": "http://evil.invalid"},
                json=request(record).model_dump(),
            ).status_code
            == 403
        )
        response = client.post(
            "/api/v1/calibration/tasks",
            headers=headers,
            json=request(record).model_dump(),
        )
        assert response.status_code == 201, response.text
        task_id = response.json()["id"]
        with client.websocket_connect(f"/ws/calibration/{task_id}") as socket:
            socket.send_json({"token": "test-token"})
            while True:
                item = socket.receive_json()
                if item["task"]["status"] == "COMPLETED":
                    break
        result = client.get(
            f"/api/v1/calibration/tasks/{task_id}/result", headers=headers
        ).json()
        assert len(result["measurements"]) == 8
        csv = client.get(f"/api/v1/calibration/tasks/{task_id}/export", headers=headers)
        assert "simulation,COMPLETED" in csv.text
        assert "ALN" in csv.text and "BHY" in csv.text
        assert (
            client.get("/api/v1/calibration/tasks/missing", headers=headers).status_code
            == 404
        )
        assert (
            client.post(
                f"/api/v1/calibration/tasks/{task_id}/confirm",
                headers=headers,
                json={"step": "CONFIRM_PICKUP", "confirmed": True},
            ).status_code
            == 422
        )
        assert (
            client.post(
                f"/api/v1/calibration/tasks/{task_id}/release",
                headers=headers,
                json={"robot_stopped_and_station_safe": False},
            ).status_code
            == 422
        )
