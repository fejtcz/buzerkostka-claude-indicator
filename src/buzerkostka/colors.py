"""Colour parsing, channel remapping and brightness scaling."""

from __future__ import annotations

NAMED_COLORS = {
    "off": (0, 0, 0),
    "black": (0, 0, 0),
    "red": (255, 0, 0),
    "green": (0, 255, 0),
    "blue": (0, 0, 255),
    "cyan": (0, 200, 200),
    "teal": (0, 200, 200),
    "magenta": (255, 0, 255),
    "purple": (160, 0, 255),
    "yellow": (255, 200, 0),
    "orange": (255, 110, 0),
    "amber": (255, 150, 0),
    "white": (255, 255, 255),
    "warmwhite": (255, 200, 140),
    "pink": (255, 60, 120),
}

#: Channel orders the cube may need. See ``docs/color-order.md`` -- the
#: stock firmware feeds FastLED ``CRGB(g, r, b)`` into a strip declared as
#: ``GRB``, so depending on the actual WS2812 revision on your board the
#: red and green channels may come out swapped.
CHANNEL_ORDERS = ("rgb", "rbg", "grb", "gbr", "brg", "bgr")


def parse_color(value) -> tuple:
    """Parse ``"#RRGGBB"``, ``"RRGGBB"``, ``"#RGB"``, a name or ``[r, g, b]``."""
    if isinstance(value, (list, tuple)):
        if len(value) != 3:
            raise ValueError("colour list must have exactly 3 items: %r" % (value,))
        return tuple(clamp8(int(c)) for c in value)

    if not isinstance(value, str):
        raise ValueError("unsupported colour value: %r" % (value,))

    text = value.strip().lower()
    if text in NAMED_COLORS:
        return NAMED_COLORS[text]

    text = text.lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        raise ValueError("cannot parse colour %r" % (value,))
    try:
        return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
    except ValueError:
        raise ValueError("cannot parse colour %r" % (value,))


def to_hex(rgb) -> str:
    return "#%02X%02X%02X" % (clamp8(rgb[0]), clamp8(rgb[1]), clamp8(rgb[2]))


def clamp8(value) -> int:
    value = int(round(value))
    if value < 0:
        return 0
    if value > 255:
        return 255
    return value


def apply_channel_order(rgb, order: str) -> tuple:
    """Reorder the logical (r, g, b) triple into what the device expects.

    ``order`` names the *device* channel order. ``"rgb"`` is a no-op;
    ``"grb"`` swaps red and green, which is what a Buzerkostka needs when
    its firmware/LED combination double-swaps those two channels.
    """
    order = (order or "rgb").lower()
    if order == "rgb":
        return (clamp8(rgb[0]), clamp8(rgb[1]), clamp8(rgb[2]))
    if order not in CHANNEL_ORDERS:
        raise ValueError(
            "unknown channel order %r (expected one of %s)"
            % (order, ", ".join(CHANNEL_ORDERS))
        )
    index = {"r": 0, "g": 1, "b": 2}
    return tuple(clamp8(rgb[index[ch]]) for ch in order)


def scale(rgb, level: float) -> tuple:
    """Scale a colour by a linear PWM factor in ``0.0 .. 1.0``."""
    if level <= 0.0:
        return (0, 0, 0)
    if level >= 1.0:
        return (clamp8(rgb[0]), clamp8(rgb[1]), clamp8(rgb[2]))
    return tuple(clamp8(channel * level) for channel in rgb)


def hsv_to_rgb(hue: float, saturation: float = 1.0, value: float = 1.0) -> tuple:
    """Minimal HSV -> RGB, ``hue`` in turns (0.0 .. 1.0). Used by ``rainbow``."""
    hue = hue % 1.0
    sector = int(hue * 6.0) % 6
    offset = hue * 6.0 - int(hue * 6.0)
    p = value * (1.0 - saturation)
    q = value * (1.0 - saturation * offset)
    t = value * (1.0 - saturation * (1.0 - offset))
    table = (
        (value, t, p),
        (q, value, p),
        (p, value, t),
        (p, q, value),
        (t, p, value),
        (value, p, q),
    )
    r, g, b = table[sector]
    return (clamp8(r * 255), clamp8(g * 255), clamp8(b * 255))
