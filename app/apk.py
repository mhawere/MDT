"""
apk.py — Scan apk_input/ and resolve package names from APK manifests.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

import config


_APK_SOURCE_FILE = config.PROJECT_ROOT / ".apk_source_dir.txt"


def get_apk_dir() -> Path:
    """Return the active APK directory, honoring saved user override if present."""
    try:
        if _APK_SOURCE_FILE.exists():
            raw = _APK_SOURCE_FILE.read_text(encoding="utf-8", errors="replace").strip()
            if raw:
                p = Path(raw)
                config.APK_INPUT_DIR = p
                return p
    except Exception:
        pass
    return config.APK_INPUT_DIR


def set_apk_dir(path: Path) -> Path:
    """Set and persist the active APK directory."""
    p = path.expanduser()
    if not p.is_absolute():
        p = (config.PROJECT_ROOT / p).resolve()
    else:
        p = p.resolve()

    if not p.exists():
        raise ValueError(f"APK directory does not exist: {p}")
    if not p.is_dir():
        raise ValueError(f"APK path is not a directory: {p}")
    try:
        next(p.iterdir())
    except PermissionError as exc:
        raise ValueError(f"APK directory is not readable: {p}") from exc
    except StopIteration:
        pass

    p.mkdir(parents=True, exist_ok=True)
    _APK_SOURCE_FILE.write_text(str(p), encoding="utf-8")
    config.APK_INPUT_DIR = p
    return p


def scan_apks() -> list[Path]:
    """Return up to MAX_DEVICES APKs from apk_input/, sorted by name."""
    apk_dir = get_apk_dir()
    apk_dir.mkdir(parents=True, exist_ok=True)
    apks = sorted(apk_dir.glob("*.apk"))
    if len(apks) > config.MAX_DEVICES:
        print(
            f"[MDT] ⚠  Found {len(apks)} APKs — using first {config.MAX_DEVICES} only."
        )
        apks = apks[: config.MAX_DEVICES]
    return apks


def resolve_package(apk_path: Path) -> str:
    """
    Extract the Android package name from an APK.
    Primary:  pyaxmlparser  (pure-Python, no SDK dependency)
    Fallback: aapt2 dump badging
    Raises RuntimeError if both fail.
    """
    # Primary: pyaxmlparser
    try:
        from pyaxmlparser import APK
        a = APK(str(apk_path))
        pkg = a.packagename
        if pkg:
            return pkg
    except Exception as e:
        _primary_err = str(e)
    else:
        _primary_err = "empty package name"

    # Fallback: aapt2
    aapt2 = (
        config.ANDROID_SDK_ROOT
        / "build-tools"
    )
    # Find any available build-tools version
    aapt2_bin: Optional[Path] = None
    if aapt2.exists():
        for ver_dir in sorted(aapt2.iterdir(), reverse=True):
            candidate = ver_dir / "aapt2"
            if candidate.exists():
                aapt2_bin = candidate
                break

    if aapt2_bin:
        try:
            result = subprocess.run(
                [str(aapt2_bin), "dump", "badging", str(apk_path)],
                capture_output=True, text=True, timeout=30,
            )
            for line in result.stdout.splitlines():
                if line.startswith("package:"):
                    for part in line.split():
                        if part.startswith("name="):
                            return part[5:].strip("'\"")
        except Exception as e:
            raise RuntimeError(
                f"pyaxmlparser failed ({_primary_err}), aapt2 also failed: {e}"
            ) from e

    raise RuntimeError(
        f"Cannot resolve package name for {apk_path.name}. "
        f"pyaxmlparser error: {_primary_err}. aapt2 not available."
    )
