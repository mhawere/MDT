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
import app.live_reload as live_reload_mod
import app.activity_log as activity_log_mod
import app.browse as browse_mod
from app.sdk import ensure_sdk_ready
from app.sdk_config import SDK_NOT_CONFIGURED_MSG, SdkNotReadyError, detect_sdk_candidates, get_sdk_status, set_sdk_path


# ── Session timestamp (once per server start) ─────────────────────────────────
SESSION_TS = datetime.now().strftime("%Y%m%d_%H%M%S")
_apk_sync_lock = asyncio.Lock()
_apk_watcher_task: asyncio.Task | None = None
_resources_adapted = False
_reboot_locks: dict[int, asyncio.Lock] = {}


class ApkDirPayload(BaseModel):
    path: str


class SdkPathPayload(BaseModel):
    path: str


class TestRunPayload(BaseModel):
    tests: list[str] | None = None


class LiveReloadEnablePayload(BaseModel):
    watch_path: str | None = None
    mode: str = "apk"
    remote_path: str | None = None


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
    if not ds.active:
        return

    if ds.state == "error":
        await app_state.broadcast(ds.index, {"type": "state", **ds.to_dict()})
        return

    if stagger_sec > 0:
        await asyncio.sleep(stagger_sec)

    try:
        _ensure_resources_adapted()
        ensure_sdk_ready()

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

    except SdkNotReadyError as exc:
        await _emit_state(ds, "error", str(exc)[:400])
    except Exception as exc:
        msg = str(exc)[:400]
        if "avdmanager" in msg.lower() or "no such file" in msg.lower():
            msg = SDK_NOT_CONFIGURED_MSG
        await _emit_state(ds, "error", msg)


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
    level = "error" if state == "error" else "success" if state == "running" else "info"
    detail = msg or state
    apk_name = ds.apk_path.name if ds.apk_path else f"device {ds.index}"
    await activity_log_mod.activity_log.emit(
        level,
        f"[{apk_name}] {detail}",
        source="device",
        device_index=ds.index,
    )


async def _startup() -> None:
    """Scan APKs and kick off background orchestration for each device."""
    from app.sdk_config import apply_sdk_root, refresh_tool_paths, resolve_sdk_root

    apply_sdk_root(resolve_sdk_root())
    refresh_tool_paths()
    _ensure_resources_adapted()
    await activity_log_mod.activity_log.emit(
        "info",
        f"MDT server started — session {SESSION_TS}",
        source="server",
    )
    await _sync_devices_from_apk_dir(initial=True)


def _ensure_device_slots() -> None:
    """Ensure placeholder slots exist for indices 0..MAX_DEVICES-1."""
    existing = {d.index for d in app_state.devices}
    for i in range(config.MAX_DEVICES):
        if i in existing:
            continue
        ds = DeviceState(
            index=i,
            serial=config.emulator_serial(i),
            avd_name=f"mdt_{i}",
            active=False,
            state="closed",
        )
        app_state.register_device(ds)


def _assigned_apk_paths() -> set[str]:
    return {
        str(ds.apk_path.resolve())
        for ds in app_state.devices
        if ds.active and ds.apk_path is not None and ds.apk_path.exists()
    }


def _next_unassigned_apk(apks: list[Path]) -> Path | None:
    assigned = _assigned_apk_paths()
    for apk in apks:
        key = str(apk.resolve())
        if key not in assigned:
            return apk
    return None


async def _teardown_device(ds: DeviceState) -> None:
    """Stop emulator, cancel tasks, and release resources for a device slot."""
    await _cancel_task(ds.reboot_task)
    ds.reboot_task = None

    try:
        await live_reload_mod.disable(ds.index)
    except Exception:
        pass

    await _cancel_task(ds.streamer_task)
    await _cancel_task(ds.logcat_task)
    ds.streamer_task = None
    ds.logcat_task = None

    if ds.logcat_proc and hasattr(ds.logcat_proc, "returncode"):
        if ds.logcat_proc.returncode is None:
            try:
                ds.logcat_proc.kill()
            except Exception:
                pass
    ds.logcat_proc = None

    await emulator.stop_emulator(ds)
    ds.emulator_proc = None


