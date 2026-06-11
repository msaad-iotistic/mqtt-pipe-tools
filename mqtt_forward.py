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
import signal
import socket
import sys
import time
import uuid
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mqtt_cat import MQTTNetcat, COMPRESSION_TYPES, COMPRESSION_NONE, Encryptor

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


def load_profiles_config() -> dict:
    config = {}
    if not os.path.exists(DEFAULT_PROFILES_FILE):
        return config
    try:
        with open(DEFAULT_PROFILES_FILE, "r") as f:
            profiles = json.load(f)
        if DEFAULT_PROFILE_NAME in profiles:
            profile = profiles[DEFAULT_PROFILE_NAME]
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
    except Exception:
        pass
    return config


def load_env_config() -> dict:
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
    profile = {}
    for key in ["host", "port", "username", "password", "ca_certs", "certfile", "keyfile"]:
        if key in env_config:
            profile[key] = env_config[key]
    for key in ["tls", "insecure"]:
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
    if "host" not in profile:
        print("Error: No MQTT host specified. Use --host or set MQTT_HOST in .env", file=sys.stderr)
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
        if args.secret == "secret123":
            logger.warning("Using default secret 'secret123' for auto-encryption. "
                         "For better security, use --secret with a custom value.")
            print("⚠️  Warning: Using default secret for encryption. "
                  "Use --secret for better security.", file=sys.stderr)
        else:
            logger.info("Auto-encryption enabled with custom secret")
    return enc


def get_transfer_config(args, env_config: dict) -> dict:
    qos = args.qos if args.qos is not None else int(env_config.get("qos", 0))
    chunk_size = args.chunk_size or int(env_config.get("chunk_size", 65536))
    compress = args.compress or "none"
    compression_type = COMPRESSION_TYPES.get(compress, COMPRESSION_NONE)
    return {"qos": qos, "chunk_size": chunk_size, "compression_type": compression_type}


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
    )


# ─── FRAMING ────────────────────────────────────────────────────────────────

def send_control(client: MQTTNetcat, msg_type: str, payload: dict = None):
    message = {"type": msg_type}
    if payload:
        message.update(payload)
    data = bytes([TAG_CONTROL]) + json.dumps(message).encode()
    client.send(data)


def send_data_chunk(client: MQTTNetcat, chunk: bytes):
    client.send(bytes([TAG_DATA]) + chunk)


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
        return TAG_DATA, body
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


# ─── BUFFER-BURST SENDER ────────────────────────────────────────────────────

class BufferBurstSender:
    """Buffer data and send as one large MQTT message after batch_sec seconds."""

    def __init__(self, client: MQTTNetcat, batch_sec: float = 2.0, max_batch_size: int = 1048576):
        self._client = client
        self._batch_sec = batch_sec
        self._max_batch_size = max_batch_size
        self._buf = bytearray()
        self._first_byte_time: Optional[float] = None

    def write(self, data: bytes):
        self._buf.extend(data)
        if self._first_byte_time is None:
            self._first_byte_time = time.monotonic()
        if len(self._buf) >= self._max_batch_size:
            self.flush()

    def check_timeout(self):
        if self._buf and self._first_byte_time is not None:
            if time.monotonic() - self._first_byte_time >= self._batch_sec:
                self.flush()

    def flush(self):
        if self._buf:
            send_data_chunk(self._client, bytes(self._buf))
            self._buf.clear()
            self._first_byte_time = None

    def __bool__(self):
        return len(self._buf) > 0


# ─── AUTHENTICATION ─────────────────────────────────────────────────────────

