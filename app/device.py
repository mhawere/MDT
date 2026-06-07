"""
device.py — adb wrappers: install, launch, screencap, input commands, wm size.
"""
from __future__ import annotations

import asyncio
import re
import subprocess
from pathlib import Path
from typing import AsyncIterator, Optional

import config
from app.adb_util import adb_bin, adb_env


def _adb() -> str:
    return adb_bin()


def _env() -> dict:
    return adb_env()


# ── Sync helpers (used outside async context) ─────────────────────────────────

def adb_run(serial: str, *args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_adb(), "-s", serial] + list(args),
        env=_env(),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# ── Async helpers ─────────────────────────────────────────────────────────────

async def adb_shell(serial: str, cmd: str, timeout: int = 30) -> str:
    proc = await asyncio.create_subprocess_exec(
        _adb(), "-s", serial, "shell", cmd,
        env=_env(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return stdout.decode(errors="replace").strip()
    except asyncio.TimeoutError:
        proc.kill()
        return ""


async def install_apk(serial: str, apk_path: Path) -> tuple[bool, str]:
    """
    Install APK on device. Returns (success, output).
    -r = replace existing, -g = grant all runtime permissions.
    """
    proc = await asyncio.create_subprocess_exec(
        _adb(), "-s", serial, "install", "-r", "-g", str(apk_path),
        env=_env(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
    out = (stdout + stderr).decode(errors="replace")
    success = proc.returncode == 0 and "Success" in out
    return success, out


async def launch_app(serial: str, package: str) -> bool:
    """Launch app via monkey; returns True on success."""
    out = await adb_shell(
        serial,
        f"monkey -p {package} -c android.intent.category.LAUNCHER 1",
        timeout=20,
    )
    return "Events injected" in out or "monkey" in out.lower()


async def get_screen_size(serial: str) -> tuple[int, int]:
    """Parse `adb shell wm size` → (width, height). Defaults to 1080×2340."""
    out = await adb_shell(serial, "wm size", timeout=10)
    m = re.search(r"(\d+)x(\d+)", out)
    if m:
        return int(m.group(1)), int(m.group(2))
    return 1080, 2340


async def screenrecord_stream(
    serial: str,
    size: str | None = None,
    bitrate: int = 8_000_000,
    time_limit: int = 180,
) -> AsyncIterator[bytes]:
    args = [
        _adb(), "-s", serial, "exec-out", "screenrecord",
        "--output-format=h264", "--time-limit", str(time_limit),
        "--bit-rate", str(bitrate),
    ]
    if size:
        args += ["--size", str(size)]
    args += ["-"]
    proc = await asyncio.create_subprocess_exec(
        *args,
        env=_env(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                break
            yield chunk
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass


async def input_tap(serial: str, x: int, y: int) -> None:
    await adb_shell(serial, f"input tap {x} {y}", timeout=10)


async def input_swipe(serial: str, x1: int, y1: int, x2: int, y2: int, ms: int = 300) -> None:
    await adb_shell(serial, f"input swipe {x1} {y1} {x2} {y2} {ms}", timeout=10)


async def input_text(serial: str, text: str) -> None:
    # Escape spaces → %s; escape shell-special chars
    escaped = text.replace("\\", "\\\\").replace("'", "\\'").replace(" ", "%s").replace(
        "&", "\\&").replace("|", "\\|").replace(";", "\\;").replace("`", "\\`")
    await adb_shell(serial, f"input text '{escaped}'", timeout=10)


async def input_keyevent(serial: str, keycode: int) -> None:
    await adb_shell(serial, f"input keyevent {keycode}", timeout=10)


async def rotate_screen(serial: str, current_rotation: int = 0) -> int:
    """Cycle rotation 0→1→2→3→0. Returns new rotation value."""
    new_rot = (current_rotation + 1) % 4
    await adb_shell(serial, f"settings put system user_rotation {new_rot}", timeout=10)
    return new_rot


async def get_pid(serial: str, package: str) -> Optional[str]:
    """Get the main PID for a package. Returns None if not running."""
    out = await adb_shell(serial, f"pidof {package}", timeout=10)
    out = out.strip()
    if out and out.isdigit():
        return out
    # fallback: ps
    ps = await adb_shell(serial, f"ps -A | grep {package}", timeout=10)
    for line in ps.splitlines():
        parts = line.split()
        if len(parts) > 1:
            return parts[1]
    return None


async def clear_logcat(serial: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        _adb(), "-s", serial, "logcat", "-c",
        env=_env(),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await asyncio.wait_for(proc.communicate(), timeout=10)


async def reboot_device(serial: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        _adb(), "-s", serial, "reboot",
        env=_env(),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(proc.communicate(), timeout=15)
    except asyncio.TimeoutError:
        pass
