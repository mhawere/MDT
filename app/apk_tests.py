"""
apk_tests.py — Built-in automated APK test runners for MDT devices.
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import app.device as device
from app.state import DeviceState, app_state

TestFn = Callable[[DeviceState], Awaitable[dict[str, Any]]]

ALL_TESTS = (
    "launch",
    "crash_detection",
    "anr_detection",
    "permission_audit",
    "activity_smoke",
    "memory_baseline",
    "network_connectivity",
    "ui_responsiveness",
)


@dataclass
class TestRunState:
    run_id: str
    index: int
    status: str = "pending"  # pending | running | passed | failed | error
    tests: list[str] = field(default_factory=list)
    results: list[dict[str, Any]] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    current_test: str | None = None


_active_runs: dict[int, TestRunState] = {}
_run_lock = asyncio.Lock()


def list_available_tests() -> list[str]:
    return list(ALL_TESTS)


def get_run_state(index: int) -> TestRunState | None:
    return _active_runs.get(index)


async def _emit_test(index: int, payload: dict[str, Any]) -> None:
    await app_state.broadcast(index, {"type": "test", **payload})


async def _run_single(ds: DeviceState, test_name: str) -> dict[str, Any]:
    fn = _TEST_REGISTRY.get(test_name)
    if not fn:
        return {
            "test": test_name,
            "status": "error",
            "passed": False,
            "duration_ms": 0,
            "message": f"Unknown test: {test_name}",
            "details": {},
        }

    started = time.monotonic()
    await _emit_test(ds.index, {"event": "start", "test": test_name})
    try:
        details = await fn(ds)
        passed = bool(details.pop("passed", True))
        message = str(details.pop("message", "OK" if passed else "Failed"))
        result = {
            "test": test_name,
            "status": "passed" if passed else "failed",
            "passed": passed,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "message": message,
            "details": details,
        }
    except Exception as exc:
        result = {
            "test": test_name,
            "status": "error",
            "passed": False,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "message": str(exc)[:400],
            "details": {},
        }

    await _emit_test(ds.index, {"event": "result", **result})
    return result


async def run_tests(ds: DeviceState, test_names: list[str] | None = None) -> TestRunState:
    """Run selected tests asynchronously; returns run state immediately after scheduling."""
    names = test_names or list(ALL_TESTS)
    unknown = [n for n in names if n not in ALL_TESTS]
    if unknown:
        raise ValueError(f"Unknown tests: {', '.join(unknown)}")

    async with _run_lock:
        if index_run := _active_runs.get(ds.index):
            if index_run.status == "running":
                raise RuntimeError("Tests already running on this device")

        run_id = f"{ds.index}-{int(time.time() * 1000)}"
        state = TestRunState(run_id=run_id, index=ds.index, tests=names, status="running")
        _active_runs[ds.index] = state

    asyncio.create_task(_execute_run(ds, state), name=f"tests_{ds.index}")
    return state


async def run_single_test(ds: DeviceState, test_name: str) -> TestRunState:
    return await run_tests(ds, [test_name])


async def _execute_run(ds: DeviceState, state: TestRunState) -> None:
    await _emit_test(ds.index, {
        "event": "run_start",
        "run_id": state.run_id,
        "tests": state.tests,
    })

    all_passed = True
    for name in state.tests:
        state.current_test = name
        await _emit_test(ds.index, {
            "event": "progress",
            "run_id": state.run_id,
            "current_test": name,
            "completed": len(state.results),
            "total": len(state.tests),
        })
        result = await _run_single(ds, name)
        state.results.append(result)
        if not result.get("passed"):
            all_passed = False

    state.current_test = None
    state.finished_at = time.time()
    state.status = "passed" if all_passed else "failed"
    await _emit_test(ds.index, {
        "event": "run_complete",
        "run_id": state.run_id,
        "status": state.status,
        "results": state.results,
        "duration_ms": int((state.finished_at - state.started_at) * 1000),
    })


# ── Individual tests ──────────────────────────────────────────────────────────

async def _test_launch(ds: DeviceState) -> dict[str, Any]:
    if not ds.package:
        return {"passed": False, "message": "No package configured for device"}

    await device.adb_shell(ds.serial, f"am force-stop {ds.package}", timeout=15)
    await asyncio.sleep(0.5)
    launched = await device.launch_app(ds.serial, ds.package)
    await asyncio.sleep(3)
    pid = await device.get_pid(ds.serial, ds.package)
    alive = pid is not None
    return {
        "passed": launched and alive,
        "message": "App launched and process alive" if alive else "App process not running after launch",
        "pid": pid,
        "launched": launched,
    }


async def _test_crash_detection(ds: DeviceState) -> dict[str, Any]:
    log = await device.adb_shell(
        ds.serial,
        "logcat -d -v brief | grep -E 'FATAL EXCEPTION|AndroidRuntime' | tail -n 20",
        timeout=20,
    )
    hits = [ln for ln in log.splitlines() if ln.strip()]
    return {
        "passed": len(hits) == 0,
        "message": "No fatal crashes in logcat" if not hits else f"Found {len(hits)} crash line(s)",
        "matches": hits[:10],
    }


async def _test_anr_detection(ds: DeviceState) -> dict[str, Any]:
    log = await device.adb_shell(
        ds.serial,
        "logcat -d -v brief | grep -E 'ANR in|NOT RESPONDING' | tail -n 20",
        timeout=20,
    )
    hits = [ln for ln in log.splitlines() if ln.strip()]
    return {
        "passed": len(hits) == 0,
        "message": "No ANR traces in logcat" if not hits else f"Found {len(hits)} ANR line(s)",
        "matches": hits[:10],
    }


async def _test_permission_audit(ds: DeviceState) -> dict[str, Any]:
    if not ds.package:
        return {"passed": False, "message": "No package configured for device"}

    dump = await device.adb_shell(ds.serial, f"dumpsys package {ds.package}", timeout=30)
    declared: list[str] = []
    granted: list[str] = []
    for line in dump.splitlines():
        line = line.strip()
        if "android.permission." in line:
            m = re.search(r"(android\.permission\.\S+)", line)
            if not m:
                continue
            perm = m.group(1).rstrip(":,")
            if perm not in declared:
                declared.append(perm)
            if "granted=true" in line.lower() or "GRANTED" in line:
                if perm not in granted:
                    granted.append(perm)

    missing = [p for p in declared if p not in granted]
    return {
        "passed": True,
        "message": f"{len(declared)} declared, {len(granted)} granted",
        "declared_count": len(declared),
        "granted_count": len(granted),
        "declared": declared[:30],
        "granted": granted[:30],
        "not_granted": missing[:30],
    }


async def _test_activity_smoke(ds: DeviceState) -> dict[str, Any]:
    if not ds.package:
        return {"passed": False, "message": "No package configured for device"}

    out = await device.adb_shell(
        ds.serial,
        f"dumpsys activity activities | grep -E 'mResumedActivity|topResumedActivity' | head -n 5",
        timeout=20,
    )
    in_foreground = ds.package in out
    if not in_foreground:
        await device.launch_app(ds.serial, ds.package)
        await asyncio.sleep(2)
        out = await device.adb_shell(
            ds.serial,
            f"dumpsys activity activities | grep -E 'mResumedActivity|topResumedActivity' | head -n 5",
            timeout=20,
        )
        in_foreground = ds.package in out

    return {
        "passed": in_foreground,
        "message": "Main activity reachable" if in_foreground else "Package not in resumed activity",
        "activity_dump": out[:500],
    }


async def _test_memory_baseline(ds: DeviceState) -> dict[str, Any]:
    if not ds.package:
        return {"passed": False, "message": "No package configured for device"}

    meminfo = await device.adb_shell(ds.serial, f"dumpsys meminfo {ds.package}", timeout=25)
    total_pss = None
    for line in meminfo.splitlines():
        if "TOTAL PSS" in line.upper() or "TOTAL" in line.upper() and "kB" in line:
            nums = re.findall(r"(\d+)", line)
            if nums:
                total_pss = int(nums[0])
                break

    return {
        "passed": total_pss is not None,
        "message": f"TOTAL PSS: {total_pss} kB" if total_pss else "Could not parse meminfo",
        "total_pss_kb": total_pss,
        "meminfo_excerpt": "\n".join(meminfo.splitlines()[:15]),
    }


async def _test_network_connectivity(ds: DeviceState) -> dict[str, Any]:
    if not ds.package:
        return {"passed": False, "message": "No package configured for device"}

    perm_dump = await device.adb_shell(ds.serial, f"dumpsys package {ds.package}", timeout=20)
    has_internet = "android.permission.INTERNET" in perm_dump
    dns = await device.adb_shell(ds.serial, "ping -c 1 -W 2 8.8.8.8", timeout=10)
    dns_ok = "1 received" in dns or "1 packets transmitted, 1 received" in dns

    return {
        "passed": has_internet and dns_ok,
        "message": "Internet permission declared and DNS reachable" if (has_internet and dns_ok)
        else "Missing permission or DNS unreachable",
        "has_internet_permission": has_internet,
        "dns_ping": dns[:200],
    }


async def _test_ui_responsiveness(ds: DeviceState) -> dict[str, Any]:
    if ds.state not in ("running", "installing"):
        return {"passed": False, "message": f"Device not ready (state={ds.state})"}

    cx = max(1, ds.screen_w // 2)
    cy = max(1, ds.screen_h // 2)
    await device.input_tap(ds.serial, cx, cy)
    await asyncio.sleep(1)
    await device.input_tap(ds.serial, cx, max(1, cy // 2))
    await asyncio.sleep(2)

    crash_log = await device.adb_shell(
        ds.serial,
        "logcat -d -v brief -t 30 | grep -E 'FATAL EXCEPTION|AndroidRuntime' | tail -n 5",
        timeout=15,
    )
    crashed = bool(crash_log.strip())
    pid = await device.get_pid(ds.serial, ds.package or "")
    return {
        "passed": not crashed and pid is not None,
        "message": "Taps completed without crash" if not crashed else "Crash detected after taps",
        "process_alive": pid is not None,
        "crash_lines": crash_log.splitlines()[:5],
    }


_TEST_REGISTRY: dict[str, TestFn] = {
    "launch": _test_launch,
    "crash_detection": _test_crash_detection,
    "anr_detection": _test_anr_detection,
    "permission_audit": _test_permission_audit,
    "activity_smoke": _test_activity_smoke,
    "memory_baseline": _test_memory_baseline,
    "network_connectivity": _test_network_connectivity,
    "ui_responsiveness": _test_ui_responsiveness,
}
