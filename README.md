<h1 align="center">Triplet</h1>

<p align="center"><b>Test three Android apps side-by-side, in your browser, with one command.</b></p>

<p align="center">
  <img src="https://img.shields.io/badge/platform-Windows%2010%2F11-0078D6?logo=windows&logoColor=white" alt="Windows">
  <img src="https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/backend-FastAPI-009688?logo=fastapi&logoColor=white" alt="FastAPI">
  <img src="https://img.shields.io/badge/streaming-H.264%20%2F%20WebCodecs-ff5252" alt="H.264 / WebCodecs">
  <img src="https://img.shields.io/badge/license-MIT-blue" alt="MIT">
</p>

---

Triplet boots up to **three headless Android emulators**, installs one APK into each, and streams them **live and fully interactive, side-by-side in your browser**. Each app leaves behind its own **colour-coded log trail** — green for OK, yellow for warnings, red for errors — both live in the UI and saved to disk.

Drop your APKs in a folder, run one command, and test. No physical phones, no Android Studio, no manual SDK setup, no Docker.

<p align="center">
  <img src="docs/screenshot.png" alt="Triplet running three emulators side-by-side" width="100%">
</p>

> Add your own screenshot at `docs/screenshot.png`.

## Features

- **Three emulators, side-by-side** — one APK per emulator, fully isolated so each app's state and logs stay clean.
- **Smooth H.264 video** — screens stream as real H.264 decoded on your GPU via the browser's WebCodecs API and painted to a `<canvas>`. Not screenshot polling.
- **Full interaction** — tap, swipe, type, and Back / Home / Recents, mapped straight through to each device.
- **Colour-coded log trails** — per-app logcat parsed by level into green / yellow / red, live-tailed in the UI and persisted to `logs/` as both JSONL and raw `.log`.
- **Per-device controls** — restart app, reinstall, reboot device, rotate, save screenshot.
- **One command, self-contained** — the Python venv and the entire Android SDK live inside the project folder. Nothing is installed globally.
- **Quickboot** — emulators snapshot on exit, so every run after the first warms up in seconds.

## Requirements

- **Windows 10 or 11**
- **Python 3.11+** (on `PATH` as `py` or `python`)
- **Hardware acceleration (WHPX)** — required for usable emulator speed. Enable it once in an **admin** PowerShell, then reboot:
  ```powershell
  Enable-WindowsOptionalFeature -Online -FeatureName HypervisorPlatform -All
  ```
- **A Chromium browser** — Chrome or Edge. WebCodecs `VideoDecoder` is required for the H.264 stream.
- **~6–12 GB free RAM** for three emulators running at once.
- **Internet on first run** — Triplet downloads the Android command-line tools and a system image into `.android-sdk/` automatically.

## Quick start

```bash
git clone https://github.com/<you>/triplet.git
cd triplet
.\start.bat
```

`start.bat` creates the virtual environment, installs dependencies, bootstraps the Android SDK if needed, boots the emulators, and opens your browser at **http://localhost:8000**.

The **first run is slow** — it downloads the SDK + system image and cold-boots the emulators. Every run after that is fast.

## Usage

1. Drop up to **three** `.apk` files into the `apk_input/` folder.
2. Run `.\start.bat`.
3. Watch each pane boot → install → launch, then test away.

Logs for the session are written to `logs/` as `<apk-name>_<timestamp>.jsonl` and `<apk-name>_<timestamp>.log`.

If `apk_input/` is empty, the server still starts and waits — just add APKs and restart.

## Configuration

Everything tunable lives in **`config.py`**:

| Setting | Default | Description |
| --- | --- | --- |
| `API_LEVEL` | `34` | Android system image API level. |
| `DEVICE_PROFILE` | `pixel_5` | AVD hardware profile. |
| `MAX_DEVICES` | `3` | Max emulators / APKs at once. |
| `EMULATOR_MEMORY_MB` | `4096` | RAM per emulator (auto-lowered under memory pressure). |
| `EMULATOR_GPU_MODE` | `host` | Use the real GPU. |
| `SCREENRECORD_SIZE` | `720x1560` | Video resolution (keep the device aspect ratio; `None` = native). |
| `SCREENRECORD_BITRATE` | `8_000_000` | H.264 bitrate in bits/sec. |
| `SERVER_PORT` | `8000` | Local web UI port. |

Lower `SCREENRECORD_SIZE` / `SCREENRECORD_BITRATE` or `EMULATOR_MEMORY_MB` if you're tight on resources.

## How it works

FastAPI orchestrates each device's full lifecycle in the background: **ensure AVD → boot headless emulator → install APK → launch → start logcat + screen stream.** Per-device failures are isolated, so one bad APK won't take down the others.

Each device gets a multiplexed WebSocket. The server streams raw H.264 (from `adb screenrecord`) and parsed log events down it; the browser decodes the video with WebCodecs and sends tap / swipe / text / key commands back up, which the server injects via `adb input`. `screenrecord` has a 180-second hard limit, so Triplet transparently respawns the stream — you'll see at most a sub-second blip every few minutes.

## Project structure

```
triplet/
├── start.bat            # one-command launcher
├── run.py               # entrypoint: env setup → SDK bootstrap → server → browser
├── config.py            # all paths, ports, and tunables
├── requirements.txt
├── app/
│   ├── main.py          # FastAPI app, routes, WebSockets, orchestration
│   ├── sdk.py           # Android SDK bootstrap + accel check
│   ├── avd.py           # AVD create / reuse
│   ├── emulator.py      # headless emulator launch + boot wait
│   ├── device.py        # adb wrappers: install, launch, input, screenrecord
│   ├── apk.py           # scan apk_input, resolve package names
│   ├── logs.py          # logcat capture, level→colour, trail writer
│   ├── streamer.py      # per-device H.264 stream broadcaster
│   └── state.py         # in-memory device state + WebSocket registry
├── static/              # index.html, style.css, app.js (vanilla, WebCodecs decoder)
├── apk_input/           # drop .apk files here
├── logs/                # per-app log trails (runtime)
└── .android-sdk/        # project-local SDK (auto-created)
```

## Known limitations

- **Windows only.** Acceleration relies on WHPX.
- **Chromium only.** The video path needs WebCodecs (Chrome / Edge).
- **Resource-heavy.** Three emulators want real RAM and CPU.
- **Stream latency ~0.5–1s.** `screenrecord` buffers; fine for functional testing, not for timing-sensitive work.
- **Emulators only.** No physical-device support in this version.

## Roadmap

- scrcpy-based stream for sub-200ms latency (drop-in to the existing WebCodecs decoder)
- Physical-device support over `adb`
- Side-by-side log diffing across the three apps

## Tech stack

Python · FastAPI · Uvicorn · WebSockets · vanilla HTML/CSS/JS · WebCodecs · Android SDK / emulator / adb. No framework, no build step, no TypeScript, no Docker, no database.

## License

MIT — see [`LICENSE`](LICENSE).

---

<p align="center"><sub>Built by NOX Tech Solutions.</sub></p>
