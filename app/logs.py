"""
logs.py — Logcat capture, level→color parsing, JSONL+raw trail writer, broadcaster.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Optional

import config
from app.adb_util import adb_bin, adb_env
from app.state import DeviceState, app_state
import app.device as device


def _adb() -> str:
    return adb_bin()


# ── Log line parsing ───────────────────────────────────────────────────────────
# threadtime format: MM-DD HH:MM:SS.mmm  PID  TID L TAG: message
_LOG_RE = re.compile(
    r"^(\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)\s+"  # timestamp
    r"(\d+)\s+(\d+)\s+"                            # PID TID
    r"([VDIWEF])\s+"                               # level
    r"([^:]+):\s*(.*)"                             # tag: message
)

LEVEL_COLOR = {
    "V": "ok",
    "D": "ok",
    "I": "ok",
    "W": "warn",
    "E": "error",
    "F": "error",
}


def _parse_line(raw: str) -> Optional[dict]:
    m = _LOG_RE.match(raw.strip())
    if not m:
        return None
    ts, pid, tid, level, tag, msg = m.groups()
    return {
        "type":  "log",
        "ts":    ts,
        "pid":   pid,
        "tid":   tid,
        "level": level,
        "color": LEVEL_COLOR.get(level, "ok"),
        "tag":   tag.strip(),
        "msg":   msg,
        "raw":   raw.rstrip(),
    }


# ── Trail writers ─────────────────────────────────────────────────────────────

def _open_trail(ds: DeviceState, session_ts: str) -> tuple[Path, Path]:
    """Return (jsonl_path, raw_path) for this device's session, creating them."""
    config.LOG_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    base = f"{ds.apk_path.stem if ds.apk_path else 'device'}_{session_ts}"
    jsonl_path = config.LOG_OUTPUT_DIR / f"{base}.jsonl"
    raw_path   = config.LOG_OUTPUT_DIR / f"{base}.log"
    return jsonl_path, raw_path


# ── Main async logcat task ────────────────────────────────────────────────────

async def run_logcat(ds: DeviceState, session_ts: str) -> None:
    """
    Async task: stream logcat for ds, parse, persist, and broadcast.
    Handles PID resolution and app restarts.
    """
    jsonl_path, raw_path = _open_trail(ds, session_ts)
    adb = _adb()
    env = adb_env()

    # Clear logcat buffer at session start
    await device.clear_logcat(ds.serial)

    # Resolve PID (retry a few times after launch)
    pid: Optional[str] = None
    for attempt in range(8):
        pid = await device.get_pid(ds.serial, ds.package or "")
        if pid:
            break
        await asyncio.sleep(2)

    proc: Optional[asyncio.subprocess.Process] = None

    async def _start_logcat(pid: Optional[str]):
        cmd = [adb, "-s", ds.serial, "logcat", "-v", "threadtime"]
        if pid:
            cmd += ["--pid", pid]
        return await asyncio.create_subprocess_exec(
            *cmd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

    try:
        proc = await _start_logcat(pid)
        ds.logcat_proc = proc
        next_counter_emit = 0.0
        dirty_lines = 0

        with open(jsonl_path, "a", encoding="utf-8") as jf, \
             open(raw_path, "a", encoding="utf-8") as rf:

            while True:
                try:
                    line_bytes = await asyncio.wait_for(
                        proc.stdout.readline(), timeout=5
                    )
                except asyncio.TimeoutError:
                    # Check if app restarted (PID changed)
                    new_pid = await device.get_pid(ds.serial, ds.package or "")
                    if new_pid and new_pid != pid:
                        # Restart logcat with new PID
                        pid = new_pid
                        proc.kill()
                        await proc.wait()
                        proc = await _start_logcat(pid)
                        ds.logcat_proc = proc
                    continue

                if not line_bytes:
                    # Process ended
                    await asyncio.sleep(2)
                    # Restart logcat
                    pid = await device.get_pid(ds.serial, ds.package or "")
                    proc = await _start_logcat(pid)
                    ds.logcat_proc = proc
                    continue

                raw = line_bytes.decode(errors="replace")

                # Filter to package if no PID filter
                if not pid and ds.package and ds.package not in raw:
                    continue

                entry = _parse_line(raw)
                if entry is None:
                    continue

                # Update counters
                color = entry["color"]
                if color == "ok":
                    ds.cnt_ok += 1
                elif color == "warn":
                    ds.cnt_warn += 1
                elif color == "error":
                    ds.cnt_error += 1

                # Persist
                jf.write(json.dumps(entry) + "\n")
                rf.write(raw.rstrip() + "\n")
                dirty_lines += 1

                # Broadcast log line immediately for live tailing.
                await app_state.broadcast(ds.index, entry)

                now = asyncio.get_running_loop().time()

                # Flush in small batches to avoid syncing on every single line.
                if dirty_lines >= 20:
                    jf.flush()
                    rf.flush()
                    dirty_lines = 0

                # Throttle counter pushes; high-frequency updates can flood the UI.
                if now >= next_counter_emit:
                    next_counter_emit = now + 0.25
                    await app_state.broadcast(ds.index, {
                        "type":      "counters",
                        "cnt_ok":    ds.cnt_ok,
                        "cnt_warn":  ds.cnt_warn,
                        "cnt_error": ds.cnt_error,
                    })

    except asyncio.CancelledError:
        pass
    finally:
        if proc and proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
