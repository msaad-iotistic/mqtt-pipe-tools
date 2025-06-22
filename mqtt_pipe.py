#!/usr/bin/env python3
import argparse
import base64
import contextlib
import json
import logging
import os
import select
import signal
import ssl
import sys
import time
from typing import Dict, Optional, Tuple, TypedDict

import paho.mqtt.client as mqtt
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

# Default configuration
DEFAULTS = {
    "CHUNK_SIZE": 1024 * 64,
    "QOS": 0,
    "KEEPALIVE": 60,
    "MAX_PENDING": 10,
    "QOS0_DELAY_MS": 1,
}

# MQTT error mapping
MQTT_ERRORS = {
    mqtt.MQTT_ERR_CONN_LOST: "Broker connection lost",
    mqtt.MQTT_ERR_CONN_REFUSED: "Connection refused",
    mqtt.MQTT_ERR_NO_CONN: "No connection available",
}


# Type definitions
class ProfileType(TypedDict):
    host: str
    port: int
    username: Optional[str]
    password: Optional[str]
    tls: bool
    insecure: bool
    ca_certs: Optional[str]
    certfile: Optional[str]
    keyfile: Optional[str]
    encryption_key: Optional[str]
    encryption_salt: Optional[str]
    encryption_iterations: int


class UserDataType(TypedDict):
    topics: Dict[str, str]
    disconnected: Optional[int]
    qos: int
    pending_count: int
    max_pending: int
    current_chunk: Optional[bytes]
    qos0_delay: float
    logger: logging.Logger
    encryptor: Optional["Encryptor"]


class Encryptor:
    """Handles AES-GCM encryption/decryption with key derivation and AAD"""

    def __init__(self, password: Optional[str] = None, salt: bytes = b"", iterations: int = 210000):
        self.key = None
        if password:
            if len(password) < 32:
                raise ValueError("Encryption key must be at least 32 characters")
            self.derive_key(password.encode(), salt, iterations)

    def __del__(self):
        """Securely wipe key from memory"""
        if self.key:
            # Overwrite key in memory
            try:
                for i in range(len(self.key)):
                    self.key[i : i + 1] = b"\x00"
            except TypeError:
                # Key is immutable bytes, we can't modify it
                pass
            finally:
                self.key = None

    def derive_key(self, password: bytes, salt: bytes, iterations: int):
        """Derive a key from password using PBKDF2"""
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=iterations,
            backend=default_backend(),
        )
        self.key = kdf.derive(password)

    def encrypt(self, plaintext: bytes, aad: bytes) -> bytes:
        """Encrypt data with AES-GCM using Additional Authenticated Data (AAD)"""
        if not self.key:
            return plaintext

        nonce = os.urandom(12)
        aesgcm = AESGCM(self.key)
        ciphertext = aesgcm.encrypt(nonce, plaintext, aad)
        return nonce + ciphertext

    def decrypt(self, ciphertext: bytes, aad: bytes) -> bytes:
        """Decrypt data with AES-GCM using Additional Authenticated Data (AAD)"""
        if not self.key or len(ciphertext) < 12:
            return ciphertext

        nonce = ciphertext[:12]
        ciphertext = ciphertext[12:]
        aesgcm = AESGCM(self.key)
        try:
            return aesgcm.decrypt(nonce, ciphertext, aad)
        except Exception as e:
            raise ValueError(f"Decryption failed: {str(e)}") from e


def load_profiles(filename: str) -> Dict[str, ProfileType]:
    """Load MQTT profiles from JSON file with validation"""
    try:
        with open(filename, "r") as f:
            profiles = json.load(f)

            # Basic profile validation
            for name, profile in profiles.items():
                if "host" not in profile:
                    raise ValueError(f"Profile '{name}' missing 'host' field")
                if "port" not in profile:
                    raise ValueError(f"Profile '{name}' missing 'port' field")

            return profiles
    except Exception as e:
        logging.error(f"Error loading profiles: {str(e)}")
        sys.exit(1)


