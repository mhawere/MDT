"""
browse.py — Directory listing for the folder picker UI.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException

import config
from app import apk as apk_mod


def default_browse_path() -> Path:
    apk = apk_mod.get_apk_dir()
    if apk.exists() and apk.is_dir():
        return apk.resolve()
    home = Path.home()
    if home.exists():
        return home.resolve()
    return config.PROJECT_ROOT.resolve()


def resolve_browse_path(path: str) -> Path:
    if not path or not path.strip():
        return default_browse_path()

    raw = path.strip()
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = (config.PROJECT_ROOT / p).resolve()
    else:
        try:
            p = p.resolve(strict=True)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"Path does not exist: {raw}") from exc

    if not p.is_dir():
        raise HTTPException(status_code=400, detail="Path is not a directory")

    try:
        next(p.iterdir())
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="Directory is not readable") from exc
    except StopIteration:
        pass

    return p


def list_directory(path: Path) -> dict:
    directories: list[dict[str, str]] = []
    apk_files: list[dict[str, str]] = []

    try:
        entries = sorted(path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="Directory is not readable") from exc

    for entry in entries:
        try:
            if entry.is_dir():
                directories.append({
                    "name": entry.name,
                    "path": str(entry.resolve()),
                })
            elif entry.is_file() and entry.suffix.lower() == ".apk":
                apk_files.append({
                    "name": entry.name,
                    "path": str(entry.resolve()),
                })
        except (PermissionError, OSError):
            continue

    parent = str(path.parent.resolve()) if path.parent != path else None

    return {
        "path": str(path),
        "parent": parent,
        "name": path.name or str(path),
        "directories": directories,
        "apk_files": apk_files,
        "apk_count": len(apk_files),
        "home": str(Path.home().resolve()),
    }
