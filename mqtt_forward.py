#!/usr/bin/env python3
"""
mqtt-forward: TCP tunnel over MQTT with buffer-and-burst

Server: mqtt-forward --listen :8080
Client: mqtt-forward --connect host:22 --code 42-cosmic-dolphin
"""
import argparse
import base64
import hashlib
import json
import logging
import os
import random
import select
import shlex
import signal
import socket
import sys
import time
import uuid
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mqtt_cat import (MQTTNetcat, COMPRESSION_TYPES, COMPRESSION_NONE, Encryptor,
                      HAVE_CRYPTOGRAPHY, BUILTIN_PROFILES, set_force_fallback)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WORDLIST_FILE = os.path.join(SCRIPT_DIR, "wordlist.txt")
ENV_FILE = os.path.join(SCRIPT_DIR, ".env")
DEFAULT_PROFILES_FILE = "/opt/config/mqtt_profiles.json"
DEFAULT_PROFILE_NAME = "iotistic"
DEFAULT_LOG_FILE = os.path.join(SCRIPT_DIR, "mqtt-forward.log")
PROTOCOL_VERSION = "1.0"
TOPIC_BASE = "forward"

logger = logging.getLogger("mqtt-forward")

# Framing
TAG_CONTROL = 0x00
TAG_DATA = 0x01

# Control message types
MSG_READY = "ready"
MSG_CHALLENGE = "challenge"
MSG_CHALLENGE_RESPONSE = "challenge_response"
MSG_ACCEPTED = "accepted"
MSG_REJECTED = "rejected"
MSG_CONNECTED = "connected"
MSG_DISCONNECT = "disconnect"
MSG_ERROR = "error"
MSG_PING = "ping"          # heartbeat
MSG_PONG = "pong"          # heartbeat reply
MSG_BYE = "bye"            # graceful peer shutdown
MSG_BUSY = "busy"          # server already has a connected client

# Presence / heartbeat timing (seconds)
PING_INTERVAL = 10
PEER_TIMEOUT = 30          # declare peer dead after this long with no message


# ─── HELPERS ────────────────────────────────────────────────────────────────

def load_wordlist() -> list:
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
    words = load_wordlist()
    number = random.randint(1, 99)
    chosen = random.sample(words, num_words)
    return f"{number}-{'-'.join(chosen)}"


def hash_code(code: str) -> str:
    return hashlib.sha256(code.encode('utf-8')).hexdigest()[:16]


def parse_rate(s: str) -> float:
    """Parse a rate string with optional k/m/g suffix to bytes (e.g. '500k' → 512000.0)."""
    s = s.strip().lower()
    if s.endswith('g'):
        return float(s[:-1]) * 1024 ** 3
    if s.endswith('m'):
        return float(s[:-1]) * 1024 ** 2
    if s.endswith('k'):
        return float(s[:-1]) * 1024
    return float(s)


def parse_env_file(filepath: str) -> dict:
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
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                    val = val[1:-1]
                env[key] = val
    except Exception:
        pass
    return env


def load_profiles_config(profiles_file: str = DEFAULT_PROFILES_FILE,
                         profile_name: str = DEFAULT_PROFILE_NAME,
                         strict: bool = False) -> dict:
    """Load one named profile from a profiles JSON file.

    When strict, a missing file or missing profile is a fatal error (used when the
    user explicitly requested a file/profile). Otherwise returns {} so the caller
    can fall back to other config sources.
    """
    config = {}
    if not os.path.exists(profiles_file):
        if strict:
            print(f"Error: Profiles file not found: {profiles_file}", file=sys.stderr)
            sys.exit(1)
        return config
    try:
        with open(profiles_file, "r") as f:
            profiles = json.load(f)
        if profile_name not in profiles:
            if strict:
                print(f"Error: Profile '{profile_name}' not found in {profiles_file}. "
                      f"Available: {', '.join(profiles) or '(none)'}", file=sys.stderr)
                sys.exit(1)
            return config
        profile = profiles[profile_name]
        key_mapping = {
            "host": "host", "port": "port", "username": "username",
            "password": "password", "tls": "tls", "insecure": "insecure",
            "ca_certs": "ca_certs", "certfile": "certfile", "keyfile": "keyfile",
            "encryption_key": "encryption_key", "encryption_salt": "encryption_salt",
            "encryption_iterations": "encryption_iterations",
            "qos": "qos", "chunk_size": "chunk_size", "compression": "compression",
        }
        for profile_key, conf_key in key_mapping.items():
            if profile_key in profile and profile[profile_key]:
                config[conf_key] = profile[profile_key]
    except SystemExit:
        raise
    except Exception as e:
        if strict:
            print(f"Error: Failed to read profiles file {profiles_file}: {e}", file=sys.stderr)
            sys.exit(1)
    return config


def load_env_config(args=None) -> dict:
    """Resolve broker config. Precedence: explicit --profiles-file/--profile >
    .env file > default profiles file.
    """
    profiles_file = getattr(args, "profiles_file", None)
    profile_name = getattr(args, "profile", None)
    if profiles_file or profile_name:
        return load_profiles_config(
            profiles_file or DEFAULT_PROFILES_FILE,
            profile_name or DEFAULT_PROFILE_NAME,
            strict=True,
        )

    config = {}
    if os.path.exists(ENV_FILE):
        env = parse_env_file(ENV_FILE)
        mapping = {
            "MQTT_HOST": "host", "MQTT_PORT": "port", "MQTT_USERNAME": "username",
            "MQTT_PASSWORD": "password", "MQTT_TLS": "tls", "MQTT_INSECURE": "insecure",
            "MQTT_CA_CERTS": "ca_certs", "MQTT_CERTFILE": "certfile", "MQTT_KEYFILE": "keyfile",
            "MQTT_ENCRYPTION_KEY": "encryption_key", "MQTT_ENCRYPTION_SALT": "encryption_salt",
            "MQTT_ENCRYPTION_ITERATIONS": "encryption_iterations",
            "MQTT_QOS": "qos", "MQTT_CHUNK_SIZE": "chunk_size", "MQTT_COMPRESSION": "compression",
        }
        for env_key, conf_key in mapping.items():
            val = env.get(env_key)
            if val is not None and val != "":
                config[conf_key] = val
        return config
    return load_profiles_config()