def setup_arg_parser() -> argparse.ArgumentParser:
    """Create and configure argument parser"""
    parser = argparse.ArgumentParser(
        description="MQTT Netcat-like Tool",
        epilog="Example: ./mqttnc.py listen myapp profiles.json production",
    )

    # Positional arguments
    parser.add_argument(
        "mode",
        choices=["listen", "connect"],
        help="Operation mode: 'listen' waits for connections, 'connect' initiates connections",
    )
    parser.add_argument("prefix", help="Topic prefix for communication")
    parser.add_argument("profiles_file", help="JSON file containing MQTT profiles")
    parser.add_argument("profile_name", help="Profile name to use from profiles file")

    # Performance parameters
    perf_group = parser.add_argument_group("Performance Parameters")
    perf_group.add_argument(
        "--qos",
        type=int,
        choices=[0, 1, 2],
        default=DEFAULTS["QOS"],
        help=f"Quality of Service level (default: {DEFAULTS['QOS']})",
    )
    perf_group.add_argument(
        "--keepalive",
        type=int,
        default=DEFAULTS["KEEPALIVE"],
        help=f"Keepalive interval in seconds (default: {DEFAULTS['KEEPALIVE']})",
    )
    perf_group.add_argument(
        "--chunk-size",
        type=int,
        default=DEFAULTS["CHUNK_SIZE"],
        help=f"Chunk size for reading stdin (bytes, default: {DEFAULTS['CHUNK_SIZE']})",
    )
    perf_group.add_argument(
        "--max-pending",
        type=int,
        default=DEFAULTS["MAX_PENDING"],
        help=f"Max pending acknowledgments before throttling (default: {DEFAULTS['MAX_PENDING']})",
    )
    perf_group.add_argument(
        "--qos0-delay",
        type=float,
        default=DEFAULTS["QOS0_DELAY_MS"],
        help=f"Delay between QoS 0 sends in milliseconds (default: {DEFAULTS['QOS0_DELAY_MS']} ms)",
    )

    # Logging control
    log_group = parser.add_argument_group("Logging Control")
    log_group.add_argument(
        "-v", "--verbose", action="store_true", help="Enable verbose debug logging"
    )
    log_group.add_argument(
        "-q", "--quiet", action="store_true", help="Reduce output to warnings and errors only"
    )
    log_group.add_argument("--log-file", help="File to write logs to (default: stderr)")

    return parser


def validate_arguments(args: argparse.Namespace, logger: logging.Logger) -> float:
    """Validate and warn about problematic argument values"""
    # Validate chunk size
    if args.chunk_size < 256:
        logger.warning("Small chunk sizes (<256B) may reduce performance")
    elif args.chunk_size > 1024 * 1024:
        logger.warning("Large chunk sizes (>1MB) may cause buffer issues")

    # Validate max pending
    if args.max_pending < 1:
        logger.error("Max pending must be at least 1")
        sys.exit(1)

    # Convert milliseconds to seconds for internal use
    qos0_delay_seconds = args.qos0_delay / 1000.0

    # Validate QoS 0 delay
    if args.qos0_delay < 0:
        logger.error("QoS 0 delay must be non-negative")
        sys.exit(1)
    elif args.qos0_delay == 0:
        logger.warning("Zero QoS 0 delay may overwhelm broker/receiver")
    elif args.qos0_delay > 100:  # 100ms
        logger.warning("Large QoS 0 delay (>100ms) may reduce throughput")

    return qos0_delay_seconds


def get_topics(mode: str, prefix: str) -> Dict[str, str]:
    """Determine publish/subscribe topics based on operation mode"""
    if mode == "listen":
        return {"subscribe": f"{prefix}/listen", "publish": f"{prefix}/connect"}
    return {"subscribe": f"{prefix}/connect", "publish": f"{prefix}/listen"}


def configure_tls(client: mqtt.Client, profile: ProfileType, logger: logging.Logger):
    """Configure TLS settings for MQTT client"""
    if not profile.get("tls", False):
        return

    tls_args = {
        "ca_certs": profile.get("ca_certs"),
        "certfile": profile.get("certfile"),
        "keyfile": profile.get("keyfile"),
        "cert_reqs": ssl.CERT_REQUIRED,
        "tls_version": ssl.PROTOCOL_TLS_CLIENT,
    }

    # Allow self-signed certificates if requested
    if profile.get("insecure", False):
        tls_args["cert_reqs"] = ssl.CERT_NONE

    # Remove None values from tls_args
    tls_args = {k: v for k, v in tls_args.items() if v is not None}

    try:
        client.tls_set(**tls_args)
        client.tls_insecure_set(profile.get("insecure", False))
        logger.debug("TLS configuration applied")
    except Exception as e:
        logger.error(f"TLS setup error: {str(e)}")
        sys.exit(1)


