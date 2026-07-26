# MQTT Pipe Tools

Tools for piping data through MQTT brokers, including a Magic Wormhole-like file transfer utility and a TCP tunnel (mqtt-forward).

## Quick Start

### One-Line Install
```bash
curl -sSL https://raw.githubusercontent.com/msaad-iotistic/mqtt-pipe-tools/main/scripts/quick-install.sh | bash
```

This will clone to `~/.local/share/mqtt-pipe-tools`, install dependencies, and create the commands.

### Usage
```bash
# Send a file (generates pairing code) using a built-in public broker preset
mqtt-wormhole --broker emqx myfile.pdf
# Output: Pairing code: 42-cosmic-dolphin

# Receive on another machine
mqtt-wormhole --broker emqx --code 42-cosmic-dolphin
```

`--broker` (`-b`) selects a built-in public broker so no config is needed:
`emqx` (broker.emqx.io), `mosquitto` (test.mosquitto.org), `eclipse`
(mqtt.eclipseprojects.io). You can still use `--host`/`--port` directly, or set up
a `.env`/profile for a private broker. The same flag works for `mqtt-forward`.

## Installation

### One-Line Install (Recommended)
```bash
curl -sSL https://raw.githubusercontent.com/msaad-iotistic/mqtt-pipe-tools/main/scripts/quick-install.sh | bash
```

**Custom location:**
```bash
MQTT_TOOLS_DIR=~/tools/mqtt-pipe-tools bash <(curl -sSL https://raw.githubusercontent.com/msaad-iotistic/mqtt-pipe-tools/main/scripts/quick-install.sh)
```

### Manual Install
```bash
git clone https://github.com/msaad-iotistic/mqtt-pipe-tools.git
cd mqtt-pipe-tools
./install.sh              # Install for current user
sudo ./install.sh         # Install system-wide
./install.sh --uninstall  # Remove
```

The installer will:
- Detect or create a Python virtual environment
- Install optional packages best-effort (never required)
- Create `mqtt-wormhole`, `mqtt-cat`, and `mqtt-forward` commands

### Dependencies

The tools run with **no pip-installed dependencies**: `paho-mqtt` is vendored under
`_vendor/`, so they work even on minimal/embedded systems where `pip` is broken or
absent. The following packages are **optional** enhancements:

```bash
pip install -r requirements-optional.txt   # optional, never required
```

| Package | Effect when present | Effect when absent |
|---|---|---|
| `cryptography` | AES-GCM encryption | Auto-encryption uses a built-in stdlib fallback; an explicit `--encryption-key` is rejected with a clear error |
| `tqdm` | Rich progress bars | Simple text progress fallback |

> **Encryption interop:** auto-encryption requires **both peers to have matching
> `cryptography` availability**. A peer using AES-GCM and a peer using the stdlib
> fallback cannot talk to each other — the mismatch surfaces loudly as an
> authentication failure, never as silent corruption.
>
> To bridge a mismatch, force both peers onto the same scheme:
> - `--force-fallback-encryption` — use the built-in stdlib scheme even if
>   `cryptography` is installed (so a crypto-capable peer can talk to one without it).
> - `--allow-insecure-encryption` — allow an explicit `--encryption-key` to use the
>   fallback scheme when `cryptography` is missing, instead of erroring out.

## mqtt-wormhole

Magic Wormhole-like file transfer over MQTT. Send files between machines using memorable pairing codes.

### Features
- **Pairing codes** like `42-cosmic-dolphin` for easy sharing
- **Challenge-response authentication** prevents unauthorized access
- **Auto-encryption** enabled by default (time-based key derivation)
- **Brute force protection** with 3-attempt limit
- **Progress bars** with speed and ETA
- **Multi-file/directory** support (auto-tarballed)
- **SHA256 checksums** verified on receive
- **Compression** enabled by default (deflate)
- **Manual encryption** support via mqtt-cat

### Usage

