"""Shared adb path and environment helpers."""
from __future__ import annotations

from app.sdk import _sdk_env, adb_path


def adb_bin() -> str:
    return str(adb_path())


def adb_env() -> dict:
    return _sdk_env()
