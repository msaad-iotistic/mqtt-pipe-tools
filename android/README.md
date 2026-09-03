# MQTT Pipe — Android (trial)

A **proof-of-concept** Android app that runs the existing `mqtt_forward` TCP tunnel
by embedding the *real* Python engine with [Chaquopy](https://chaquo.com/chaquopy/).
No rewrite: `mqtt_cat.py` + `mqtt_forward.py` + vendored `paho` run as-is; a thin
Kotlin UI and a foreground service drive them.

## What it does
Runs the forwarder in either mode over an MQTT broker:
- **listen**  — opens a local TCP port on the phone (apps hit `127.0.0.1:PORT`), tunnelled to the peer. Needs a pairing code.
- **connect** — connects out to a remote `host:port` and generates the pairing code.

## Prerequisites
- Android Studio (Koala+) with SDK **34**; an emulator (`x86_64`) or a device (`arm64-v8a`).
- First Gradle sync downloads Chaquopy and builds the `cryptography` wheel — allow a few minutes and network access.

## Build & run
0. Populate the embedded engine (gitignored, not duplicated in git): `./sync_python.sh`
1. Open the `android/` folder in Android Studio → let it sync.
2. Run on a device/emulator. (No Gradle wrapper is committed; Android Studio generates it, or run `gradle wrapper` once.)
3. CLI build after the wrapper exists: `./gradlew assembleDebug` → `app/build/outputs/apk/debug/`.

Versions in `build.gradle` (AGP 8.5.2 / Kotlin 1.9.24 / Chaquopy 15.0.1 / Python 3.11)
are conventional — **bump them to match your installed SDK/Studio if sync complains.**

## Encryption
Bundles `cryptography` → **AES-GCM**, interoperable with normal desktop peers. The
AES-GCM and stdlib-fallback wire formats are *not* interoperable (`mqtt_cat.py` docstring),
so peers must also have `cryptography` (they usually do). A wrong/short key fails loudly.

## Keeping the embedded Python in sync
The three modules are **copied** into `app/src/main/python/` (Chaquopy packages real
files reliably; symlinks are iffy). After editing the root `mqtt_cat.py`/`mqtt_forward.py`,
re-copy them:

    ./sync_python.sh

## Verify end-to-end (app ⇄ desktop)
On the desktop, expose a service (e.g. SSH on :22) as the tunnel *server*:

    python mqtt_forward.py --connect 127.0.0.1:22 --broker emqx --code demo-code -e "$(python -c 'print("k"*32)')"

In the app: **listen**, address `127.0.0.1:2222`, broker `emqx`, code `demo-code`,
the same 32-char key → **Start**. Then from the phone (Termux/SSH app) connect to
`127.0.0.1:2222` — you reach the desktop's service through MQTT. Background the app:
the foreground-service notification keeps the tunnel alive; **Stop** tears it down.

> The Python engine + the new `stop_event` were verified on desktop with a real
> loopback tunnel through `broker.emqx.io` (round-trip OK, clean stop). Only the
> Gradle/Chaquopy APK build itself is unverified here (no Android SDK in the dev env).

## Changes to the shared Python
Two small, `# ponytail`-marked edits in `mqtt_forward.py`, behind defaults (CLI unchanged):
- signal handlers wrapped in `try/except ValueError` (off-main-thread registration would crash the service);
- optional `stop_event` on `do_server`/`do_client`/`MuxForwarder` so the service stops the tunnel cleanly.

## Deferred (later phases)
- **Wormhole file transfer** — reusable, but needs the Storage Access Framework and replacing its `SIGALRM` tar-timeout.
- **Background reliability** — Doze/battery-optimization exemptions, reconnect on network change.

## Note on the UI
Built with plain XML views + `AppCompatActivity` instead of Jetpack Compose (the
approved plan said Compose). Same functionality — this just drops the Compose-compiler↔Kotlin
version coupling so the first build is less fragile. Swap to Compose later if wanted.