def on_connect(client: mqtt.Client, userdata: UserDataType, flags: Dict, rc: int):
    """Callback when connection to broker is established"""
    logger = userdata["logger"]
    topics = userdata["topics"]

    if rc == 0:
        client.subscribe(topics["subscribe"], qos=userdata["qos"])
        logger.info(f"Connected to broker, subscribed to {topics['subscribe']}")
        logger.debug(f"Connection flags: {flags}, QoS: {userdata['qos']}")
    else:
        logger.error(f"Connection failed with code {rc}")
        userdata["disconnected"] = rc


def on_message(client: mqtt.Client, userdata: UserDataType, msg: mqtt.MQTTMessage):
    """Callback for received messages"""
    logger = userdata["logger"]
    encryptor = userdata.get("encryptor")

    logger.debug(
        f"Received message on {msg.topic} " f"(QoS: {msg.qos}, Size: {len(msg.payload)} bytes)"
    )

    try:
        payload = msg.payload

        # Decrypt if encryption is enabled
        if encryptor:
            payload = encryptor.decrypt(payload, msg.topic.encode())
            logger.debug("Message decrypted successfully")

        # Write to stdout with error handling
        try:
            sys.stdout.buffer.write(payload)
            sys.stdout.buffer.flush()
        except BrokenPipeError:
            logger.warning("Broken pipe - output closed")
            client.disconnect()
        except Exception as e:
            logger.error(f"Error writing to stdout: {str(e)}")

    except Exception as e:
        logger.error(f"Message processing error: {str(e)}")


def on_disconnect(client: mqtt.Client, userdata: UserDataType, rc: int):
    """Callback when disconnected from broker"""
    logger = userdata["logger"]
    userdata["disconnected"] = rc
    if rc != 0:
        message = MQTT_ERRORS.get(rc, f"Unexpected disconnect (rc: {rc})")
        logger.error(message)
    else:
        logger.info("Disconnected from broker")


def on_publish(client: mqtt.Client, userdata: UserDataType, mid: int):
    """Callback when message is acknowledged by broker"""
    userdata["pending_count"] -= 1
    logger = userdata["logger"]
    logger.debug(f"Message acknowledged (mid: {mid})")


def on_log(client: mqtt.Client, userdata: UserDataType, level: int, buf: str):
    """Callback for MQTT client logging"""
    logger = userdata["logger"]
    level_name = logging.getLevelName(level)
    logger.debug(f"[MQTT/{level_name}] {buf}")


def setup_encryption(profile: ProfileType, userdata: UserDataType):
    """Set up encryption if configured"""
    if "encryption_key" in profile:
        try:
            salt = (
                base64.b64decode(profile["encryption_salt"])
                if "encryption_salt" in profile
                else b""
            )
            iterations = profile.get("encryption_iterations", 210000)
            userdata["encryptor"] = Encryptor(profile["encryption_key"], salt, iterations)
            logger = userdata["logger"]
            logger.info("Encryption enabled")
            logger.debug(f"Using salt: {base64.b64encode(salt).decode()}")
            logger.debug(f"PBKDF2 iterations: {iterations}")
        except Exception as e:
            userdata["logger"].error(f"Encryption setup failed: {str(e)}")
            sys.exit(1)


