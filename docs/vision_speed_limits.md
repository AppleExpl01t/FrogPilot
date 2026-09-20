# Vision speed limits on the current release source

This branch ports VSL onto source revision `d21d9d277`, the source parent of the September 19, 2026 compiled release used by the fork's `master`. It preserves that source generation's Navigation speed-limit input, three source-priority slots, Mapbox fallback, model selection and UI layout. It does not replace `master` with the unrelated VSL-Port history.

The detector and number classifier originate from [firestar5683's StarPilot Dom branch](https://github.com/firestar5683/StarPilot/tree/b990a776b2fceefaca4d678cf87067874f4b1672). Model provenance, hashes, preprocessing, StarPilot attribution and retained license notices are in [the model README](../frogpilot/selfdrive/assets/vision_models/README.md). The heading recognizer derives from PaddlePaddle's English PP-OCRv5 model. The StarPilot weights declare AGPL-3.0; retaining notices does not resolve their corresponding-source or distribution obligations.

## Behavior

Vision is an optional speed-limit source, disabled by default. Enable Vision Speed Limit Detection and select Vision in Speed Limit Controller's priorities, or use it with the speed-limit display. Recognized numbers are U.S. mph values even when the display is metric. Existing speed offsets, confirmation, override and fallback behavior remain part of the controller.

The worker consumes the narrow road camera, validates its NV12 layout and timestamps, and requires repeated sign evidence. Hash-pinned models, confidence and regulatory-heading checks reject unsupported or ambiguous observations. It clears the source on parking, road-name changes, stale camera/worker data and process transitions. Vision is not persisted as the previous-limit fallback.

For this release, the daemon serializes JSON through native byte-valued Params and reads the shared `RoadName` parameter. It obtains gear eligibility from an appended `frogpilotCarState.drivingGear` field instead of opening another `carState` reader. The planner publishes an appended Vision source field for the five-row speed-limit panel. New schema ordinals preserve the release's existing fields. Models live under its `frogpilot/selfdrive/assets` convention.

Recognition cannot establish lane applicability or whether a conditional sign is active. Nighttime, glare, occlusion and unfamiliar sign designs remain limitations. The driver must verify the applicable limit.

## Validation and delivery status

This source port has an isolated Linux x86 UI build and native Params, schema, CAN and VisionIPC bindings. All 195 focused tests passed. They exercise real ONNX inference through padded NV12 VisionIPC frames, native Params and the release's actual speed-limit controller, plus source priorities, Navigation preservation, expiration, overrides, gear validity, manager gating and reader capacity. The VSL Python modules and tests pass Ruff.

This release's Qt source needed an explicit `QPainterPath` include for the desktop build. The local desktop build also used privately extracted Qt development packages and ICU 66 for its existing MapLibre binary; no GTA environment or system package installation was changed. The dependency lock retains all existing versions and hashes, with OpenCV headless promoted to an explicit runtime dependency.

**This branch has not been built or installed on a comma device.** Device and driving results reported for the original VSL-Port revision do not validate this different release integration. Its schema and UI changes require a complete compatible ARM build and successful startup before installation or a vehicle test. The source executable-mode correction from the release's later fix is retained; the corresponding compiled binary must be produced during release preparation.

Focused checks in a prepared Linux checkout:

```sh
export PYTHONDONTWRITEBYTECODE=1
export OPENPILOT_PREFIX=vsl_release_tests
pytest -n0 -o addopts='' frogpilot/system/tests/test_speed_limit_vision.py frogpilot/selfdrive/controls/tests/test_vision_speed_limit_controller.py
scons -j4 --minimal selfdrive/ui/ui
```

The newer compiled `master` remains unchanged while that device preparation is pending. Private recordings and local build helpers are not included in this branch.