async def _close_device_slot(ds: DeviceState) -> None:
    """Close a device slot: stop emulator and clear APK assignment."""
    await _teardown_device(ds)
    ds.active = False
    ds.apk_path = None
    ds.package = None
    ds.state = "closed"
    ds.status_msg = ""
    ds.error_msg = ""
    ds.reboot_failures = 0
    ds.cnt_ok = ds.cnt_warn = ds.cnt_error = 0
    await app_state.broadcast(ds.index, {"type": "state", **ds.to_dict()})
    await activity_log_mod.activity_log.emit(
        "info",
        f"Device slot {ds.index} closed",
        source="control",
        device_index=ds.index,
    )


async def _open_device_slot(ds: DeviceState) -> None:
    """Open a closed slot and boot an emulator with the next available APK."""
    if ds.active:
        raise HTTPException(status_code=409, detail="Device slot is already active")

    try:
        ensure_sdk_ready()
    except SdkNotReadyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    apks = apk_mod.scan_apks()
    apk_path = _next_unassigned_apk(apks)
    if apk_path is None:
        raise HTTPException(status_code=400, detail="No APK available to assign")

    try:
        package = apk_mod.resolve_package(apk_path)
    except Exception as exc:
        package = None
        print(f"[MDT] ⚠  Could not resolve package for {apk_path.name}: {exc}")

    ds.active = True
    ds.apk_path = apk_path
    ds.package = package
    ds.state = "idle" if package else "error"
    ds.status_msg = ""
    ds.error_msg = (
        f"Cannot parse package name from {apk_path.name}" if package is None else ""
    )
    ds.reboot_failures = 0

    await activity_log_mod.activity_log.emit(
        "info",
        f"Opening device slot {ds.index} with {apk_path.name}",
        source="control",
        device_index=ds.index,
    )

    if package is None:
        await _emit_state(ds, "error", ds.error_msg)
        return

    stagger = sum(1 for d in app_state.devices if d.active and d.index < ds.index) * config.BOOT_STAGGER_SEC
    asyncio.create_task(
        _orchestrate_device(ds, stagger_sec=stagger),
        name=f"orchestrate_{ds.index}",
    )


async def _assign_apk_to_device(ds: DeviceState, apk_path: Path) -> None:
    """Set APK/package on a device and prepare for orchestration."""
    try:
        package = apk_mod.resolve_package(apk_path)
    except Exception as exc:
        package = None
        print(f"[MDT] ⚠  Could not resolve package for {apk_path.name}: {exc}")

    ds.apk_path = apk_path
    ds.package = package
    if package is None:
        ds.state = "error"
        ds.error_msg = f"Cannot parse package name from {apk_path.name}"
        ds.status_msg = ds.error_msg
    else:
        ds.state = "idle"
        ds.status_msg = ""
        ds.error_msg = ""


async def _resync_devices_for_apk_change(apks: list[Path]) -> None:
    """Tear down active devices and reassign APKs after the APK folder changes."""
    _ensure_device_slots()
    await activity_log_mod.activity_log.emit(
        "info",
        "Resyncing devices after APK folder change",
        source="apk",
    )

    to_orchestrate: list[tuple[DeviceState, float]] = []

    for ds in app_state.sorted_devices():
        if not ds.active:
            continue

        await _teardown_device(ds)
        ds.reboot_failures = 0

        if ds.index < len(apks):
            await _assign_apk_to_device(ds, apks[ds.index])
            if ds.state != "error":
                stagger = ds.index * config.BOOT_STAGGER_SEC
                to_orchestrate.append((ds, stagger))
            else:
                await _emit_state(ds, "error", ds.error_msg)
        else:
            await _close_device_slot(ds)
            continue

        await app_state.broadcast(ds.index, {"type": "state", **ds.to_dict()})

    for ds in app_state.sorted_devices():
        if not ds.active:
            await app_state.broadcast(ds.index, {"type": "state", **ds.to_dict()})

    if not apks and not app_state.active_devices():
        print("[MDT] No APKs found in selected APK directory — waiting for APKs.")
        await activity_log_mod.activity_log.emit(
            "warn",
            f"No APKs found in {apk_mod.get_apk_dir()} — waiting for APKs",
            source="apk",
        )

    for ds, stagger in to_orchestrate:
        asyncio.create_task(
            _orchestrate_device(ds, stagger_sec=stagger),
            name=f"orchestrate_{ds.index}",
        )