def build_profile(args, env_config: dict) -> dict:
    """Build MQTT profile. Precedence: CLI flags > --broker preset > env config."""
    profile = {}
    # An explicit --broker preset bypasses ambient env config entirely (only CLI
    # flags below override it); otherwise fall back to .env / profiles config.
    if getattr(args, "broker", None):
        if args.broker not in BUILTIN_PROFILES:
            print(f"Error: Unknown broker '{args.broker}'. Available: "
                  f"{', '.join(BUILTIN_PROFILES)}", file=sys.stderr)
            sys.exit(1)
        profile.update(BUILTIN_PROFILES[args.broker])
    else:
        for key in ["host", "port", "username", "password", "ca_certs", "certfile", "keyfile"]:
            if key in env_config:
                profile[key] = env_config[key]
        for key in ["tls", "insecure"]:
            if key in env_config:
                val = env_config[key]
                # .env values are strings; profiles-file values are native JSON bools.
                profile[key] = val if isinstance(val, bool) else str(val).lower() in ("true", "1", "yes")
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
    if "host" not in profile:
        broker_list = ", ".join("{}={}".format(n, p["host"]) for n, p in BUILTIN_PROFILES.items())
        print(
            "Error: No MQTT broker specified. Provide one of the following "
            "(highest priority first):\n"
            "\n"
            "  1. CLI flags         --host HOST [--port PORT] [--username U] [--password P] [--tls]\n"
            "  2. Built-in preset   --broker NAME   (public, no-auth brokers)\n"
            "                       " + broker_list + "\n"
            "  3. .env file         set MQTT_HOST (and MQTT_PORT, etc.) in " + ENV_FILE + "\n"
            "  4. Profiles file     --profiles-file PATH   (default: " + DEFAULT_PROFILES_FILE + ")\n"
            "                       --profile NAME         (default: " + DEFAULT_PROFILE_NAME + ")\n"
            "\n"
            "Note: --broker bypasses .env/profiles config; individual CLI flags override --broker.\n"
            "Example:  mqtt-forward --broker emqx --listen :8080",
            file=sys.stderr,
        )
        sys.exit(1)
    if "port" not in profile:
        profile["port"] = 8883 if profile.get("tls") else 1883
    return profile


