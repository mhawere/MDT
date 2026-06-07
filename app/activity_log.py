"""
activity_log.py — Global activity ring buffer and WebSocket broadcast.
"""
from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
from typing import Any

from fastapi import WebSocket

MAX_ENTRIES = 500


class ActivityLog:
    def __init__(self, max_entries: int = MAX_ENTRIES) -> None:
        self._buffer: deque[dict[str, Any]] = deque(maxlen=max_entries)
        self._ws: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    def _make_entry(
        self,
        level: str,
        message: str,
        *,
        source: str = "MDT",
        device_index: int | None = None,
    ) -> dict[str, Any]:
        return {
            "type": "activity",
            "ts": datetime.now(timezone.utc).astimezone().strftime("%H:%M:%S.%f")[:-3],
            "level": level,
            "source": source,
            "message": message,
            "device_index": device_index,
        }

    def add(
        self,
        level: str,
        message: str,
        *,
        source: str = "MDT",
        device_index: int | None = None,
    ) -> dict[str, Any]:
        entry = self._make_entry(level, message, source=source, device_index=device_index)
        self._buffer.append(entry)
        return entry

    async def emit(
        self,
        level: str,
        message: str,
        *,
        source: str = "MDT",
        device_index: int | None = None,
    ) -> dict[str, Any]:
        entry = self.add(level, message, source=source, device_index=device_index)
        await self._broadcast(entry)
        return entry

    def get_recent(self, limit: int = 200) -> list[dict[str, Any]]:
        items = list(self._buffer)
        return items[-limit:]

    def clear(self) -> None:
        self._buffer.clear()

    async def register_ws(self, ws: WebSocket) -> None:
        async with self._lock:
            self._ws.add(ws)

    async def unregister_ws(self, ws: WebSocket) -> None:
        async with self._lock:
            self._ws.discard(ws)

    async def _broadcast(self, entry: dict[str, Any]) -> None:
        dead: set[WebSocket] = set()
        async with self._lock:
            sockets = set(self._ws)
        for ws in sockets:
            try:
                await ws.send_json(entry)
            except Exception:
                dead.add(ws)
        if dead:
            async with self._lock:
                self._ws.difference_update(dead)


activity_log = ActivityLog()
