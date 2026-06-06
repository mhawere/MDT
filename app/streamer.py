"""
streamer.py — Per-device H.264 stream loop → WebSocket broadcaster.
"""
from __future__ import annotations

import asyncio

import config
import app.device as device
from app.state import DeviceState, app_state


async def stream_screen(ds: DeviceState) -> None:
    """
    Async task: stream H.264 chunks from screenrecord and broadcast them
    to all WebSocket clients watching this device.
    """
    while True:
        try:
            if app_state.ws_count(ds.index) == 0:
                await asyncio.sleep(0.2)
                continue

            await app_state.broadcast(ds.index, {"type": "video_reset"})
            async for chunk in device.screenrecord_stream(
                ds.serial,
                size=config.SCREENRECORD_SIZE,
                bitrate=config.SCREENRECORD_BITRATE,
                time_limit=config.SCREENRECORD_TIME_LIMIT,
            ):
                if app_state.ws_count(ds.index) == 0:
                    break
                await app_state.broadcast_bytes(ds.index, chunk)

        except asyncio.CancelledError:
            break
        except Exception:
            await asyncio.sleep(0.5)