def do_challenge_response_auth(client: MQTTNetcat, enc_config: dict, code: str,
                                is_server: bool, challenge_msg: dict = None) -> bool:
    """Perform challenge-response authentication. Returns True on success."""
    if not enc_config.get("auto_encrypt"):
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
    if enc_config.get("auto_encrypt") and enc_config.get("secret") != "secret123":
        print(f"  mqtt-forward --listen :PORT --code {code} --secret {enc_config['secret']}", file=sys.stderr)
    else:
        print(f"  mqtt-forward --listen :PORT --code {code}", file=sys.stderr)
    print(file=sys.stderr)

    client = create_client("connect", code, profile, enc_config, transfer_config, verbose=args.verbose)

    cleanup_done = False
    tcp_sock: Optional[socket.socket] = None

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
        if tcp_sock:
            try:
                tcp_sock.close()
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
                print("Waiting for client to open local port...", file=sys.stderr)
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

            if mtype != MSG_CONNECTED:
                continue

            # ── Owner opened a local connection: connect to remote and forward ──
            print("Client ready, connecting to remote service...", file=sys.stderr)
            logger.info("Client has local TCP connection")

            tcp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            tcp_sock.settimeout(10)
            try:
                tcp_sock.connect((remote_host, remote_port))
                tcp_sock.setblocking(False)
                print(f"Connected to {remote_host}:{remote_port}", file=sys.stderr)
                logger.info(f"Connected to remote service {remote_host}:{remote_port}")
            except (OSError, socket.timeout) as e:
                print(f"Error: Could not connect to {remote_host}:{remote_port}: {e}", file=sys.stderr)
                send_control(client, MSG_ERROR, {"message": str(e)})
                tcp_sock.close()
                tcp_sock = None
                continue

            # Bidirectional forwarding loop
            sender = BufferBurstSender(client, batch_sec=args.batch_sec, max_batch_size=args.max_batch_size)
            print("Tunnel established. Forwarding...", file=sys.stderr)
            logger.info("Tunnel established, starting forwarding loop")

            mqtt_lost = False
            session_ended = False
            while True:
                if client.userdata.get("disconnected") is not None:
                    print("\nMQTT connection lost.", file=sys.stderr)
                    logger.warning("MQTT disconnected, exiting")
                    mqtt_lost = True
                    break

                monitor.maybe_ping()
                if monitor.is_peer_dead():
                    print("\nClient timed out (no heartbeat).", file=sys.stderr)
                    logger.warning("Client heartbeat timeout during forwarding")
                    session_ended = True
                    break

                try:
                    rlist, _, _ = select.select([tcp_sock], [], [], 0.1)
                except (select.error, OSError):
                    break

                # TCP → MQTT
                if tcp_sock in rlist:
                    try:
                        data = tcp_sock.recv(65536)
                        if data:
                            sender.write(data)
                        else:
                            print("\nRemote service disconnected.", file=sys.stderr)
                            logger.info("Remote service disconnected")
                            sender.flush()
                            send_control(client, MSG_DISCONNECT)
                            break
                    except (OSError, ConnectionError):
                        break

                sender.check_timeout()

                # MQTT → TCP
                inner_break = False
                while True:
                    tag, body = recv_message(client, timeout=0, monitor=monitor)
                    if tag is None:
                        break
                    if tag == TAG_DATA and body:
                        try:
                            tcp_sock.sendall(body)
                        except (OSError, ConnectionError):
                            inner_break = True
                            break
                    elif tag == TAG_CONTROL and isinstance(body, dict):
                        btype = body.get("type")
                        if btype == MSG_DISCONNECT:
                            print("\nLocal client disconnected.", file=sys.stderr)
                            logger.info("Local client disconnected")
                            inner_break = True
                            break
                        elif btype == MSG_BYE and body.get("sid") == owner_sid:
                            print("\nClient disconnected. Freeing session.", file=sys.stderr)
                            logger.info("Owner client said BYE during forwarding")
                            session_ended = True
                            inner_break = True
                            break
                        elif btype == MSG_READY and body.get("sid") != owner_sid:
                            # Another client tried to attach mid-session → reject
                            logger.info(f"Rejecting client {body.get('sid')}: session busy")
                            send_control(client, MSG_BUSY, {"sid": body.get("sid")})
                        elif btype == MSG_ERROR:
                            print(f"\nClient error: {body.get('message', 'unknown')}", file=sys.stderr)
                            inner_break = True
                            break
                if inner_break:
                    break

            # Close current remote connection
            try:
                tcp_sock.close()
            except Exception:
                pass
            tcp_sock = None

            if mqtt_lost:
                break

            if session_ended:
                authenticated = False
                owner_sid = None
                print("\nSession ended. Waiting for client to connect...", file=sys.stderr)
                logger.info("Session ended, waiting for new client")
                continue

            print("\nConnection closed. Waiting for next local connection...", file=sys.stderr)
            logger.info("Connection closed, waiting for next")

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
    tcp_conn: Optional[socket.socket] = None

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
        for sock in [tcp_conn, tcp_listener]:
            if sock:
                try:
                    sock.close()
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

            if msg_type == MSG_CHALLENGE and not authenticated:
                print("Authenticating with server...", file=sys.stderr)
                if not do_challenge_response_auth(client, enc_config, code, is_server=False, challenge_msg=msg):
                    print("✗ Authentication failed!", file=sys.stderr)
                    cleanup()
                    sys.exit(1)
                print("✓ Authentication successful!", file=sys.stderr)
                authenticated = True
                monitor.reset()

                # Auth succeeded. Set up local TCP listener immediately.
                logger.info("Setting up local TCP listener")
                tcp_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                tcp_listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                tcp_listener.bind((listen_host, listen_port))
                tcp_listener.listen(1)
                tcp_listener.setblocking(False)
                logger.info(f"TCP listener bound to {listen_host}:{listen_port}")
                print(f"Listening on {listen_host}:{listen_port}, waiting for local connection...", file=sys.stderr)
                connected = True  # Exit the auth loop, enter the persistent forwarding loop

        # Outer loop: accept multiple sequential local connections
        while True:
            if client.userdata.get("disconnected") is not None:
                print("\nMQTT connection lost.", file=sys.stderr)
                break

            # Wait for a local TCP connection
            tcp_conn = None
            while tcp_conn is None:
                if client.userdata.get("disconnected") is not None:
                    print("\nMQTT connection lost.", file=sys.stderr)
                    cleanup()
                    return

                # Keep heartbeat alive and watch for the server going away
                monitor.maybe_ping()
                if monitor.is_peer_dead():
                    print("\nServer timed out (no heartbeat).", file=sys.stderr)
                    logger.warning("Server heartbeat timeout while idle")
                    cleanup()
                    return
                ctl = recv_control(client, timeout=0.2, monitor=monitor)
                if ctl is not None and ctl.get("type") == MSG_BYE:
                    print("\nServer disconnected.", file=sys.stderr)
                    logger.info("Server said BYE while idle")
                    cleanup()
                    return

                try:
                    rlist, _, _ = select.select([tcp_listener], [], [], 0.2)
                    if tcp_listener in rlist:
                        tcp_conn, addr = tcp_listener.accept()
                        tcp_conn.setblocking(False)
                        print(f"Local TCP client connected from {addr}", file=sys.stderr)
                        logger.info(f"Local TCP client connected from {addr}")
                        send_control(client, MSG_CONNECTED, {"sid": session_id})
                except (select.error, OSError):
                    pass

            # Bidirectional forwarding loop
            sender = BufferBurstSender(client, batch_sec=args.batch_sec, max_batch_size=args.max_batch_size)
            print("Tunnel established. Forwarding...", file=sys.stderr)
            logger.info("Tunnel established, starting forwarding loop")

            mqtt_lost = False
            server_gone = False
            while True:
                if client.userdata.get("disconnected") is not None:
                    print("\nMQTT connection lost.", file=sys.stderr)
                    logger.warning("MQTT disconnected, exiting")
                    mqtt_lost = True
                    break

                monitor.maybe_ping()
                if monitor.is_peer_dead():
                    print("\nServer timed out (no heartbeat).", file=sys.stderr)
                    logger.warning("Server heartbeat timeout during forwarding")
                    server_gone = True
                    break

                try:
                    rlist, _, _ = select.select([tcp_conn], [], [], 0.1)
                except (select.error, OSError):
                    break

                # TCP → MQTT
                if tcp_conn in rlist:
                    try:
                        data = tcp_conn.recv(65536)
                        if data:
                            sender.write(data)
                        else:
                            print("\nLocal TCP client disconnected.", file=sys.stderr)
                            logger.info("Local TCP client disconnected")
                            sender.flush()
                            send_control(client, MSG_DISCONNECT)
                            break
                    except (OSError, ConnectionError):
                        break

                sender.check_timeout()

                # MQTT → TCP
                inner_break = False
                while True:
                    tag, body = recv_message(client, timeout=0, monitor=monitor)
                    if tag is None:
                        break
                    if tag == TAG_DATA and body:
                        try:
                            tcp_conn.sendall(body)
                        except (OSError, ConnectionError):
                            inner_break = True
                            break
                    elif tag == TAG_CONTROL and isinstance(body, dict):
                        btype = body.get("type")
                        if btype == MSG_DISCONNECT:
                            print("\nRemote service disconnected.", file=sys.stderr)
                            logger.info("Remote service disconnected")
                            inner_break = True
                            break
                        elif btype == MSG_BYE:
                            print("\nServer disconnected.", file=sys.stderr)
                            logger.info("Server said BYE during forwarding")
                            server_gone = True
                            inner_break = True
                            break
                        elif btype == MSG_ERROR:
                            print(f"\nServer error: {body.get('message', 'unknown')}", file=sys.stderr)
                            inner_break = True
                            break
                if inner_break:
                    break

            # Close current local connection, loop back for next
            try:
                tcp_conn.close()
            except Exception:
                pass
            tcp_conn = None

            if mqtt_lost:
                break

            if server_gone:
                print("Server is gone. Exiting.", file=sys.stderr)
                break

            print("\nConnection closed. Waiting for next local connection...", file=sys.stderr)
            logger.info("Connection closed, waiting for next")

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
    tunnel_group.add_argument("--batch-sec", type=float, default=0.5,
                              help="Buffer seconds before burst (default: 0.5)")
    tunnel_group.add_argument("--max-batch-size", type=int, default=65536,
                              help="Max bytes before forced flush, one MQTT packet (default: 65536)")

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
                           help="Secret for auto-encryption (default: 'secret123')")
    enc_group.add_argument("--key-window", type=int, default=1000,
                           help="Time window in seconds for auto-encryption (default: 1000)")
    enc_group.add_argument("--no-auto-encrypt", action="store_true",
                           help="Disable automatic encryption")

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

    env_config = load_env_config()

    if args.listen:
        do_client(args, env_config)
    else:
        do_server(args, env_config)


if __name__ == "__main__":
    main()