def derive_time_based_key(secret: str, code: str, window_size: int = 1000, time_offset: int = 0) -> tuple:
    current_time = int(time.time())
    time_window = (current_time // window_size) + time_offset
    password = f"{secret}-{code}"
    if len(password) < 32:
        padding_needed = 32 - len(password)
        padding = "-mqtt-forward-auto-encrypt"[:padding_needed]
        password = password + padding
    salt = time_window.to_bytes(8, byteorder='big', signed=True)
    salt_b64 = base64.b64encode(salt).decode('ascii')
    logger.debug(f"Time-based key derivation: window={time_window}, offset={time_offset}")
    return password, salt_b64


def get_encryption_config(args, env_config: dict, code: str = None) -> dict:
    enc = {}
    explicit_key = args.encryption_key or env_config.get("encryption_key")

    # An explicit user-supplied key requires real AES-GCM (cryptography). Auto-encryption
    # can fall back to the stdlib scheme, but an explicit key must not be silently downgraded
    # unless the user explicitly opts in (--allow-insecure-encryption / --force-fallback-encryption).
    allow_insecure = getattr(args, "allow_insecure_encryption", False) or getattr(args, "force_fallback_encryption", False)
    if explicit_key and not HAVE_CRYPTOGRAPHY:
        if not allow_insecure:
            print(
                "Error: --encryption-key requires the 'cryptography' package, which is not "
                "installed.\nInstall it (pip install cryptography), or pass --allow-insecure-encryption "
                "to use the weaker built-in fallback scheme, or omit --encryption-key to use "
                "auto-encryption.",
                file=sys.stderr,
            )
            sys.exit(1)
        logger.warning("Using explicit --encryption-key with the weaker stdlib fallback "
                       "(cryptography not installed; --allow-insecure-encryption set).")
        print("⚠️  Warning: using --encryption-key with the built-in fallback scheme "
              "(weaker than AES-GCM). The peer must use the same.", file=sys.stderr)

    enc["encryption_key"] = explicit_key
    enc["encryption_salt"] = args.encryption_salt or env_config.get("encryption_salt")
    iterations = args.encryption_iterations or env_config.get("encryption_iterations")
    enc["encryption_iterations"] = int(iterations) if iterations else 210000
    enc["auto_encrypt"] = False
    enc["secret"] = args.secret
    enc["key_window"] = args.key_window
    if not explicit_key and not args.no_auto_encrypt and code:
        enc["auto_encrypt"] = True
        derived_key, derived_salt = derive_time_based_key(args.secret, code, args.key_window)
        enc["encryption_key"] = derived_key
        enc["encryption_salt"] = derived_salt
        # Warn when falling back to the weaker stdlib scheme (cryptography missing).
        if not HAVE_CRYPTOGRAPHY:
            logger.warning("cryptography library not found — using built-in stdlib "
                           "encryption fallback (weaker than AES-GCM). The peer must "
                           "also lack cryptography for the tunnel to succeed.")
            print("⚠️  Warning: 'cryptography' not installed — using built-in fallback "
                  "encryption (weaker than AES-GCM). Install it with: pip install cryptography",
                  file=sys.stderr)
        if args.secret == "secret123":
            logger.warning("Using default secret 'secret123' for auto-encryption. "
                         "For better security, use --secret with a custom value.")
            print("⚠️  Warning: Using default secret for encryption. "
                  "Use --secret for better security.", file=sys.stderr)
        else:
            logger.info("Auto-encryption enabled with custom secret")
    return enc


def get_transfer_config(args, env_config: dict) -> dict:
    # Default QoS 0 (fire-and-forget): lowest broker load, no in-flight window
    # pressure under concurrent multiplexed connections. Relies on TCP ordering
    # within the MQTT session for stream integrity. Override via --qos or MQTT_QOS.
    qos = args.qos if args.qos is not None else int(env_config.get("qos", 0))
    chunk_size = args.chunk_size or int(env_config.get("chunk_size", 65536))
    compress = args.compress or "none"
    compression_type = COMPRESSION_TYPES.get(compress, COMPRESSION_NONE)
    return {"qos": qos, "chunk_size": chunk_size, "compression_type": compression_type}


def build_client_command(args, code: str, enc_config: dict) -> str:
    """Build the suggested client command, mirroring the server's interop-relevant
    options (broker, encryption, non-default compression). Reproduces only flags the
    user explicitly passed on the CLI; server-only / operational options (--connect,
    --rate-limit, --qos, --chunk-size, etc.) are omitted. The listen port is the
    client's own choice, so it stays a :PORT placeholder.
    """
    parts = ["mqtt-forward", "--listen", ":PORT", "--code", code]

    # Broker connection — explicit CLI flags only.
    if args.broker:
        parts += ["--broker", args.broker]
    else:
        if args.profiles_file:
            parts += ["--profiles-file", args.profiles_file]
        if args.profile:
            parts += ["--profile", args.profile]
        if args.host:
            parts += ["--host", args.host]
        if args.port:
            parts += ["--port", str(args.port)]
        if args.username:
            parts += ["--username", args.username]
        if args.password:
            parts += ["--password", args.password]
        if args.tls:
            parts.append("--tls")
        if args.insecure:
            parts.append("--insecure")
        if args.ca_certs:
            parts += ["--ca-certs", args.ca_certs]

    # Compression — only when explicitly non-default (default is "none"; mismatch breaks decompress).
    if args.compress and args.compress != "none":
        parts += ["--compress", args.compress]

    # Encryption.
    if args.no_auto_encrypt:
        parts.append("--no-auto-encrypt")
    if args.encryption_key:
        # Explicit-key path (auto_encrypt is False whenever a key is supplied).
        parts += ["--encryption-key", args.encryption_key]
        if args.encryption_salt:
            parts += ["--encryption-salt", args.encryption_salt]
        if args.encryption_iterations:
            parts += ["--encryption-iterations", str(args.encryption_iterations)]
    elif enc_config.get("auto_encrypt"):
        if enc_config.get("secret") and enc_config["secret"] != "secret123":
            parts += ["--secret", enc_config["secret"]]
        if args.key_window != 1000:
            parts += ["--key-window", str(args.key_window)]

    # Encryption-scheme flags must match on both ends. The sender uses the stdlib
    # fallback whenever cryptography is missing OR it was forced — in either case the
    # client must use the same scheme, so emit --force-fallback-encryption (which also
    # lets a crypto-less client accept an explicit key, covering the case where the
    # sender simply lacks cryptography and never passed a flag).
    encryption_active = bool(args.encryption_key) or enc_config.get("auto_encrypt")
    using_fallback = not HAVE_CRYPTOGRAPHY or getattr(args, "force_fallback_encryption", False)
    if encryption_active and using_fallback:
        parts.append("--force-fallback-encryption")

    return " ".join(shlex.quote(p) for p in parts)


def create_client(mode: str, code: str, profile: dict, enc_config: dict,
                  transfer_config: dict, verbose: bool = False) -> MQTTNetcat:
    hashed_code = hash_code(code)
    prefix = f"{TOPIC_BASE}/{hashed_code}"
    return MQTTNetcat(
        mode=mode, prefix=prefix, profile=profile,
        qos=transfer_config["qos"], chunk_size=transfer_config["chunk_size"],
        compression_type=transfer_config["compression_type"],
        verbose=verbose, quiet=True,
        encryption_key=enc_config.get("encryption_key"),
        encryption_salt=enc_config.get("encryption_salt"),
        encryption_iterations=enc_config.get("encryption_iterations", 210000),
        # Explicit keys are already gated in get_encryption_config; any key reaching
        # here without cryptography is auto-derived and may use the stdlib fallback.
        allow_fallback_encryption=True,
    )


# ─── FRAMING ────────────────────────────────────────────────────────────────

def send_control(client: MQTTNetcat, msg_type: str, payload: dict = None):
    message = {"type": msg_type}
    if payload:
        message.update(payload)
    data = bytes([TAG_CONTROL]) + json.dumps(message).encode()
    client.send(data)


def send_data_chunk(client: MQTTNetcat, chunk: bytes, cid: int = 0):
    # Frame: [TAG_DATA][cid:4 big-endian][payload]. cid multiplexes many
    # concurrent connections over the single MQTT topic pair.
    client.send(bytes([TAG_DATA]) + cid.to_bytes(4, "big") + chunk)


def recv_message(client: MQTTNetcat, timeout: float = 60, monitor: "PeerMonitor" = None):
    raw = client.receive(timeout=timeout)
    if raw is None or len(raw) == 0:
        return None, None
    # Any inbound message proves the peer is alive
    if monitor is not None:
        monitor.note_rx()
    tag = raw[0]
    body = raw[1:]
    if tag == TAG_CONTROL:
        try:
            msg = json.loads(body.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return TAG_CONTROL, None
        # Transparently handle heartbeat so callers never see ping/pong
        mtype = msg.get("type") if isinstance(msg, dict) else None
        if mtype == MSG_PING:
            send_control(client, MSG_PONG)
            return None, None
        if mtype == MSG_PONG:
            return None, None
        return TAG_CONTROL, msg
    elif tag == TAG_DATA:
        # Returns (cid, payload). Older single-stream frames have no cid; if the
        # body is shorter than the 4-byte header treat it as cid 0.
        if len(body) >= 4:
            cid = int.from_bytes(body[:4], "big")
            return TAG_DATA, (cid, body[4:])
        return TAG_DATA, (0, body)
    else:
        return None, None


def recv_control(client: MQTTNetcat, timeout: float = 60, drain_data=None,
                 monitor: "PeerMonitor" = None) -> Optional[dict]:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        tag, body = recv_message(client, timeout=min(remaining, 2), monitor=monitor)
        if tag == TAG_CONTROL and isinstance(body, dict):
            return body
        if tag == TAG_DATA and drain_data is not None:
            drain_data.append(body)


class TokenBucket:
    """Token bucket for rate limiting.

    Works for both bytes/sec (consume(len(data))) and publish/sec (consume(1)).
    has_tokens() refills without consuming — use it to gate rlist in select loops.
    """

    def __init__(self, rate: float, burst: float = None):
        self._rate = rate
        self._capacity = burst if burst is not None else rate
        self._tokens = float(self._capacity)
        self._last = time.monotonic()

    def _refill(self):
        now = time.monotonic()
        self._tokens = min(self._capacity,
                           self._tokens + self._rate * (now - self._last))
        self._last = now

    def consume(self, n: float = 1) -> None:
        # Always deduct — overdraft is intentional. has_tokens() gating in the
        # select loop prevents sustained overrun; the overdraft is bounded by
        # one READ_SIZE burst before the bucket goes negative and blocks.
        self._refill()
        self._tokens -= n

    def has_tokens(self, n: float = 1) -> bool:
        self._refill()
        return self._tokens >= n


class PeerMonitor:
    """Tracks peer liveness via periodic heartbeat pings.

    Any received message resets the liveness timer (see recv_message).
    Call maybe_ping() each loop iteration to emit heartbeats, and
    is_peer_dead() to detect a peer that has gone away.
    """

    def __init__(self, client: MQTTNetcat,
                 ping_interval: float = PING_INTERVAL,
                 timeout: float = PEER_TIMEOUT):
        self._client = client
        self._ping_interval = ping_interval
        self._timeout = timeout
        now = time.monotonic()
        self._last_rx = now
        self._last_ping = now

    def reset(self):
        now = time.monotonic()
        self._last_rx = now
        self._last_ping = now

    def note_rx(self):
        self._last_rx = time.monotonic()

    def maybe_ping(self):
        now = time.monotonic()
        if now - self._last_ping >= self._ping_interval:
            send_control(self._client, MSG_PING)
            self._last_ping = now

    def is_peer_dead(self) -> bool:
        return (time.monotonic() - self._last_rx) > self._timeout



class MuxForwarder:
    """Multiplex many concurrent TCP connections over one MQTT topic pair.

    Each connection is keyed by a 4-byte cid carried in every TAG_DATA frame and
    in the CONNECTED/DISCONNECT/ERROR control messages. A single select() loop
    services the local listener (client mode), every live connection socket, and
    the inbound MQTT queue — so a browser's parallel requests are carried
    simultaneously instead of one-at-a-time. Returns a reason string when the
    session ends so the caller can reconnect/reset exactly as before.
    """

    READ_SIZE = 65536

    def __init__(self, client, monitor, args, *, listener=None,
                 connect_addr=None, session_id=None, owner_sid=None):
        self.client = client
        self.monitor = monitor
        self.args = args
        self.listener = listener            # client (listen) mode
        self.connect_addr = connect_addr    # server (connect) mode
        self.session_id = session_id
        self.owner_sid = owner_sid
        self.conns = {}                     # cid -> socket
        self.by_sock = {}                   # socket -> cid
        self.outbufs = {}                   # cid -> bytearray pending local writes
        self._next_cid = 1
        self._byte_bucket = TokenBucket(parse_rate(args.rate_limit)) if args.rate_limit else None
        self._pub_bucket  = TokenBucket(float(args.max_pub_rate)) if args.max_pub_rate else None
        self._max_conns   = args.max_connections

    def _new_cid(self):
        cid = self._next_cid
        self._next_cid += 1
        return cid

    def _register(self, cid, sock):
        sock.setblocking(False)
        self.conns[cid] = sock
        self.by_sock[sock] = cid
        self.outbufs[cid] = bytearray()

    def _close(self, cid, notify=True):
        sock = self.conns.pop(cid, None)
        self.outbufs.pop(cid, None)
        if sock is not None:
            self.by_sock.pop(sock, None)
            try:
                sock.close()
            except Exception:
                pass
        if notify:
            send_control(self.client, MSG_DISCONNECT, {"cid": cid})

    def _accept(self):
        try:
            sock, addr = self.listener.accept()
        except (OSError, BlockingIOError):
            return
        if self._max_conns and len(self.conns) >= self._max_conns:
            logger.warning(f"Connection from {addr} rejected: max_connections={self._max_conns} reached")
            sock.close()
            return
        cid = self._new_cid()
        self._register(cid, sock)
        logger.info(f"Local connection cid={cid} from {addr}")
        send_control(self.client, MSG_CONNECTED, {"sid": self.session_id, "cid": cid})

    def _open_remote(self, cid):
        host, port = self.connect_addr
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(10)
            sock.connect((host, port))
            self._register(cid, sock)
            logger.info(f"Opened remote connection cid={cid} to {host}:{port}")
        except (OSError, socket.timeout) as e:
            logger.warning(f"cid={cid} connect to {host}:{port} failed: {e}")
            send_control(self.client, MSG_DISCONNECT, {"cid": cid})

    def _read_local(self, sock):
        cid = self.by_sock.get(sock)
        if cid is None:
            return
        try:
            data = sock.recv(self.READ_SIZE)
        except (BlockingIOError, InterruptedError):
            return
        except (OSError, ConnectionError):
            self._close(cid)
            return
        if data:
            if self._byte_bucket:
                self._byte_bucket.consume(len(data))   # deduct; gating is in run()
            if self._pub_bucket:
                self._pub_bucket.consume(1)
            send_data_chunk(self.client, data, cid=cid)
        else:
            self._close(cid)

    def _queue_write(self, cid, payload):
        sock = self.conns.get(cid)
        if sock is None:
            return
        buf = self.outbufs[cid]
        buf.extend(payload)
        self._flush(sock)

    def _flush(self, sock):
        cid = self.by_sock.get(sock)
        if cid is None:
            return
        buf = self.outbufs.get(cid)
        if not buf:
            return
        try:
            sent = sock.send(buf)
            del buf[:sent]
        except (BlockingIOError, InterruptedError):
            pass
        except (OSError, ConnectionError):
            self._close(cid)

    def _drain_mqtt(self):
        """Process all queued inbound MQTT messages. Returns False if the
        session has ended (BYE)."""
        while True:
            tag, body = recv_message(self.client, timeout=0, monitor=self.monitor)
            if tag is None:
                return True
            if tag == TAG_DATA:
                cid, payload = body
                if payload:
                    self._queue_write(cid, payload)
            elif tag == TAG_CONTROL and isinstance(body, dict):
                btype = body.get("type")
                cid = body.get("cid")
                if btype == MSG_CONNECTED and self.connect_addr is not None:
                    if self.owner_sid is None or body.get("sid") == self.owner_sid:
                        if cid is not None and cid not in self.conns:
                            self._open_remote(cid)
                elif btype == MSG_DISCONNECT:
                    if cid is not None:
                        self._close(cid, notify=False)
                elif btype == MSG_ERROR:
                    if cid is not None:
                        self._close(cid, notify=False)
                    else:
                        logger.warning(f"Peer error: {body.get('message', 'unknown')}")
                elif btype == MSG_READY and self.connect_addr is not None \
                        and self.owner_sid is not None and body.get("sid") != self.owner_sid:
                    send_control(self.client, MSG_BUSY, {"sid": body.get("sid")})
                elif btype == MSG_BYE:
                    return False

    def run(self):
        """Service connections until the session ends. Returns a reason."""
        while True:
            if self.client.userdata.get("disconnected") is not None:
                return "mqtt_lost"
            self.monitor.maybe_ping()
            if self.monitor.is_peer_dead():
                return "peer_dead"

            rlist = list(self.conns.values())
            # Stall TCP reads when a rate bucket is exhausted; listener is
            # always included so new connections can still be accepted.
            if (self._byte_bucket is not None and not self._byte_bucket.has_tokens(1)) or \
               (self._pub_bucket is not None and not self._pub_bucket.has_tokens(1)):
                rlist = []
            if self.listener is not None:
                rlist.append(self.listener)
            wlist = [self.conns[c] for c, b in self.outbufs.items() if b and c in self.conns]
            try:
                readable, writable, _ = select.select(rlist, wlist, [], 0.1)
            except (select.error, OSError, ValueError):
                readable, writable = [], []

            if self.listener is not None and self.listener in readable:
                self._accept()
                readable = [s for s in readable if s is not self.listener]

            for sock in readable:
                self._read_local(sock)
            for sock in writable:
                self._flush(sock)

            if not self._drain_mqtt():
                return "session_ended"

    def close_all(self):
        for cid in list(self.conns):
            self._close(cid, notify=False)


# ─── AUTHENTICATION ─────────────────────────────────────────────────────────

def do_challenge_response_auth(client: MQTTNetcat, enc_config: dict, code: str,
                                is_server: bool, challenge_msg: dict = None) -> bool:
    """Perform challenge-response authentication. Returns True on success."""
    if not enc_config.get("auto_encrypt"):
        if is_server:
            send_control(client, MSG_ACCEPTED, {"window_offset": 0})
        return True

    if is_server:
        logger.info("Starting challenge-response authentication")
        nonce = base64.b64encode(os.urandom(24)).decode('ascii')
        send_control(client, MSG_CHALLENGE, {"nonce": nonce})
        logger.info("Sent CHALLENGE to client")

        response_msg = recv_control(client, timeout=60)
        if response_msg is None or response_msg.get("type") != MSG_CHALLENGE_RESPONSE:
            logger.error("No challenge response received")
            return False

        encrypted_nonce = response_msg.get("encrypted_nonce")
        if not encrypted_nonce:
            logger.error("Challenge response missing encrypted_nonce")
            return False

        verified = False
        successful_offset = None
        for offset in [0, -1, 1]:
            try:
                derived_key, derived_salt = derive_time_based_key(
                    enc_config['secret'], code, enc_config['key_window'], offset)
                salt_bytes = base64.b64decode(derived_salt)
                encryptor = Encryptor(password=derived_key, salt=salt_bytes,
                                      iterations=enc_config.get("encryption_iterations", 210000))
                aad = f"{TOPIC_BASE}/{code}".encode()
                encrypted_bytes = base64.b64decode(encrypted_nonce)
                decrypted = encryptor.decrypt(encrypted_bytes, aad)
                if decrypted.decode('ascii') == nonce:
                    verified = True
                    successful_offset = offset
                    logger.info(f"Authentication successful with window offset {offset}")
                    break
            except Exception as e:
                logger.debug(f"Decryption failed with offset {offset}: {e}")

        if verified:
            send_control(client, MSG_ACCEPTED, {"window_offset": successful_offset})
            return True
        else:
            send_control(client, MSG_REJECTED, {"attempts_remaining": 0, "final": True})
            return False

    else:
        # Client side - use provided challenge_msg or receive one
        msg = challenge_msg
        if msg is None:
            msg = recv_control(client, timeout=60)
        if msg is None or msg.get("type") != MSG_CHALLENGE:
            logger.error("No challenge received from server")
            return False

        nonce = msg.get("nonce")
        if not nonce:
            logger.error("Challenge missing nonce")
            return False

        logger.info("Received authentication challenge")
        try:
            derived_key, derived_salt = derive_time_based_key(
                enc_config['secret'], code, enc_config['key_window'], 0)
            salt_bytes = base64.b64decode(derived_salt)
            encryptor = Encryptor(password=derived_key, salt=salt_bytes,
                                  iterations=enc_config.get("encryption_iterations", 210000))
            aad = f"{TOPIC_BASE}/{code}".encode()
            encrypted = encryptor.encrypt(nonce.encode('ascii'), aad)
            encrypted_b64 = base64.b64encode(encrypted).decode('ascii')
            send_control(client, MSG_CHALLENGE_RESPONSE, {"encrypted_nonce": encrypted_b64})

            auth_result = recv_control(client, timeout=60)
            if auth_result is None:
                logger.error("No authentication response")
                return False

            if auth_result.get("type") == MSG_ACCEPTED:
                window_offset = auth_result.get("window_offset", 0)
                logger.info(f"Authentication successful! Window offset: {window_offset}")
                if window_offset != 0:
                    derived_key, derived_salt = derive_time_based_key(
                        enc_config['secret'], code, enc_config['key_window'], window_offset)
                    salt_bytes = base64.b64decode(derived_salt)
                    client.userdata["encryptor"] = Encryptor(
                        password=derived_key, salt=salt_bytes,
                        iterations=enc_config.get("encryption_iterations", 210000))
                return True
            else:
                logger.warning("Authentication rejected")
                return False
        except Exception as e:
            logger.error(f"Authentication error: {e}", exc_info=True)
            return False


# ─── SERVER MODE (connects to remote service) ────────────────────────────────

def parse_connect_addr(addr: str) -> tuple:
    """Parse 'host:port' into (host, port)."""
    host, port_str = addr.rsplit(':', 1)
    return (host, int(port_str))


def do_server(args, env_config: dict):
    """Server: generates code, connects to remote TCP service via MQTT tunnel."""
    profile = build_profile(args, env_config)
    code = args.code or generate_code()
    enc_config = get_encryption_config(args, env_config, code)
    transfer_config = get_transfer_config(args, env_config)
    remote_host, remote_port = parse_connect_addr(args.connect)

    logger.info(f"Starting forward server with code: {code}")
    logger.info(f"Remote service: {remote_host}:{remote_port}")
    if enc_config.get("auto_encrypt"):
        logger.info(f"Auto-encryption enabled (window: {enc_config['key_window']}s)")

    print(f"Generated pairing code: {code}", file=sys.stderr)
    print(f"Will connect to {remote_host}:{remote_port}", file=sys.stderr)
    if enc_config.get("auto_encrypt"):
        if enc_config.get("secret") == "secret123":
            print("🔒 Auto-encryption: enabled (default secret)", file=sys.stderr)
        else:
            print("🔒 Auto-encryption: enabled (custom secret)", file=sys.stderr)
    print(f"On the client side, run:", file=sys.stderr)
    print(f"  {build_client_command(args, code, enc_config)}", file=sys.stderr)
    print(file=sys.stderr)

    client = create_client("connect", code, profile, enc_config, transfer_config, verbose=args.verbose)

    cleanup_done = False

    def cleanup():
        nonlocal cleanup_done
        if cleanup_done:
            return
        cleanup_done = True
        # Tell the peer we are going away so it doesn't wait on a dead tunnel
        try:
            send_control(client, MSG_BYE)
            time.sleep(0.2)
        except Exception:
            pass
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

    monitor = PeerMonitor(client)

    try:
        client.connect()
        time.sleep(0.5)

        owner_sid = None          # session ID of the currently attached client
        authenticated = False

        print("Waiting for client to connect...", file=sys.stderr)
        logger.info("Waiting for client")

        # Main session loop: one client at a time
        while True:
            if client.userdata.get("disconnected") is not None:
                print("\nMQTT connection lost.", file=sys.stderr)
                break

            # Heartbeat + liveness while a client is attached
            if authenticated:
                monitor.maybe_ping()
                if monitor.is_peer_dead():
                    print("\nClient timed out (no heartbeat). Freeing session.", file=sys.stderr)
                    logger.warning("Client heartbeat timeout, freeing session")
                    authenticated = False
                    owner_sid = None
                    print("Waiting for client to connect...", file=sys.stderr)

            msg = recv_control(client, timeout=2, monitor=monitor)
            if msg is None:
                continue

            mtype = msg.get("type")
            msg_sid = msg.get("sid")

            # ── A client wants to attach ──
            if mtype == MSG_READY:
                if authenticated and owner_sid is not None and msg_sid != owner_sid:
                    # Already serving another client → reject this one
                    print(f"Rejecting extra client {msg_sid}: already in use.", file=sys.stderr)
                    logger.info(f"Rejecting client {msg_sid}: session busy")
                    send_control(client, MSG_BUSY, {"sid": msg_sid})
                    continue
                # Free slot (or the same client re-attaching) → authenticate
                print("Client connecting, authenticating...", file=sys.stderr)
                logger.info(f"Authenticating client {msg_sid}")
                if not do_challenge_response_auth(client, enc_config, code, is_server=True):
                    print("✗ Authentication failed!", file=sys.stderr)
                    logger.warning("Authentication failed")
                    continue
                print("✓ Authentication successful!", file=sys.stderr)
                logger.info(f"Client {msg_sid} authenticated")
                authenticated = True
                owner_sid = msg_sid
                monitor.reset()
                print("Ready. Forwarding connections (multiplexed)...", file=sys.stderr)
                logger.info("Entering multiplexed forwarding")

                mux = MuxForwarder(client, monitor, args,
                                   connect_addr=(remote_host, remote_port),
                                   owner_sid=owner_sid)
                try:
                    reason = mux.run()
                finally:
                    mux.close_all()

                authenticated = False
                owner_sid = None
                if reason == "mqtt_lost":
                    print("\nMQTT connection lost.", file=sys.stderr)
                    break
                print(f"\nSession ended ({reason}). Waiting for client to connect...",
                      file=sys.stderr)
                logger.info(f"Session ended ({reason}), waiting for new client")
                continue

            # ── Owner gracefully leaving ──
            if mtype == MSG_BYE:
                if authenticated and msg_sid == owner_sid:
                    print("\nClient disconnected. Freeing session.", file=sys.stderr)
                    logger.info("Owner client said BYE, freeing session")
                    authenticated = False
                    owner_sid = None
                    print("Waiting for client to connect...", file=sys.stderr)
                continue

            # Ignore anything before auth or from a non-owner
            if not authenticated or (msg_sid is not None and msg_sid != owner_sid):
                continue

            # MSG_CONNECTED and data are handled inside MuxForwarder; nothing
            # else to do in the session loop.

    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        logger.error(f"Server failed: {e}", exc_info=True)
        sys.exit(1)
    finally:
        cleanup()


# ─── CLIENT MODE (listens locally) ──────────────────────────────────────────

def parse_listen_addr(addr: str) -> tuple:
    """Parse 'host:port' or ':port' into (host, port)."""
    if ':' in addr:
        host, port_str = addr.rsplit(':', 1)
        return (host or '0.0.0.0', int(port_str))
    return ('0.0.0.0', int(addr))


def do_client(args, env_config: dict):
    """Client: enters code, listens on local TCP port, forwards through MQTT."""
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
    listen_host, listen_port = parse_listen_addr(args.listen)

    logger.info(f"Starting forward client with code: {code}")
    logger.info(f"Local listen: {listen_host}:{listen_port}")
    if enc_config.get("auto_encrypt"):
        logger.info(f"Auto-encryption enabled (window: {enc_config['key_window']}s)")

    print(f"Connecting to broker...", file=sys.stderr)
    print(f"Will listen on {listen_host}:{listen_port}", file=sys.stderr)
    if enc_config.get("auto_encrypt"):
        if enc_config.get("secret") == "secret123":
            print("🔒 Auto-encryption: enabled (default secret)", file=sys.stderr)
        else:
            print("🔒 Auto-encryption: enabled (custom secret)", file=sys.stderr)

    client = create_client("listen", code, profile, enc_config, transfer_config, verbose=args.verbose)

    session_id = uuid.uuid4().hex[:12]
    logger.info(f"Client session id: {session_id}")

    cleanup_done = False
    tcp_listener: Optional[socket.socket] = None

    def cleanup():
        nonlocal cleanup_done
        if cleanup_done:
            return
        cleanup_done = True
        # Tell the server we are leaving so it frees the session immediately
        try:
            send_control(client, MSG_BYE, {"sid": session_id})
            time.sleep(0.2)
        except Exception:
            pass
        if tcp_listener:
            try:
                tcp_listener.close()
            except Exception:
                pass
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

    monitor = PeerMonitor(client)

    try:
        client.connect()
        time.sleep(0.5)

        # Send READY until authenticated
        print("Waiting for server...", file=sys.stderr)
        logger.info("Sending READY messages")
        last_ready = 0
        authenticated = False
        connected = False

        while not connected:
            now = time.monotonic()
            if not authenticated and now - last_ready >= 2:
                send_control(client, MSG_READY, {"sid": session_id})
                last_ready = now

            msg = recv_control(client, timeout=2, monitor=monitor)
            if msg is None:
                continue

            msg_type = msg.get("type")

            if msg_type == MSG_BUSY and msg.get("sid") == session_id:
                print("✗ Server is already serving another client. Try again later.",
                      file=sys.stderr)
                logger.warning("Rejected by server: session busy")
                cleanup()
                sys.exit(1)

            if msg_type == MSG_BYE:
                print("\nServer disconnected.", file=sys.stderr)
                logger.info("Server said BYE while waiting")
                cleanup()
                sys.exit(1)

            if msg_type == MSG_ACCEPTED and not authenticated:
                # No-auth path: server skipped challenge-response (--no-auto-encrypt)
                print("✓ Authentication successful!", file=sys.stderr)
                authenticated = True
                monitor.reset()

            elif msg_type == MSG_CHALLENGE and not authenticated:
                print("Authenticating with server...", file=sys.stderr)
                if not do_challenge_response_auth(client, enc_config, code, is_server=False, challenge_msg=msg):
                    print("✗ Authentication failed!", file=sys.stderr)
                    cleanup()
                    sys.exit(1)
                print("✓ Authentication successful!", file=sys.stderr)
                authenticated = True
                monitor.reset()

            if authenticated and not connected:
                # Auth succeeded (either path). Set up local TCP listener.
                logger.info("Setting up local TCP listener")
                tcp_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                tcp_listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                tcp_listener.bind((listen_host, listen_port))
                tcp_listener.listen(128)
                tcp_listener.setblocking(False)
                logger.info(f"TCP listener bound to {listen_host}:{listen_port}")
                print(f"Listening on {listen_host}:{listen_port}, waiting for local connection...", file=sys.stderr)
                connected = True  # Exit the auth loop, enter the persistent forwarding loop

        # Multiplexed forwarding: accept and carry many concurrent local
        # connections simultaneously over the single MQTT channel.
        print("Tunnel ready. Forwarding (multiplexed)...", file=sys.stderr)
        logger.info("Entering multiplexed forwarding")
        mux = MuxForwarder(client, monitor, args,
                           listener=tcp_listener, session_id=session_id)
        try:
            reason = mux.run()
        finally:
            mux.close_all()

        if reason == "mqtt_lost":
            print("\nMQTT connection lost.", file=sys.stderr)
        elif reason == "peer_dead":
            print("\nServer timed out (no heartbeat).", file=sys.stderr)
        else:
            print("\nServer disconnected.", file=sys.stderr)
        logger.info(f"Client forwarding ended ({reason})")
        cleanup()
        return

    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        logger.error(f"Client failed: {e}", exc_info=True)
        sys.exit(1)
    finally:
        cleanup()


# ─── CLI ────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mqtt-forward",
        description="TCP tunnel over MQTT with buffer-and-burst",
        epilog=(
            "Examples:\n"
            "  Server: mqtt-forward --connect remote.service:80\n"
            "  Client: mqtt-forward --listen :8080 --code 42-cosmic-dolphin\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    mode_group = parser.add_argument_group("Mode (required, pick one)")
    mode_group.add_argument("--connect", "-c", type=str, default=None,
                            help="Server mode: connect to remote host:port (the actual service)")
    mode_group.add_argument("--listen", "-l", type=str, default=None,
                            help="Client mode: listen on [host:]port for local TCP connections")
    mode_group.add_argument("--code", type=str, default=None,
                            help="Pairing code (auto-generated for server, required for client)")

    tunnel_group = parser.add_argument_group("Tunnel")
    tunnel_group.add_argument("--rate-limit", type=str, default="1m",
                              help="Max bytes/sec over MQTT (e.g. 500k, 2m). Applies backpressure to TCP senders. (default: 1m)")
    tunnel_group.add_argument("--max-pub-rate", type=int, default=20,
                              help="Max MQTT publishes/sec (default: 20)")
    tunnel_group.add_argument("--max-connections", type=int, default=10,
                              help="Max concurrent TCP connections (new connections are silently dropped above limit) (default: 10)")

    broker_group = parser.add_argument_group("Broker")
    broker_group.add_argument("--broker", "-b", type=str, metavar="NAME",
                              help=f"Use a built-in public broker preset: {', '.join(BUILTIN_PROFILES)}")
    broker_group.add_argument("--profiles-file", type=str, metavar="PATH",
                              help=f"Path to a profiles JSON file (default: {DEFAULT_PROFILES_FILE})")
    broker_group.add_argument("--profile", type=str, metavar="NAME",
                              help=f"Named profile to load from the profiles file (default: {DEFAULT_PROFILE_NAME})")
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
                           help="Secret for auto-encryption (default: 'secret123')")
    enc_group.add_argument("--key-window", type=int, default=1000,
                           help="Time window in seconds for auto-encryption (default: 1000)")
    enc_group.add_argument("--no-auto-encrypt", action="store_true",
                           help="Disable automatic encryption")
    enc_group.add_argument("--force-fallback-encryption", action="store_true",
                           help="Use the built-in stdlib encryption scheme even if 'cryptography' "
                                "is installed (for interop with a peer that lacks it)")
    enc_group.add_argument("--allow-insecure-encryption", action="store_true",
                           help="Allow --encryption-key with the weaker fallback scheme when "
                                "'cryptography' is not installed (instead of erroring)")

    xfer_group = parser.add_argument_group("Transfer")
    xfer_group.add_argument("--qos", type=int, choices=[0, 1, 2], default=None, help="QoS level (default: from .env or 0)")
    xfer_group.add_argument("--chunk-size", type=int, default=None, help="Chunk size in bytes (default: from .env or 65536)")
    xfer_group.add_argument("--compress", choices=list(COMPRESSION_TYPES.keys()), default=None, help="Compression")

    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    parser.add_argument("--log-file", type=str, default=None, help=f"Log file path (default: {DEFAULT_LOG_FILE})")

    return parser


def setup_logging(log_file: str, verbose: bool = False):
    log_level = logging.DEBUG if verbose else logging.INFO
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    file_handler = logging.FileHandler(log_file, mode='a')
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)
    logger.setLevel(log_level)
    logger.addHandler(file_handler)
    if verbose:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setLevel(logging.DEBUG)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.listen and not args.connect:
        parser.error("Must specify either --listen or --connect")

    if args.listen and args.connect:
        parser.error("Cannot specify both --listen and --connect")

    log_file = args.log_file if args.log_file else DEFAULT_LOG_FILE
    setup_logging(log_file, args.verbose)
    logger.info("=" * 60)
    logger.info(f"mqtt-forward started (version {PROTOCOL_VERSION})")

    # Force the stdlib fallback scheme for all Encryptors when requested.
    if getattr(args, "force_fallback_encryption", False):
        set_force_fallback(True)
        logger.info("Forcing built-in stdlib encryption scheme (--force-fallback-encryption)")
        if HAVE_CRYPTOGRAPHY:
            print("ℹ️  Using built-in fallback encryption (forced); peer must do the same.",
                  file=sys.stderr)

    env_config = load_env_config(args)

    if args.listen:
        do_client(args, env_config)
    else:
        do_server(args, env_config)


if __name__ == "__main__":
    main()