```bash
# Send a file
mqtt-wormhole myfile.pdf

# Send a directory
mqtt-wormhole ./my-folder/

# Send multiple files
mqtt-wormhole file1.txt file2.jpg

# Send with a fixed code (useful for scripts/non-interactive shells)
mqtt-wormhole --code mycode myfile.pdf

# Receive (prompts for code)
mqtt-wormhole

# Receive with known code
mqtt-wormhole --code 42-cosmic-dolphin

# Receive to specific directory
mqtt-wormhole --code 42-cosmic-dolphin --output ~/Downloads/

# Specify broker
mqtt-wormhole --host broker.example.com --port 8883 --tls myfile.pdf

# Use custom secret for auto-encryption (recommended)
mqtt-wormhole --secret mysecret123 myfile.pdf
# Receiver must use the same secret:
mqtt-wormhole --code 42-cosmic-dolphin --secret mysecret123

# Disable auto-encryption
mqtt-wormhole --no-auto-encrypt myfile.pdf

# Adjust time window for key validity (default: 1000 seconds)
mqtt-wormhole --key-window 2000 myfile.pdf

# Handle existing files
mqtt-wormhole --code 42-cosmic-dolphin  # Will prompt if file exists
mqtt-wormhole --code 42-cosmic-dolphin --force-overwrite  # Auto-overwrite without prompt
```

### Auto-Encryption with Challenge-Response Authentication

By default, mqtt-wormhole automatically encrypts transfers when no explicit encryption key is configured. This provides security without additional setup.

**How it works:**
- Encryption key is derived from: `secret + pairing_code + time_window`
- Default secret is `'secret123'` (provides basic security)
- Time windows prevent replay attacks (default: 1000 seconds ≈ 16 minutes)
- **Challenge-response authentication** verifies receiver has correct key before sending files
- Sender tries ±1 time window tolerance to handle clock skew
- **3-attempt limit** prevents brute force attacks

**Authentication Protocol:**
1. Receiver sends READY
2. Sender sends cleartext challenge (random nonce)
3. Receiver encrypts nonce with their key and responds
4. Sender decrypts response (tries ±1 windows) and verifies
5. If correct: transfer proceeds
6. If incorrect: connection terminates (allows retry with fixed secret)
7. After 3 failed attempts: sender exits (code unusable)

**Security benefits:**
- Unauthorized receivers never see file metadata
- Brute force prevention with 3-attempt limit
- Early authentication before any data transfer
- Clean retry mechanism for fixing typos

**Security recommendations:**
```bash
# Use a custom secret for better security
mqtt-wormhole --secret "my-strong-secret-phrase" myfile.pdf

# On receiver (must use same secret):
mqtt-wormhole --code 42-cosmic-dolphin --secret "my-strong-secret-phrase"
```

**Disable auto-encryption:**
```bash
mqtt-wormhole --no-auto-encrypt myfile.pdf
```

**Note:** Auto-encryption is only enabled when you haven't configured `MQTT_ENCRYPTION_KEY` in your `.env` file or profiles. Explicit encryption keys always take precedence.

**Protocol version:** the wire protocol is `3.0` (challenge-response auth in 2.0; windowed cumulative ACK + go-back-N retransmission in 3.0). Both peers must run the same protocol version — a mismatch is rejected at handshake, not silently.

### File Overwrite Handling

When receiving a file that already exists, mqtt-wormhole provides three options:

```
File 'document.pdf' already exists!
Overwrite? [y=overwrite/N=cancel/r=rename] 
```

**Options:**
- **`y`** - Overwrite the existing file
- **`n`** or **Enter** - Cancel the transfer
- **`r`** - Enter a new filename

**Rename Flow:**
```
Enter new filename: document_v2.pdf
Will save as: document_v2.pdf
```

