"""A minimal, dependency-free MQTT 3.1.1 client.

Only the subset this project needs is implemented: CONNECT, PUBLISH at
QoS 0, SUBSCRIBE at QoS 0 (used by ``buzerkostka discover``), PINGREQ and
DISCONNECT. That is deliberately small -- shipping a stdlib-only client
means the whole project runs on a stock ``python3`` with no ``pip
install``, no virtualenv and no lockfile to rot.

If you would rather not speak MQTT at all, set ``"transport": "http"``
and the daemon will drive the cube's own ``GET /set-color`` endpoint over
the LAN instead.

Reference: MQTT Version 3.1.1, OASIS Standard.
"""

from __future__ import annotations

import os
import random
import socket
import struct
import time

# Control packet types (upper nibble of byte 1).
CONNECT = 0x10
CONNACK = 0x20
PUBLISH = 0x30
SUBSCRIBE = 0x80
SUBACK = 0x90
PINGREQ = 0xC0
PINGRESP = 0xD0
DISCONNECT = 0xE0

CONNACK_ERRORS = {
    0: "connection accepted",
    1: "unacceptable protocol version",
    2: "client identifier rejected",
    3: "server unavailable",
    4: "bad username or password",
    5: "not authorized",
}


class MqttError(Exception):
    """Any protocol, socket or authentication failure."""


def encode_remaining_length(length: int) -> bytes:
    """Encode MQTT's variable-length integer (1-4 bytes)."""
    if length < 0 or length > 268435455:
        raise MqttError("remaining length out of range: %d" % length)
    out = bytearray()
    while True:
        byte = length % 128
        length //= 128
        if length > 0:
            byte |= 0x80
        out.append(byte)
        if length == 0:
            return bytes(out)


def encode_string(text: str) -> bytes:
    """Encode an MQTT UTF-8 string (2-byte big-endian length prefix)."""
    raw = text.encode("utf-8")
    if len(raw) > 0xFFFF:
        raise MqttError("string too long for MQTT: %d bytes" % len(raw))
    return struct.pack(">H", len(raw)) + raw


def default_client_id() -> str:
    """A stable-ish but collision-resistant client id.

    Brokers kick off an existing session when a second client connects
    with the same id, so the random suffix matters when several machines
    drive the same cube.
    """
    return "buzerkostka-%d-%04x" % (os.getpid(), random.randint(0, 0xFFFF))