def setup_mqtt_client(
    profile: ProfileType,
    args: argparse.Namespace,
    topics: Dict[str, str],
    qos0_delay_seconds: float,
    logger: logging.Logger,
) -> Tuple[mqtt.Client, UserDataType]:
    """Create and configure MQTT client"""
    # Userdata for sharing state with callbacks
    userdata: UserDataType = {
        "topics": topics,
        "disconnected": None,
        "qos": args.qos,
        "pending_count": 0,
        "max_pending": args.max_pending,
        "current_chunk": None,
        "qos0_delay": qos0_delay_seconds,
        "logger": logger,
        "encryptor": None,
    }

    # Set up encryption
    setup_encryption(profile, userdata)

    # Generate client ID with mode and timestamp
    client_id = f"mqttnc_{args.mode}_{int(time.time())}"
    client = mqtt.Client(client_id=client_id, clean_session=True, userdata=userdata)

    # Configure MQTT logging
    client.on_log = on_log

    # Register callbacks
    client.on_connect = on_connect
    client.on_message = on_message
    client.on_disconnect = on_disconnect
    client.on_publish = on_publish

    # Set credentials if available
    if "username" in profile and "password" in profile:
        username = profile["username"].strip()
        password = profile["password"].strip()
        client.username_pw_set(username, password)
        logger.debug(f"Using credentials for user: {username}")

    # Configure TLS
    configure_tls(client, profile, logger)

    # Connect to broker with error handling
    try:
        logger.debug(
            f"Connecting to {profile['host']}:{profile['port']} " f"(Keepalive: {args.keepalive}s)"
        )
        client.connect(profile["host"], int(profile["port"]), args.keepalive)
    except ConnectionRefusedError:
        logger.error("Connection refused. Check broker availability and port.")
        sys.exit(1)
    except ssl.SSLError as e:
        logger.error(f"TLS handshake failed: {str(e)}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Connection error: {str(e)}")
        sys.exit(1)

    return client, userdata


