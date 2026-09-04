"""State definitions and the frame renderer.

A *state* is a colour plus an effect. The renderer is a pure function of
``(state, elapsed_seconds)``, which keeps it trivially testable and means
the daemon can be restarted mid-animation without visible glitches.

Two flavours of state exist:

*persistent*
    ``solid``, ``off``, ``breathe``, ``blink``, ``rainbow``. Lasts until
    something else replaces it.

*transient*
    ``flash`` with a ``count``. Plays for ``count * period`` seconds and
    then hands control back to the session's *base* state. This is how
    "green blink on finish" or "magenta stutter on error" work without
    losing track of what the session was actually doing.
"""

from __future__ import annotations

import math

from .colors import apply_channel_order, parse_color, scale, hsv_to_rgb

EFFECTS = ("solid", "off", "breathe", "blink", "flash", "rainbow")


class State:
    """A resolved, validated state definition."""

    __slots__ = (
        "name",
        "color",
        "effect",
        "level",
        "min",
        "max",
        "period",
        "duty",
        "count",
        "priority",
        "timeout",
    )

    def __init__(self, name: str, definition: dict):
        definition = definition or {}
        self.name = name
        self.color = parse_color(definition.get("color", "#000000"))
        self.effect = definition.get("effect", "solid")
        if self.effect not in EFFECTS:
            raise ValueError(
                "state %r: unknown effect %r" % (name, definition.get("effect"))
            )
        self.level = _clamp01(definition.get("level", 1.0))
        self.min = _clamp01(definition.get("min", 0.05))
        self.max = _clamp01(definition.get("max", 1.0))
        self.period = max(0.05, float(definition.get("period", 1.0)))
        self.duty = _clamp01(definition.get("duty", 0.5))
        count = definition.get("count")
        self.count = int(count) if count else 0
        self.priority = int(definition.get("priority", 0))
        timeout = definition.get("timeout")
        self.timeout = float(timeout) if timeout else None

    @property
    def transient(self) -> bool:
        """Does this state finish on its own?"""
        return self.effect == "flash" and self.count > 0

    @property
    def duration(self):
        """Total play time for a transient state, else ``None``."""
        return self.count * self.period if self.transient else None

    @property
    def animated(self) -> bool:
        """Does the frame change over time? Static states let the daemon sleep."""
        return self.effect in ("breathe", "blink", "flash", "rainbow")

    def __repr__(self):  # pragma: no cover - debugging aid
        return "<State %s %s>" % (self.name, self.effect)


def _clamp01(value) -> float:
    value = float(value)
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def build_states(config_states: dict) -> dict:
    """Turn the raw config mapping into ``{name: State}``."""
    states = {}
    for name, definition in (config_states or {}).items():
        states[name] = State(name, definition)
    if "off" not in states:
        states["off"] = State("off", {"color": "#000000", "effect": "off"})
    return states


def render(state: State, elapsed: float, gamma: float = 2.0):
    """Compute the colour for ``state`` ``elapsed`` seconds into its life.

    Returns ``(r, g, b)``, or ``None`` when a transient state has finished
    and the caller should fall back to the base state.
    """
    if elapsed < 0.0:
        elapsed = 0.0

    if state.effect == "off":
        return (0, 0, 0)

    if state.effect == "solid":
        return scale(state.color, state.level)

    if state.effect == "breathe":
        # Raised cosine gives a smooth turnaround at both ends; the gamma
        # exponent makes the dim half last longer, which is what reads as
        # "breathing" rather than "fading".
        wave = (1.0 - math.cos(2.0 * math.pi * elapsed / state.period)) / 2.0
        level = state.min + (state.max - state.min) * (wave ** gamma)
        return scale(state.color, level)

    if state.effect == "blink":
        phase = (elapsed % state.period) / state.period
        return scale(state.color, state.max if phase < state.duty else 0.0)

    if state.effect == "flash":
        if state.transient and elapsed >= state.duration:
            return None
        phase = (elapsed % state.period) / state.period
        return scale(state.color, state.max if phase < state.duty else 0.0)

    if state.effect == "rainbow":
        return scale(hsv_to_rgb(elapsed / state.period), state.max)

    raise ValueError("unhandled effect %r" % state.effect)  # pragma: no cover


def next_frame_delay(state: State, fps: int) -> float:
    """How long the render loop may sleep before the frame changes.

    Static states get a long sleep so an idle daemon costs no CPU;
    ``blink`` only needs to wake at its edges, but sleeping a full frame
    keeps the code simple and 20 fps is already nearly free.
    """
    if not state.animated:
        return 1.0
    return 1.0 / max(1, int(fps))


def frame_for_transport(rgb, brightness: float, channel_order: str):
    """Apply the global brightness multiplier and the device channel order."""
    if brightness < 1.0:
        rgb = scale(rgb, brightness)
    return apply_channel_order(rgb, channel_order)
