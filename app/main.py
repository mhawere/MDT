"""
main.py — FastAPI application: routes, WebSocket endpoints, lifespan orchestration.
"""
from __future__ import annotations

import asyncio
import json
import ctypes
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
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


# ── Session timestamp (once per server start) ─────────────────────────────────
SESSION_TS = datetime.now().strftime("%Y%m%d_%H%M%S")
_apk_sync_lock = asyncio.Lock()
_apk_watcher_task: asyncio.Task | None = None


class ApkDirPayload(BaseModel):
    path: str

# ── Orchestration ─────────────────────────────────────────────────────────────

async def _orchestrate_device(ds: DeviceState) -> None:
    """
    Full device lifecycle: ensure AVD → launch emulator → wait boot →
    install APK → launch app → start logcat + screencap.
    Errors are isolated to this device; others continue.
    """
    # Don't boot an emulator if we already know this device is broken (e.g. unresolvable package)
    if ds.state == "error":
        await app_state.broadcast(ds.index, {"type": "state", **ds.to_dict()})
        return

    try:
        # 1. Ensure AVD exists
        avd_mod.ensure_avd(ds.index)

        # 2. Launch headless emulator
        await _emit_state(ds, "booting", "Starting emulator…")
        emulator.launch_emulator(ds)

        # 3. Wait for boot
        booted = await emulator.wait_for_boot(ds)
        if not booted:
            return  # state already set to error inside wait_for_boot

        # 4. Get screen dimensions
        ds.screen_w, ds.screen_h = await device.get_screen_size(ds.serial)

        if ds.apk_path is None:
            # No APK — just show the emulator screen
            await _emit_state(ds, "running", "")
        else:
            # 5. Install APK
            await _emit_state(ds, "installing", f"Installing {ds.apk_path.name}…")
            ok, out = await device.install_apk(ds.serial, ds.apk_path)
            if not ok:
                await _emit_state(ds, "error", f"Install failed: {out[-300:]}")
                return

            # 6. Launch app
            await device.launch_app(ds.serial, ds.package or "")
            await _emit_state(ds, "running", "")

            # 7. Start logcat
            ds.logcat_task = asyncio.create_task(
                logs_mod.run_logcat(ds, SESSION_TS),
                name=f"logcat_{ds.index}",
            )

        # 8. Start screen streamer
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


def _win_mem_available_mb() -> int:
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


def _adapt_startup_resources(requested_devices: int) -> int:
    """
    Reduce memory pressure before launching emulators.
    Keeps requested device count and only tunes per-emulator RAM.
    """
    if requested_devices <= 0:
        return 0

    available_mb = _win_mem_available_mb()

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


async def _startup() -> None:
    """Scan APKs and kick off background orchestration for each device."""
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

        for apk_path in apks:
            key = str(apk_path.resolve())
            if key in existing:
                continue
            if len(app_state.devices) >= config.MAX_DEVICES:
                break

            i = len(app_state.devices)
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

            if package is None:
                ds.state = "error"
                ds.error_msg = f"Cannot parse package name from {apk_path.name}"
                ds.status_msg = ds.error_msg

            asyncio.create_task(_orchestrate_device(ds), name=f"orchestrate_{i}")


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
        # Cancel logcat + streamer tasks
        for task in (ds.logcat_task, ds.streamer_task):
            if task and not task.done():
                task.cancel()

        # Stop logcat subprocess if any
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

app = FastAPI(title="Multi-Device Tester", lifespan=lifespan)

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
    }


@app.post("/api/apk_dir")
async def set_apk_dir(payload: ApkDirPayload):
    p = apk_mod.set_apk_dir(Path(payload.path))
    await _sync_devices_from_apk_dir(initial=False)
    return {"ok": True, "apk_dir": str(p)}


# ── Control endpoints ─────────────────────────────────────────────────────────

@app.post("/api/device/{index}/restart_app")
async def restart_app(index: int):
    ds = app_state.get(index)
    if not ds or not ds.package:
        return {"ok": False, "msg": "Device or package not found"}
    await device.adb_shell(ds.serial, f"am force-stop {ds.package}")
    await asyncio.sleep(1)
    await device.launch_app(ds.serial, ds.package)
    return {"ok": True}


@app.post("/api/device/{index}/reinstall")
async def reinstall(index: int):
    ds = app_state.get(index)
    if not ds or not ds.apk_path:
        return {"ok": False, "msg": "Device or APK not found"}
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
    ds = app_state.get(index)
    if not ds:
        return {"ok": False}
    await _emit_state(ds, "booting", "Rebooting device…")
    await device.reboot_device(ds.serial)
    asyncio.create_task(_wait_and_relaunch(ds), name=f"reboot_{index}")
    return {"ok": True}


async def _wait_and_relaunch(ds: DeviceState) -> None:
    booted = await emulator.wait_for_boot(ds)
    if booted and ds.apk_path:
        await device.install_apk(ds.serial, ds.apk_path)
        await device.launch_app(ds.serial, ds.package or "")
        await _emit_state(ds, "running", "")


@app.post("/api/device/{index}/rotate")
async def rotate(index: int):
    ds = app_state.get(index)
    if not ds:
        return {"ok": False}
    rot = getattr(ds, "_rotation", 0)
    new_rot = await device.rotate_screen(ds.serial, rot)
    ds._rotation = new_rot  # type: ignore[attr-defined]
    if ds.streamer_task and not ds.streamer_task.done():
        ds.streamer_task.cancel()
        try:
            await asyncio.wait_for(ds.streamer_task, timeout=2)
        except Exception:
            pass
    ds.streamer_task = asyncio.create_task(
        streamer_mod.stream_screen(ds),
        name=f"streamer_{index}",
    )
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


# ── WebSocket ─────────────────────────────────────────────────────────────────

@app.websocket("/ws/{index}")
async def device_ws(websocket: WebSocket, index: int):
    """
    Multiplexed per-device WebSocket.
    Receives JSON commands (tap, swipe, text, key) from the browser.
    Sends JSON messages (frame, log, state, counters) to the browser.
    """
    await websocket.accept()
    await app_state.add_ws(index, websocket)

    # Send current state immediately on connect
    ds = app_state.get(index)
    if ds:
        await websocket.send_json({"type": "state", **ds.to_dict()})

    try:
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=30)
            except asyncio.TimeoutError:
                # Send a keepalive ping
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
        # Drop transient adb/input errors to keep interaction loop fluid.
        return
