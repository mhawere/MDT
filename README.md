# Multi-Device Tester (MDT)

MDT is a Windows-native tool for running up to 3 Android emulators side by side, installing one APK into each, showing live video in the browser, and recording color-coded logs.

## Quick Start

1. Put your `.apk` files in `apk_input/`.
2. Run `start.bat` from PowerShell or Command Prompt.
3. Open `http://localhost:8000` if the browser does not open automatically.

On first run MDT downloads the Android SDK command-line tools and required emulator packages into `.android-sdk/`.

## Browser Requirement

Use Chrome or Edge. The video pane uses WebCodecs `VideoDecoder`, which is required for smooth H.264 playback.

## Performance / Acceleration

MDT uses the Windows emulator backend. If performance looks bad, enable WHPX in an elevated PowerShell and reboot:

```powershell
Enable-WindowsOptionalFeature -Online -FeatureName HypervisorPlatform -All
```

If the emulator still feels slow, verify that virtualization is enabled in BIOS/UEFI and that Windows is fully rebooted after changing hypervisor features.

## Logs and Files

- APKs: `apk_input/`
- Live trails: `logs/`
- SDK: `.android-sdk/`
- Python venv: `.venv/`

## Device Controls

Each device card includes live video, tap/swipe input, text entry, Back/Home/Recents, app restart, reinstall, reboot, rotate, and screenshot save.

## Configuration

Edit `config.py` to change device count, emulator RAM, boot timeout, stream size, and bitrate.

## Shutdown

Press `Ctrl+C` in the terminal running `start.bat`. MDT stops all emulators and logcat processes cleanly.
