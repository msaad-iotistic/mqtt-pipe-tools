# MQTT Pipe Tools

Tools for piping data through MQTT brokers, including a Magic Wormhole-like file transfer utility.

## Quick Start

```bash
# Clone and install
git clone https://github.com/Mohammad-Saad-Acacus/mqtt-pipe-tools.git
cd mqtt-pipe-tools
./install.sh

# Send a file (generates pairing code)
mqtt-wormhole --host broker.emqx.io myfile.pdf
# Output: Pairing code: 42-cosmic-dolphin

# Receive on another machine
mqtt-wormhole --host broker.emqx.io --code 42-cosmic-dolphin
```

## Installation

### Automatic (Recommended)
```bash
./install.sh              # Install for current user
sudo ./install.sh         # Install system-wide
./install.sh --uninstall  # Remove
```

The installer will:
- Detect or create a Python virtual environment
- Install dependencies automatically
- Create `mqtt-wormhole` and `mqttcat` commands

### Manual
```bash
pip install -r requirements.txt
```

## mqtt-wormhole

Magic Wormhole-like file transfer over MQTT. Send files between machines using memorable pairing codes.

### Features
- **Pairing codes** like `42-cosmic-dolphin` for easy sharing
- **Progress bars** with speed and ETA
- **Multi-file/directory** support (auto-tarballed)
- **SHA256 checksums** verified on receive
- **Compression** enabled by default (deflate)
- **Encryption** support via mqttcat

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
```

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
| `MQTT_ENCRYPTION_KEY` | End-to-end encryption key |
| `MQTT_COMPRESSION` | Compression (deflate/none) |

## mqttcat

Netcat-like MQTT client for piping data through brokers.

### Usage
```bash
# Listen mode (subscribe)
mqttcat listen my/topic profiles.json profile_name

# Connect mode (publish stdin)
echo "Hello" | mqttcat connect my/topic profiles.json profile_name
```

### Binary Data Example
```bash
# Send image
cat image.jpg | mqttcat connect images/topic profiles.json test

# Receive image
mqttcat listen images/topic profiles.json test > received.jpg
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
