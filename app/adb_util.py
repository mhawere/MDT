"""Shared adb path and environment helpers."""
from __future__ import annotations

import config
from app.sdk import _sdk_env


def adb_bin() -> str:
    return str(config.ANDROID_SDK_ROOT / "platform-tools" / "adb.exe")


def adb_env() -> dict:
    return _sdk_env()
