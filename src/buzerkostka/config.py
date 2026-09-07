"""Configuration: built-in defaults, JSON file, environment overrides.

Precedence, lowest to highest:

1. :data:`DEFAULTS` below
2. ``~/.config/buzerkostka/config.json`` (or ``$BUZERKOSTKA_CONFIG``)
3. ``BUZERKOSTKA_*`` environment variables

Keeping usable defaults in code means a fresh clone only ever needs the
two things we cannot guess: the broker address and the cube's topic.
"""

from __future__ import annotations

import copy
import json
import os

from .paths import (  # re-exported: these are part of the config surface
    CONFIG_ENV,
    MAX_SOCKET_PATH,
    STATE_DIR_ENV,
    config_path,
    preferred_state_dir,
    socket_path,
    state_dir,
    state_dir_status,
)

__all__ = [
    "CONFIG_ENV",
    "ConfigError",
    "DEFAULTS",
    "ENV_OVERRIDES",
    "MAX_SOCKET_PATH",
    "STATE_DIR_ENV",
    "apply_env",
    "config_path",
    "deep_merge",
    "load",
    "preferred_state_dir",
    "save",
    "socket_path",
    "state_dir",
    "state_dir_status",
    "validate",
]

#: The complete default configuration. Every key is documented in
#: ``config.example.json``, which is generated from this dict.
DEFAULTS = {
    # --- where to send colours -------------------------------------
    "transport": "mqtt",  # mqtt | http | console | null
    "mqtt": {
        "host": "",
        "port": 1883,
        "username": "",
        "password": "",
        "client_id": "",  # empty -> auto-generated per process
        "keepalive": 30,
        "tls": False,
        "tls_insecure": False,
        "ca_certs": "",
        "timeout": 5.0,
        "retain": False,
    },
    "http": {
        "host": "",  # the cube's LAN address, e.g. 192.168.1.50
        "port": 80,
        "timeout": 2.0,
    },
    "device": {
        # Command topic from the cube's web config, normally
        # devices/kostka/<MAC-without-colons>.
        "topic": "",
        # Only needed if you set a device password in the cube's portal.
        "password": "",
        # The stock firmware feeds FastLED CRGB(g, r, b) into a strip
        # declared GRB, so red/green may arrive swapped. Run
        # `buzerkostka calibrate` if your colours look wrong.
        "channel_order": "rgb",
    },
    # --- rendering ---------------------------------------------------
    "render": {
        "fps": 20,  # animation frames per second
        "brightness": 1.0,  # global multiplier, 0.0 - 1.0
        "gamma": 2.0,  # waveform shaping for breathe/pulse
        "min_interval_ms": 40,  # hard floor between two publishes
        "resend_interval_s": 30,  # re-publish a static colour periodically
        "off_on_exit": True,  # blank the LED when the daemon stops
    },
    # --- daemon behaviour -------------------------------------------
    "daemon": {
        "session_ttl_s": 28800,  # forget a session after 8 h of silence
        "exit_after_idle_s": 0,  # 0 = stay resident forever
        "log_max_bytes": 262144,
        # Answering "No" to a permission prompt (or pressing Esc) fires no
        # hook whatsoever, so the daemon tails each session's transcript
        # for the interruption marker instead. See watch.py.
        "watch_transcript": True,
        "watch_interval_s": 1.0,
    },
    "enabled": True,
    # --- the visual language ----------------------------------------
    #
    # effect:  solid | off | breathe | blink | flash | rainbow
    # level:   brightness for `solid`                      (0.0 - 1.0)
    # min/max: brightness range for `breathe`              (0.0 - 1.0)
    # period:  seconds for one full cycle
    # duty:    fraction of the period that is lit (blink/flash)
    # count:   number of blinks for `flash` (makes it transient)
    # priority: when several sessions disagree, highest wins
    # timeout: seconds before this state decays to `idle` (safety net for
    #          a Claude Code process that dies without a SessionEnd hook)
    #
    # The shipped palette is the same three-colour language as the
    # macos-claude-indicator notch glow, so one glance means the same
    # thing on the cube and on the screen: green breathing = waiting for
    # you, solid red = Claude is busy, orange blinking = needs your answer.
    "states": {
        "off": {"color": "#000000", "effect": "off", "priority": 0},
        "idle": {
            "color": "#00FF40",
            "effect": "breathe",
            "period": 4.0,
            "min": 0.08,
            "max": 0.60,
            "priority": 10,
        },
        "working": {
            "color": "#FF0000",
            "effect": "solid",
            "level": 1.0,
            "priority": 30,
            "timeout": 900,
        },
        "permission": {
            "color": "#FF8000",
            "effect": "blink",
            "period": 0.5,
            "duty": 0.5,
            "priority": 80,
            "timeout": 1800,
        },
    },
    # --- hook event -> state ----------------------------------------
    #
    # Either a state name, or {"state": …, "base": …}. A transient state
    # (`flash` with a count) plays once and then reverts to the session's
    # base state; `base` lets an event change what it reverts *to*.
    # Setting `base` without `state` only moves the baseline.
    "events": {
        "session-start": "idle",
        "prompt-submit": "working",
        "tool-start": "working",
        "tool-end": "working",
        "tool-error": "working",
        "permission": "permission",
        "permission-denied": "working",
        "idle-nudge": "permission",
        "elicitation": "permission",
        "interrupted": "idle",
        "stop": "idle",
        "stop-error": "idle",
        "compact-start": "working",
        "compact-end": "working",
        "subagent-start": "working",
        "subagent-stop": "working",
        "teammate-idle": "permission",
        "session-end": "off",
    },
}


