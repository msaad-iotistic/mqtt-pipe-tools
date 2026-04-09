#!/usr/bin/env python3
"""
mqtt-wormhole: Magic Wormhole-like file transfer over MQTT

Send files:    mqtt-wormhole myfile.pdf
Receive files: mqtt-wormhole
"""
import argparse
import base64
import hashlib
import io
import json
import logging
import os
import random
import signal
import sys
import tarfile
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None


# Add parent directory to path so we can import mqtt_cat
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mqtt_cat import MQTTNetcat, COMPRESSION_TYPES, COMPRESSION_NONE

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WORDLIST_FILE = os.path.join(SCRIPT_DIR, "wordlist.txt")
ENV_FILE = os.path.join(SCRIPT_DIR, ".env")
DEFAULT_PROFILES_FILE = "/opt/config/mqtt_profiles.json"
DEFAULT_PROFILE_NAME = "iotistic"
DEFAULT_LOG_FILE = os.path.join(SCRIPT_DIR, "mqtt-wormhole.log")
PROTOCOL_VERSION = "2.0"
TOPIC_BASE = "wormhole"

# Logger will be configured in main
logger = logging.getLogger("mqtt-wormhole")

# Framing: each MQTT message is prefixed with a 1-byte type tag
TAG_CONTROL = 0x00  # JSON control message
TAG_DATA = 0x01     # Raw file data

# Control message types
MSG_READY = "ready"
MSG_CHALLENGE = "challenge"
MSG_CHALLENGE_RESPONSE = "challenge_response"
MSG_ACCEPTED = "accepted"
MSG_REJECTED = "rejected"
MSG_METADATA = "metadata"
MSG_ACK = "ack"
MSG_DONE = "done"
MSG_ERROR = "error"


# ─── HELPERS ────────────────────────────────────────────────────────────────

def load_wordlist() -> list:
    """Load wordlist from file for pairing code generation."""
    try:
        with open(WORDLIST_FILE, "r") as f:
            words = [line.strip() for line in f if line.strip()]
        if len(words) < 50:
            raise ValueError("Wordlist too small")
        return words
    except (FileNotFoundError, ValueError):
        return [
            "alpha", "bravo", "charlie", "delta", "echo", "foxtrot",
            "golf", "hotel", "india", "juliet", "kilo", "lima", "mike",
            "november", "oscar", "papa", "quebec", "romeo", "sierra",
            "tango", "uniform", "victor", "whiskey", "xray", "yankee",
            "zulu", "anchor", "breeze", "coral", "drift", "ember",
            "falcon", "granite", "harbor", "ivory", "jungle", "kayak",
            "lantern", "marble", "nebula", "orchid", "prism", "quartz",
            "ripple", "summit", "thunder", "umbrella", "velvet", "willow",
        ]


def generate_code(num_words: int = 2) -> str:
    """Generate a memorable pairing code like '7-guitar-nebula'."""
    words = load_wordlist()
    number = random.randint(1, 99)
    chosen = random.sample(words, num_words)
    return f"{number}-{'-'.join(chosen)}"


def hash_code(code: str) -> str:
    """Hash a pairing code for use in MQTT topics."""
    return hashlib.sha256(code.encode('utf-8')).hexdigest()[:16]