async def _activate_initial_devices(apks: list[Path]) -> None:
    """On first startup, activate one slot per discovered APK (up to MAX_DEVICES)."""
    _ensure_device_slots()
    new_devices: list[DeviceState] = []

    for i, apk_path in enumerate(apks[: config.MAX_DEVICES]):
        ds = app_state.get(i)
        if ds is None:
            continue
        if ds.active and ds.apk_path == apk_path:
            continue

        if ds.active:
            await _teardown_device(ds)

        ds.active = True
        await _assign_apk_to_device(ds, apk_path)
        new_devices.append(ds)
        await activity_log_mod.activity_log.emit(
            "info",
            f"Discovered APK: {apk_path.name}",
            source="apk",
            device_index=i,
        )

    for idx, ds in enumerate(new_devices):
        if ds.state == "error":
            await _emit_state(ds, "error", ds.error_msg)
            continue
        stagger = idx * config.BOOT_STAGGER_SEC
        asyncio.create_task(
            _orchestrate_device(ds, stagger_sec=stagger),
            name=f"orchestrate_{ds.index}",
        )


async def _sync_devices_from_apk_dir(
    initial: bool = False,
    force_resync: bool = False,
) -> None:
    async with _apk_sync_lock:
        apks = apk_mod.scan_apks()

        if force_resync:
            await _resync_devices_for_apk_change(apks)
            return

        if initial:
            if not apks:
                _ensure_device_slots()
                print("[MDT] No APKs found in selected APK directory — server ready, waiting for APKs.")
                await activity_log_mod.activity_log.emit(
                    "warn",
                    f"No APKs found in {apk_mod.get_apk_dir()} — waiting for APKs",
                    source="apk",
                )
                return
            await _activate_initial_devices(apks)
            return

        # Background watcher: drop active devices whose APK vanished from disk.
        _ensure_device_slots()
        apk_keys = {str(p.resolve()) for p in apks}

        for ds in list(app_state.devices):
            if not ds.active or ds.apk_path is None:
                continue
            key = str(ds.apk_path.resolve())
            if ds.apk_path.exists() and key in apk_keys:
                continue
            await activity_log_mod.activity_log.emit(
                "warn",
                f"APK removed: {ds.apk_path.name} — closing slot {ds.index}",
                source="apk",
                device_index=ds.index,
            )
            await _close_device_slot(ds)

        # Do not auto-open closed slots when new APKs appear — user must click Add.


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
    await activity_log_mod.activity_log.emit("info", "Shutting down devices…", source="server")
    await live_reload_mod.shutdown_all()
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
    _ensure_device_slots()
    return [ds.to_dict() for ds in app_state.sorted_devices()]


@app.get("/api/config")
async def get_config():
    apks = apk_mod.scan_apks()
    _ensure_device_slots()
    sdk = get_sdk_status()
    return {
        "max_devices":    config.MAX_DEVICES,
        "log_dir":        str(config.LOG_OUTPUT_DIR),
        "apk_dir":        str(apk_mod.get_apk_dir()),
        "apk_count":      len(apks),
        "active_devices": len(app_state.active_devices()),
        "session_ts":     SESSION_TS,
        "tests":          apk_tests_mod.list_available_tests(),
        "sdk":            sdk,
    }


@app.get("/api/sdk")
async def get_sdk():
    return get_sdk_status()