def setup_logging(verbose: bool, quiet: bool, log_file: Optional[str] = None) -> logging.Logger:
    """Configure logging system with proper levels"""
    logger = logging.getLogger("mqttnc")

    # Set base logging level
    if verbose:
        logger.setLevel(logging.DEBUG)
    elif quiet:
        logger.setLevel(logging.WARNING)
    else:
        logger.setLevel(logging.INFO)

    # Clear any existing handlers
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)

    # Create formatter
    formatter = logging.Formatter(
        "%(asctime)s - %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Create file handler if specified
    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    else:
        # Create console handler
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    return logger


def main_loop(client: mqtt.Client, userdata: UserDataType, chunk_size: int):
    """Main processing loop with throttling and I/O handling"""
    logger = userdata["logger"]
    running = True
    immediate_shutdown = False
    last_send_time = 0

    logger.debug("Starting main processing loop")
    logger.debug(f"Chunk size: {chunk_size} bytes")
    logger.debug(f"Max pending: {userdata['max_pending']}")
    logger.debug(f"QoS 0 delay: {userdata['qos0_delay']*1000:.1f}ms")

    # Set up signal handling
    # Signal handler for graceful shutdown
    def signal_handler(sig, frame):
        nonlocal running
        nonlocal immediate_shutdown
        if running:
            # First Ctrl+C - initiate graceful shutdown
            logger.info("Shutting down...")
            running = False
        elif not immediate_shutdown:
            # Second Ctrl+C - force exit without sending remaining data
            immediate_shutdown = True
            logger.warning("Force shutdown requested. Disconnecting mqtt client...")
            client.disconnect()
            client.loop_stop()
            sys.exit(1)
        else:
            # Third Ctrl+C - force exit immediately; without closing mqtt client
            logger.error("Force IMMEDIATE shutdown requested")
            sys.exit(1)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        while running and not immediate_shutdown:
            # Check for disconnects
            if userdata["disconnected"] is not None:
                if userdata["disconnected"] != 0:
                    logger.error("Disconnected from broker, exiting.")
                break

            # Determine if we can send more data
            credit_available = True
            if userdata["qos"] > 0:
                # QoS 1/2: Check pending acknowledgments
                if userdata["pending_count"] >= userdata["max_pending"]:
                    credit_available = False
                    logger.debug(
                        f"Throttling: {userdata['pending_count']}/"
                        f"{userdata['max_pending']} pending messages"
                    )
            else:
                # QoS 0: Rate limit using specified delay
                current_time = time.monotonic()
                if current_time - last_send_time < userdata["qos0_delay"]:
                    credit_available = False
                    elapsed = (current_time - last_send_time) * 1000
                    logger.debug(
                        f"QoS0 throttling: {elapsed:.1f}ms since last send "
                        f"(limit: {userdata['qos0_delay']*1000:.1f}ms)"
                    )
                else:
                    last_send_time = current_time

            # Check for available data
            rlist, _, _ = select.select([sys.stdin], [], [], 0.1)

            if rlist and credit_available:
                # Read and send new data
                logger.debug(f"Reading stdin (chunk size: {chunk_size})")
                data = sys.stdin.buffer.read1(chunk_size)
                if not data:  # EOF
                    logger.debug("EOF reached on stdin")
                    break
                send_data(client, userdata, data)

            # Retry any failed chunks
            elif userdata["current_chunk"] and credit_available:
                logger.debug("Retrying previous chunk")
                send_data(client, userdata, userdata["current_chunk"], retry=True)

            # Brief pause when throttled
            elif not credit_available:
                sleep_time = min(0.01, userdata["qos0_delay"])
                time.sleep(sleep_time)

    except Exception as e:
        logger.error(f"Runtime error: {str(e)}")
    finally:
        # Clean up resources
        logger.debug("Cleaning up resources")
        with contextlib.suppress(Exception):
            client.disconnect()
            client.loop_stop()
        logger.debug("Cleanup complete")


def send_data(client: mqtt.Client, userdata: UserDataType, data: bytes, retry: bool = False):
    """Publish data to MQTT broker with error handling"""
    logger = userdata["logger"]
    topic = userdata["topics"]["publish"]
    qos = userdata["qos"]
    encryptor = userdata.get("encryptor")

    action = "Retrying" if retry else "Sending"
    logger.debug(
        f"{action} {len(data)} bytes to {topic} " f"(QoS: {qos}, Encrypted: {bool(encryptor)})"
    )

    try:
        # Encrypt data if encryption is enabled
        if encryptor:
            data = encryptor.encrypt(data, topic.encode())
            logger.debug(f"Encrypted payload size: {len(data)} bytes")

        result = client.publish(topic, data, qos=qos)

        if result.rc == mqtt.MQTT_ERR_SUCCESS:
            if qos > 0:
                userdata["pending_count"] += 1
            if retry:
                userdata["current_chunk"] = None  # Clear stored chunk on successful retry

            if qos > 0:
                logger.debug(
                    f"Message sent (mid: {result.mid}), " f"pending: {userdata['pending_count']}"
                )
            else:
                logger.debug("Message sent (QoS 0)")
        else:
            error_msg = f"{'Retry' if retry else 'Publish'} failed (rc: {result.rc})"
            if not retry:
                error_msg += ", storing for retry"
                userdata["current_chunk"] = data
            logger.error(error_msg)

    except Exception as e:
        logger.error(f"Error sending data: {str(e)}")
        if not retry:
            userdata["current_chunk"] = data


def main():
    """Main application entry point"""
    # Parse arguments
    parser = setup_arg_parser()
    args = parser.parse_args()

    # Set up logging
    logger = setup_logging(args.verbose, args.quiet, args.log_file)

    # Validate arguments
    qos0_delay_seconds = validate_arguments(args, logger)

    # Load MQTT profile
    profiles = load_profiles(args.profiles_file)
    profile = profiles.get(args.profile_name)
    if not profile:
        logger.error(f"Profile '{args.profile_name}' not found")
        sys.exit(1)

    # Determine topics
    topics = get_topics(args.mode, args.prefix)

    # Log configuration details
    logger.info(f"Starting in {args.mode} mode with prefix: {args.prefix}")
    logger.debug(f"Configuration: {json.dumps(vars(args), indent=2)}")
    logger.debug(f"QoS0 delay: {qos0_delay_seconds:.6f}s")

    # Redact sensitive information in debug output
    safe_profile = profile.copy()
    if "password" in safe_profile:
        safe_profile["password"] = "***REDACTED***"
    if "encryption_key" in safe_profile:
        safe_profile["encryption_key"] = "***REDACTED***"
    logger.debug(f"Profile details: {json.dumps(safe_profile, indent=2)}")

    # Setup MQTT client
    client, userdata = setup_mqtt_client(profile, args, topics, qos0_delay_seconds, logger)

    # Start MQTT network loop
    client.loop_start()

    # Run main processing loop
    main_loop(client, userdata, args.chunk_size)


if __name__ == "__main__":
    main()