def parse_env_file(filepath: str) -> dict:
    """Parse a .env file into a dict of key=value pairs."""
    env = {}
    try:
        with open(filepath, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip()
                # Strip surrounding quotes
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                    val = val[1:-1]
                env[key] = val
    except Exception:
        pass
    return env


def load_profiles_config() -> dict:
    """Load configuration from mqtt_profiles.json."""
    config = {}
    if not os.path.exists(DEFAULT_PROFILES_FILE):
        return config
    
    try:
        with open(DEFAULT_PROFILES_FILE, "r") as f:
            profiles = json.load(f)
        
        if DEFAULT_PROFILE_NAME in profiles:
            profile = profiles[DEFAULT_PROFILE_NAME]
            # Map profile keys to our config keys
            key_mapping = {
                "host": "host",
                "port": "port",
                "username": "username",
                "password": "password",
                "tls": "tls",
                "insecure": "insecure",
                "ca_certs": "ca_certs",
                "certfile": "certfile",
                "keyfile": "keyfile",
                "encryption_key": "encryption_key",
                "encryption_salt": "encryption_salt",
                "encryption_iterations": "encryption_iterations",
                "qos": "qos",
                "chunk_size": "chunk_size",
                "compression": "compression",
            }
            for profile_key, conf_key in key_mapping.items():
                if profile_key in profile and profile[profile_key]:
                    config[conf_key] = profile[profile_key]
    except Exception:
        pass
    
    return config


def load_env_config() -> dict:
    """Load configuration from .env file, fallback to mqtt_profiles.json."""
    config = {}
    
    # Priority 1: .env file
    if os.path.exists(ENV_FILE):
        env = parse_env_file(ENV_FILE)
        mapping = {
            "MQTT_HOST": "host",
            "MQTT_PORT": "port",
            "MQTT_USERNAME": "username",
            "MQTT_PASSWORD": "password",
            "MQTT_TLS": "tls",
            "MQTT_INSECURE": "insecure",
            "MQTT_CA_CERTS": "ca_certs",
            "MQTT_CERTFILE": "certfile",
            "MQTT_KEYFILE": "keyfile",
            "MQTT_ENCRYPTION_KEY": "encryption_key",
            "MQTT_ENCRYPTION_SALT": "encryption_salt",
            "MQTT_ENCRYPTION_ITERATIONS": "encryption_iterations",
            "MQTT_QOS": "qos",
            "MQTT_CHUNK_SIZE": "chunk_size",
            "MQTT_COMPRESSION": "compression",
            "MQTT_FORCE_OVERWRITE": "force_overwrite",
        }
        for env_key, conf_key in mapping.items():
            val = env.get(env_key)
            if val is not None and val != "":
                config[conf_key] = val
        return config
    
    # Priority 2: mqtt_profiles.json
    return load_profiles_config()


def build_profile(args, env_config: dict) -> dict:
    """Build MQTT profile from args and env config. Args override env."""
    profile = {}

    for key in ["host", "port", "username", "password", "ca_certs", "certfile", "keyfile"]:
        if key in env_config:
            profile[key] = env_config[key]

    for key in ["tls", "insecure", "force_overwrite"]:
        if key in env_config:
            profile[key] = env_config[key].lower() in ("true", "1", "yes")

    if args.host:
        profile["host"] = args.host
    if args.port:
        profile["port"] = int(args.port)
    elif "port" in profile:
        profile["port"] = int(profile["port"])
    if args.username:
        profile["username"] = args.username
    if args.password:
        profile["password"] = args.password
    if args.tls:
        profile["tls"] = True
    if args.insecure:
        profile["insecure"] = True
    if args.ca_certs:
        profile["ca_certs"] = args.ca_certs
    if args.force_overwrite:
        profile["force_overwrite"] = True

    if "host" not in profile:
        print("Error: No MQTT host specified. Use --host or set MQTT_HOST in .env", file=sys.stderr)
        sys.exit(1)
    if "port" not in profile:
        profile["port"] = 8883 if profile.get("tls") else 1883

    return profile


def get_encryption_config(args, env_config: dict, code: str = None) -> dict:
    """Get encryption settings from args/env, with auto-encryption support."""
    enc = {}
    
    # Check for explicit encryption configuration
    explicit_key = args.encryption_key or env_config.get("encryption_key")
    enc["encryption_key"] = explicit_key
    enc["encryption_salt"] = args.encryption_salt or env_config.get("encryption_salt")
    iterations = args.encryption_iterations or env_config.get("encryption_iterations")
    enc["encryption_iterations"] = int(iterations) if iterations else 210000
    
    # Auto-encryption: enabled when no explicit key and not disabled
    enc["auto_encrypt"] = False
    enc["secret"] = args.secret
    enc["key_window"] = args.key_window
    
    if not explicit_key and not args.no_auto_encrypt and code:
        # Enable auto-encryption
        enc["auto_encrypt"] = True
        derived_key, derived_salt = derive_time_based_key(args.secret, code, args.key_window)
        enc["encryption_key"] = derived_key
        enc["encryption_salt"] = derived_salt
        
        # Warn if using default secret
        if args.secret == "secret123":
            logger.warning("Using default secret 'secret123' for auto-encryption. "
                         "For better security, use --secret with a custom value.")
            print("⚠️  Warning: Using default secret for encryption. "
                  "Use --secret for better security.", file=sys.stderr)
        else:
            logger.info(f"Auto-encryption enabled with custom secret")
    
    return enc


def get_transfer_config(args, env_config: dict) -> dict:
    """Get transfer settings from args/env."""
    qos = args.qos if args.qos is not None else int(env_config.get("qos", 1))
    chunk_size = args.chunk_size or int(env_config.get("chunk_size", 65536))
    compress = args.compress or env_config.get("compression", "deflate")
    compression_type = COMPRESSION_TYPES.get(compress, COMPRESSION_NONE)
    return {
        "qos": qos,
        "chunk_size": chunk_size,
        "compression_type": compression_type,
    }


def derive_time_based_key(secret: str, code: str, window_size: int = 1000, time_offset: int = 0) -> tuple:
    """
    Derive encryption key from secret + code + time window.
    
    Args:
        secret: User-provided secret (default: 'secret123')
        code: Pairing code
        window_size: Time window in seconds (default: 1000)
        time_offset: Offset in windows for ±1 tolerance (0, -1, or +1)
    
    Returns:
        (encryption_key, encryption_salt) tuple
    """
    current_time = int(time.time())
    time_window = (current_time // window_size) + time_offset
    
    # Create password from secret + code
    password = f"{secret}-{code}"
    
    # Ensure password meets minimum 32 character requirement for Encryptor
    # Pad with static string if needed (time window is in salt, not password)
    if len(password) < 32:
        # Pad with a static suffix to reach minimum length
        # Use a repeating pattern that doesn't include time_window
        # so sender and receiver with different time windows still match
        padding_needed = 32 - len(password)
        padding = "-mqtt-wormhole-auto-encrypt"[:padding_needed]
        password = password + padding
    
    # Use time_window as salt (convert to 8-byte representation)
    salt = time_window.to_bytes(8, byteorder='big', signed=True)
    
    # Encode salt as base64 for compatibility with existing encryption system
    salt_b64 = base64.b64encode(salt).decode('ascii')
    
    logger.debug(f"Time-based key derivation: window={time_window}, offset={time_offset}, salt={salt_b64}")
    
    return password, salt_b64


def check_path_stability(path: Path, interval: float = 1.0) -> list:
    """Check if files under path are still being written to.
    
    Samples file sizes and mtimes twice, separated by `interval` seconds.
    Returns list of (filepath, reason) tuples for unstable files.
    """
    def snapshot(root: Path):
        snap = {}
        targets = root.rglob("*") if root.is_dir() else [root]
        for f in targets:
            if not f.is_file():
                continue
            try:
                st = f.stat()
                snap[str(f)] = (st.st_size, st.st_mtime)
            except OSError:
                snap[str(f)] = (None, None)
        return snap

    snap1 = snapshot(path)
    time.sleep(interval)
    snap2 = snapshot(path)

    unstable = []
    for fpath, (sz1, mt1) in snap1.items():
        sz2, mt2 = snap2.get(fpath, (None, None))
        if sz1 is None or sz2 is None:
            continue
        if sz1 != sz2:
            unstable.append((fpath, f"size changed: {human_size(sz1)} -> {human_size(sz2)}"))
        elif mt1 != mt2:
            unstable.append((fpath, "modification time changed"))
    # Files that appeared between snapshots
    for fpath in set(snap2) - set(snap1):
        unstable.append((fpath, "new file appeared"))
    return unstable


def wait_for_stable_path(path: Path, max_wait: float = 5.0, check_interval: float = 1.0):
    """Wait until files under path stop changing, or warn and ask user.
    
    Returns True if stable (safe to proceed), False if user chose to abort.
    """
    unstable = check_path_stability(path, interval=check_interval)
    if not unstable:
        return True

    deadline = time.monotonic() + max_wait
    while unstable and time.monotonic() < deadline:
        print(f"\n⚠️  Files still being written to:", file=sys.stderr)
        for fp, reason in unstable[:5]:
            print(f"  - {Path(fp).name}: {reason}", file=sys.stderr)
        if len(unstable) > 5:
            print(f"  ... and {len(unstable) - 5} more", file=sys.stderr)
        remaining = int(deadline - time.monotonic())
        print(f"Waiting for files to stabilize ({remaining}s remaining)...", file=sys.stderr)
        unstable = check_path_stability(path, interval=min(check_interval * 2, 3.0))

    if not unstable:
        print("Files stabilized.", file=sys.stderr)
        return True

    # Still unstable after max_wait
    print(f"\n⚠️  Some files are still being written to after {max_wait:.0f}s:", file=sys.stderr)
    for fp, reason in unstable[:5]:
        print(f"  - {Path(fp).name}: {reason}", file=sys.stderr)
    try:
        response = input("Proceed anyway? [y/N] ").strip().lower()
        return response in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.", file=sys.stderr)
        return False


def safe_tar_add(tar: tarfile.TarFile, filepath: str, arcname: str, timeout: int = 10):
    """Add a file to tar, handling errors and timeouts for files that may block."""
    def _alarm_handler(signum, frame):
        raise TimeoutError(f"Timed out reading {filepath}")

    old_handler = signal.signal(signal.SIGALRM, _alarm_handler)
    signal.alarm(timeout)
    try:
        tar.add(filepath, arcname=arcname)
    except TimeoutError:
        logger.warning(f"Timed out adding {filepath} to archive (>{timeout}s)")
        print(f"  ⚠️  Skipped (timeout): {Path(filepath).name}", file=sys.stderr)
    except (OSError, IOError, tarfile.TarError) as e:
        logger.warning(f"Skipped file {filepath}: {e}")
        print(f"  ⚠️  Skipped: {Path(filepath).name} ({e})", file=sys.stderr)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)


def compute_sha256(filepath: str) -> str:
    """Compute SHA256 hash of a file."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def human_size(num_bytes: float) -> str:
    """Format byte count as human-readable string."""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(num_bytes) < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} PB"


def make_progress_bar(total: int, desc: str):
    """Create a progress bar (tqdm if available, else simple fallback)."""
    if tqdm:
        return tqdm(
            total=total,
            desc=desc,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{rate_fmt}, ETA: {remaining}]",
            file=sys.stderr,
        )
    return SimpleProgress(total, desc)


class SimpleProgress:
    """Fallback progress bar when tqdm is not installed."""

    def __init__(self, total: int, desc: str):
        self.total = total
        self.current = 0
        self.desc = desc
        self.start_time = time.monotonic()
        self._last_print = 0

    def update(self, n: int):
        self.current += n
        now = time.monotonic()
        if now - self._last_print < 0.5 and self.current < self.total:
            return
        self._last_print = now
        elapsed = now - self.start_time
        speed = self.current / elapsed if elapsed > 0 else 0
        pct = (self.current / self.total * 100) if self.total > 0 else 0
        eta = (self.total - self.current) / speed if speed > 0 else 0
        print(
            f"\r{self.desc}: {human_size(self.current)}/{human_size(self.total)} "
            f"({pct:.0f}%) [{human_size(speed)}/s, ETA: {eta:.0f}s]",
            end="", flush=True, file=sys.stderr,
        )

    def close(self):
        print(file=sys.stderr)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# ─── FRAMING PROTOCOL ──────────────────────────────────────────────────────
# Each MQTT message sent by this tool is prefixed with a 1-byte tag:
#   TAG_CONTROL (0x00) + JSON bytes  → control/signalling message
#   TAG_DATA    (0x01) + raw bytes   → file data chunk
# This allows a single MQTTNetcat channel to carry both control and data.

def send_control(client: MQTTNetcat, msg_type: str, payload: dict = None):
    """Send a tagged control message."""
    message = {"type": msg_type}
    if payload:
        message.update(payload)
    data = bytes([TAG_CONTROL]) + json.dumps(message).encode()
    client.send(data)


def send_data_chunk(client: MQTTNetcat, chunk: bytes):
    """Send a tagged data chunk."""
    client.send(bytes([TAG_DATA]) + chunk)


def recv_message(client: MQTTNetcat, timeout: float = 60):
    """Receive one tagged message. Returns (tag, payload) or (None, None) on timeout."""
    raw = client.receive(timeout=timeout)
    if raw is None or len(raw) == 0:
        return None, None
    tag = raw[0]
    body = raw[1:]
    if tag == TAG_CONTROL:
        try:
            return TAG_CONTROL, json.loads(body.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return TAG_CONTROL, None
    elif tag == TAG_DATA:
        return TAG_DATA, body
    else:
        return None, None


def recv_control(client: MQTTNetcat, timeout: float = 60, drain_data=None) -> Optional[dict]:
    """Receive next control message, optionally buffering any data chunks that arrive."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        tag, body = recv_message(client, timeout=min(remaining, 2))
        if tag == TAG_CONTROL and isinstance(body, dict):
            return body
        if tag == TAG_DATA and drain_data is not None:
            drain_data.append(body)


# ─── FILE METADATA ──────────────────────────────────────────────────────────

def collect_file_metadata(paths: list, no_archive: bool = False):
    """Collect metadata for files/directories to transfer. Returns (metadata_dict, tar_path_or_None)."""
    files_meta = []
    tar_path = None

    resolved = []
    for p in paths:
        rp = Path(p).resolve()
        if not rp.exists():
            print(f"Error: '{p}' does not exist", file=sys.stderr)
            sys.exit(1)
        resolved.append(rp)

    # Single directory -> tarball
    if len(resolved) == 1 and resolved[0].is_dir():
        dir_path = resolved[0]
        file_count = sum(1 for _ in dir_path.rglob("*") if _.is_file())
        dir_size = sum(f.stat().st_size for f in dir_path.rglob("*") if f.is_file())
        if no_archive:
            print(f"Archiving directory (no compression): {dir_path.name}/ ({file_count} files, {human_size(dir_size)})",
                  file=sys.stderr)
        else:
            print(f"Compressing directory: {dir_path.name}/ ({file_count} files, {human_size(dir_size)})",
                  file=sys.stderr)

        # Check if files are still being written to
        if not wait_for_stable_path(dir_path):
            print("Aborted.", file=sys.stderr)
            sys.exit(1)

        tar_suffix = ".tar" if no_archive else ".tar.gz"
        tar_mode = "w:" if no_archive else "w:gz"
        tar_fd, tar_path = tempfile.mkstemp(suffix=tar_suffix)
        os.close(tar_fd)
        with tarfile.open(tar_path, tar_mode) as tar:
            # Add files individually so we can handle per-file errors
            tar.add(str(dir_path), arcname=dir_path.name, recursive=False)
            for item in sorted(dir_path.rglob("*")):
                arcname = str(Path(dir_path.name) / item.relative_to(dir_path))
                safe_tar_add(tar, str(item), arcname)

        tar_size = os.path.getsize(tar_path)
        checksum = compute_sha256(tar_path)
        tar_name = f"{dir_path.name}{tar_suffix}"
        files_meta.append({
            "name": tar_name,
            "size": tar_size,
            "checksum": f"sha256:{checksum}",
            "is_archive": True,
            "original_name": dir_path.name,
            "file_count": file_count,
        })
        transfer_type = "directory"
        total_size = tar_size

    # Multiple files -> tarball
    elif len(resolved) > 1:
        multi_size = sum(f.stat().st_size for f in resolved)
        print(f"Bundling {len(resolved)} files ({human_size(multi_size)})", file=sys.stderr)

        # Check stability of each file
        for rp in resolved:
            if not wait_for_stable_path(rp):
                print("Aborted.", file=sys.stderr)
                sys.exit(1)

        tar_suffix = ".tar" if no_archive else ".tar.gz"
        tar_mode = "w:" if no_archive else "w:gz"
        tar_fd, tar_path = tempfile.mkstemp(suffix=tar_suffix)
        os.close(tar_fd)
        with tarfile.open(tar_path, tar_mode) as tar:
            for rp in resolved:
                safe_tar_add(tar, str(rp), rp.name)

        tar_size = os.path.getsize(tar_path)
        checksum = compute_sha256(tar_path)
        files_meta.append({
            "name": f"bundle{tar_suffix}",
            "size": tar_size,
            "checksum": f"sha256:{checksum}",
            "is_archive": True,
            "original_files": [rp.name for rp in resolved],
            "file_count": len(resolved),
        })
        transfer_type = "multi"
        total_size = tar_size
        file_count = len(resolved)

    # Single file
    else:
        fp = resolved[0]
        # Check if file is still being written to
        if not wait_for_stable_path(fp):
            print("Aborted.", file=sys.stderr)
            sys.exit(1)
        size = fp.stat().st_size
        checksum = compute_sha256(str(fp))
        files_meta.append({
            "name": fp.name,
            "size": size,
            "checksum": f"sha256:{checksum}",
            "is_archive": False,
        })
        transfer_type = "single"
        total_size = size
        file_count = 1

    metadata = {
        "version": PROTOCOL_VERSION,
        "transfer_type": transfer_type,
        "files": files_meta,
        "total_size": total_size,
        "file_count": file_count if transfer_type != "single" else 1,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    return metadata, tar_path


def create_client(mode: str, code: str, profile: dict, enc_config: dict,
                  transfer_config: dict, verbose: bool = False) -> MQTTNetcat:
    """Create a single MQTTNetcat instance for the given code."""
    hashed_code = hash_code(code)
    prefix = f"{TOPIC_BASE}/{hashed_code}"
    return MQTTNetcat(
        mode=mode,
        prefix=prefix,
        profile=profile,
        qos=transfer_config["qos"],
        chunk_size=transfer_config["chunk_size"],
        compression_type=transfer_config["compression_type"],
        verbose=verbose,
        quiet=True,
        encryption_key=enc_config.get("encryption_key"),
        encryption_salt=enc_config.get("encryption_salt"),
        encryption_iterations=enc_config.get("encryption_iterations", 210000),
    )


# ─── SEND MODE ──────────────────────────────────────────────────────────────

def do_send(args, env_config: dict):
    """Handle sending files."""
    profile = build_profile(args, env_config)
    
    code = args.code or generate_code()
    enc_config = get_encryption_config(args, env_config, code)
    transfer_config = get_transfer_config(args, env_config)

    logger.info(f"Starting send mode with code: {code}")
    if enc_config.get("auto_encrypt"):
        logger.info(f"Auto-encryption enabled (window: {enc_config['key_window']}s)")

    # Collect file metadata and prepare tarball if needed
    metadata, tar_path = collect_file_metadata(args.files, no_archive=args.no_archive)
    total_size = metadata["total_size"]
    send_path = tar_path if tar_path else str(Path(args.files[0]).resolve())
    logger.info(f"Prepared files: {metadata['file_count']} file(s), total size: {human_size(total_size)}")

    print(file=sys.stderr)
    print(f"Your pairing code is: \033[1;36m{code}\033[0m", file=sys.stderr)
    if enc_config.get("auto_encrypt"):
        if enc_config.get("secret") == "secret123":
            print(f"🔒 Auto-encryption: enabled (default secret)", file=sys.stderr)
        else:
            print(f"🔒 Auto-encryption: enabled (custom secret)", file=sys.stderr)
    print(file=sys.stderr)
    print(f"On the receiving end, run:", file=sys.stderr)
    if enc_config.get("auto_encrypt") and enc_config.get("secret") != "secret123":
        print(f"  \033[1mmqtt-wormhole --code {code} --secret {enc_config['secret']}\033[0m", file=sys.stderr)
    else:
        print(f"  \033[1mmqtt-wormhole --code {code}\033[0m", file=sys.stderr)
    print(file=sys.stderr)

    # Sender uses "connect" mode (publishes to prefix/listen, subscribes to prefix/connect)
    logger.info(f"Connecting to broker: {profile.get('host', 'default')}:{profile.get('port', 1883)}")
    client = create_client("connect", code, profile, enc_config, transfer_config, verbose=args.verbose)

    cleanup_done = False

    def cleanup():
        nonlocal cleanup_done
        if cleanup_done:
            return
        cleanup_done = True
        try:
            client.disconnect()
        except Exception:
            pass
        if tar_path and os.path.exists(tar_path):
            os.unlink(tar_path)

    def signal_handler(sig, frame):
        print("\nInterrupted. Cleaning up...", file=sys.stderr)
        cleanup()
        sys.exit(1)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Track authentication attempts across connection retries
    auth_attempts = 0
    max_attempts = 3
    
    try:
        client.connect()
        time.sleep(0.5)

        # Wait for receiver READY
        print("Waiting for receiver to connect...", file=sys.stderr)
        logger.info("Waiting for receiver READY message")
        while True:
            msg = recv_control(client, timeout=3600)
            if msg and msg.get("type") == MSG_READY:
                print("Receiver connected!", file=sys.stderr)
                logger.info("Receiver connected and ready")
                break

        # Challenge-response authentication
        if enc_config.get("auto_encrypt"):
            print("Authenticating receiver...", file=sys.stderr)
            logger.info("Starting challenge-response authentication")
            
            # Generate random cleartext nonce
            nonce = base64.b64encode(os.urandom(24)).decode('ascii')
            logger.debug(f"Generated nonce: {nonce}")
            
            # Send challenge
            send_control(client, MSG_CHALLENGE, {"nonce": nonce})
            
            # Wait for challenge response
            response_msg = recv_control(client, timeout=60)
            if response_msg is None or response_msg.get("type") != MSG_CHALLENGE_RESPONSE:
                print("Error: No challenge response from receiver. Aborting.", file=sys.stderr)
                logger.error("No challenge response received")
                cleanup()
                sys.exit(1)
            
            encrypted_nonce = response_msg.get("encrypted_nonce")
            if not encrypted_nonce:
                print("Error: Invalid challenge response. Aborting.", file=sys.stderr)
                logger.error("Challenge response missing encrypted_nonce")
                cleanup()
                sys.exit(1)
            
            # Try to decrypt with ±1 windows and verify
            verified = False
            successful_offset = None
            
            for offset in [0, -1, 1]:
                try:
                    # Derive key for this window
                    derived_key, derived_salt = derive_time_based_key(
                        enc_config['secret'], code, enc_config['key_window'], offset
                    )
                    
                    # Create encryptor
                    salt_bytes = base64.b64decode(derived_salt)
                    from mqtt_cat import Encryptor
                    encryptor = Encryptor(
                        password=derived_key,
                        salt=salt_bytes,
                        iterations=enc_config.get("encryption_iterations", 210000)
                    )
                    
                    # Try to decrypt
                    aad = f"{TOPIC_BASE}/{code}".encode()
                    encrypted_bytes = base64.b64decode(encrypted_nonce)
                    decrypted = encryptor.decrypt(encrypted_bytes, aad)
                    decrypted_nonce = decrypted.decode('ascii')
                    
                    if decrypted_nonce == nonce:
                        verified = True
                        successful_offset = offset
                        logger.info(f"Authentication successful with window offset {offset}")
                        break
                        
                except Exception as e:
                    logger.debug(f"Decryption failed with offset {offset}: {e}")
                    continue
            
            if verified:
                # Send ACCEPTED with window offset
                send_control(client, MSG_ACCEPTED, {"window_offset": successful_offset})
                print("✓ Authentication successful!", file=sys.stderr)
            else:
                # Authentication failed
                auth_attempts += 1
                attempts_remaining = max_attempts - auth_attempts
                
                logger.warning(f"Authentication failed. Attempts: {auth_attempts}/{max_attempts}")
                print(f"✗ Authentication failed. Attempts remaining: {attempts_remaining}", file=sys.stderr)
                
                # Send REJECTED
                send_control(client, MSG_REJECTED, {
                    "attempts_remaining": attempts_remaining,
                    "final": auth_attempts >= max_attempts
                })
                
                cleanup()
                
                if auth_attempts >= max_attempts:
                    print(f"\nMaximum authentication attempts ({max_attempts}) reached. Exiting.", file=sys.stderr)
                    logger.error("Maximum authentication attempts reached, exiting")
                    sys.exit(1)
                else:
                    print("\nReceiver can retry with correct secret.", file=sys.stderr)
                    sys.exit(1)
        
        # Send metadata
        print("Sending file info...", file=sys.stderr)
        logger.info(f"Sending metadata: {metadata['files'][0]['name']}")
        send_control(client, MSG_METADATA, metadata)

        # Wait for ACK (receiver accepted)
        while True:
            msg = recv_control(client, timeout=60)
            if msg is None:
                print("Error: Receiver did not acknowledge. Aborting.", file=sys.stderr)
                cleanup()
                sys.exit(1)
            if msg.get("type") == MSG_ACK:
                break
            if msg.get("type") == MSG_ERROR:
                print(f"Receiver error: {msg.get('message', 'unknown')}", file=sys.stderr)
                cleanup()
                sys.exit(1)

        # Stream file data
        file_info = metadata["files"][0]
        desc = f"Sending {file_info['name']}"
        chunk_size = transfer_config["chunk_size"]
        logger.info(f"Starting file transfer: {file_info['name']} ({human_size(total_size)})")

        with open(send_path, "rb") as f, make_progress_bar(total_size, desc) as pbar:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                send_data_chunk(client, chunk)
                pbar.update(len(chunk))
                # Brief throttle for QoS 0 to avoid flooding
                if transfer_config["qos"] == 0:
                    time.sleep(0.001)

        # Signal transfer complete
        logger.info("File transfer complete, sending DONE message")
        time.sleep(0.5)
        send_control(client, MSG_DONE)

        # Wait for final confirmation
        msg = recv_control(client, timeout=30)
        if msg and msg.get("type") == MSG_ACK:
            if msg.get("checksum_ok"):
                print("Transfer complete! Checksum verified.", file=sys.stderr)
                logger.info("Transfer successful, checksum verified by receiver")
            else:
                print("Transfer complete! (checksum mismatch on receiver)", file=sys.stderr)
                logger.warning("Transfer complete but checksum mismatch on receiver")
        else:
            print("Transfer sent (no final confirmation from receiver).", file=sys.stderr)
            logger.warning("No final confirmation received from receiver")

    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        logger.info("Send interrupted by user")
    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        logger.error(f"Send failed with error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        cleanup()


# ─── RECEIVE MODE ───────────────────────────────────────────────────────────

def do_receive(args, env_config: dict):
    """Handle receiving files."""
    profile = build_profile(args, env_config)
    
    code = args.code
    if not code:
        try:
            code = input("Enter pairing code: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.", file=sys.stderr)
            sys.exit(1)
    if not code:
        print("Error: No pairing code provided.", file=sys.stderr)
        sys.exit(1)

    enc_config = get_encryption_config(args, env_config, code)
    transfer_config = get_transfer_config(args, env_config)

    logger.info(f"Starting receive mode with code: {code}")
    if enc_config.get("auto_encrypt"):
        logger.info(f"Auto-encryption enabled (window: {enc_config['key_window']}s)")
    output_dir = Path(args.output).resolve() if args.output else Path.cwd()
    output_dir.mkdir(parents=True, exist_ok=True)
    logger.info(f"Output directory: {output_dir}")

    print(f"Connecting to broker...", file=sys.stderr)
    if enc_config.get("auto_encrypt"):
        if enc_config.get("secret") == "secret123":
            print(f"🔒 Auto-encryption: enabled (default secret)", file=sys.stderr)
        else:
            print(f"🔒 Auto-encryption: enabled (custom secret)", file=sys.stderr)
    logger.info(f"Connecting to broker: {profile.get('host', 'default')}:{profile.get('port', 1883)}")

    # Receiver uses "listen" mode (publishes to prefix/connect, subscribes to prefix/listen)
    client = create_client("listen", code, profile, enc_config, transfer_config, verbose=args.verbose)

    cleanup_done = False

    def cleanup():
        nonlocal cleanup_done
        if cleanup_done:
            return
        cleanup_done = True
        try:
            client.disconnect()
        except Exception:
            pass

    def signal_handler(sig, frame):
        print("\nInterrupted. Cleaning up...", file=sys.stderr)
        cleanup()
        sys.exit(1)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        client.connect()
        time.sleep(0.5)

        # Send READY and wait for challenge or metadata
        print("Waiting for sender...", file=sys.stderr)
        logger.info("Sending READY messages")
        last_ready = 0
        challenge_received = False
        metadata = None

        # Send READY until we get a response, then wait for metadata
        while metadata is None:
            now = time.monotonic()
            if not challenge_received and now - last_ready >= 2:
                send_control(client, MSG_READY)
                last_ready = now

            msg = recv_control(client, timeout=2)
            if msg is None:
                continue
                
            msg_type = msg.get("type")
            
            # Handle challenge-response authentication
            if msg_type == MSG_CHALLENGE and enc_config.get("auto_encrypt"):
                challenge_received = True
                nonce = msg.get("nonce")
                
                if not nonce:
                    print("Error: Invalid challenge from sender.", file=sys.stderr)
                    logger.error("Challenge missing nonce")
                    cleanup()
                    sys.exit(1)
                
                logger.info("Received authentication challenge")
                print("Authenticating with sender...", file=sys.stderr)
                
                # Encrypt nonce with current time window key
                try:
                    derived_key, derived_salt = derive_time_based_key(
                        enc_config['secret'], code, enc_config['key_window'], 0
                    )
                    
                    salt_bytes = base64.b64decode(derived_salt)
                    from mqtt_cat import Encryptor
                    encryptor = Encryptor(
                        password=derived_key,
                        salt=salt_bytes,
                        iterations=enc_config.get("encryption_iterations", 210000)
                    )
                    
                    # Encrypt the nonce
                    aad = f"{TOPIC_BASE}/{code}".encode()
                    nonce_bytes = nonce.encode('ascii')
                    encrypted = encryptor.encrypt(nonce_bytes, aad)
                    encrypted_b64 = base64.b64encode(encrypted).decode('ascii')
                    
                    # Send challenge response
                    send_control(client, MSG_CHALLENGE_RESPONSE, {"encrypted_nonce": encrypted_b64})
                    logger.debug("Sent challenge response")
                    
                    # Wait for ACCEPTED or REJECTED
                    auth_result = recv_control(client, timeout=60)
                    if auth_result is None:
                        print("Error: No authentication response from sender.", file=sys.stderr)
                        logger.error("No authentication response received")
                        cleanup()
                        sys.exit(1)
                    
                    if auth_result.get("type") == MSG_ACCEPTED:
                        window_offset = auth_result.get("window_offset", 0)
                        logger.info(f"Authentication successful! Window offset: {window_offset}")
                        print("✓ Authentication successful!", file=sys.stderr)
                        
                        # Update encryptor to use the correct window offset
                        if window_offset != 0:
                            derived_key, derived_salt = derive_time_based_key(
                                enc_config['secret'], code, enc_config['key_window'], window_offset
                            )
                            salt_bytes = base64.b64decode(derived_salt)
                            client.userdata["encryptor"] = Encryptor(
                                password=derived_key,
                                salt=salt_bytes,
                                iterations=enc_config.get("encryption_iterations", 210000)
                            )
                        
                        # Authentication successful, now wait for metadata
                        # Don't break here - continue in the loop to receive metadata
                        challenge_received = True
                        
                    elif auth_result.get("type") == MSG_REJECTED:
                        attempts_remaining = auth_result.get("attempts_remaining", 0)
                        is_final = auth_result.get("final", False)
                        
                        logger.warning(f"Authentication rejected. Attempts remaining: {attempts_remaining}")
                        print(f"✗ Authentication failed. Attempts remaining: {attempts_remaining}", file=sys.stderr)
                        
                        if is_final:
                            print("\nMaximum authentication attempts reached. Sender has exited.", file=sys.stderr)
                        else:
                            print("\nPlease check your --secret and try again.", file=sys.stderr)
                        
                        cleanup()
                        sys.exit(1)
                    
                except Exception as e:
                    print(f"Error during authentication: {e}", file=sys.stderr)
                    logger.error(f"Authentication error: {e}", exc_info=True)
                    cleanup()
                    sys.exit(1)
            
            # Handle metadata (for non-auto-encrypt or after successful auth)
            elif msg_type == MSG_METADATA:
                metadata = msg
                logger.info(f"Received metadata: {metadata['files'][0]['name']}")
                break

        # Display file info
        file_info = metadata["files"][0]
        total_size = metadata["total_size"]
        transfer_type = metadata.get("transfer_type", "single")
        logger.info(f"Transfer type: {transfer_type}, size: {human_size(total_size)}")

        print(file=sys.stderr)
        if transfer_type == "directory":
            print(f"Receiving directory: {file_info.get('original_name', file_info['name'])}/ "
                  f"({file_info.get('file_count', '?')} files, {human_size(total_size)})",
                  file=sys.stderr)
        elif transfer_type == "multi":
            names = file_info.get("original_files", [])
            print(f"Receiving {len(names)} files ({human_size(total_size)}):", file=sys.stderr)
            for n in names[:10]:
                print(f"  - {n}", file=sys.stderr)
            if len(names) > 10:
                print(f"  ... and {len(names) - 10} more", file=sys.stderr)
        else:
            print(f"Receiving: {file_info['name']} ({human_size(total_size)})", file=sys.stderr)
        print(file=sys.stderr)

        # Check for existing file and handle overwrite confirmation early
        out_file = output_dir / file_info["name"]
        force_overwrite = profile.get("force_overwrite", False)
        
        if out_file.exists() and not force_overwrite:
            print(f"File '{out_file.name}' already exists!", file=sys.stderr)
            while True:
                try:
                    response = input("Overwrite? [y=overwrite/N=cancel/r=rename] ").strip().lower()
                    if response in ["", "n", "no"]:
                        print("Transfer cancelled by user.", file=sys.stderr)
                        cleanup()
                        sys.exit(0)
                    elif response in ["y", "yes"]:
                        break
                    elif response in ["r", "rename"]:
                        while True:
                            try:
                                new_name = input("Enter new filename: ").strip()
                                if not new_name:
                                    print("Filename cannot be empty.", file=sys.stderr)
                                    continue
                                # Validate for path traversal and invalid characters
                                if '/' in new_name or '\\' in new_name or '\0' in new_name:
                                    print("Invalid filename: cannot contain path separators.", file=sys.stderr)
                                    continue
                                if new_name.startswith('.'):
                                    print("Invalid filename: cannot start with a dot.", file=sys.stderr)
                                    continue
                                new_out_file = output_dir / new_name
                                if new_out_file.exists():
                                    print(f"File '{new_name}' also exists. Please choose another name.", file=sys.stderr)
                                    continue
                                out_file = new_out_file
                                print(f"Will save as: {out_file.name}", file=sys.stderr)
                                break
                            except (EOFError, KeyboardInterrupt):
                                print("\nTransfer cancelled.", file=sys.stderr)
                                cleanup()
                                sys.exit(0)
                        break
                    else:
                        print("Please enter 'y', 'n', or 'r'", file=sys.stderr)
                except (EOFError, KeyboardInterrupt):
                    print("\nTransfer cancelled.", file=sys.stderr)
                    cleanup()
                    sys.exit(0)
        elif out_file.exists() and force_overwrite:
            print(f"Will overwrite existing file: {out_file.name}", file=sys.stderr)

        # Accept transfer
        send_control(client, MSG_ACK)
        logger.info("Sent ACK, starting data reception")

        # Receive data chunks
        desc = f"Receiving {file_info['name']}"
        received_data = io.BytesIO()
        received_bytes = 0
        done = False

        with make_progress_bar(total_size, desc) as pbar:
            while not done:
                # After first successful decrypt, we can use normal recv_message
                # since the client's encryptor is already set to the correct key
                tag, body = recv_message(client, timeout=30)
                if tag is None:
                    continue
                if tag == TAG_DATA and body:
                    received_data.write(body)
                    received_bytes += len(body)
                    pbar.update(len(body))
                elif tag == TAG_CONTROL and isinstance(body, dict):
                    if body.get("type") == MSG_DONE:
                        done = True
                    elif body.get("type") == MSG_ERROR:
                        print(f"\nSender error: {body.get('message', 'unknown')}", file=sys.stderr)
                        cleanup()
                        sys.exit(1)

        # Drain any remaining data chunks that arrived after DONE
        while True:
            tag, body = recv_message(client, timeout=0.5)
            if tag is None:
                break
            if tag == TAG_DATA and body:
                received_data.write(body)
                received_bytes += len(body)

        # Verify checksum
        received_data.seek(0)
        h = hashlib.sha256()
        while True:
            block = received_data.read(65536)
            if not block:
                break
            h.update(block)
        actual_checksum = f"sha256:{h.hexdigest()}"
        expected_checksum = file_info.get("checksum", "")
        checksum_ok = actual_checksum == expected_checksum

        if checksum_ok:
            print("Checksum verified!", file=sys.stderr)
            logger.info(f"Checksum verified: {actual_checksum}")
        else:
            print("Warning: Checksum mismatch!", file=sys.stderr)
            print(f"  Expected: {expected_checksum}", file=sys.stderr)
            print(f"  Got:      {actual_checksum}", file=sys.stderr)
            logger.warning(f"Checksum mismatch! Expected: {expected_checksum}, Got: {actual_checksum}")

        # Save file
        received_data.seek(0)
        with open(str(out_file), "wb") as f:
            while True:
                block = received_data.read(65536)
                if not block:
                    break
                f.write(block)

        # Extract archive if needed
        if file_info.get("is_archive") and (str(out_file).endswith(".tar.gz") or str(out_file).endswith(".tar")):
            print("Extracting archive...", file=sys.stderr)
            try:
                tar_mode = "r:gz" if str(out_file).endswith(".tar.gz") else "r:"
                with tarfile.open(str(out_file), tar_mode) as tar:
                    tar.extractall(path=str(output_dir))
                os.unlink(str(out_file))
                if transfer_type == "directory":
                    print(f"Extracted to: {output_dir / file_info.get('original_name', '')}/",
                          file=sys.stderr)
                else:
                    print(f"Extracted {file_info.get('file_count', '?')} files to: {output_dir}/",
                          file=sys.stderr)
            except Exception as e:
                print(f"Warning: Extraction failed ({e}), archive saved as: {out_file}",
                      file=sys.stderr)
        else:
            print(f"Saved to: {out_file}", file=sys.stderr)

        # Send final ACK with checksum result
        send_control(client, MSG_ACK, {"checksum_ok": checksum_ok})
        print("Transfer complete!", file=sys.stderr)
        logger.info(f"Transfer complete, saved to: {out_file}")

    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        logger.info("Receive interrupted by user")
    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        logger.error(f"Receive failed with error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        cleanup()


# ─── CLI ────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mqtt-wormhole",
        description="Magic Wormhole-like file transfer over MQTT",
        epilog=(
            "Examples:\n"
            "  Send:    mqtt-wormhole myfile.pdf\n"
            "  Send:    mqtt-wormhole --code mycode myfile.pdf  # fixed code for scripts\n"
            "  Receive: mqtt-wormhole --code 7-guitar-nebula\n"
            "  Send:    mqtt-wormhole --host broker.example.com myfile.pdf\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "files", nargs="*", default=[],
        help="File(s) or directory to send. If omitted, enters receive mode.",
    )

    mode_group = parser.add_argument_group("Mode")
    mode_group.add_argument(
        "--receive", "-r", action="store_true",
        help="Force receive mode (even if files are provided)",
    )
    mode_group.add_argument(
        "--code", "-c", type=str, default=None,
        help="Pairing code (auto-generated if omitted for send, required for receive)",
    )
    mode_group.add_argument(
        "--output", "-o", type=str, default=None,
        help="Output directory for received files (default: current directory)",
    )

    broker_group = parser.add_argument_group("Broker")
    broker_group.add_argument("--host", "-H", type=str, help="MQTT broker host")
    broker_group.add_argument("--port", "-P", type=str, help="MQTT broker port")
    broker_group.add_argument("--username", "-u", type=str, help="MQTT username")
    broker_group.add_argument("--password", "-p", type=str, help="MQTT password")
    broker_group.add_argument("--tls", action="store_true", help="Enable TLS")
    broker_group.add_argument("--insecure", action="store_true", help="Allow insecure TLS")
    broker_group.add_argument("--ca-certs", type=str, help="CA certificate file")

    enc_group = parser.add_argument_group("Encryption")
    enc_group.add_argument("--encryption-key", "-e", type=str, help="Encryption key")
    enc_group.add_argument("--encryption-salt", type=str, help="Encryption salt (base64)")
    enc_group.add_argument("--encryption-iterations", type=int, help="PBKDF2 iterations")
    enc_group.add_argument("--secret", "-s", type=str, default="secret123", 
                          help="Secret for auto-encryption key derivation (default: 'secret123')")
    enc_group.add_argument("--key-window", type=int, default=1000,
                          help="Time window in seconds for auto-encryption key validity (default: 1000)")
    enc_group.add_argument("--no-auto-encrypt", action="store_true",
                          help="Disable automatic encryption (when no explicit key is configured)")

    xfer_group = parser.add_argument_group("Transfer")
    xfer_group.add_argument("--qos", type=int, choices=[0, 1, 2], default=None, help="QoS level (default: 1)")
    xfer_group.add_argument("--chunk-size", type=int, default=None, help="Chunk size in bytes (default: 65536)")
    xfer_group.add_argument("--compress", choices=list(COMPRESSION_TYPES.keys()), default=None, help="Compression")
    xfer_group.add_argument("--no-archive", action="store_true", help="Skip gzip compression when archiving directories (use plain tar, faster for large/active dirs)")
    xfer_group.add_argument("--force-overwrite", action="store_true", help="Automatically overwrite existing files without confirmation (bypasses overwrite/rename prompt)")

    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    parser.add_argument("--log-file", type=str, default=None, help=f"Log file path (default: {DEFAULT_LOG_FILE})")

    return parser


def setup_logging(log_file: str, verbose: bool = False):
    """Configure logging to file and optionally console."""
    log_level = logging.DEBUG if verbose else logging.INFO
    
    # Create formatter
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # File handler
    file_handler = logging.FileHandler(log_file, mode='a')
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)
    
    # Configure logger
    logger.setLevel(log_level)
    logger.addHandler(file_handler)
    
    # Add console handler if verbose
    if verbose:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setLevel(logging.DEBUG)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)


def main():
    parser = build_parser()
    args = parser.parse_args()

    # Setup logging
    log_file = args.log_file if args.log_file else DEFAULT_LOG_FILE
    setup_logging(log_file, args.verbose)
    logger.info("="*60)
    logger.info(f"mqtt-wormhole started (version {PROTOCOL_VERSION})")

    env_config = load_env_config()

    is_receive = args.receive or (len(args.files) == 0)

    if is_receive:
        do_receive(args, env_config)
    else:
        do_send(args, env_config)


if __name__ == "__main__":
    main()
