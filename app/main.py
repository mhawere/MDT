"""
main.py — FastAPI application: routes, WebSocket endpoints, lifespan orchestration.
"""
from __future__ import annotations

import asyncio
import json
import platform
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles

import config
from app.state import DeviceState, app_state
import app.avd as avd_mod
import app.device as device
import app.emulator as emulator
import app.logs as logs_mod
import app.streamer as streamer_mod
import app.apk as apk_mod
import app.apk_tests as apk_tests_mod


# ── Session timestamp (once per server start) ─────────────────────────────────
SESSION_TS = datetime.now().strftime("%Y%m%d_%H%M%S")
_apk_sync_lock = asyncio.Lock()
_apk_watcher_task: asyncio.Task | None = None
_resources_adapted = False


class ApkDirPayload(BaseModel):
    path: str


class TestRunPayload(BaseModel):
    tests: list[str] | None = None


def _mem_available_mb() -> int:
    """Cross-platform available memory probe."""
    try:
        import psutil
        return int(psutil.virtual_memory().available // (1024 * 1024))
    except Exception:
        pass

    if sys.platform == "win32":
        try:
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            s = MS()
            s.dwLength = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s))
            return int(s.ullAvailPhys // (1024 * 1024))
        except Exception:
            pass

    if platform.system() == "Linux":
        try:
            with open("/proc/meminfo", encoding="utf-8") as f:
                info = {}
                for line in f:
                    key, val = line.split(":", 1)
                    info[key.strip()] = int(val.strip().split()[0])
            avail = info.get("MemAvailable") or info.get("MemFree", 0)
            return int(avail // 1024)
        except Exception:
            pass

    return 8192


def _adapt_startup_resources(requested_devices: int) -> int:
    """
    Reduce memory pressure before launching emulators.
    Keeps requested device count and only tunes per-emulator RAM.
    """
    if requested_devices <= 0:
        return 0

    available_mb = _mem_available_mb()

    reserve_mb = 2048
    per_device_overhead_mb = 700
    min_emu_mb = 1024

    budget_mb = max(0, available_mb - reserve_mb)
    desired_total = requested_devices * (config.EMULATOR_MEMORY_MB + per_device_overhead_mb)
    if desired_total <= budget_mb:
        return requested_devices

    max_mem_per_device = (budget_mb // requested_devices) - per_device_overhead_mb
    if max_mem_per_device >= min_emu_mb:
        tuned = int(max_mem_per_device)
        if tuned < config.EMULATOR_MEMORY_MB:
            print(
                f"[MDT] Memory pressure detected ({available_mb} MB available). "
                f"Lowering EMULATOR_MEMORY_MB from {config.EMULATOR_MEMORY_MB} to {tuned}."
            )
            config.EMULATOR_MEMORY_MB = tuned
        return requested_devices

    if config.EMULATOR_MEMORY_MB != min_emu_mb:
        print(f"[MDT] Lowering EMULATOR_MEMORY_MB to {min_emu_mb} for stability.")
        config.EMULATOR_MEMORY_MB = min_emu_mb
    return requested_devices


def _ensure_resources_adapted() -> None:
    global _resources_adapted
    if _resources_adapted:
        return
    _adapt_startup_resources(config.MAX_DEVICES)
    _resources_adapted = True


def _get_device_or_404(index: int) -> DeviceState:
    ds = app_state.get(index)
    if not ds:
        raise HTTPException(status_code=404, detail=f"Device index {index} not found")
    return ds


async def _cancel_task(task: asyncio.Task | None, timeout: float = 2) -> None:
    if task and not task.done():
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=timeout)
        except Exception:
            pass


async def _restart_streamer(ds: DeviceState) -> None:
    await _cancel_task(ds.streamer_task)
    ds.streamer_task = asyncio.create_task(
        streamer_mod.stream_screen(ds),
        name=f"streamer_{ds.index}",
    )


async def _restart_logcat(ds: DeviceState) -> None:
    await _cancel_task(ds.logcat_task)
    if ds.package:
        ds.logcat_task = asyncio.create_task(
            logs_mod.run_logcat(ds, SESSION_TS),
            name=f"logcat_{ds.index}",
        )


# ── Orchestration ─────────────────────────────────────────────────────────────

async def _orchestrate_device(ds: DeviceState, stagger_sec: float = 0) -> None:
    """
    Full device lifecycle: ensure AVD → launch emulator → wait boot →
    install APK → launch app → start logcat + screencap.
    Errors are isolated to this device; others continue.
    """
    if ds.state == "error":
        await app_state.broadcast(ds.index, {"type": "state", **ds.to_dict()})
        return

    if stagger_sec > 0:
        await asyncio.sleep(stagger_sec)

    try:
        _ensure_resources_adapted()

        avd_mod.ensure_avd(ds.index)

        await _emit_state(ds, "booting", "Starting emulator…")
        emulator.launch_emulator(ds)

        booted = await emulator.wait_for_boot(ds)
        if not booted:
            return

        ds.screen_w, ds.screen_h = await device.get_screen_size(ds.serial)

        if ds.apk_path is None:
            await _emit_state(ds, "running", "")
        else:
            await _emit_state(ds, "installing", f"Installing {ds.apk_path.name}…")
            ok, out = await device.install_apk(ds.serial, ds.apk_path)
            if not ok:
                await _emit_state(ds, "error", f"Install failed: {out[-300:]}")
                return

            await device.launch_app(ds.serial, ds.package or "")
            await _emit_state(ds, "running", "")

            ds.logcat_task = asyncio.create_task(
                logs_mod.run_logcat(ds, SESSION_TS),
                name=f"logcat_{ds.index}",
            )

        ds.streamer_task = asyncio.create_task(
            streamer_mod.stream_screen(ds),
            name=f"streamer_{ds.index}",
        )

    except Exception as exc:
        await _emit_state(ds, "error", str(exc)[:400])


async def _emit_state(ds: DeviceState, state: str, msg: str = "") -> None:
    ds.state = state
    ds.status_msg = msg
    ds.error_msg = msg if state == "error" else ""
    await app_state.broadcast(ds.index, {
        "type":      "state",
        "state":     ds.state,
        "status_msg": ds.status_msg,
        "error_msg": ds.error_msg,
        **ds.to_dict(),
    })


async def _startup() -> None:
    """Scan APKs and kick off background orchestration for each device."""
    _ensure_resources_adapted()
    await _sync_devices_from_apk_dir(initial=True)


async def _sync_devices_from_apk_dir(initial: bool = False) -> None:
    async with _apk_sync_lock:
        apks = apk_mod.scan_apks()

        if not app_state.devices and not apks and initial:
            print("[MDT] No APKs found in selected APK directory — server ready, waiting for APKs.")
            return

        existing = {
            str(ds.apk_path.resolve()): ds
            for ds in app_state.devices
            if ds.apk_path is not None and ds.apk_path.exists()
        }

        new_devices: list[DeviceState] = []
        for apk_path in apks:
            key = str(apk_path.resolve())
            if key in existing:
                continue
            if len(app_state.devices) + len(new_devices) >= config.MAX_DEVICES:
                break

            i = len(app_state.devices) + len(new_devices)
            try:
                package = apk_mod.resolve_package(apk_path)
            except Exception as exc:
                package = None
                print(f"[MDT] ⚠  Could not resolve package for {apk_path.name}: {exc}")

            serial = config.emulator_serial(i)
            avd_name = f"mdt_{i}"
            ds = DeviceState(
                index=i,
                serial=serial,
                avd_name=avd_name,
                apk_path=apk_path,
                package=package,
            )
            app_state.register_device(ds)
            new_devices.append(ds)

            if package is None:
                ds.state = "error"
                ds.error_msg = f"Cannot parse package name from {apk_path.name}"
                ds.status_msg = ds.error_msg

        for idx, ds in enumerate(new_devices):
            stagger = idx * config.BOOT_STAGGER_SEC
            asyncio.create_task(
                _orchestrate_device(ds, stagger_sec=stagger),
                name=f"orchestrate_{ds.index}",
            )


async def _watch_apk_dir() -> None:
    while True:
        try:
            await _sync_devices_from_apk_dir(initial=False)
            await asyncio.sleep(3)
        except asyncio.CancelledError:
            break
        except Exception:
            await asyncio.sleep(3)


async def _shutdown() -> None:
    """Gracefully stop all devices."""
    print("\n[MDT] Shutting down devices…")
    global _apk_watcher_task
    if _apk_watcher_task and not _apk_watcher_task.done():
        _apk_watcher_task.cancel()
        try:
            await asyncio.wait_for(_apk_watcher_task, timeout=2)
        except Exception:
            pass
    tasks = []
    for ds in app_state.devices:
        for task in (ds.logcat_task, ds.streamer_task):
            if task and not task.done():
                task.cancel()

        if ds.logcat_proc and hasattr(ds.logcat_proc, "returncode"):
            if ds.logcat_proc.returncode is None:
                try:
                    ds.logcat_proc.kill()
                except Exception:
                    pass

        tasks.append(asyncio.create_task(emulator.stop_emulator(ds)))

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    print("[MDT] All emulators stopped ✓")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _apk_watcher_task
    await _startup()
    _apk_watcher_task = asyncio.create_task(_watch_apk_dir(), name="apk_dir_watcher")
    yield
    await _shutdown()


# ── App setup ─────────────────────────────────────────────────────────────────

app = FastAPI(title="MDT — Multi-Device Tester", lifespan=lifespan)

_STATIC = Path(__file__).parent.parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")


# ── HTTP routes ───────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse(str(_STATIC / "index.html"))


@app.get("/api/devices")
async def list_devices():
    return [ds.to_dict() for ds in app_state.devices]


@app.get("/api/config")
async def get_config():
    return {
        "max_devices": config.MAX_DEVICES,
        "log_dir":     str(config.LOG_OUTPUT_DIR),
        "apk_dir":     str(apk_mod.get_apk_dir()),
        "session_ts":  SESSION_TS,
        "tests":       apk_tests_mod.list_available_tests(),
    }


@app.post("/api/apk_dir")
async def set_apk_dir(payload: ApkDirPayload):
    try:
        p = apk_mod.set_apk_dir(Path(payload.path))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _sync_devices_from_apk_dir(initial=False)
    return {"ok": True, "apk_dir": str(p)}


# ── Control endpoints ─────────────────────────────────────────────────────────

@app.post("/api/device/{index}/restart_app")
async def restart_app(index: int):
    ds = _get_device_or_404(index)
    if not ds.package:
        raise HTTPException(status_code=400, detail="Device has no package")
    await device.adb_shell(ds.serial, f"am force-stop {ds.package}")
    await asyncio.sleep(1)
    await device.launch_app(ds.serial, ds.package)
    return {"ok": True}


@app.post("/api/device/{index}/reinstall")
async def reinstall(index: int):
    ds = _get_device_or_404(index)
    if not ds.apk_path:
        raise HTTPException(status_code=400, detail="Device has no APK")
    await _emit_state(ds, "installing", "Reinstalling…")
    ok, out = await device.install_apk(ds.serial, ds.apk_path)
    if ok:
        await device.launch_app(ds.serial, ds.package or "")
        await _emit_state(ds, "running", "")
    else:
        await _emit_state(ds, "error", f"Reinstall failed: {out[-300:]}")
    return {"ok": ok, "out": out}


@app.post("/api/device/{index}/reboot")
async def reboot_device_ep(index: int):
    ds = _get_device_or_404(index)
    await _emit_state(ds, "booting", "Rebooting device…")
    await device.reboot_device(ds.serial)
    asyncio.create_task(_wait_and_relaunch(ds), name=f"reboot_{index}")
    return {"ok": True}


async def _wait_and_relaunch(ds: DeviceState) -> None:
    await _cancel_task(ds.streamer_task)
    await _cancel_task(ds.logcat_task)

    booted = await emulator.wait_for_boot(ds)
    if not booted:
        return

    ds.screen_w, ds.screen_h = await device.get_screen_size(ds.serial)

    if ds.apk_path:
        await device.install_apk(ds.serial, ds.apk_path)
        await device.launch_app(ds.serial, ds.package or "")

    await _emit_state(ds, "running", "")

    if ds.package:
        await _restart_logcat(ds)
    await _restart_streamer(ds)


@app.post("/api/device/{index}/rotate")
async def rotate(index: int):
    ds = _get_device_or_404(index)
    rot = getattr(ds, "_rotation", 0)
    new_rot = await device.rotate_screen(ds.serial, rot)
    ds._rotation = new_rot  # type: ignore[attr-defined]

    ds.screen_w, ds.screen_h = await device.get_screen_size(ds.serial)

    await _cancel_task(ds.streamer_task)
    await _restart_streamer(ds)
    await _restart_logcat(ds)

    return {"ok": True, "rotation": new_rot}


@app.post("/api/restart_all")
async def restart_all():
    for ds in app_state.devices:
        if ds.package:
            await device.adb_shell(ds.serial, f"am force-stop {ds.package}")
            await asyncio.sleep(0.5)
            await device.launch_app(ds.serial, ds.package)
    return {"ok": True}


@app.post("/api/reinstall_all")
async def reinstall_all():
    results = []
    for ds in app_state.devices:
        if ds.apk_path:
            ok, out = await device.install_apk(ds.serial, ds.apk_path)
            results.append({"index": ds.index, "ok": ok})
            if ok:
                await device.launch_app(ds.serial, ds.package or "")
    return {"results": results}


# ── APK test endpoints ────────────────────────────────────────────────────────

@app.get("/api/device/{index}/tests")
async def list_tests(index: int):
    _get_device_or_404(index)
    return {"tests": apk_tests_mod.list_available_tests()}


@app.get("/api/device/{index}/tests/status")
async def test_status(index: int):
    _get_device_or_404(index)
    run = apk_tests_mod.get_run_state(index)
    if not run:
        return {"status": "idle", "results": []}
    return {
        "run_id": run.run_id,
        "status": run.status,
        "current_test": run.current_test,
        "tests": run.tests,
        "results": run.results,
        "duration_ms": int((run.finished_at or __import__("time").time()) - run.started_at) * 1000,
    }


@app.post("/api/device/{index}/tests/run")
async def run_tests(index: int, payload: TestRunPayload | None = None):
    ds = _get_device_or_404(index)
    if ds.state not in ("running", "installing"):
        raise HTTPException(status_code=409, detail=f"Device not ready (state={ds.state})")
    try:
        run = await apk_tests_mod.run_tests(ds, payload.tests if payload else None)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ok": True,
        "run_id": run.run_id,
        "tests": run.tests,
        "status": run.status,
    }


@app.post("/api/device/{index}/tests/{test_name}")
async def run_single_test(index: int, test_name: str):
    ds = _get_device_or_404(index)
    if ds.state not in ("running", "installing"):
        raise HTTPException(status_code=409, detail=f"Device not ready (state={ds.state})")
    if test_name not in apk_tests_mod.ALL_TESTS:
        raise HTTPException(status_code=404, detail=f"Unknown test: {test_name}")
    try:
        run = await apk_tests_mod.run_single_test(ds, test_name)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "ok": True,
        "run_id": run.run_id,
        "tests": run.tests,
        "status": run.status,
    }


