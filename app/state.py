"""
state.py — In-memory session/device state and WebSocket connection registry.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from fastapi import WebSocket


# ── Device state machine ─────────────────────────────────────────────────────
STATES = ("idle", "booting", "installing", "running", "error")


@dataclass
class DeviceState:
    index: int
    serial: str
    avd_name: str
    apk_path: Optional[Path] = None
    package: Optional[str]   = None
    state: str               = "idle"   # one of STATES
    status_msg: str          = ""
    error_msg: str           = ""
    screen_w: int            = 1080
    screen_h: int            = 2340
    # log counters
    cnt_ok: int    = 0
    cnt_warn: int  = 0
    cnt_error: int = 0

    # runtime handles (not serialised)
    emulator_proc: object       = field(default=None, repr=False)
    logcat_proc: object         = field(default=None, repr=False)
    streamer_task: object       = field(default=None, repr=False)
    logcat_task: object         = field(default=None, repr=False)

    def to_dict(self) -> dict:
        return {
            "index":     self.index,
            "serial":    self.serial,
            "avd_name":  self.avd_name,
            "apk_path":  str(self.apk_path) if self.apk_path else None,
            "package":   self.package,
            "state":     self.state,
            "status_msg": self.status_msg,
            "error_msg": self.error_msg,
            "screen_w":  self.screen_w,
            "screen_h":  self.screen_h,
            "cnt_ok":    self.cnt_ok,
            "cnt_warn":  self.cnt_warn,
            "cnt_error": self.cnt_error,
        }


# ── Global registry ───────────────────────────────────────────────────────────

class AppState:
    def __init__(self) -> None:
        self.devices: list[DeviceState] = []
        # Per-device WebSocket sets: index → set of WebSocket
        self._ws: dict[int, set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    def register_device(self, ds: DeviceState) -> None:
        self.devices.append(ds)
        self._ws[ds.index] = set()

    def get(self, index: int) -> Optional[DeviceState]:
        for d in self.devices:
            if d.index == index:
                return d
        return None

    def by_serial(self, serial: str) -> Optional[DeviceState]:
        for d in self.devices:
            if d.serial == serial:
                return d
        return None

    async def add_ws(self, index: int, ws: WebSocket) -> None:
        async with self._lock:
            self._ws.setdefault(index, set()).add(ws)

    async def remove_ws(self, index: int, ws: WebSocket) -> None:
        async with self._lock:
            self._ws.get(index, set()).discard(ws)

    async def broadcast(self, index: int, msg: dict) -> None:
        """Send JSON message to all WebSockets for device `index`."""
        dead: set[WebSocket] = set()
        sockets = set(self._ws.get(index, set()))
        for ws in sockets:
            try:
                await ws.send_json(msg)
            except Exception:
                dead.add(ws)
        if dead:
            async with self._lock:
                self._ws.get(index, set()).difference_update(dead)

    async def broadcast_bytes(self, index: int, payload: bytes) -> None:
        """Send binary message (screen frame) to all WebSockets for device `index`."""
        dead: set[WebSocket] = set()
        sockets = set(self._ws.get(index, set()))
        for ws in sockets:
            try:
                await ws.send_bytes(payload)
            except Exception:
                dead.add(ws)
        if dead:
            async with self._lock:
                self._ws.get(index, set()).difference_update(dead)

    async def broadcast_all(self, msg: dict) -> None:
        for idx in self._ws:
            await self.broadcast(idx, msg)

    def ws_count(self, index: int) -> int:
        return len(self._ws.get(index, set()))


# Singleton
app_state = AppState()
