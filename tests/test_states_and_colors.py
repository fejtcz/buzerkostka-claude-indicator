"""Tests for colour maths and the effect renderer."""

import pytest

from buzerkostka.colors import (
    apply_channel_order,
    hsv_to_rgb,
    parse_color,
    scale,
    to_hex,
)
from buzerkostka.states import State, build_states, frame_for_transport, render


# ----------------------------------------------------------------- colours
@pytest.mark.parametrize(
    "value,expected",
    [
        ("#FF0000", (255, 0, 0)),
        ("ff0000", (255, 0, 0)),
        ("#f00", (255, 0, 0)),
        ("orange", (255, 110, 0)),
        ([1, 2, 3], (1, 2, 3)),
        ((300, -5, 10), (255, 0, 10)),
    ],
)
def test_parse_color(value, expected):
    assert parse_color(value) == expected


@pytest.mark.parametrize("value", ["#GGGGGG", "nope", "#12345", 42, [1, 2]])
def test_parse_color_rejects_nonsense(value):
    with pytest.raises(ValueError):
        parse_color(value)


def test_to_hex_round_trips():
    assert to_hex(parse_color("#1A2B3C")) == "#1A2B3C"


def test_channel_order_rgb_is_a_no_op():
    assert apply_channel_order((10, 20, 30), "rgb") == (10, 20, 30)


def test_channel_order_grb_swaps_red_and_green():
    """What a Buzerkostka needs when its firmware double-swaps R and G."""
    assert apply_channel_order((255, 0, 0), "grb") == (0, 255, 0)
    assert apply_channel_order((0, 255, 0), "grb") == (255, 0, 0)
    assert apply_channel_order((0, 0, 255), "grb") == (0, 0, 255)


def test_every_channel_order_is_a_permutation():
    from buzerkostka.colors import CHANNEL_ORDERS

    for order in CHANNEL_ORDERS:
        assert sorted(apply_channel_order((1, 2, 3), order)) == [1, 2, 3]


def test_unknown_channel_order_raises():
    with pytest.raises(ValueError):
        apply_channel_order((1, 2, 3), "xyz")


def test_scale_clamps_and_rounds():
    assert scale((255, 255, 255), 0.0) == (0, 0, 0)
    assert scale((255, 255, 255), 1.0) == (255, 255, 255)
    assert scale((100, 200, 50), 0.5) == (50, 100, 25)


def test_hsv_covers_the_wheel():
    assert hsv_to_rgb(0.0) == (255, 0, 0)
    assert hsv_to_rgb(1 / 3) == (0, 255, 0)
    assert hsv_to_rgb(2 / 3) == (0, 0, 255)


# ------------------------------------------------------------------ states
def test_solid_is_constant():
    state = State("working", {"color": "#FF0000", "effect": "solid"})
    assert render(state, 0.0) == (255, 0, 0)
    assert render(state, 99.0) == (255, 0, 0)
    assert not state.animated


def test_off_is_always_black():
    state = State("off", {"color": "#FFFFFF", "effect": "off"})
    assert render(state, 5.0) == (0, 0, 0)


def test_breathe_starts_dark_peaks_mid_cycle_and_returns():
    state = State(
        "idle",
        {"color": "#FFFFFF", "effect": "breathe", "period": 4.0, "min": 0.0, "max": 1.0},
    )
    assert render(state, 0.0) == (0, 0, 0)
    assert render(state, 2.0) == (255, 255, 255)
    assert render(state, 4.0) == (0, 0, 0)
    # Monotone rising over the first half.
    levels = [render(state, t)[0] for t in (0.0, 0.5, 1.0, 1.5, 2.0)]
    assert levels == sorted(levels)


def test_breathe_respects_its_min_and_max():
    state = State(
        "idle",
        {"color": "#FFFFFF", "effect": "breathe", "period": 2.0, "min": 0.2, "max": 0.6},
    )
    assert render(state, 0.0) == (51, 51, 51)
    assert render(state, 1.0) == (153, 153, 153)


def test_blink_is_a_square_wave():
    state = State(
        "permission",
        {"color": "#FF8000", "effect": "blink", "period": 0.5, "duty": 0.5},
    )
    assert render(state, 0.0) == (255, 128, 0)
    assert render(state, 0.3) == (0, 0, 0)
    assert render(state, 0.5) == (255, 128, 0), "the wave must repeat"


def test_blink_duty_shifts_the_edge():
    state = State("x", {"color": "#FFFFFF", "effect": "blink", "period": 1.0, "duty": 0.2})
    assert render(state, 0.1) == (255, 255, 255)
    assert render(state, 0.3) == (0, 0, 0)


def test_flash_finishes_and_signals_the_caller():
    state = State(
        "done",
        {"color": "#00FF40", "effect": "flash", "count": 1, "period": 0.6, "duty": 0.7},
    )
    assert state.transient
    assert state.duration == pytest.approx(0.6)
    assert render(state, 0.0) == (0, 255, 64)
    assert render(state, 0.5) == (0, 0, 0)
    assert render(state, 0.6) is None, "a finished flash returns None"


def test_multi_blink_flash_duration():
    state = State("error", {"effect": "flash", "count": 4, "period": 0.25, "color": "#F0F"})
    assert state.duration == pytest.approx(1.0)


def test_unknown_effect_is_rejected_at_construction():
    with pytest.raises(ValueError):
        State("bad", {"effect": "strobe"})


def test_build_states_always_provides_off():
    states = build_states({"idle": {"color": "#00FFFF"}})
    assert "off" in states


def test_frame_for_transport_combines_brightness_and_order():
    assert frame_for_transport((255, 0, 0), 0.5, "grb") == (0, 128, 0)