@app.post("/api/sdk")
async def post_sdk(payload: SdkPathPayload):
    try:
        status = set_sdk_path(payload.path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await activity_log_mod.activity_log.emit(
        "success",
        f"Android SDK set to {status['sdk_root']}",
        source="sdk",
    )
    return {"ok": True, **status}


@app.post("/api/sdk/detect")
async def detect_sdk():
    candidates = detect_sdk_candidates()
    best = candidates[0] if candidates else None
    if best and best.get("valid"):
        set_sdk_path(best["sdk_root"])
        await activity_log_mod.activity_log.emit(
            "success",
            f"Auto-detected Android SDK at {best['sdk_root']}",
            source="sdk",
        )
    return {"ok": True, "candidates": candidates, "selected": best}


@app.get("/api/browse")
async def browse_directory(path: str = ""):
    """List directories and APK files at path for the folder picker UI."""
    resolved = browse_mod.resolve_browse_path(path)
    return browse_mod.list_directory(resolved)


@app.post("/api/apk_dir")
async def set_apk_dir(payload: ApkDirPayload):
    try:
        p = apk_mod.set_apk_dir(Path(payload.path))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await activity_log_mod.activity_log.emit(
        "success",
        f"APK folder set to {p}",
        source="apk",
    )
    await _sync_devices_from_apk_dir(force_resync=True)
    _ensure_device_slots()
    return {
        "ok": True,
        "apk_dir": str(p),
        "devices": [ds.to_dict() for ds in app_state.sorted_devices()],
    }


@app.get("/api/activity")
async def get_activity(limit: int = 200):
    return {"entries": activity_log_mod.activity_log.get_recent(limit)}


@app.post("/api/activity/clear")
async def clear_activity():
    activity_log_mod.activity_log.clear()
    await activity_log_mod.activity_log.emit("info", "Activity log cleared", source="ui")
    return {"ok": True}


def _require_active_device(index: int) -> DeviceState:
    ds = _get_device_or_404(index)
    if not ds.active:
        raise HTTPException(status_code=409, detail=f"Device slot {index} is closed")
    return ds


# ── Control endpoints ─────────────────────────────────────────────────────────

@app.post("/api/device/{index}/close")
async def close_device(index: int):
    ds = _get_device_or_404(index)
    if not ds.active:
        return {"ok": True, **ds.to_dict()}
    await _close_device_slot(ds)
    return {"ok": True, **ds.to_dict()}


@app.post("/api/device/{index}/open")
async def open_device(index: int):
    ds = _get_device_or_404(index)
    await _open_device_slot(ds)
    return {"ok": True, **ds.to_dict()}


@app.post("/api/device/{index}/restart_app")
async def restart_app(index: int):
    ds = _require_active_device(index)
    if not ds.package:
        raise HTTPException(status_code=400, detail="Device has no package")
    await activity_log_mod.activity_log.emit(
        "info", f"Restarting app {ds.package}", source="control", device_index=index,
    )
    await device.adb_shell(ds.serial, f"am force-stop {ds.package}")
    await asyncio.sleep(1)
    await device.launch_app(ds.serial, ds.package)
    return {"ok": True}


@app.post("/api/device/{index}/reinstall")
async def reinstall(index: int):
    ds = _require_active_device(index)
    if not ds.apk_path:
        raise HTTPException(status_code=400, detail="Device has no APK")
    await activity_log_mod.activity_log.emit(
        "info", f"Reinstalling {ds.apk_path.name}", source="control", device_index=index,
    )
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
    ds = _require_active_device(index)
    if ds.reboot_task and not ds.reboot_task.done():
        raise HTTPException(status_code=409, detail="Reboot already in progress")

    if ds.reboot_failures >= config.REBOOT_MAX_ATTEMPTS:
        await activity_log_mod.activity_log.emit(
            "warn",
            f"Manual reboot retry — resetting failure counter ({ds.reboot_failures})",
            source="control",
            device_index=index,
        )
        ds.reboot_failures = 0

    await activity_log_mod.activity_log.emit(
        "info", "Rebooting emulator", source="control", device_index=index,
    )
    await _emit_state(ds, "booting", "Rebooting device…")
    await device.reboot_device(ds.serial)
    ds.reboot_task = asyncio.create_task(_wait_and_relaunch(ds), name=f"reboot_{index}")
    return {"ok": True}


async def _wait_and_relaunch(ds: DeviceState) -> None:
    lock = _reboot_locks.setdefault(ds.index, asyncio.Lock())
    async with lock:
        try:
            await _cancel_task(ds.streamer_task)
            await _cancel_task(ds.logcat_task)

            booted = await emulator.wait_for_boot(ds, after_reboot=True)
            if not booted:
                ds.reboot_failures += 1
                if ds.reboot_failures >= config.REBOOT_MAX_ATTEMPTS:
                    msg = (
                        f"Reboot failed {ds.reboot_failures} times — "
                        "stopped to prevent a reboot loop"
                    )
                    await _emit_state(ds, "error", msg)
                    await activity_log_mod.activity_log.emit(
                        "error",
                        msg,
                        source="device",
                        device_index=ds.index,
                    )
                return

            ds.reboot_failures = 0
            ds.screen_w, ds.screen_h = await device.get_screen_size(ds.serial)

            if ds.package:
                await device.launch_app(ds.serial, ds.package)

            await _emit_state(ds, "running", "")

            if ds.package:
                await _restart_logcat(ds)
            await _restart_streamer(ds)
        except Exception as exc:
            ds.reboot_failures += 1
            await _emit_state(ds, "error", str(exc)[:400])
            if ds.reboot_failures >= config.REBOOT_MAX_ATTEMPTS:
                await activity_log_mod.activity_log.emit(
                    "error",
                    f"Reboot loop stopped after {ds.reboot_failures} failures",
                    source="device",
                    device_index=ds.index,
                )
        finally:
            ds.reboot_task = None


@app.post("/api/device/{index}/rotate")
async def rotate(index: int):
    ds = _require_active_device(index)
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
    await activity_log_mod.activity_log.emit("info", "Restarting all apps", source="control")
    for ds in app_state.active_devices():
        if ds.package:
            await device.adb_shell(ds.serial, f"am force-stop {ds.package}")
            await asyncio.sleep(0.5)
            await device.launch_app(ds.serial, ds.package)
    return {"ok": True}


@app.post("/api/reinstall_all")
async def reinstall_all():
    await activity_log_mod.activity_log.emit("info", "Reinstalling all APKs", source="control")
    results = []
    for ds in app_state.active_devices():
        if ds.apk_path:
            ok, out = await device.install_apk(ds.serial, ds.apk_path)
            results.append({"index": ds.index, "ok": ok})
            if ok:
                await device.launch_app(ds.serial, ds.package or "")
    return {"results": results}


# ── Live reload endpoints ─────────────────────────────────────────────────────

@app.get("/api/device/{index}/live-reload/status")
async def live_reload_status(index: int):
    _get_device_or_404(index)
    return live_reload_mod.get_state(index).to_dict()


@app.post("/api/device/{index}/live-reload/enable")
async def live_reload_enable(index: int, payload: LiveReloadEnablePayload | None = None):
    ds = _require_active_device(index)
    if ds.state not in ("running", "installing"):
        raise HTTPException(status_code=409, detail=f"Device not ready (state={ds.state})")

    body = payload or LiveReloadEnablePayload()
    watch_path = body.watch_path
    if not watch_path:
        if ds.apk_path:
            watch_path = str(ds.apk_path)
        else:
            raise HTTPException(status_code=400, detail="watch_path is required")

    try:
        lr = await live_reload_mod.enable(
            ds,
            watch_path=watch_path,
            mode=body.mode,
            remote_path=body.remote_path,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {"ok": True, **lr.to_dict()}


@app.post("/api/device/{index}/live-reload/disable")
async def live_reload_disable(index: int):
    _get_device_or_404(index)
    lr = await live_reload_mod.disable(index)
    return {"ok": True, **lr.to_dict()}


@app.post("/api/device/{index}/live-reload/sync")
async def live_reload_sync_now(index: int):
    ds = _require_active_device(index)
    lr = live_reload_mod.get_state(index)
    if not lr.enabled:
        raise HTTPException(status_code=409, detail="Live reload is not enabled")
    try:
        result = await live_reload_mod.sync_now(ds)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": True, **result, **lr.to_dict()}


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
    ds = _require_active_device(index)
    if ds.state not in ("running", "installing"):
        raise HTTPException(status_code=409, detail=f"Device not ready (state={ds.state})")
    test_list = payload.tests if payload and payload.tests else "all"
    await activity_log_mod.activity_log.emit(
        "info",
        f"Starting tests: {test_list}",
        source="tests",
        device_index=index,
    )
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
    ds = _require_active_device(index)
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

@app.websocket("/ws/activity")
async def activity_ws(websocket: WebSocket):
    """Global activity log stream for the dashboard panel."""
    await websocket.accept()
    await activity_log_mod.activity_log.register_ws(websocket)
    try:
        await websocket.send_json({
            "type": "activity_history",
            "entries": activity_log_mod.activity_log.get_recent(200),
        })
        while True:
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=30)
            except asyncio.TimeoutError:
                try:
                    await websocket.send_json({"type": "ping"})
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await activity_log_mod.activity_log.unregister_ws(websocket)


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
    if ds and ds.active:
        await websocket.send_json({"type": "state", **ds.to_dict()})
        lr = live_reload_mod.get_state(index)
        if lr.enabled:
            await websocket.send_json({"type": "live_reload", **lr.to_dict()})
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
            if not ds or not ds.active or ds.state not in ("running", "installing"):
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
