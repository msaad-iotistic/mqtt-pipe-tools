#!/usr/bin/env python3
import argparse
import base64
import contextlib
import json
import logging
import os
import queue
import select
import signal
import ssl
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, TypedDict, Union

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


class MQTTNetcat:
    """MQTT Netcat-like tool with programmatic interface"""

    def __init__(
        self,
        mode: str,
        prefix: str,
        profile: Union[Dict[str, Any], str, None] = None,
        profile_name: Optional[str] = None,
        profiles_file: Optional[str] = None,
        qos: int = DEFAULTS["QOS"],
        keepalive: int = DEFAULTS["KEEPALIVE"],
        chunk_size: int = DEFAULTS["CHUNK_SIZE"],
        max_pending: int = DEFAULTS["MAX_PENDING"],
        qos0_delay: float = DEFAULTS["QOS0_DELAY_MS"],
        verbose: bool = False,
        quiet: bool = False,
        log_file: Optional[str] = None,
        receive_callback: Optional[Callable[[bytes], None]] = None,
    ):
        """
        Initialize MQTTNetcat instance

        :param mode: 'listen' or 'connect'
        :param prefix: Topic prefix for communication
        :param profile: Profile dictionary or path to profile JSON file
        :param profile_name: Profile name to use from profiles file (if using profiles_file)
        :param profiles_file: JSON file containing MQTT profiles (if using profile_name)
        :param qos: Quality of Service level (0, 1, or 2)
        :param keepalive: Keepalive interval in seconds
        :param chunk_size: Chunk size for data transmission
        :param max_pending: Max pending acknowledgments before throttling
        :param qos0_delay: Delay between QoS 0 sends in milliseconds
        :param verbose: Enable verbose logging
        :param quiet: Reduce logging to warnings and errors only
        :param log_file: File to write logs to
        :param receive_callback: Callback function for received data
        """
        self.mode = mode
        self.prefix = prefix
        self.profile = profile
        self.profile_name = profile_name
        self.profiles_file = profiles_file
        self.qos = qos
        self.keepalive = keepalive
        self.chunk_size = chunk_size
        self.max_pending = max_pending
        self.qos0_delay = qos0_delay
        self.verbose = verbose
        self.quiet = quiet
        self.log_file = log_file
        self.receive_callback = receive_callback

        # Runtime state
        self.logger = self._setup_logging()
        self.qos0_delay_seconds = self._validate_arguments()
        self.topics = self._get_topics()
        self.userdata: UserDataType = {
            "topics": self.topics,
            "disconnected": None,
            "qos": self.qos,
            "pending_count": 0,
            "max_pending": self.max_pending,
            "current_chunk": None,
            "qos0_delay": self.qos0_delay_seconds,
            "logger": self.logger,
            "encryptor": None,
        }
        self.client = None
        self.receive_queue = queue.Queue()
        self.running = False
        self.immediate_shutdown = False
        self.last_send_time = 0

        # Process profile input
        self._process_profile_input()
        self._setup_encryption()

    def _setup_logging(self) -> logging.Logger:
        """Configure logging system with proper levels"""
        logger = logging.getLogger(f"mqttnc_{self.mode}")

        # Set base logging level
        if self.verbose:
            logger.setLevel(logging.DEBUG)
        elif self.quiet:
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
        if self.log_file:
            file_handler = logging.FileHandler(self.log_file)
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        else:
            # Create console handler
            console_handler = logging.StreamHandler(sys.stderr)
            console_handler.setFormatter(formatter)
            logger.addHandler(console_handler)

        return logger

    def _validate_arguments(self) -> float:
        """Validate and warn about problematic argument values"""
        # Validate chunk size
        if self.chunk_size < 256:
            self.logger.warning("Small chunk sizes (<256B) may reduce performance")
        elif self.chunk_size > 1024 * 1024:
            self.logger.warning("Large chunk sizes (>1MB) may cause buffer issues")

        # Validate max pending
        if self.max_pending < 1:
            self.logger.error("Max pending must be at least 1")
            raise ValueError("Max pending must be at least 1")

        # Convert milliseconds to seconds for internal use
        qos0_delay_seconds = self.qos0_delay / 1000.0

        # Validate QoS 0 delay
        if self.qos0_delay < 0:
            self.logger.error("QoS 0 delay must be non-negative")
            raise ValueError("QoS 0 delay must be non-negative")
        elif self.qos0_delay == 0:
            self.logger.warning("Zero QoS 0 delay may overwhelm broker/receiver")
        elif self.qos0_delay > 100:  # 100ms
            self.logger.warning("Large QoS 0 delay (>100ms) may reduce throughput")

        return qos0_delay_seconds

    def _process_profile_input(self):
        """Process profile input from various sources"""
        # Case 1: Profile provided directly as a dictionary
        if isinstance(self.profile, dict):
            self.logger.debug("Using directly provided profile dictionary")
            self._validate_profile(self.profile)
            return

        # Case 2: Profile provided as a string (file path)
        if isinstance(self.profile, str):
            self.logger.debug(f"Loading profile from file: {self.profile}")
            self.profiles_file = self.profile
            self.profile_name = "default"

        # Case 3: Using profiles_file and profile_name
        if self.profiles_file and self.profile_name:
            self.logger.debug(f"Loading profiles from: {self.profiles_file}")
            profiles = self._load_profiles(self.profiles_file)
            self.profile = profiles.get(self.profile_name)
            if not self.profile:
                raise ValueError(f"Profile '{self.profile_name}' not found in {self.profiles_file}")
            return

        # No valid profile source found
        raise ValueError(
            "No valid profile provided. Must specify profile, profile_name+profiles_file, or profile as file path"
        )

    def _validate_profile(self, profile: dict):
        """Validate profile structure"""
        if "host" not in profile:
            raise ValueError("Profile missing 'host' field")
        if "port" not in profile:
            raise ValueError("Profile missing 'port' field")
        self.profile = profile

    def _load_profiles(self, filename: str) -> Dict[str, Any]:
        """Load MQTT profiles from JSON file"""
        try:
            with open(filename, "r") as f:
                profiles = json.load(f)
                return profiles
        except Exception as e:
            self.logger.error(f"Error loading profiles: {str(e)}")
            raise

    def _get_topics(self) -> Dict[str, str]:
        """Determine publish/subscribe topics based on operation mode"""
        if self.mode == "listen":
            return {"subscribe": f"{self.prefix}/listen", "publish": f"{self.prefix}/connect"}
        return {"subscribe": f"{self.prefix}/connect", "publish": f"{self.prefix}/listen"}

    def _setup_encryption(self):
        """Set up encryption if configured"""
        if not self.profile:
            return

        if "encryption_key" in self.profile:
            try:
                salt = (
                    base64.b64decode(self.profile["encryption_salt"])
                    if "encryption_salt" in self.profile
                    else b""
                )
                iterations = self.profile.get("encryption_iterations", 210000)
                self.userdata["encryptor"] = Encryptor(
                    self.profile["encryption_key"], salt, iterations
                )
                self.logger.info("Encryption enabled")
                self.logger.debug(f"Using salt: {base64.b64encode(salt).decode()}")
                self.logger.debug(f"PBKDF2 iterations: {iterations}")
            except Exception as e:
                self.logger.error(f"Encryption setup failed: {str(e)}")
                raise

    def _configure_tls(self):
        """Configure TLS settings for MQTT client"""
        if not self.profile.get("tls", False):
            return

        tls_args = {
            "ca_certs": self.profile.get("ca_certs"),
            "certfile": self.profile.get("certfile"),
            "keyfile": self.profile.get("keyfile"),
            "cert_reqs": ssl.CERT_REQUIRED,
            "tls_version": ssl.PROTOCOL_TLS_CLIENT,
        }

        # Allow self-signed certificates if requested
        if self.profile.get("insecure", False):
            tls_args["cert_reqs"] = ssl.CERT_NONE

        # Remove None values from tls_args
        tls_args = {k: v for k, v in tls_args.items() if v is not None}

        try:
            self.client.tls_set(**tls_args)
            self.client.tls_insecure_set(self.profile.get("insecure", False))
            self.logger.debug("TLS configuration applied")
        except Exception as e:
            self.logger.error(f"TLS setup error: {str(e)}")
            raise

    def _on_connect(self, client: mqtt.Client, userdata: UserDataType, flags: Dict, rc: int):
        """Callback when connection to broker is established"""
        if rc == 0:
            self.client.subscribe(self.topics["subscribe"], qos=self.qos)
            self.logger.info(f"Connected to broker, subscribed to {self.topics['subscribe']}")
            self.logger.debug(f"Connection flags: {flags}, QoS: {self.qos}")
        else:
            self.logger.error(f"Connection failed with code {rc}")
            self.userdata["disconnected"] = rc

    def _on_message(self, client: mqtt.Client, userdata: UserDataType, msg: mqtt.MQTTMessage):
        """Callback for received messages"""
        self.logger.debug(
            f"Received message on {msg.topic} " f"(QoS: {msg.qos}, Size: {len(msg.payload)} bytes)"
        )

        try:
            payload = msg.payload

            # Decrypt if encryption is enabled
            encryptor = self.userdata.get("encryptor")
            if encryptor:
                payload = encryptor.decrypt(payload, msg.topic.encode())
                self.logger.debug("Message decrypted successfully")

            # Deliver data to either callback or queue
            if self.receive_callback:
                self.receive_callback(payload)
            else:
                self.receive_queue.put(payload)

        except Exception as e:
            self.logger.error(f"Message processing error: {str(e)}")

    def _on_disconnect(self, client: mqtt.Client, userdata: UserDataType, rc: int):
        """Callback when disconnected from broker"""
        self.userdata["disconnected"] = rc
        if rc != 0:
            message = MQTT_ERRORS.get(rc, f"Unexpected disconnect (rc: {rc})")
            self.logger.error(message)
        else:
            self.logger.info("Disconnected from broker")

    def _on_publish(self, client: mqtt.Client, userdata: UserDataType, mid: int):
        """Callback when message is acknowledged by broker"""
        self.userdata["pending_count"] -= 1
        self.logger.debug(f"Message acknowledged (mid: {mid})")

    def _on_log(self, client: mqtt.Client, userdata: UserDataType, level: int, buf: str):
        """Callback for MQTT client logging"""
        level_name = logging.getLevelName(level)
        self.logger.debug(f"[MQTT/{level_name}] {buf}")

    def connect(self):
        """Connect to MQTT broker and start network loop"""
        if not self.profile:
            raise RuntimeError("No profile available for connection")

        # Generate client ID with mode and timestamp
        client_id = f"mqttnc_{self.mode}_{int(time.time())}"
        self.client = mqtt.Client(client_id=client_id, clean_session=True, userdata=self.userdata)

        # Configure MQTT logging
        self.client.on_log = self._on_log

        # Register callbacks
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect
        self.client.on_publish = self._on_publish

        # Set credentials if available
        if "username" in self.profile and "password" in self.profile:
            username = self.profile["username"].strip()
            password = self.profile["password"].strip()
            self.client.username_pw_set(username, password)
            self.logger.debug(f"Using credentials for user: {username}")

        # Configure TLS
        self._configure_tls()

        # Connect to broker with error handling
        try:
            self.logger.debug(
                f"Connecting to {self.profile['host']}:{self.profile['port']} "
                f"(Keepalive: {self.keepalive}s)"
            )
            self.client.connect(self.profile["host"], int(self.profile["port"]), self.keepalive)
        except ConnectionRefusedError:
            self.logger.error("Connection refused. Check broker availability and port.")
            raise
        except ssl.SSLError as e:
            self.logger.error(f"TLS handshake failed: {str(e)}")
            raise
        except Exception as e:
            self.logger.error(f"Connection error: {str(e)}")
            raise

        # Start network loop in a background thread
        self.client.loop_start()

    def disconnect(self):
        """Disconnect from MQTT broker and clean up resources"""
        if self.client:
            self.logger.debug("Disconnecting from broker")
            self.client.disconnect()
            self.client.loop_stop()
            self.logger.info("Disconnected from broker")

    def send(self, data: bytes):
        """
        Send data to remote endpoint

        :param data: Bytes to send
        """
        self._send_data(data)

    def receive(self, timeout: Optional[float] = None) -> Optional[bytes]:
        """
        Receive data from remote endpoint

        :param timeout: Maximum time to wait in seconds (None blocks indefinitely)
        :return: Received bytes or None if timeout occurs
        """
        try:
            return self.receive_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def _send_data(self, data: bytes, retry: bool = False):
        """Publish data to MQTT broker with error handling"""
        if not self.client or not self.profile:
            self.logger.error("Cannot send data - not connected or no profile")
            return

        topic = self.topics["publish"]
        encryptor = self.userdata.get("encryptor")

        action = "Retrying" if retry else "Sending"
        self.logger.debug(
            f"{action} {len(data)} bytes to {topic} "
            f"(QoS: {self.qos}, Encrypted: {bool(encryptor)})"
        )

        try:
            # Encrypt data if encryption is enabled
            if encryptor:
                data = encryptor.encrypt(data, topic.encode())
                self.logger.debug(f"Encrypted payload size: {len(data)} bytes")

            result = self.client.publish(topic, data, qos=self.qos)

            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                if self.qos > 0:
                    self.userdata["pending_count"] += 1
                if retry:
                    self.userdata["current_chunk"] = None  # Clear stored chunk on success

                if self.qos > 0:
                    self.logger.debug(
                        f"Message sent (mid: {result.mid}), "
                        f"pending: {self.userdata['pending_count']}"
                    )
                else:
                    self.logger.debug("Message sent (QoS 0)")
            else:
                error_msg = f"{'Retry' if retry else 'Publish'} failed (rc: {result.rc})"
                if not retry:
                    error_msg += ", storing for retry"
                    self.userdata["current_chunk"] = data
                self.logger.error(error_msg)

        except Exception as e:
            self.logger.error(f"Error sending data: {str(e)}")
            if not retry:
                self.userdata["current_chunk"] = data

    def run_cli(self):
        """Run in CLI mode (reads from stdin, writes to stdout)"""

        # Signal handler for graceful shutdown
        def signal_handler(sig, frame):
            if self.running:
                # First Ctrl+C - initiate graceful shutdown
                self.logger.info("Shutting down...")
                self.running = False
            elif not self.immediate_shutdown:
                # Second Ctrl+C - force exit without sending remaining data
                self.immediate_shutdown = True
                self.logger.warning("Force shutdown requested. Disconnecting mqtt client...")
                self.disconnect()
                sys.exit(1)
            else:
                # Third Ctrl+C - force exit immediately
                self.logger.error("Force IMMEDIATE shutdown requested")
                sys.exit(1)

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        self.running = True
        self.logger.debug("Starting CLI processing loop")
        self.logger.debug(f"Chunk size: {self.chunk_size} bytes")
        self.logger.debug(f"Max pending: {self.userdata['max_pending']}")
        self.logger.debug(f"QoS 0 delay: {self.userdata['qos0_delay']*1000:.1f}ms")

        try:
            while self.running and not self.immediate_shutdown:
                # Check for disconnects
                if self.userdata["disconnected"] is not None:
                    if self.userdata["disconnected"] != 0:
                        self.logger.error("Disconnected from broker, exiting.")
                    break

                # Determine if we can send more data
                credit_available = True
                if self.qos > 0:
                    # QoS 1/2: Check pending acknowledgments
                    if self.userdata["pending_count"] >= self.userdata["max_pending"]:
                        credit_available = False
                        self.logger.debug(
                            f"Throttling: {self.userdata['pending_count']}/"
                            f"{self.userdata['max_pending']} pending messages"
                        )
                else:
                    # QoS 0: Rate limit using specified delay
                    current_time = time.monotonic()
                    if current_time - self.last_send_time < self.userdata["qos0_delay"]:
                        credit_available = False
                        elapsed = (current_time - self.last_send_time) * 1000
                        self.logger.debug(
                            f"QoS0 throttling: {elapsed:.1f}ms since last send "
                            f"(limit: {self.userdata['qos0_delay']*1000:.1f}ms)"
                        )
                    else:
                        self.last_send_time = current_time

                # Check for available data
                rlist, _, _ = select.select([sys.stdin], [], [], 0.1)

                if rlist and credit_available:
                    # Read and send new data
                    self.logger.debug(f"Reading stdin (chunk size: {self.chunk_size})")
                    data = sys.stdin.buffer.read1(self.chunk_size)
                    if not data:  # EOF
                        self.logger.debug("EOF reached on stdin")
                        break
                    self._send_data(data)

                # Retry any failed chunks
                elif self.userdata["current_chunk"] and credit_available:
                    self.logger.debug("Retrying previous chunk")
                    self._send_data(self.userdata["current_chunk"], retry=True)

                # Brief pause when throttled
                elif not credit_available:
                    sleep_time = min(0.01, self.userdata["qos0_delay"])
                    time.sleep(sleep_time)

                # Process received data
                while not self.receive_queue.empty():
                    try:
                        data = self.receive_queue.get_nowait()
                        sys.stdout.buffer.write(data)
                        sys.stdout.buffer.flush()
                    except BrokenPipeError:
                        self.logger.warning("Broken pipe - output closed")
                        self.disconnect()
                        return
                    except Exception as e:
                        self.logger.error(f"Error writing to stdout: {str(e)}")

        except Exception as e:
            self.logger.error(f"Runtime error: {str(e)}")
        finally:
            # Clean up resources
            self.logger.debug("Cleaning up resources")
            self.disconnect()
            self.logger.debug("Cleanup complete")


def main():
    """Main CLI entry point"""
    # Parse arguments
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

    args = parser.parse_args()

    # Create and run MQTTNetcat instance in CLI mode
    try:
        nc = MQTTNetcat(
            mode=args.mode,
            prefix=args.prefix,
            profile=args.profile,
            profile_name=args.profile_name,
            profiles_file=args.profiles_file,
            qos=args.qos,
            keepalive=args.keepalive,
            chunk_size=args.chunk_size,
            max_pending=args.max_pending,
            qos0_delay=args.qos0_delay,
            verbose=args.verbose,
            quiet=args.quiet,
            log_file=args.log_file,
        )
        nc.connect()
        nc.run_cli()
    except Exception as e:
        logging.error(f"Fatal error: {str(e)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