**Security Features:**
- Path traversal prevention (blocks `/` and `\`)
- Null byte injection prevention
- Hidden file prevention (blocks names starting with `.`)
- Validates that the new filename doesn't already exist

**Automatic Overwrite:**
```bash
# CLI flag to auto-overwrite without prompting
mqtt-wormhole --code 42-cosmic-dolphin --force-overwrite

# Environment variable for persistent behavior
echo "MQTT_FORCE_OVERWRITE=true" >> .env
```

### Configuration

Broker config is resolved in this order (first found wins):
1. Command-line flags (`--host`, `--port`, `--username`, …)
2. `--broker NAME` preset (bypasses `.env`/profiles; individual CLI flags still override it)
3. `.env` file in script directory
4. `/opt/config/mqtt_profiles.json` (profile: `iotistic`)

Passing `--profiles-file` or `--profile` explicitly bypasses the `.env` file and loads
from the profiles file directly.

#### .env file
```bash
cp .env.example .env
# Edit with your broker details
```

#### Environment Variables
| Variable | Description |
|----------|-------------|
| `MQTT_HOST` | Broker hostname |
| `MQTT_PORT` | Broker port (default: 1883, or 8883 with TLS) |
| `MQTT_USERNAME` | Authentication username |
| `MQTT_PASSWORD` | Authentication password |
| `MQTT_TLS` | Enable TLS (true/false) |
| `MQTT_INSECURE` | Allow insecure TLS, skip cert verification (true/false) |
| `MQTT_CA_CERTS` | CA certificate file path |
| `MQTT_ENCRYPTION_KEY` | Manual end-to-end encryption key (disables auto-encryption) |
| `MQTT_ENCRYPTION_SALT` | Encryption salt (base64) |
| `MQTT_ENCRYPTION_ITERATIONS` | PBKDF2 iterations |
| `MQTT_QOS` | QoS level 0/1/2 (default: 1) |
| `MQTT_CHUNK_SIZE` | Chunk size in bytes (default: 65536) |
| `MQTT_ACK_WINDOW` | Chunks sent before waiting for a receiver ack (default: 64) |
| `MQTT_COMPRESSION` | Compression (deflate/none) |
| `MQTT_FORCE_OVERWRITE` | Auto-overwrite existing files without confirmation (true/false) |

**Auto-encryption CLI options:**
| Option | Description |
|--------|-------------|
| `--secret` | Secret for auto-encryption (default: 'secret123') |
| `--key-window` | Time window in seconds for key validity (default: 1000) |
| `--no-auto-encrypt` | Disable automatic encryption |

**Transfer CLI options:**
| Option | Description |
|--------|-------------|
| `--qos` | QoS level 0/1/2 (default: 1) |
| `--chunk-size` | Chunk size in bytes (default: 65536) |
| `--ack-window` | Chunks sent before waiting for a receiver ack, for flow control (default: 64) |
| `--compress` | Compression: `none`/`deflate` |
| `--force-overwrite` | Auto-overwrite existing files without confirmation |

## mqtt-forward

TCP tunnel over MQTT — expose a local TCP service (e.g. SSH) to another machine
through an MQTT broker, no port forwarding or public IP required.

### Usage
```bash
# Server: run where the real service lives, generates a pairing code
mqtt-forward --connect localhost:22
# Output: Generated pairing code: 42-cosmic-dolphin

# Client: run on the machine that wants to reach it
mqtt-forward --listen 2222 --code 42-cosmic-dolphin

# Then, on the client machine:
ssh -p 2222 localhost
```

`--broker`/`--host`/`--secret`/encryption options work the same as mqtt-wormhole.
Additional tunnel controls:

| Option | Description |
|--------|-------------|
| `--rate-limit` | Max bytes/sec over MQTT, e.g. `500k`, `2m` (default: `500k`) |
| `--max-pub-rate` | Max MQTT publishes/sec (default: 10) |
| `--max-connections` | Max concurrent TCP connections (default: 10) |

### Running as a systemd service

`systemd/mqtt-forward@.service` runs the `--connect` (server) side persistently —
one instance per exposed service:

```bash
sudo cp systemd/mqtt-forward@.service /etc/systemd/system/
sudo mkdir -p /etc/mqtt-forward
sudo cp systemd/example.env /etc/mqtt-forward/ssh.env   # edit TARGET/CODE
sudo systemctl enable --now mqtt-forward@ssh
```

The unit's `ExecStart` path and `User` are set for a specific machine/account —
edit both to match your install location before enabling.

## mqtt-cat

Netcat-like MQTT client for piping data through brokers.

### Usage
```bash
# Listen mode (subscribe)
mqtt-cat listen my/topic profiles.json profile_name

# Connect mode (publish stdin)
echo "Hello" | mqtt-cat connect my/topic profiles.json profile_name
```

### Binary Data Example
```bash
# Send image
cat image.jpg | mqtt-cat connect images/topic profiles.json test

# Receive image
mqtt-cat listen images/topic profiles.json test > received.jpg
```

## Features
- **Binary-safe** data handling
- **Profile-based** configuration
- **QoS 0/1/2** support
- **TLS/SSL** encryption
- **End-to-end encryption** (AES-GCM)
- **Compression** (deflate)
- **Chunking** for large payloads
- **Clean shutdown** on SIGINT/SIGTERM

## License

MIT