class MqttClient:
    """Blocking MQTT 3.1.1 client, QoS 0 only."""

    def __init__(
        self,
        host: str,
        port: int = 1883,
        username=None,
        password=None,
        client_id=None,
        keepalive: int = 30,
        tls: bool = False,
        tls_insecure: bool = False,
        ca_certs=None,
        timeout: float = 5.0,
    ):
        self.host = host
        self.port = int(port)
        self.username = username or None
        self.password = password or None
        self.client_id = client_id or default_client_id()
        self.keepalive = max(5, int(keepalive))
        self.tls = bool(tls)
        self.tls_insecure = bool(tls_insecure)
        self.ca_certs = ca_certs
        self.timeout = float(timeout)

        self._sock = None
        self._last_sent = 0.0
        self._packet_id = 0
        self._buffer = b""

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------
    @property
    def connected(self) -> bool:
        return self._sock is not None

    def connect(self) -> None:
        if self._sock is not None:
            return

        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        if self.tls:
            import ssl

            if self.ca_certs:
                context = ssl.create_default_context(cafile=self.ca_certs)
            else:
                context = ssl.create_default_context()
            if self.tls_insecure:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            try:
                sock = context.wrap_socket(sock, server_hostname=self.host)
            except Exception as exc:  # pragma: no cover - environment specific
                sock.close()
                raise MqttError("TLS handshake failed: %s" % exc)

        self._sock = sock
        self._buffer = b""
        try:
            self._send_connect()
            self._await_connack()
        except Exception:
            self.close()
            raise

    def _send_connect(self) -> None:
        flags = 0x02  # clean session
        if self.username:
            flags |= 0x80
        if self.password:
            flags |= 0x40

        variable_header = (
            encode_string("MQTT")
            + bytes([0x04])  # protocol level 4 == MQTT 3.1.1
            + bytes([flags])
            + struct.pack(">H", self.keepalive)
        )
        payload = encode_string(self.client_id)
        if self.username:
            payload += encode_string(self.username)
        if self.password:
            payload += encode_string(self.password)

        body = variable_header + payload
        self._write(bytes([CONNECT]) + encode_remaining_length(len(body)) + body)

    def _await_connack(self) -> None:
        deadline = time.time() + self.timeout
        while True:
            packet_type, _flags, body = self._read_packet(deadline)
            if packet_type == CONNACK:
                if len(body) < 2:
                    raise MqttError("malformed CONNACK")
                code = body[1]
                if code != 0:
                    raise MqttError(
                        "broker refused connection: %s (code %d)"
                        % (CONNACK_ERRORS.get(code, "unknown error"), code)
                    )
                return
            # Anything before CONNACK is a protocol violation; ignore
            # gracefully rather than crash the daemon.
            if time.time() > deadline:
                raise MqttError("timed out waiting for CONNACK")

    def disconnect(self) -> None:
        """Send DISCONNECT then drop the socket. Never raises."""
        if self._sock is None:
            return
        try:
            self._write(bytes([DISCONNECT]) + b"\x00")
        except Exception:
            pass
        self.close()

    def close(self) -> None:
        """Drop the socket without a DISCONNECT. Never raises."""
        sock, self._sock = self._sock, None
        self._buffer = b""
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Publishing / subscribing
    # ------------------------------------------------------------------
    def publish(self, topic: str, payload, retain: bool = False) -> None:
        """Publish at QoS 0 (fire and forget, no PUBACK to wait for)."""
        if self._sock is None:
            raise MqttError("not connected")
        if isinstance(payload, str):
            payload = payload.encode("utf-8")

        header = PUBLISH | (0x01 if retain else 0x00)
        body = encode_string(topic) + payload
        self._write(bytes([header]) + encode_remaining_length(len(body)) + body)

    def subscribe(self, topic: str, qos: int = 0) -> int:
        if self._sock is None:
            raise MqttError("not connected")
        self._packet_id = (self._packet_id % 0xFFFF) + 1
        body = (
            struct.pack(">H", self._packet_id) + encode_string(topic) + bytes([qos & 0x03])
        )
        self._write(
            bytes([SUBSCRIBE | 0x02]) + encode_remaining_length(len(body)) + body
        )

        deadline = time.time() + self.timeout
        while time.time() <= deadline:
            packet_type, _flags, ack = self._read_packet(deadline)
            if packet_type == SUBACK and len(ack) >= 3:
                if ack[2] & 0x80:
                    raise MqttError("broker rejected subscription to %r" % topic)
                return self._packet_id
        raise MqttError("timed out waiting for SUBACK")

    def read_messages(self, timeout: float = 1.0):
        """Poll for inbound PUBLISH packets. Returns ``[(topic, payload)]``.

        Also answers the broker's PINGRESP bookkeeping, so calling this in
        a loop is enough to keep a subscriber alive.
        """
        if self._sock is None:
            raise MqttError("not connected")
        messages = []
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return messages
            try:
                packet_type, _flags, body = self._read_packet(
                    deadline, block_hint=remaining
                )
            except socket.timeout:
                return messages
            if packet_type == PUBLISH:
                if len(body) < 2:
                    continue
                topic_len = struct.unpack(">H", body[0:2])[0]
                topic = body[2 : 2 + topic_len].decode("utf-8", "replace")
                # QoS 0 has no packet identifier, so the payload starts
                # right after the topic.
                messages.append((topic, body[2 + topic_len :]))

    def ping(self) -> None:
        if self._sock is None:
            raise MqttError("not connected")
        self._write(bytes([PINGREQ]) + b"\x00")

    def keepalive_tick(self) -> None:
        """Send a PINGREQ if the connection has been quiet for too long.

        Called from the daemon's render loop. A cube that sits on a solid
        colour publishes nothing for minutes, and brokers disconnect idle
        clients at 1.5x keepalive.
        """
        if self._sock is None:
            return
        if time.time() - self._last_sent < self.keepalive * 0.5:
            return
        self.ping()
        self._drain()

    # ------------------------------------------------------------------
    # Low-level I/O
    # ------------------------------------------------------------------
    def _write(self, data: bytes) -> None:
        assert self._sock is not None
        try:
            self._sock.sendall(data)
        except OSError as exc:
            self.close()
            raise MqttError("send failed: %s" % exc)
        self._last_sent = time.time()

    def _drain(self) -> None:
        """Discard whatever the broker has sent us (PINGRESPs, mostly).

        Without this the kernel receive buffer for a long-running
        publisher slowly fills with PINGRESP packets.
        """
        if self._sock is None:
            return
        self._sock.settimeout(0.0)
        try:
            while True:
                chunk = self._sock.recv(4096)
                if not chunk:
                    self.close()
                    return
        except (BlockingIOError, socket.timeout):
            pass
        except OSError:
            self.close()
            return
        finally:
            if self._sock is not None:
                self._sock.settimeout(self.timeout)

    def _recv_into_buffer(self, block_hint) -> None:
        assert self._sock is not None
        self._sock.settimeout(block_hint if block_hint is not None else self.timeout)
        try:
            chunk = self._sock.recv(4096)
        except socket.timeout:
            raise
        except OSError as exc:
            self.close()
            raise MqttError("receive failed: %s" % exc)
        if not chunk:
            self.close()
            raise MqttError("broker closed the connection")
        self._buffer += chunk

    def _read_packet(self, deadline: float, block_hint=None):
        """Read one full control packet. Returns ``(type, flags, body)``."""
        while True:
            parsed = self._try_parse_packet()
            if parsed is not None:
                return parsed
            if time.time() > deadline:
                raise socket.timeout("packet read timed out")
            self._recv_into_buffer(block_hint)

    def _try_parse_packet(self):
        buffer = self._buffer
        if len(buffer) < 2:
            return None

        multiplier = 1
        length = 0
        index = 1
        while True:
            if index >= len(buffer):
                return None  # remaining-length field not complete yet
            byte = buffer[index]
            length += (byte & 0x7F) * multiplier
            index += 1
            if not byte & 0x80:
                break
            multiplier *= 128
            if multiplier > 128 ** 3:
                self.close()
                raise MqttError("malformed remaining length")

        total = index + length
        if len(buffer) < total:
            return None

        header = buffer[0]
        body = buffer[index:total]
        self._buffer = buffer[total:]
        return (header & 0xF0, header & 0x0F, body)

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc_info):
        self.disconnect()
        return False
