import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from openpilot.common.conversions import Conversions as CV
from openpilot.common.params import Params
from openpilot.frogpilot.common.vision_speed_limit import Detection, HEARTBEAT_TIMEOUT, SpeedLimitConfirmation, VISION_SPEED_LIMIT_PARAM
from openpilot.frogpilot.selfdrive.controls.lib import speed_limit_controller as slc_module


@pytest.fixture
def controller(tmp_path, monkeypatch):
  persistent = Params(str(tmp_path / "persistent"))
  memory = Params(str(tmp_path / "memory"))
  monkeypatch.setattr(slc_module, "params", persistent)
  monkeypatch.setattr(slc_module, "params_memory", memory)
  monkeypatch.setattr(slc_module.time, "monotonic", lambda: 100.0)
  slc = slc_module.SpeedLimitController()
  slc.frogpilot_toggles = SimpleNamespace(
    speed_limit_controller=True, vision_speed_limit_detection=True, is_metric=False,
    speed_limit_priority1="Vision", speed_limit_priority2="Navigation", speed_limit_priority3="Dashboard",
    speed_limit_priority_highest=False, speed_limit_priority_lowest=False,
    speed_limit_confirmation_higher=False, speed_limit_confirmation_lower=False,
    slc_fallback_previous_speed_limit=True, slc_mapbox_filler=False, speed_limit_filler=False,
    map_speed_lookahead_higher=0, map_speed_lookahead_lower=0,
    speed_limit_controller_override_manual=True, speed_limit_controller_override_set_speed=False,
    **{f"speed_limit_offset{i}": float(i) for i in range(1, 8)},
  )
  state = SpeedLimitConfirmation()
  state.update(Detection(45, 0.95), 99.0, 99.0)
  state.update(Detection(45, 0.95), 99.2, 99.2)
  memory.put(VISION_SPEED_LIMIT_PARAM, json.dumps(state.snapshot(100.0)))
  yield slc
  slc.close()


def messages(navigation=35, dashboard=30, gas=False, enabled=True):
  return {
    "frogpilotCarState": SimpleNamespace(dashboardSpeedLimit=dashboard * CV.MPH_TO_MS, accelPressed=False, decelPressed=False),
    "frogpilotNavigation": SimpleNamespace(navigationSpeedLimit=navigation * CV.MPH_TO_MS),
    "carControl": SimpleNamespace(longActive=True),
    "controlsState": SimpleNamespace(enabled=enabled),
    "carState": SimpleNamespace(gasPressed=gas),
  }


def update(slc, **kwargs):
  slc.update_limits({}, datetime(2026, 9, 20, tzinfo=UTC), False, 20, messages(**kwargs))


def test_native_params_json_reaches_controller_without_persisting_vision(controller):
  assert isinstance(slc_module.params_memory.get(VISION_SPEED_LIMIT_PARAM), bytes)
  update(controller)
  assert controller.source == "Vision"
  assert controller.target == pytest.approx(45 * CV.MPH_TO_MS)
  assert controller.offset == 4
  assert slc_module.params.get_float("PreviousSpeedLimit") == 0


@pytest.mark.parametrize("slot", [1, 2, 3])
def test_vision_priority_slots(controller, slot):
  for index in (1, 2, 3):
    setattr(controller.frogpilot_toggles, f"speed_limit_priority{index}", "Vision" if index == slot else "None")
  update(controller)
  assert controller.source == "Vision"


def test_existing_navigation_source_is_preserved(controller):
  controller.frogpilot_toggles.vision_speed_limit_detection = False
  update(controller)
  assert controller.source == "Navigation"
  assert controller.target == pytest.approx(35 * CV.MPH_TO_MS)


@pytest.mark.parametrize("priority, expected", [("highest", "Vision"), ("lowest", "Dashboard")])
def test_highest_lowest_include_vision(controller, priority, expected):
  setattr(controller.frogpilot_toggles, f"speed_limit_priority_{priority}", True)
  update(controller)
  assert controller.source == expected


@pytest.mark.parametrize("snapshot", [b"{", b"null", b"[]", b"\xff", b'{"speedLimit": 20}'])
def test_invalid_snapshot_falls_back_to_navigation(controller, snapshot):
  slc_module.params_memory.put(VISION_SPEED_LIMIT_PARAM, snapshot)
  update(controller)
  assert controller.source == "Navigation"


def test_stale_camera_clears_vision_and_previous_fallback(controller, monkeypatch):
  update(controller, navigation=0, dashboard=0)
  controller.overridden_speed = 30
  monkeypatch.setattr(slc_module.time, "monotonic", lambda: 103.0)
  update(controller, navigation=0, dashboard=0)
  assert controller.target == 0
  assert controller.overridden_speed == 0
  assert controller.previous_target == 0
  assert controller.source == "None"
  update(controller, navigation=0, dashboard=0)
  assert controller.target == 0


