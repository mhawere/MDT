"""
emulator.py — Start/stop headless Android emulators, wait for boot.
"""
from __future__ import annotations

import asyncio
import subprocess
import threading
import time

import config
from app.sdk import _sdk_env, adb_path, emulator_bin_path
from app.state import DeviceState, app_state


def _emulator_bin() -> str:
    return str(emulator_bin_path())


def _adb_bin() -> str:
    return str(adb_path())


def _read_stderr(proc: subprocess.Popen, buf: list[str]) -> None:
    if proc.stderr is None:
        return
    try:
        for line in proc.stderr:
            text = line.decode(errors="replace").strip()
            if text:
                buf.append(text)
                if len(buf) > 50:
                    buf.pop(0)
    except Exception:
        pass


def launch_emulator(ds: DeviceState) -> subprocess.Popen:
    """Start a headless emulator process and attach it to ds.emulator_proc."""
    console_port, _ = config.emulator_ports(ds.index)
    gpu_mode = getattr(config, "EMULATOR_GPU_MODE", "host")
    cmd = [
        _emulator_bin(),
        "-avd",     ds.avd_name,
        "-port",    str(console_port),
        "-no-window",
        "-no-boot-anim",
        "-camera-back", "none",
        "-camera-front", "none",
        "-no-audio",
        "-netfast",
        "-gpu",     gpu_mode,
        "-memory",  str(config.EMULATOR_MEMORY_MB),
        "-accel",   "auto",
    ]
    env = _sdk_env()
    stderr_buf: list[str] = []
    proc = subprocess.Popen(
        cmd,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    ds.emulator_stderr: list[str] = stderr_buf  # type: ignore[attr-defined]
    threading.Thread(target=_read_stderr, args=(proc, stderr_buf), daemon=True).start()
    ds.emulator_proc = proc
    return proc


def _emulator_stderr_snippet(ds: DeviceState, max_chars: int = 400) -> str:
    buf = getattr(ds, "emulator_stderr", None) or []
    if not buf:
        proc = ds.emulator_proc
        if proc and proc.poll() is not None:
            return f"Emulator process exited with code {proc.returncode}."
        return ""
    text = "\n".join(buf[-8:])
    if len(text) > max_chars:
        return text[-max_chars:]
    return text


async def _adb_get_state(adb: str, serial: str, env: dict) -> str | None:
    """Return adb get-state (device/offline/etc.) or None if unavailable."""
    proc = await asyncio.create_subprocess_exec(
        adb, "-s", serial, "get-state",
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
    except asyncio.TimeoutError:
        proc.kill()
        return None
    if proc.returncode != 0:
        return None
    return stdout.decode(errors="replace").strip()


async def _device_is_online(adb: str, serial: str, env: dict) -> bool:
    return await _adb_get_state(adb, serial, env) == "device"


async def wait_for_disconnect(ds: DeviceState, timeout: float) -> bool:
    """
    Wait until adb loses the device (e.g. after adb reboot).
    Returns True if disconnect was observed, False on timeout.
    """
    adb = _adb_bin()
    serial = ds.serial
    env = _sdk_env()
    deadline = time.monotonic() + timeout
    saw_online = False

    await asyncio.sleep(1.0)

    while time.monotonic() < deadline:
        proc = ds.emulator_proc
        if proc and proc.poll() is not None:
            return True

        if await _device_is_online(adb, serial, env):
            saw_online = True
        elif saw_online:
            return True

        await asyncio.sleep(0.5)

    return False


async def wait_for_boot(ds: DeviceState, *, after_reboot: bool = False) -> bool:
    """
    Wait until the emulator is fully booted.
    Returns True on success, False on timeout.
    Updates ds.state and broadcasts state changes.

    When after_reboot=True, waits for adb disconnect first so stale
    sys.boot_completed=1 from the previous session is not mistaken for done.
    """
    adb = _adb_bin()
    serial = ds.serial
    env = _sdk_env()
    deadline = time.monotonic() + config.EMULATOR_BOOT_TIMEOUT
    poll_sec = getattr(config, "EMULATOR_BOOT_POLL_SEC", 2)

    if after_reboot:
        await _broadcast_state(ds, "booting", "Waiting for device to restart…")
        disconnect_timeout = getattr(config, "REBOOT_DISCONNECT_TIMEOUT", 45)
        disconnected = await wait_for_disconnect(ds, disconnect_timeout)
        if not disconnected:
            await _broadcast_state(ds, "booting", "Device still online — waiting for full boot…")

    # Step 1: wait-for-device
    await _broadcast_state(ds, "booting", "Waiting for emulator device…")
    try:
        proc = await asyncio.create_subprocess_exec(
            adb, "-s", serial, "wait-for-device",
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            await _broadcast_state(ds, "error", "Timed out waiting for emulator device.")
            return False
        await asyncio.wait_for(proc.wait(), timeout=remaining)
    except asyncio.TimeoutError:
        detail = _emulator_stderr_snippet(ds)
        msg = "Timed out waiting for emulator device."
        if detail:
            msg += f" Emulator stderr: {detail}"
        await _broadcast_state(ds, "error", msg)
        return False

    # Step 2: poll boot_completed + bootanim stopped
    await _broadcast_state(ds, "booting", "Waiting for Android boot…")
    reconnected_at: float | None = None
    saw_boot_anim_running = False
    min_uptime = getattr(config, "REBOOT_MIN_UPTIME_SEC", 5) if after_reboot else 0

    while time.monotonic() < deadline:
        proc = ds.emulator_proc
        if proc and proc.poll() is not None:
            detail = _emulator_stderr_snippet(ds)
            msg = f"Emulator exited before boot (code {proc.returncode})."
            if detail:
                msg += f" stderr: {detail}"
            await _broadcast_state(ds, "error", msg)
            return False

        try:
            boot_done = await _adb_shell_output(adb, serial, "getprop sys.boot_completed", env)
            anim_done = await _adb_shell_output(adb, serial, "getprop init.svc.bootanim", env)
            boot = boot_done.strip()
            anim = anim_done.strip()

            if reconnected_at is None and boot in ("", "0"):
                reconnected_at = time.monotonic()
            if anim == "running":
                saw_boot_anim_running = True

            if boot == "1" and anim == "stopped":
                if after_reboot and reconnected_at is not None:
                    uptime = time.monotonic() - reconnected_at
                    if uptime < min_uptime:
                        await asyncio.sleep(poll_sec)
                        continue
                    if not saw_boot_anim_running and uptime < min_uptime * 2:
                        await asyncio.sleep(poll_sec)
                        continue
                return True
        except Exception:
            pass
        await asyncio.sleep(poll_sec)

    detail = _emulator_stderr_snippet(ds)
    msg = "Emulator boot timed out."
    if detail:
        msg += f" stderr: {detail}"
    await _broadcast_state(ds, "error", msg)
    return False


async def _adb_shell_output(adb: str, serial: str, cmd: str, env: dict) -> str:
    proc = await asyncio.create_subprocess_exec(
        adb, "-s", serial, "shell", cmd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    return stdout.decode(errors="replace")


async def _broadcast_state(ds: DeviceState, state: str, msg: str = "") -> None:
    ds.state = state
    if msg:
        ds.status_msg = msg
    ds.error_msg = msg if state == "error" else ""
    await app_state.broadcast(ds.index, {
        "type":       "state",
        "state":      ds.state,
        "status_msg": ds.status_msg,
        "error_msg":  ds.error_msg,
    })


async def stop_emulator(ds: DeviceState) -> None:
    """Gracefully stop the emulator for this device."""
    adb = _adb_bin()
    env = _sdk_env()
    # Preferred: emu kill via adb
    try:
        proc = await asyncio.create_subprocess_exec(
            adb, "-s", ds.serial, "emu", "kill",
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=10)
    except Exception:
        pass

    # Fallback: kill the process directly
    if ds.emulator_proc and ds.emulator_proc.poll() is None:
        try:
            ds.emulator_proc.terminate()
            ds.emulator_proc.wait(timeout=5)
        except Exception:
            try:
                ds.emulator_proc.kill()
            except Exception:
                pass
