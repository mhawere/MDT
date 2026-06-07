"""
emulator.py — Start/stop headless Android emulators, wait for boot.
"""
from __future__ import annotations

import asyncio
import subprocess
import threading
import time

import config
from app.sdk import _sdk_env
from app.state import DeviceState, app_state


def _emulator_bin() -> str:
    return str(config.ANDROID_SDK_ROOT / "emulator" / "emulator.exe")


def _adb_bin() -> str:
    return str(config.ANDROID_SDK_ROOT / "platform-tools" / "adb.exe")


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


async def wait_for_boot(ds: DeviceState) -> bool:
    """
    Wait until the emulator is fully booted.
    Returns True on success, False on timeout.
    Updates ds.state and broadcasts state changes.
    """
    adb = _adb_bin()
    serial = ds.serial
    env = _sdk_env()
    deadline = time.monotonic() + config.EMULATOR_BOOT_TIMEOUT

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
            if boot_done.strip() == "1" and anim_done.strip() == "stopped":
                return True
        except Exception:
            pass
        await asyncio.sleep(3)

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
    ds.error_msg = msg if state == "error" else ""
    await app_state.broadcast(ds.index, {
        "type":      "state",
        "state":     ds.state,
        "error_msg": ds.error_msg,
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