def test_pending_vision_approval_clears_on_source_loss(controller):
  controller.target = 30 * CV.MPH_TO_MS
  controller.source = "Dashboard"
  controller.frogpilot_toggles.speed_limit_confirmation_higher = True
  update(controller)
  assert controller.confirmation_source == "Vision"
  assert controller.unconfirmed_speed_limit > 0
  slc_module.params_memory.put_bool("SpeedLimitAccepted", True)
  slc_module.params_memory.remove(VISION_SPEED_LIMIT_PARAM)
  update(controller, navigation=0)
  assert controller.confirmation_source == "None"
  assert controller.unconfirmed_speed_limit == 0
  assert not slc_module.params_memory.get_bool("SpeedLimitAccepted")


def test_gas_override_and_disengagement(controller):
  update(controller)
  controller.update_override(35, 30, messages(gas=True))
  assert controller.overridden_speed == 30
  controller.update_override(35, 30, messages(enabled=False))
  assert controller.overridden_speed == 0


def test_metric_display_preserves_mph_sign_units(controller):
  controller.frogpilot_toggles.is_metric = True
  update(controller)
  assert controller.target * CV.MS_TO_KPH == pytest.approx(72.42048)


def test_real_camera_models_and_params_reach_controller(controller, mocker):
  from pathlib import Path

  import cv2
  import numpy as np
  from cereal import messaging
  from msgq.visionipc import VisionIpcClient, VisionIpcServer, VisionStreamType
  from openpilot.frogpilot.system.speed_limit_vision import RUNTIME_LOOP_HZ, SpeedLimitVisionDaemon

  params = slc_module.params_memory
  services = ["deviceState", "frogpilotCarState"]
  pm = messaging.PubMaster(services)
  sm = messaging.SubMaster(services, frequency=RUNTIME_LOOP_HZ)
  daemon = SpeedLimitVisionDaemon(params, sm, VisionIpcClient, VisionStreamType, mocker.Mock())
  clock = mocker.patch("time.monotonic", return_value=100.0)

  folder = Path(__file__).parents[3] / "system" / "tests" / "fixtures" / "vision_speed_limit"
  frames = []
  for number in (140, 146):
    frame = cv2.imread(str(folder / f"sign_20_frame_{number}.png"))
    height, width = frame.shape[:2]
    # Exercise the actual padded NV12 layout delivered through VisionIPC.
    stride = width + 16
    uv_offset = stride * (height + 8)
    buffer = np.zeros(uv_offset + height // 2 * stride, dtype=np.uint8)
    planar = cv2.cvtColor(frame, cv2.COLOR_BGR2YUV_I420).ravel()
    y_size = height * width
    buffer[:height * stride].reshape(height, stride)[:, :width] = planar[:y_size].reshape(height, width)
    chroma = buffer[uv_offset:].reshape(height // 2, stride)[:, :width]
    chroma[:, 0::2] = planar[y_size:y_size * 5 // 4].reshape(height // 2, width // 2)
    chroma[:, 1::2] = planar[y_size * 5 // 4:].reshape(height // 2, width // 2)
    frames.append(buffer)

  stream = VisionStreamType.VISION_STREAM_ROAD
  server = VisionIpcServer("camerad")
  server.create_buffers_with_sizes(stream, 4, False, width, height, len(frames[0]), stride, uv_offset)
  server.start_listener()
  try:
    assert stream in VisionIpcClient.available_streams("camerad", block=True)
    for tick in range(RUNTIME_LOOP_HZ + 1):
      now = 100 + tick / RUNTIME_LOOP_HZ
      clock.return_value = now
      car = messaging.new_message("frogpilotCarState", valid=True)
      car.logMonoTime = int(now * 1e9)
      car.frogpilotCarState.drivingGear = True
      pm.send("frogpilotCarState", car)
      if tick % (RUNTIME_LOOP_HZ // 2) == 0:
        device = messaging.new_message("deviceState", valid=True)
        device.logMonoTime = int(now * 1e9)
        device.deviceState.started = True
        device.deviceState.memoryUsagePercent = 30
        device.deviceState.cpuUsagePercent = [10] * 8
        pm.send("deviceState", device)
      if tick <= RUNTIME_LOOP_HZ // 2:
        sm.update(0)
        if tick < RUNTIME_LOOP_HZ // 2:
          continue
        assert sm.all_checks(["deviceState", "frogpilotCarState"])
        assert daemon.connect_camera()
      server.send(stream, frames[tick % 2], frame_id=tick, timestamp_eof=int(now * 1e9))
      daemon.step(now)

    assert sm.all_checks(["deviceState", "frogpilotCarState"])
    assert json.loads(params.get(VISION_SPEED_LIMIT_PARAM))["speedLimit"] == pytest.approx(20 * CV.MPH_TO_MS)
    update(controller)
    assert controller.source == "Vision"
    assert controller.target == pytest.approx(20 * CV.MPH_TO_MS)

    clock.return_value += HEARTBEAT_TIMEOUT + 1
    update(controller, dashboard=0, navigation=0)
    assert controller.target == 0
  finally:
    daemon.clear("Stopped", disconnect=True)
    del server


def test_native_params_drive_transition(tmp_path):
  from openpilot.common.params import ParamKeyType

  memory = Params(str(tmp_path))
  snapshot = {"speedLimit": 20.0, "confidence": 0.95, "detectedAt": 99.0, "timestamp": 100.0}
  memory.put(VISION_SPEED_LIMIT_PARAM, json.dumps(snapshot))
  assert json.loads(memory.get(VISION_SPEED_LIMIT_PARAM)) == snapshot
  memory.clear_all(ParamKeyType.CLEAR_ON_OFFROAD_TRANSITION)
  assert memory.get(VISION_SPEED_LIMIT_PARAM) is None


def test_process_only_runs_onroad_when_enabled():
  from openpilot.system.manager.process_config import managed_processes, run_speed_limit_vision

  toggles = SimpleNamespace(vision_speed_limit_detection=False)
  assert not run_speed_limit_vision(True, None, None, False, False, toggles)
  toggles.vision_speed_limit_detection = True
  assert not run_speed_limit_vision(False, None, None, False, False, toggles)
  assert run_speed_limit_vision(True, None, None, False, False, toggles)
  assert managed_processes["speed_limit_vision"].should_run is run_speed_limit_vision


def test_runtime_subscription_accepts_messages_at_its_loop_rate(mocker):
  from cereal import messaging
  from openpilot.frogpilot.system.speed_limit_vision import RUNTIME_LOOP_HZ, SpeedLimitVisionDaemon, main

  run = mocker.patch.object(SpeedLimitVisionDaemon, "run", autospec=True)
  main()
  sm = run.call_args.args[0].sm
  for tick in range(RUNTIME_LOOP_HZ + 1):
    messages = [messaging.new_message("frogpilotCarState", valid=True).as_reader()]
    if tick % (RUNTIME_LOOP_HZ // 2) == 0:
      messages.append(messaging.new_message("deviceState", valid=True).as_reader())
    sm.update_msgs(100 + tick / RUNTIME_LOOP_HZ, messages)
  assert sm.all_checks(["deviceState", "frogpilotCarState"])


def test_vision_worker_preserves_existing_car_state_readers(mocker):
  from cereal import messaging
  from openpilot.frogpilot.system.speed_limit_vision import SpeedLimitVisionDaemon, main

  # FrogPilot fills all 15 slots. A sixteenth reader evicts existing consumers.
  pm = messaging.PubMaster(["carState"])
  readers = [messaging.sub_sock("carState", conflate=True) for _ in range(15)]
  mocker.patch.object(SpeedLimitVisionDaemon, "run")
  main()
  for timestamp in range(1, 4):
    message = messaging.new_message("carState", valid=True)
    message.logMonoTime = timestamp
    pm.send("carState", message)
    for reader in readers:
      received = messaging.recv_one_or_none(reader)
      assert received is not None and received.logMonoTime == timestamp


@pytest.mark.parametrize("gear", ["drive", "low", "park", "reverse", "neutral", "unknown"])
@pytest.mark.parametrize("can_valid", [True, False])
def test_auxiliary_car_state_preserves_gear_and_validity(mocker, gear, can_valid):
  from cereal import car, custom
  from openpilot.selfdrive.car.card import Car

  publisher = Car.__new__(Car)
  publisher.sm = SimpleNamespace(frame=1, all_checks=lambda _: True)
  publisher.pm = mocker.Mock()
  publisher.rk = SimpleNamespace(remaining=0.0)
  publisher.last_actuators_output = car.CarControl.Actuators.new_message()
  publisher.can_rcv_cum_timeout_counter = 0
  state = car.CarState.new_message(gearShifter=gear, canValid=can_valid)
  publisher.state_publish(state, custom.FrogPilotCarState.new_message().as_reader())
  messages = {call.args[0]: call.args[1] for call in publisher.pm.send.call_args_list}
  assert messages["frogpilotCarState"].valid == messages["carState"].valid == can_valid
  assert messages["frogpilotCarState"].frogpilotCarState.drivingGear == (gear in ("drive", "low"))
