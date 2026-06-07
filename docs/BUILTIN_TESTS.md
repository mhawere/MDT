# MDT Built-in APK Tests

Each device panel includes a **Tests** tab with one-click automated checks against the installed APK. Results stream over the device WebSocket and are also available via REST polling.

## Running tests

- **Run All** — executes every test in sequence.
- **Individual buttons** — run a single test (`POST /api/device/{index}/tests/{test_name}`).
- **API batch** — `POST /api/device/{index}/tests/run` with body `{"tests": ["launch", "crash_detection"]}`.
- **Status** — `GET /api/device/{index}/tests/status`.

Tests require the device to be in `running` or `installing` state.

## Test reference

### Launch (`launch`)

Force-stops the app, relaunches via `monkey`, waits 3 seconds, then checks that a process PID exists.

- **Pass:** App process is alive after launch.
- **Fail:** Launch command failed or no PID found.

### Crash detection (`crash_detection`)

Scans logcat for `FATAL EXCEPTION` and `AndroidRuntime` lines.

- **Pass:** No crash signatures found.
- **Fail:** One or more crash lines present (see `matches` in details).

### ANR detection (`anr_detection`)

Scans logcat for `ANR in` and `NOT RESPONDING` traces.

- **Pass:** No ANR signatures.
- **Fail:** ANR lines found.

### Permission audit (`permission_audit`)

Parses `adb shell dumpsys package <package>` for declared vs granted permissions.

- **Pass:** Always informational (lists counts and permission names).
- **Interpret:** Compare `declared`, `granted`, and `not_granted` arrays.

### Activity smoke (`activity_smoke`)

Checks whether the package appears in resumed activity dumps; relaunches if needed.

- **Pass:** Package is in foreground/resumed activity.
- **Fail:** Activity stack does not show the app.

### Memory baseline (`memory_baseline`)

Captures `dumpsys meminfo <package>` and parses TOTAL PSS.

- **Pass:** PSS value parsed successfully.
- **Fail:** Could not read meminfo (app may not be running).

### Network connectivity (`network_connectivity`)

Verifies `INTERNET` permission is declared and runs `ping -c 1 8.8.8.8` on the emulator.

- **Pass:** Permission present and ping succeeds.
- **Fail:** Missing permission or DNS unreachable from emulator.

### UI responsiveness (`ui_responsiveness`)

Sends two center-screen taps, waits, then checks for new crash log lines and process liveness.

- **Pass:** No crash after taps and process still alive.
- **Fail:** Crash detected or process died.

## Result JSON shape

```json
{
  "test": "launch",
  "status": "passed",
  "passed": true,
  "duration_ms": 4123,
  "message": "App launched and process alive",
  "details": { "pid": "12345" }
}
```

WebSocket events use `type: "test"` with `event` one of: `run_start`, `progress`, `start`, `result`, `run_complete`.
