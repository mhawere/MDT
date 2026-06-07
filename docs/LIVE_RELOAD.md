# Live Reload

MDT **Live Reload** watches your Android build output and pushes updates to running emulators via `adb` — so you can iterate without manually reinstalling APKs or restarting MDT.

Each of the two emulators has an **independent toggle** in the UI (Screen tab → **Live Reload** strip at the bottom of the device card).

## How it works

```
┌─────────────┐     poll + debounce      ┌──────────────────┐
│ Watch path  │ ───────────────────────► │ live_reload.py   │
│ (.apk/file) │                            │                  │
└─────────────┘                            │  APK mode:       │
                                           │  adb install -r  │
                                           │  force-stop +    │
                                           │  relaunch app    │
                                           │                  │
                                           │  Assets mode:    │
                                           │  adb push files  │
                                           └────────┬─────────┘
                                                    │
                                                    ▼
                                           ┌──────────────────┐
                                           │ Emulator (adb)   │
                                           └──────────────────┘
```

1. You enable Live Reload on **device 0**, **device 1**, or **both**.
2. MDT polls the watch path every **1 s** (configurable in `config.py`).
3. When a file changes, MDT waits **1.5 s** (debounce) so Gradle/build tools can finish writing.
4. **APK mode** (default): `adb install -r -g` then force-stop + relaunch the app.
5. **Assets mode**: `adb push` changed files to a remote path on the device.
6. Status updates stream over the device WebSocket (`type: live_reload`).

## UI controls

| Control | Action |
| --- | --- |
| **Live Reload** checkbox | Enable/disable watching for this device only |
| **Path** | Set watch path (APK file or folder) |
| **Sync** | Manual sync while watching |
| Status line | `Watching`, `Syncing…`, `Error`, last sync time |

## REST API

| Method | Endpoint | Description |
| --- | --- | --- |
| `GET` | `/api/device/{index}/live-reload/status` | Current state |
| `POST` | `/api/device/{index}/live-reload/enable` | Start watching |
| `POST` | `/api/device/{index}/live-reload/disable` | Stop watching |
| `POST` | `/api/device/{index}/live-reload/sync` | Sync immediately |

### Enable body (JSON)

```json
{
  "watch_path": "/path/to/app-debug.apk",
  "mode": "apk",
  "remote_path": null
}
```

- **`watch_path`** — APK file, APK output directory, or assets folder. If omitted, uses the device's current APK path.
- **`mode`** — `"apk"` (default) or `"assets"`.
- **`remote_path`** — Required for `"assets"` mode (e.g. `/sdcard/Android/data/com.example.app/files`).

## Gradle / Android Studio workflow

### Standard debug APK rebuild

1. In Android Studio or your project root, note the debug APK path:
   ```
   app/build/outputs/apk/debug/app-debug.apk
   ```
2. Start MDT with your APK in `apk_input/` (or point **APK Folder** at your build output dir).
3. On the device card, click **Path** and paste the Gradle output path above.
4. Enable **Live Reload**.
5. In Android Studio: **Build → Make Project** or run `./gradlew assembleDebug`.
6. MDT detects the changed APK, reinstalls, and relaunches automatically.

### Watch the whole output folder

If your APK name varies, point at the directory:

```
app/build/outputs/apk/debug/
```

MDT picks the APK matching the device's current filename, or the first `*.apk` sorted alphabetically.

### Asset push (advanced)

For apps that read loose files from storage (not typical for production APKs):

```json
POST /api/device/0/live-reload/enable
{
  "watch_path": "./my-assets/",
  "mode": "assets",
  "remote_path": "/sdcard/myapp/assets"
}
```

## Configuration

In `config.py`:

| Setting | Default | Description |
| --- | --- | --- |
| `LIVE_RELOAD_POLL_SEC` | `1.0` | How often to scan for file changes |
| `LIVE_RELOAD_DEBOUNCE_SEC` | `1.5` | Delay after change before adb sync |

## Framework notes

| Framework | Live Reload support |
| --- | --- |
| **Native Android (Gradle)** | ✅ Full — watch `app-debug.apk` |
| **React Native** | ⚠️ Partial — rebuild debug APK; Metro hot reload is separate (not integrated) |
| **Flutter** | ⚠️ Partial — `flutter build apk --debug` then watch output APK; Flutter hot reload not integrated |
| **Expo** | ❌ Not supported — use Expo dev client / Metro separately |

MDT implements **APK hot-swap** and **asset push**, not framework-specific dev servers (Metro, Flutter VM service, etc.). Those require the app to run in debug/dev mode with their own tooling.

## Limitations

- **Polling, not inotify** — 1 s scan interval; very fast successive builds may coalesce into one sync (debounced).
- **Full reinstall for APK mode** — not incremental DEX hot-swap; suitable for debug builds, not instant RN/Flutter hot reload.
- **Device must be `running`** — enable fails if the emulator is still booting or in error.
- **Independent per device** — each emulator can watch a different path or only one can have reload enabled.
- **Windows paths** — use forward slashes or escaped backslashes in API JSON; UI prompt accepts normal Windows paths.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Status stays **Error** | Check last error tooltip; verify path exists and is readable |
| Changes not detected | Ensure you're editing the watched file, not a different build variant |
| Install succeeds but app old | Confirm package name matches; try manual **Reinstall** once |
| Gradle writes partial APK | Increase `LIVE_RELOAD_DEBOUNCE_SEC` to 2–3 s |

See also the [local testing section in README](../README.md#live-reload-local-testing).