class ConfigError(Exception):
    """The config file exists but could not be used."""


def deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge ``overlay`` into a copy of ``base``."""
    result = copy.deepcopy(base)
    for key, value in (overlay or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _as_bool(text: str) -> bool:
    return str(text).strip().lower() in ("1", "true", "yes", "on")


#: ``ENV_VAR -> (dotted config path, coercion)``. Environment overrides
#: exist so CI, containers and dotfile managers can configure the cube
#: without writing a JSON file.
ENV_OVERRIDES = {
    "BUZERKOSTKA_TRANSPORT": ("transport", str),
    "BUZERKOSTKA_MQTT_HOST": ("mqtt.host", str),
    "BUZERKOSTKA_MQTT_PORT": ("mqtt.port", int),
    "BUZERKOSTKA_MQTT_USERNAME": ("mqtt.username", str),
    "BUZERKOSTKA_MQTT_PASSWORD": ("mqtt.password", str),
    "BUZERKOSTKA_MQTT_TLS": ("mqtt.tls", _as_bool),
    "BUZERKOSTKA_HTTP_HOST": ("http.host", str),
    "BUZERKOSTKA_HTTP_PORT": ("http.port", int),
    "BUZERKOSTKA_TOPIC": ("device.topic", str),
    "BUZERKOSTKA_DEVICE_PASSWORD": ("device.password", str),
    "BUZERKOSTKA_CHANNEL_ORDER": ("device.channel_order", str),
    "BUZERKOSTKA_BRIGHTNESS": ("render.brightness", float),
    "BUZERKOSTKA_FPS": ("render.fps", int),
    "BUZERKOSTKA_ENABLED": ("enabled", _as_bool),
}


def _set_dotted(target: dict, dotted: str, value) -> None:
    parts = dotted.split(".")
    node = target
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def apply_env(config: dict, environ=None) -> dict:
    environ = os.environ if environ is None else environ
    result = copy.deepcopy(config)
    for env_name, (dotted, coerce) in ENV_OVERRIDES.items():
        raw = environ.get(env_name)
        if raw is None or raw == "":
            continue
        try:
            _set_dotted(result, dotted, coerce(raw))
        except (TypeError, ValueError):
            raise ConfigError("%s=%r is not a valid value" % (env_name, raw))
    return result


def load(path=None, environ=None) -> dict:
    """Load the effective configuration."""
    path = path or config_path()
    user = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                user = json.load(handle)
        except ValueError as exc:
            raise ConfigError("%s is not valid JSON: %s" % (path, exc))
        except OSError as exc:
            raise ConfigError("cannot read %s: %s" % (path, exc))
        if not isinstance(user, dict):
            raise ConfigError("%s must contain a JSON object" % path)

    config = apply_env(deep_merge(DEFAULTS, user), environ)
    config["_path"] = path
    return config


def save(config: dict, path=None) -> str:
    """Write a config file, dropping internal keys."""
    path = path or config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {k: v for k, v in config.items() if not k.startswith("_")}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)  # it may hold broker credentials
    except OSError:
        pass
    return path


def validate(config: dict) -> list:
    """Return a list of human readable problems (empty means fine)."""
    from .colors import CHANNEL_ORDERS, parse_color
    from .states import EFFECTS

    problems = []
    transport = (config.get("transport") or "").lower()
    if transport not in ("mqtt", "http", "console", "null", "none", "off"):
        problems.append("transport: unknown value %r" % transport)
    if transport == "mqtt":
        if not (config.get("mqtt") or {}).get("host"):
            problems.append("mqtt.host: not set")
        if not (config.get("device") or {}).get("topic"):
            problems.append("device.topic: not set")
    if transport == "http" and not (config.get("http") or {}).get("host"):
        problems.append("http.host: not set")

    order = ((config.get("device") or {}).get("channel_order") or "rgb").lower()
    if order not in CHANNEL_ORDERS:
        problems.append("device.channel_order: %r is not one of %s" % (order, ", ".join(CHANNEL_ORDERS)))

    render = config.get("render") or {}
    if not 1 <= int(render.get("fps", 20)) <= 120:
        problems.append("render.fps: must be between 1 and 120")
    if not 0.0 <= float(render.get("brightness", 1.0)) <= 1.0:
        problems.append("render.brightness: must be between 0.0 and 1.0")

    states = config.get("states") or {}
    for name, definition in states.items():
        if not isinstance(definition, dict):
            problems.append("states.%s: must be an object" % name)
            continue
        effect = definition.get("effect", "solid")
        if effect not in EFFECTS:
            problems.append(
                "states.%s.effect: %r is not one of %s"
                % (name, effect, ", ".join(sorted(EFFECTS)))
            )
        try:
            parse_color(definition.get("color", "#000000"))
        except ValueError as exc:
            problems.append("states.%s.color: %s" % (name, exc))

    for event, mapping in (config.get("events") or {}).items():
        if isinstance(mapping, str):
            targets = [mapping]
        elif isinstance(mapping, dict):
            targets = [v for k, v in mapping.items() if k in ("state", "base") and v]
        else:
            problems.append("events.%s: must be a string or an object" % event)
            continue
        for target in targets:
            if target not in states:
                problems.append(
                    "events.%s: refers to unknown state %r" % (event, target)
                )
    return problems
