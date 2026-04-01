import argparse
import base64
import json
import os
import random
import struct
import sys
import time
from typing import Dict, Optional

from mqttcat import MQTTNetcat  # Assuming the class is available


class MQTTWormhole:
    def __init__(
        self,
        user_prefix: str,
        profile: Optional[Dict] = None,
        profile_name: Optional[str] = None,
        profiles_file: Optional[str] = None,
        qos: int = 0,
        chunk_size: int = 65536,
        verbose: bool = False,
        encrypt: bool = True,
        no_compress: bool = False,
    ):
        # Generate random suffix and create full prefix
        self.rand_suffix = "".join(random.choices("0123456789abcdef", k=4))
        self.full_prefix = f"{user_prefix}-{self.rand_suffix}"

        # Generate encryption secrets if requested
        self.encrypt = encrypt
        self.encryption_key = None
        self.encryption_salt = None
        self.encryption_iterations = 210000

        if encrypt:
            self.encryption_key = base64.b64encode(os.urandom(32)).decode("utf-8")
            self.encryption_salt = base64.b64encode(os.urandom(16)).decode("utf-8")

        self.profile = profile
        self.profile_name = profile_name
        self.profiles_file = profiles_file
        self.qos = qos
        self.chunk_size = chunk_size
        self.compression = 0 if no_compress else 1  # Compression enabled by default
        self.compression_level = 6  # Default compression level
        self.verbose = verbose
        self.data_nc = None
        self.ctrl_nc = None

    def _create_netcats(self, mode: str, encrypt: bool = False):
        """Create data and control channel instances with optional encryption"""
        encryption_args = {}
        if encrypt:
            encryption_args = {
                "encryption_key": self.encryption_key,
                "encryption_salt": self.encryption_salt,
                "encryption_iterations": self.encryption_iterations,
            }

        # Data channel
        self.data_nc = MQTTNetcat(
            mode=mode,
            prefix=self.full_prefix,
            profile=self.profile,
            profile_name=self.profile_name,
            profiles_file=self.profiles_file,
            qos=self.qos,
            chunk_size=self.chunk_size,
            compression_type=self.compression,
            compression_level=self.compression_level,
            verbose=self.verbose,
            **encryption_args,
        )

        # Control channel (appended -ctrl to full prefix)
        self.ctrl_nc = MQTTNetcat(
            mode=mode,
            prefix=self.full_prefix + "-ctrl",
            profile=self.profile,
            profile_name=self.profile_name,
            profiles_file=self.profiles_file,
            qos=self.qos,
            chunk_size=self.chunk_size,
            compression_type=0,  # No compression for control channel
            verbose=self.verbose,
        )

    def _handshake(self, is_sender: bool, timeout: int = 30) -> bool:
        """Perform secure handshake protocol"""
        start_time = time.time()
        handshake_complete = False

        if is_sender:
            # Sender waits for receiver to initiate
            print("Waiting for receiver to connect...", end="", flush=True)

            while time.time() - start_time < timeout:
                hello = self.ctrl_nc.receive(timeout=0.5)
                if hello == b"HELLO":
                    # Send encryption parameters if needed
                    if self.encrypt:
                        params = json.dumps(
                            {
                                "key": self.encryption_key,
                                "salt": self.encryption_salt,
                                "iterations": self.encryption_iterations,
                                "compression": self.compression,
                                "compression_level": self.compression_level,
                            }
                        ).encode("utf-8")
                        self.ctrl_nc.send(params)
                    else:
                        # Still send compression info
                        params = json.dumps(
                            {
                                "compression": self.compression,
                                "compression_level": self.compression_level,
                            }
                        ).encode("utf-8")
                        self.ctrl_nc.send(params)

                    # Wait for confirmation
                    ack = self.ctrl_nc.receive(timeout=5)
                    if ack == b"ACK":
                        handshake_complete = True
                        print(" connected!")
                    break
                print(".", end="", flush=True)
        else:
            # Receiver initiates handshake
            print("Connecting to sender...", end="", flush=True)
            self.ctrl_nc.send(b"HELLO")

            while time.time() - start_time < timeout:
                # Check for parameters
                params = self.ctrl_nc.receive(timeout=0.5)
                if params:
                    try:
                        params_dict = json.loads(params.decode("utf-8"))

                        # Update encryption parameters if enabled
                        if self.encrypt:
                            self.encryption_key = params_dict.get("key")
                            self.encryption_salt = params_dict.get("salt")
                            self.encryption_iterations = params_dict.get("iterations", 210000)

                        # Update compression settings from sender
                        self.compression = params_dict.get("compression", 0)
                        self.compression_level = params_dict.get("compression_level", 6)

                        handshake_complete = True
                    except json.JSONDecodeError:
                        print("\nInvalid parameters received")
                        break

                # Send final ack
                if handshake_complete:
                    self.ctrl_nc.send(b"ACK")
                    print(" connected!")
                    break
                print(".", end="", flush=True)

        if not handshake_complete:
            print("\nError: Handshake timed out")
        return handshake_complete

    def send_file(self, file_path: str):
        """Send a file through MQTT with secure handshake and metadata"""
        if not os.path.isfile(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")

        # Create control channel first for handshake
        self._create_netcats("connect")
        self.ctrl_nc.connect()

        # Display connection info
        print(f"Transfer ID: {self.rand_suffix}")
        print(
            f"Receiver command: python {sys.argv[0]} receive {self.full_prefix} "
            f"--profile {self.profile} --qos {self.qos} "
            f"{'--encrypt ' if self.encrypt else ''}{'--no-compress ' if self.compression == 0 else ''}--output ."
        )

        # Perform handshake
        if not self._handshake(is_sender=True):
            return

        # Create data channel with encryption after handshake
        self._create_netcats("connect", encrypt=self.encrypt)
        self.data_nc.connect()

        # Prepare and send file metadata
        file_name = os.path.basename(file_path)
        file_size = os.path.getsize(file_path)
        name_encoded = file_name.encode("utf-8")
        header = struct.pack(f">H{len(name_encoded)}sQ", len(name_encoded), name_encoded, file_size)
        self.data_nc.send(header)

        # Stream file content
        sent_bytes = 0
        with open(file_path, "rb") as f:
            while chunk := f.read(self.chunk_size):
                self.data_nc.send(chunk)
                sent_bytes += len(chunk)
                print(
                    f"\rSent: {sent_bytes}/{file_size} bytes " f"({sent_bytes/file_size:.1%})",
                    end="",
                    flush=True,
                )
        print("\nFile sent. Waiting for receiver to complete...")

        # Keep connection alive until receiver finishes
        while self.ctrl_nc.receive(timeout=1) != b"DONE":
            pass
        print("Transfer complete!")

    def receive_file(self, output_dir: str = "."):
        """Receive a file from MQTT with secure handshake"""
        # Create control channel first for handshake
        self._create_netcats("listen")
        self.ctrl_nc.connect()

        # Perform handshake
        if not self._handshake(is_sender=False):
            return

        # Create data channel with encryption after handshake
        self._create_netcats("listen", encrypt=self.encrypt)
        self.data_nc.connect()

        # Receive and parse metadata
        name_len = struct.unpack(">H", self._receive_exact(2))[0]
        file_name = self._receive_exact(name_len).decode("utf-8")
        file_size = struct.unpack(">Q", self._receive_exact(8))[0]
        output_path = os.path.join(output_dir, file_name)

        # Stream to file
        received = 0
        with open(output_path, "wb") as f:
            while received < file_size:
                chunk = self.data_nc.receive(timeout=60)
                if not chunk:
                    raise TimeoutError("Transfer interrupted")
                f.write(chunk)
                received += len(chunk)
                print(
                    f"\rReceived: {received}/{file_size} bytes " f"({received/file_size:.1%})",
                    end="",
                    flush=True,
                )

        # Notify sender of completion
        self.ctrl_nc.send(b"DONE")
        print(f"\nFile saved to: {output_path}")

    def _receive_exact(self, num_bytes: int) -> bytes:
        """Helper to receive exact number of bytes"""
        data = b""
        while len(data) < num_bytes:
            chunk = self.data_nc.receive(timeout=30)
            if not chunk:
                raise TimeoutError("Incomplete transfer")
            data += chunk
        return data[:num_bytes]

    def shutdown(self):
        """Clean up resources"""
        for nc in [self.data_nc, self.ctrl_nc]:
            if nc:
                try:
                    nc.disconnect()
                except Exception:
                    pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Secure MQTT File Transfer (Compression enabled by default)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("prefix", help="Base topic prefix")
    parser.add_argument("--profile", help="MQTT connection profile")
    parser.add_argument("--profile-name", help="Profile name from profiles file")
    parser.add_argument("--profiles-file", help="Custom profiles JSON file")
    parser.add_argument("--qos", type=int, default=0, choices=[0, 1, 2], help="MQTT QoS level")
    parser.add_argument(
        "--no-compress", action="store_true", help="Disable compression (enabled by default)"
    )
    parser.add_argument("--encrypt", action="store_true", help="Enable end-to-end encryption")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logs")

    subparsers = parser.add_subparsers(dest="command", required=True)

    send_parser = subparsers.add_parser("send")
    send_parser.add_argument("file", help="File to send")

    recv_parser = subparsers.add_parser("receive")
    recv_parser.add_argument("--output", default=".", help="Output directory")

    args = parser.parse_args()

    try:
        wormhole = MQTTWormhole(
            user_prefix=args.prefix,
            profile=args.profile,
            profile_name=args.profile_name,
            profiles_file=args.profiles_file,
            qos=args.qos,
            verbose=args.verbose,
            encrypt=args.encrypt,
            no_compress=args.no_compress,
        )

        if args.command == "send":
            wormhole.send_file(args.file)
        else:
            wormhole.receive_file(args.output)

    except KeyboardInterrupt:
        print("\nTransfer cancelled")
    except Exception as e:
        print(f"\nError: {str(e)}")
    finally:
        if "wormhole" in locals():
            wormhole.shutdown()
        sys.exit(0)
