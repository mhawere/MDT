"""
avd.py — Create and list AVDs using avdmanager.
"""
from __future__ import annotations

import subprocess

import config
from app.sdk import ABI, _sdk_env, avdmanager_path


def avd_exists(name: str) -> bool:
    """Check if an AVD with the given name already exists."""
    avd_dir = config.ANDROID_AVD_HOME / f"{name}.avd"
    ini     = config.ANDROID_AVD_HOME / f"{name}.ini"
    return avd_dir.exists() and ini.exists()


def create_avd(index: int) -> str:
    """
    Create AVD mdt_{index} if it doesn't exist.
    Returns the AVD name.
    """
    name = f"mdt_{index}"
    if avd_exists(name):
        print(f"[MDT] AVD {name} already exists, reusing ✓")
        return name

    config.ANDROID_AVD_HOME.mkdir(parents=True, exist_ok=True)

    pkg = f"system-images;android-{config.API_LEVEL};google_apis;{ABI}"
    cmd = [
        str(avdmanager_path()), "create", "avd",
        "-n", name,
        "-k", pkg,
        "-d", config.DEVICE_PROFILE,
        "--force",
    ]
    env = _sdk_env()
    result = subprocess.run(
        cmd,
        env=env,
        input="no\n",   # decline hardware profile question
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"avdmanager failed for {name}:\n{result.stderr}"
        )
    print(f"[MDT] AVD {name} created ✓")
    return name


def ensure_avd(index: int) -> str:
    """Idempotent: create AVD if needed, return its name."""
    return create_avd(index)
