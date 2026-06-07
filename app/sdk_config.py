"""
sdk_config.py — Cross-platform Android SDK path resolution, validation, and persistence.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import config

SDK_PATH_FILE = config.PROJECT_ROOT / ".sdk_path.txt"

SDK_NOT_CONFIGURED_MSG = (
    "Android SDK not configured. Open Settings → set SDK path or click Auto-detect."
)


class SdkNotReadyError(RuntimeError):
    """Raised when the SDK is missing or incomplete for device operations."""


def platform_key() -> str:
    if sys.platform == "win32":
        return "win"
    if sys.platform == "darwin":
        return "mac"
    if sys.platform.startswith("linux"):
        return "linux"
    return "unknown"


PLATFORM = platform_key()


def _sdk_script_name(tool: str) -> str:
    return f"{tool}.bat" if PLATFORM == "win" else tool


def _bin_name(tool: str) -> str:
    return f"{tool}.exe" if PLATFORM == "win" else tool


def _adb_at(root: Path) -> Path:
    return root / "platform-tools" / _bin_name("adb")


def _emulator_at(root: Path) -> Path:
    return root / "emulator" / _bin_name("emulator")


def _aapt2_in(ver_dir: Path) -> Path:
    return ver_dir / _bin_name("aapt2")


def _project_local_sdk() -> Path:
    return (config.PROJECT_ROOT / config.SDK_DIR).resolve()


def is_project_local_sdk(path: Path | str) -> bool:
    try:
        return Path(path).resolve() == _project_local_sdk()
    except (OSError, ValueError):
        return False


def load_saved_sdk_path() -> Path | None:
    if not SDK_PATH_FILE.is_file():
        return None
    try:
        raw = SDK_PATH_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    p = Path(raw).expanduser()
    return p if p.is_dir() else None


def save_sdk_path(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    SDK_PATH_FILE.write_text(str(resolved) + "\n", encoding="utf-8")
    return resolved


def clear_saved_sdk_path() -> None:
    SDK_PATH_FILE.unlink(missing_ok=True)


def os_default_sdk_locations() -> list[Path]:
    home = Path.home()
    candidates: list[Path] = []
    if PLATFORM == "win":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            candidates.append(Path(local) / "Android" / "Sdk")
        candidates.append(home / "AppData" / "Local" / "Android" / "Sdk")
    elif PLATFORM == "mac":
        candidates.append(home / "Library" / "Android" / "sdk")
    else:
        candidates.append(home / "Android" / "Sdk")
    return candidates


def _env_sdk_candidates() -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    for var in ("MDT_ANDROID_SDK", "ANDROID_SDK_ROOT", "ANDROID_HOME"):
        val = os.environ.get(var, "").strip()
        if not val:
            continue
        p = Path(val).expanduser()
        if p.is_dir():
            out.append((var.lower(), p.resolve()))
    return out


def find_cmdline_bin(sdk_root: Path, tool: str) -> Path | None:
    """Locate sdkmanager / avdmanager under cmdline-tools or legacy tools/."""
    name = _sdk_script_name(tool)
    searches = [
        sdk_root / "cmdline-tools" / "latest" / "bin" / name,
    ]
    cmdline_root = sdk_root / "cmdline-tools"
    if cmdline_root.is_dir():
        for child in sorted(cmdline_root.iterdir(), reverse=True):
            if child.is_dir() and child.name != "latest":
                searches.append(child / "bin" / name)
    searches.append(sdk_root / "tools" / "bin" / name)
    for candidate in searches:
        if candidate.is_file():
            return candidate
    return None


def tool_paths_for(sdk_root: Path) -> dict[str, Path | None]:
    root = sdk_root.resolve()
    return {
        "adb": _adb_at(root) if _adb_at(root).is_file() else None,
        "emulator": _emulator_at(root) if _emulator_at(root).is_file() else None,
        "sdkmanager": find_cmdline_bin(root, "sdkmanager"),
        "avdmanager": find_cmdline_bin(root, "avdmanager"),
    }


def validate_sdk(sdk_root: Path) -> dict:
    """Check that core SDK tools exist under sdk_root."""
    root = sdk_root.expanduser().resolve()
    if not root.is_dir():
        return _validation_result(
            root, valid=False, source="unknown",
            missing=["sdk_root"],
            tools={},
            is_project_local=is_project_local_sdk(root),
        )

    paths = tool_paths_for(root)
    tools: dict[str, dict] = {}
    missing: list[str] = []
    for name, path in paths.items():
        found = path is not None and path.is_file()
        tools[name] = {"found": found, "path": str(path) if path else None}
        if not found:
            missing.append(name)

    build_tools = root / "build-tools"
    has_aapt2 = False
    if build_tools.is_dir():
        for ver_dir in build_tools.iterdir():
            if _aapt2_in(ver_dir).is_file():
                has_aapt2 = True
                break
    tools["aapt2"] = {"found": has_aapt2, "path": str(build_tools) if has_aapt2 else None}
    if not has_aapt2:
        missing.append("aapt2")

    platform_jar = root / "platforms" / f"android-{config.API_LEVEL}" / "android.jar"
    has_platform = platform_jar.is_file()
    tools["platform"] = {"found": has_platform, "path": str(platform_jar) if has_platform else None}
    if not has_platform:
        missing.append("platform")

    system_image = (
        root / "system-images" / f"android-{config.API_LEVEL}" / "google_apis" / "x86_64"
    )
    has_image = system_image.is_dir()
    tools["system_image"] = {
        "found": has_image,
        "path": str(system_image) if has_image else None,
    }
    if not has_image:
        missing.append("system_image")

    core = {"adb", "emulator", "sdkmanager", "avdmanager"}
    core_missing = [m for m in missing if m in core]
    valid = len(core_missing) == 0
    ready = valid and not any(m in missing for m in ("aapt2", "platform", "system_image"))

    return _validation_result(
        root,
        valid=valid,
        ready=ready,
        source="unknown",
        missing=missing,
        tools=tools,
        is_project_local=is_project_local_sdk(root),
    )


def _validation_result(
    root: Path,
    *,
    valid: bool,
    ready: bool = False,
    source: str,
    missing: list[str],
    tools: dict,
    is_project_local: bool,
) -> dict:
    return {
        "valid": valid,
        "ready": ready,
        "sdk_root": str(root),
        "source": source,
        "missing": missing,
        "tools": tools,
        "is_project_local": is_project_local,
    }


def detect_sdk_candidates() -> list[dict]:
    """Return validated SDK locations, best match first."""
    seen: set[str] = set()
    candidates: list[tuple[str, Path]] = []

    saved = load_saved_sdk_path()
    if saved:
        candidates.append(("saved", saved))

    for source, path in _env_sdk_candidates():
        key = str(path)
        if key not in seen:
            candidates.append((source, path))
            seen.add(key)

    for path in os_default_sdk_locations():
        if path.is_dir():
            key = str(path.resolve())
            if key not in seen:
                candidates.append(("os_default", path.resolve()))
                seen.add(key)

    local = _project_local_sdk()
    key = str(local)
    if key not in seen:
        candidates.append(("project", local))
        seen.add(key)

    results: list[dict] = []
    for source, path in candidates:
        v = validate_sdk(path)
        v["source"] = source
        results.append(v)

    results.sort(key=lambda r: (not r["ready"], not r["valid"], r["source"] != "saved"))
    return results


def resolve_sdk_root() -> Path:
    """
    Resolve SDK root with priority:
    1. Saved .sdk_path.txt
    2. MDT_ANDROID_SDK / ANDROID_SDK_ROOT / ANDROID_HOME
    3. OS default locations (if valid)
    4. Project-local .android-sdk
    """
    saved = load_saved_sdk_path()
    if saved:
        return saved

    for _, path in _env_sdk_candidates():
        return path

    for path in os_default_sdk_locations():
        if path.is_dir():
            v = validate_sdk(path)
            if v["valid"]:
                return path.resolve()

    return _project_local_sdk()


def apply_sdk_root(sdk_root: Path) -> Path:
    """Update config paths and process environment for the resolved SDK."""
    resolved = sdk_root.expanduser().resolve()
    config.ANDROID_SDK_ROOT = resolved
    config.ANDROID_AVD_HOME = resolved / "avd"
    config.ANDROID_AVD_HOME.mkdir(parents=True, exist_ok=True)

    os.environ["ANDROID_SDK_ROOT"] = str(resolved)
    os.environ["ANDROID_HOME"] = str(resolved)
    os.environ["ANDROID_AVD_HOME"] = str(config.ANDROID_AVD_HOME)

    path_dirs: list[str] = []
    for sub in (resolved / "platform-tools", resolved / "emulator"):
        if sub.is_dir():
            path_dirs.append(str(sub))
    cmdline_bin = find_cmdline_bin(resolved, "sdkmanager")
    if cmdline_bin:
        path_dirs.append(str(cmdline_bin.parent))

    existing = os.environ.get("PATH", "")
    if path_dirs:
        os.environ["PATH"] = os.pathsep.join(path_dirs) + os.pathsep + existing

    return resolved


def refresh_tool_paths() -> None:
    """Rebind sdk.py module-level tool paths after ANDROID_SDK_ROOT changes."""
    import app.sdk as sdk_mod

    root = config.ANDROID_SDK_ROOT
    sdk_mod.SDKMANAGER = find_cmdline_bin(root, "sdkmanager") or sdk_mod.sdkmanager_path()
    sdk_mod.AVDMANAGER = find_cmdline_bin(root, "avdmanager") or sdk_mod.avdmanager_path()


def _resolve_source(root: Path) -> str:
    saved = load_saved_sdk_path()
    if saved and str(saved.resolve()) == str(root.resolve()):
        return "saved"
    if is_project_local_sdk(root):
        return "project"
    for src, p in _env_sdk_candidates():
        if str(p.resolve()) == str(root.resolve()):
            return src
    if any(root.resolve() == p.resolve() for p in os_default_sdk_locations() if p.is_dir()):
        return "os_default"
    return "unknown"


def get_sdk_status() -> dict:
    root = resolve_sdk_root()
    apply_sdk_root(root)
    refresh_tool_paths()
    v = validate_sdk(root)
    v["source"] = _resolve_source(root)
    return v


def set_sdk_path(path: str) -> dict:
    p = Path(path).expanduser()
    if not p.is_dir():
        raise ValueError(f"SDK path is not a directory: {path}")
    resolved = save_sdk_path(p)
    apply_sdk_root(resolved)
    refresh_tool_paths()
    return get_sdk_status()
