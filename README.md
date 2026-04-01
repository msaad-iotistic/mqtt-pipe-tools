# MQTT Pipe Tools

Tools for piping data through MQTT brokers, including a Magic Wormhole-like file transfer utility.

## Quick Start

### One-Line Install
```bash
curl -sSL https://raw.githubusercontent.com/msaad-iotistic/mqtt-pipe-tools/main/scripts/quick-install.sh | bash
```

This will clone to `~/.local/share/mqtt-pipe-tools`, install dependencies, and create the commands.

### Usage
```bash
# Send a file (generates pairing code)
mqtt-wormhole --host broker.emqx.io myfile.pdf
# Output: Pairing code: 42-cosmic-dolphin

# Receive on another machine
mqtt-wormhole --host broker.emqx.io --code 42-cosmic-dolphin
```

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
- Install dependencies automatically
- Create `mqtt-wormhole` and `mqtt-cat` commands

### Dependencies Only
```bash
pip install -r requirements.txt
```

## mqtt-wormhole

Magic Wormhole-like file transfer over MQTT. Send files between machines using memorable pairing codes.

### Features
- **Pairing codes** like `42-cosmic-dolphin` for easy sharing
- **Auto-encryption** enabled by default (time-based key derivation)
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
```

### Auto-Encryption

By default, mqtt-wormhole automatically encrypts transfers when no explicit encryption key is configured. This provides security without additional setup.

**How it works:**
- Encryption key is derived from: `secret + pairing_code + time_window`
- Default secret is `'secret123'` (provides basic security)
- Time windows prevent replay attacks (default: 1000 seconds ≈ 16 minutes)
- Receiver tries ±1 time window to handle clock skew (total ~50 minutes validity)

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

### Configuration

Configuration is loaded in this order (first found wins):
1. Command-line arguments
2. `.env` file in script directory
3. `/opt/config/mqtt_profiles.json` (profile: `iotistic`)

#### .env file
```bash
cp .env.example .env
# Edit with your broker details
```

#### Environment Variables
| Variable | Description |
|----------|-------------|
| `MQTT_HOST` | Broker hostname |
| `MQTT_PORT` | Broker port (default: 1883) |
| `MQTT_USERNAME` | Authentication username |
| `MQTT_PASSWORD` | Authentication password |
| `MQTT_TLS` | Enable TLS (true/false) |
| `MQTT_ENCRYPTION_KEY` | Manual end-to-end encryption key (disables auto-encryption) |
| `MQTT_COMPRESSION` | Compression (deflate/none) |

**Auto-encryption CLI options:**
| Option | Description |
|--------|-------------|
| `--secret` | Secret for auto-encryption (default: 'secret123') |
| `--key-window` | Time window in seconds for key validity (default: 1000) |
| `--no-auto-encrypt` | Disable automatic encryption |

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
