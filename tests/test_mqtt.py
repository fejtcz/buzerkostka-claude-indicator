"""Tests for the hand-rolled MQTT 3.1.1 client.

The interesting one is ``test_publish_round_trip``: it stands up a
throwaway TCP server that speaks just enough MQTT to accept a connection,
then asserts on the exact bytes our client puts on the wire. That is the
only way to be confident in a protocol implementation you wrote yourself.
"""

import socket
import struct
import threading

import pytest

from buzerkostka.mqtt import (
    MqttClient,
    MqttError,
    encode_remaining_length,
    encode_string,
)


def test_encode_remaining_length_boundaries():
    assert encode_remaining_length(0) == b"\x00"
    assert encode_remaining_length(127) == b"\x7f"
    assert encode_remaining_length(128) == b"\x80\x01"
    assert encode_remaining_length(16383) == b"\xff\x7f"
    assert encode_remaining_length(16384) == b"\x80\x80\x01"
    assert encode_remaining_length(268435455) == b"\xff\xff\xff\x7f"


def test_encode_remaining_length_rejects_out_of_range():
    with pytest.raises(MqttError):
        encode_remaining_length(268435456)


def test_encode_string_is_length_prefixed_utf8():
    assert encode_string("MQTT") == b"\x00\x04MQTT"
    assert encode_string("kostka") == b"\x00\x06kostka"
    # Multi-byte characters count as bytes, not code points.
    assert encode_string("č") == b"\x00\x02\xc4\x8d"


class FakeBroker:
    """Accepts one client, replies CONNACK, records everything after."""

    def __init__(self, connack_code=0):
        self.connack_code = connack_code
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        self.port = self.server.getsockname()[1]
        self.received = b""
        self.connect_packet = None
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        conn, _ = self.server.accept()
        conn.settimeout(3.0)
        try:
            self.connect_packet = conn.recv(4096)
            conn.sendall(bytes([0x20, 0x02, 0x00, self.connack_code]))
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                self.received += chunk
        except OSError:
            pass
        finally:
            conn.close()

    def close(self):
        self.server.close()


def test_publish_round_trip():
    broker = FakeBroker()
    client = MqttClient("127.0.0.1", broker.port, client_id="test-client", timeout=3.0)
    try:
        client.connect()
        client.publish("devices/kostka/ABC", '{"r":255,"g":0,"b":0}')
    finally:
        client.disconnect()
        broker._thread.join(timeout=3.0)
        broker.close()

    # CONNECT: fixed header, protocol name, level 4, clean session.
    connect = broker.connect_packet
    assert connect[0] == 0x10
    assert b"\x00\x04MQTT\x04" in connect
    assert b"test-client" in connect

    published = broker.received
    assert published[0] == 0x30  # PUBLISH, QoS 0, no retain
    remaining = published[1]
    body = published[2 : 2 + remaining]
    topic_len = struct.unpack(">H", body[0:2])[0]
    assert body[2 : 2 + topic_len] == b"devices/kostka/ABC"
    assert body[2 + topic_len :] == b'{"r":255,"g":0,"b":0}'
    assert published.endswith(b"\xe0\x00")  # DISCONNECT


def test_credentials_are_sent_when_configured():
    broker = FakeBroker()
    client = MqttClient(
        "127.0.0.1", broker.port, username="ha", password="s3cret",
        client_id="cid", timeout=3.0,
    )
    try:
        client.connect()
    finally:
        client.disconnect()
        broker._thread.join(timeout=3.0)
        broker.close()

    connect = broker.connect_packet
    flags = connect[connect.index(b"\x00\x04MQTT") + 7]
    assert flags & 0x80, "username flag must be set"
    assert flags & 0x40, "password flag must be set"
    assert flags & 0x02, "clean session must be set"
    assert b"ha" in connect and b"s3cret" in connect


def test_refused_connection_raises_with_a_readable_reason():
    broker = FakeBroker(connack_code=4)
    client = MqttClient("127.0.0.1", broker.port, timeout=3.0)
    with pytest.raises(MqttError) as excinfo:
        client.connect()
    broker.close()
    assert "bad username or password" in str(excinfo.value)
    assert not client.connected


def test_publish_without_connection_raises():
    client = MqttClient("127.0.0.1", 1)
    with pytest.raises(MqttError):
        client.publish("t", "x")


def test_connection_refused_is_reported():
    # Port 1 on loopback: nothing listens there.
    client = MqttClient("127.0.0.1", 1, timeout=1.0)
    with pytest.raises((MqttError, OSError)):
        client.connect()
