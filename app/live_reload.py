"""
live_reload.py — Per-device file watching and adb push/install for hot reload.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config
import app.device as device
from app.state import DeviceState, app_state

MODES = ("apk", "assets")
STATUSES = ("stopped", "watching", "syncing", "error")

LIVE_RELOAD_POLL_SEC = getattr(config, "LIVE_RELOAD_POLL_SEC", 1.0)
LIVE_RELOAD_DEBOUNCE_SEC = getattr(config, "LIVE_RELOAD_DEBOUNCE_SEC", 1.5)


@dataclass
class LiveReloadState:
    enabled: bool = False
    watch_path: Path | None = None
    mode: str = "apk"
    remote_path: str | None = None
    status: str = "stopped"
    last_sync_at: float | None = None
    last_error: str = ""
    last_file: str | None = None
    sync_count: int = 0
    watcher_task: asyncio.Task | None = field(default=None, repr=False)
    debounce_task: asyncio.Task | None = field(default=None, repr=False)
    _snapshots: dict[str, float] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "watch_path": str(self.watch_path) if self.watch_path else None,
            "mode": self.mode,
            "remote_path": self.remote_path,
            "status": self.status,
            "last_sync_at": self._iso_last_sync(),
            "last_error": self.last_error,
            "last_file": self.last_file,
            "sync_count": self.sync_count,
        }

    def _iso_last_sync(self) -> str | None:
        if self.last_sync_at is None:
            return None
        return datetime.fromtimestamp(self.last_sync_at, tz=timezone.utc).isoformat()


_states: dict[int, LiveReloadState] = {}
_lock = asyncio.Lock()


def get_state(index: int) -> LiveReloadState:
    if index not in _states:
        _states[index] = LiveReloadState()
    return _states[index]


def validate_watch_path(path: str | Path, mode: str) -> Path:
    """Resolve and validate a watch path for the given mode."""
    if mode not in MODES:
        raise ValueError(f"Invalid mode {mode!r}; must be one of {MODES}")

    p = Path(path).expanduser()
    if not p.is_absolute():
        p = (config.PROJECT_ROOT / p).resolve()
    else:
        p = p.resolve()

    if not p.exists():
        raise ValueError(f"Watch path does not exist: {p}")

    if mode == "apk":
        if p.is_file():
            if p.suffix.lower() != ".apk":
                raise ValueError(f"APK mode requires a .apk file, got: {p.name}")
        elif not p.is_dir():
            raise ValueError(f"Watch path is not a file or directory: {p}")
    elif mode == "assets":
        if not p.is_dir():
            raise ValueError(f"Assets mode requires a directory: {p}")

    return p


def resolve_apk_target(watch_path: Path, device_apk: Path | None) -> Path | None:
    """Pick the APK file to install from watch path."""
    if watch_path.is_file() and watch_path.suffix.lower() == ".apk":
        return watch_path
    if watch_path.is_dir():
        if device_apk and device_apk.name:
            candidate = watch_path / device_apk.name
            if candidate.exists():
                return candidate
        apks = sorted(watch_path.glob("*.apk"))
        if apks:
            return apks[0]
    return None


def _file_snapshot(root: Path) -> dict[str, float]:
    """Map relative path → mtime for all files under root."""
    snap: dict[str, float] = {}
    if root.is_file():
        try:
            snap[root.name] = root.stat().st_mtime
        except OSError:
            pass
        return snap

    try:
        for f in root.rglob("*"):
            if f.is_file():
                try:
                    snap[str(f.relative_to(root))] = f.stat().st_mtime
                except (OSError, ValueError):
                    continue
    except OSError:
        pass
    return snap


def detect_changes(old: dict[str, float], new: dict[str, float]) -> list[str]:
    """Return relative paths that were added or modified."""
    changed: list[str] = []
    for path, mtime in new.items():
        if path not in old or old[path] != mtime:
            changed.append(path)
    return changed


async def _broadcast(index: int, lr: LiveReloadState) -> None:
    await app_state.broadcast(index, {
        "type": "live_reload",
        **lr.to_dict(),
    })


async def _set_status(index: int, lr: LiveReloadState, status: str, error: str = "") -> None:
    lr.status = status
    if error:
        lr.last_error = error
    await _broadcast(index, lr)


async def _cancel_task(task: asyncio.Task | None) -> None:
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def _stop_watcher(lr: LiveReloadState) -> None:
    """Cancel watcher/debounce tasks without acquiring the global lock."""
    await _cancel_task(lr.watcher_task)
    await _cancel_task(lr.debounce_task)
    lr.watcher_task = None
    lr.debounce_task = None
    lr._snapshots = {}


async def disable(index: int) -> LiveReloadState:
    """Stop watching and reset to stopped."""
    async with _lock:
        lr = get_state(index)
        lr.enabled = False
        await _stop_watcher(lr)
        lr.status = "stopped"
        lr.last_error = ""
        await _broadcast(index, lr)
        return lr


async def enable(
    ds: DeviceState,
    watch_path: str | Path,
    mode: str = "apk",
    remote_path: str | None = None,
) -> LiveReloadState:
    """Enable live reload for a device; starts background watcher."""
    index = ds.index
    p = validate_watch_path(watch_path, mode)

    if mode == "assets" and not remote_path:
        raise ValueError("remote_path is required for assets mode")

    async with _lock:
        lr = get_state(index)
        lr.enabled = False
        await _stop_watcher(lr)
        lr.enabled = True
        lr.watch_path = p
        lr.mode = mode
        lr.remote_path = remote_path
        lr.status = "watching"
        lr.last_error = ""
        lr._snapshots = _file_snapshot(p)
        lr.watcher_task = asyncio.create_task(
            _watch_loop(ds, lr),
            name=f"live_reload_{index}",
        )
        await _broadcast(index, lr)
        return lr


async def _watch_loop(ds: DeviceState, lr: LiveReloadState) -> None:
    """Poll watch path and trigger debounced sync on changes."""
    index = ds.index
    try:
        while lr.enabled and lr.watch_path:
            await asyncio.sleep(LIVE_RELOAD_POLL_SEC)
            if not lr.enabled or not lr.watch_path:
                break

            current = _file_snapshot(lr.watch_path)
            changed = detect_changes(lr._snapshots, current)
            if not changed:
                continue

            lr._snapshots = current
            trigger_file = changed[0]

            if lr.debounce_task and not lr.debounce_task.done():
                lr.debounce_task.cancel()
                try:
                    await lr.debounce_task
                except asyncio.CancelledError:
                    pass

            lr.debounce_task = asyncio.create_task(
                _debounced_sync(ds, lr, trigger_file),
                name=f"live_reload_sync_{index}",
            )
    except asyncio.CancelledError:
        pass


async def _debounced_sync(ds: DeviceState, lr: LiveReloadState, trigger_file: str) -> None:
    await asyncio.sleep(LIVE_RELOAD_DEBOUNCE_SEC)
    if not lr.enabled:
        return
    await sync_now(ds, lr, trigger_file=trigger_file)


async def sync_now(
    ds: DeviceState,
    lr: LiveReloadState | None = None,
    trigger_file: str | None = None,
) -> dict[str, Any]:
    """Perform immediate sync (install APK or push assets)."""
    index = ds.index
    lr = lr or get_state(index)

    if not lr.enabled or not lr.watch_path:
        raise RuntimeError("Live reload is not enabled")

    async with _lock:
        await _set_status(index, lr, "syncing")
        try:
            if lr.mode == "apk":
                result = await _sync_apk(ds, lr)
            else:
                result = await _sync_assets(ds, lr, trigger_file)

            lr.last_sync_at = time.time()
            lr.sync_count += 1
            lr.last_file = result.get("file") or trigger_file
            lr.last_error = ""
            lr.status = "watching"
            await _broadcast(index, lr)
            return result
        except Exception as exc:
            msg = str(exc)[:400]
            lr.last_error = msg
            await _set_status(index, lr, "error", error=msg)
            raise


async def _sync_apk(ds: DeviceState, lr: LiveReloadState) -> dict[str, Any]:
    assert lr.watch_path is not None
    apk = resolve_apk_target(lr.watch_path, ds.apk_path)
    if apk is None:
        raise RuntimeError(f"No .apk found in {lr.watch_path}")

    ok, out = await device.install_apk(ds.serial, apk)
    if not ok:
        raise RuntimeError(f"adb install failed: {out[-300:]}")

    if ds.package:
        await device.adb_shell(ds.serial, f"am force-stop {ds.package}")
        await asyncio.sleep(0.5)
        await device.launch_app(ds.serial, ds.package)

    ds.apk_path = apk
    return {"ok": True, "mode": "apk", "file": str(apk)}


async def _sync_assets(
    ds: DeviceState,
    lr: LiveReloadState,
    trigger_file: str | None,
) -> dict[str, Any]:
    assert lr.watch_path is not None
    assert lr.remote_path is not None

    root = lr.watch_path
    if trigger_file and (root / trigger_file).is_file():
        files = [root / trigger_file]
    else:
        files = [f for f in root.rglob("*") if f.is_file()]

    if not files:
        raise RuntimeError(f"No files to push in {root}")

    pushed: list[str] = []
    for local in files:
        rel = local.relative_to(root)
        remote = f"{lr.remote_path.rstrip('/')}/{rel.as_posix()}"
        ok, out = await device.push_file(ds.serial, local, remote)
        if not ok:
            raise RuntimeError(f"adb push failed for {rel}: {out[-200:]}")
        pushed.append(str(rel))

    return {"ok": True, "mode": "assets", "file": pushed[-1] if pushed else None, "pushed": pushed}


async def shutdown_all() -> None:
    """Stop all live reload watchers (called on server shutdown)."""
    for index in list(_states.keys()):
        await disable(index)