# ── WebSocket ─────────────────────────────────────────────────────────────────

@app.websocket("/ws/{index}")
async def device_ws(websocket: WebSocket, index: int):
    """
    Multiplexed per-device WebSocket.
    Receives JSON commands (tap, swipe, text, key) from the browser.
    Sends JSON messages (frame, log, state, counters, test) to the browser.
    """
    await websocket.accept()
    await app_state.add_ws(index, websocket)

    ds = app_state.get(index)
    if ds:
        await websocket.send_json({"type": "state", **ds.to_dict()})
        run = apk_tests_mod.get_run_state(index)
        if run and run.status == "running":
            await websocket.send_json({
                "type": "test",
                "event": "progress",
                "run_id": run.run_id,
                "current_test": run.current_test,
                "completed": len(run.results),
                "total": len(run.tests),
            })

    try:
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=30)
            except asyncio.TimeoutError:
                try:
                    await websocket.send_json({"type": "ping"})
                except Exception:
                    break
                continue

            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                continue

            if not ds:
                ds = app_state.get(index)
            if not ds or ds.state not in ("running", "installing"):
                continue

            asyncio.create_task(_handle_input_safe(ds, msg), name=f"input_{index}")

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await app_state.remove_ws(index, websocket)


async def _handle_input(ds: DeviceState, msg: dict) -> None:
    """Dispatch input commands from browser to adb."""
    action = msg.get("action")

    if action == "tap":
        nx, ny = msg.get("nx", 0), msg.get("ny", 0)
        x = round(nx * ds.screen_w)
        y = round(ny * ds.screen_h)
        await device.input_tap(ds.serial, x, y)

    elif action == "swipe":
        nx1, ny1 = msg.get("nx1", 0), msg.get("ny1", 0)
        nx2, ny2 = msg.get("nx2", 0), msg.get("ny2", 0)
        ms = msg.get("ms", config.INPUT_SWIPE_MS)
        x1, y1 = round(nx1 * ds.screen_w), round(ny1 * ds.screen_h)
        x2, y2 = round(nx2 * ds.screen_w), round(ny2 * ds.screen_h)
        await device.input_swipe(ds.serial, x1, y1, x2, y2, ms)

    elif action == "text":
        await device.input_text(ds.serial, msg.get("text", ""))

    elif action == "key":
        keycodes = {
            "back":    4,
            "home":    3,
            "recents": 187,
            "enter":   66,
            "del":     67,
            "power":   26,
        }
        code = keycodes.get(msg.get("key", ""), 0)
        if code:
            await device.input_keyevent(ds.serial, code)


async def _handle_input_safe(ds: DeviceState, msg: dict) -> None:
    try:
        await _handle_input(ds, msg)
    except Exception:
        return
