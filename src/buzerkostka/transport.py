"""Ways to push a colour at a Buzerkostka.

Every transport exposes the same tiny surface:

* ``send(r, g, b)`` -- best effort, raises ``TransportError`` on failure
* ``close()``       -- never raises
* ``describe()``    -- one-line human readable target, for ``doctor``

Reconnection and back-off live in :class:`ReconnectingTransport`, which
wraps the real transport so the daemon's render loop never has to care
whether the broker went away.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from .mqtt import MqttClient, MqttError


class TransportError(Exception):
    """The colour could not be delivered."""


class BaseTransport:
    def send(self, r: int, g: int, b: int) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass

    def describe(self) -> str:
        return self.__class__.__name__


class MqttTransport(BaseTransport):
    """Publish ``{"r":…,"g":…,"b":…}`` to the cube's command topic."""

    def __init__(self, mqtt_config: dict, topic: str, device_password=None):
        if not topic:
            raise TransportError(
                "no device topic configured -- run 'buzerkostka setup' or set "
                "BUZERKOSTKA_TOPIC"
            )
        if not mqtt_config.get("host"):
            raise TransportError("no MQTT host configured")
        self.topic = topic
        self.device_password = device_password or None
        self.retain = bool(mqtt_config.get("retain", False))
        self._client = MqttClient(
            host=mqtt_config["host"],
            port=mqtt_config.get("port", 1883),
            username=mqtt_config.get("username"),
            password=mqtt_config.get("password"),
            client_id=mqtt_config.get("client_id"),
            keepalive=mqtt_config.get("keepalive", 30),
            tls=mqtt_config.get("tls", False),
            tls_insecure=mqtt_config.get("tls_insecure", False),
            ca_certs=mqtt_config.get("ca_certs"),
            timeout=mqtt_config.get("timeout", 5.0),
        )

    def send(self, r: int, g: int, b: int) -> None:
        payload = {"r": int(r), "g": int(g), "b": int(b)}
        if self.device_password:
            payload["password"] = self.device_password
        try:
            if not self._client.connected:
                self._client.connect()
            self._client.publish(
                self.topic, json.dumps(payload, separators=(",", ":")), self.retain
            )
        except (MqttError, OSError) as exc:
            self._client.close()
            raise TransportError(str(exc))

    def tick(self) -> None:
        try:
            self._client.keepalive_tick()
        except (MqttError, OSError):
            self._client.close()

    def close(self) -> None:
        self._client.disconnect()

    def describe(self) -> str:
        scheme = "mqtts" if self._client.tls else "mqtt"
        auth = "%s@" % self._client.username if self._client.username else ""
        return "%s://%s%s:%d topic=%s" % (
            scheme,
            auth,
            self._client.host,
            self._client.port,
            self.topic,
        )


class HttpTransport(BaseTransport):
    """Drive the cube's own ``GET /set-color`` endpoint directly over LAN."""

    def __init__(self, http_config: dict, device_password=None):
        host = http_config.get("host")
        if not host:
            raise TransportError("no HTTP host configured for the cube")
        self.base = "http://%s:%d" % (host, int(http_config.get("port", 80)))
        self.timeout = float(http_config.get("timeout", 2.0))
        self.device_password = device_password or None

    def send(self, r: int, g: int, b: int) -> None:
        query = {"r": int(r), "g": int(g), "b": int(b)}
        if self.device_password:
            query["password"] = self.device_password
        url = "%s/set-color?%s" % (self.base, urllib.parse.urlencode(query))
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as response:
                response.read(256)
        except (urllib.error.URLError, OSError) as exc:
            raise TransportError(str(exc))

    def describe(self) -> str:
        return "%s/set-color" % self.base


class ConsoleTransport(BaseTransport):
    """Render to the terminal with a 24-bit ANSI swatch.

    Lets you develop and demo the whole state machine with no hardware --
    also what the test suite uses.
    """

    def __init__(self, stream=None):
        self.stream = stream or sys.stderr
        self.last = None

    def send(self, r: int, g: int, b: int) -> None:
        if (r, g, b) == self.last:
            return
        self.last = (r, g, b)
        self.stream.write(
            "\x1b[48;2;%d;%d;%dm    \x1b[0m #%02X%02X%02X\n" % (r, g, b, r, g, b)
        )
        self.stream.flush()

    def describe(self) -> str:
        return "console (no hardware)"


class NullTransport(BaseTransport):
    """Swallow everything.

    Selected with ``"transport": "null"``: exercises the whole state
    machine with no cube, no broker and no output at all.
    """

    def __init__(self):
        self.frames = []

    def send(self, r: int, g: int, b: int) -> None:
        self.frames.append((r, g, b))

    def describe(self) -> str:
        return "null (discarding frames)"


class ReconnectingTransport(BaseTransport):
    """Wrap a transport with exponential back-off and forced re-send.

    The daemon suppresses duplicate frames, so after a reconnect the cube
    could otherwise sit on a stale colour forever. ``dirty`` tells the
    render loop to publish the current frame again even if it is
    unchanged.
    """

    def __init__(self, inner: BaseTransport, min_backoff=1.0, max_backoff=30.0):
        self.inner = inner
        self.min_backoff = float(min_backoff)
        self.max_backoff = float(max_backoff)
        self._backoff = self.min_backoff
        self._retry_at = 0.0
        self.healthy = True
        self.dirty = False
        self.last_error = None
        self.failures = 0

    def send(self, r: int, g: int, b: int) -> bool:
        """Returns True only if the frame really reached the device."""
        now = time.time()
        if not self.healthy and now < self._retry_at:
            return False  # still cooling off; drop this frame silently
        try:
            self.inner.send(r, g, b)
        except TransportError as exc:
            self.failures += 1
            self.last_error = str(exc)
            self.healthy = False
            self._retry_at = now + self._backoff
            self._backoff = min(self.max_backoff, self._backoff * 2)
            self.dirty = True
            return False
        if not self.healthy:
            self.healthy = True
            self.last_error = None
        self._backoff = self.min_backoff
        self.dirty = False
        return True

    def tick(self) -> None:
        tick = getattr(self.inner, "tick", None)
        if tick is not None and self.healthy:
            tick()

    def close(self) -> None:
        try:
            self.inner.close()
        except Exception:
            pass

    def describe(self) -> str:
        return self.inner.describe()


def build_transport(config: dict) -> BaseTransport:
    """Create the transport named by ``config["transport"]``."""
    kind = (config.get("transport") or "mqtt").lower()
    device = config.get("device") or {}
    password = device.get("password")

    if kind == "mqtt":
        return MqttTransport(config.get("mqtt") or {}, device.get("topic"), password)
    if kind == "http":
        return HttpTransport(config.get("http") or {}, password)
    if kind == "console":
        return ConsoleTransport()
    if kind in ("null", "none", "off"):
        return NullTransport()
    raise TransportError(
        "unknown transport %r (expected mqtt, http, console or null)" % kind
    )
