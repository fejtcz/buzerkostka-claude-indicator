"""Tests for session tracking and multi-session aggregation."""

import copy
import time

import pytest

from buzerkostka.config import DEFAULTS
from buzerkostka.engine import Engine


@pytest.fixture
def engine():
    return Engine(copy.deepcopy(DEFAULTS))


def state_of(engine):
    return engine.snapshot()["active_state"]


def test_a_fresh_engine_is_dark(engine):
    assert engine.frame() == (0, 0, 0)
    assert state_of(engine) is None


def test_the_happy_path_walks_idle_working_idle(engine):
    engine.apply_event("session-start", session_id="s1")
    assert state_of(engine) == "idle"

    engine.apply_event("prompt-submit", session_id="s1")
    assert state_of(engine) == "working"
    assert engine.frame() == (255, 0, 0)

    # The shipped palette mirrors the macOS notch indicator: a finished
    # turn simply goes back to green breathing, no flash in between.
    engine.apply_event("stop", session_id="s1")
    assert state_of(engine) == "idle"


def test_a_transient_overlay_reveals_the_base_that_the_event_set():
    config = copy.deepcopy(DEFAULTS)
    config["states"]["done"] = {
        "color": "#00FF40", "effect": "flash", "count": 1, "period": 0.6, "duty": 0.7,
        "priority": 50,
    }
    config["events"]["stop"] = {"state": "done", "base": "idle"}
    engine = Engine(config)

    engine.apply_event("prompt-submit", session_id="s1")
    engine.apply_event("stop", session_id="s1")
    assert state_of(engine) == "done"

    # `done` is transient: once its 0.6 s are up the session reveals the
    # base state that `stop` set underneath it.
    engine.expire(time.time() + 1.0)
    assert state_of(engine) == "idle"


def test_permission_outranks_work_across_sessions(engine):
    engine.apply_event("prompt-submit", session_id="busy")
    engine.apply_event("permission", session_id="asking")
    assert state_of(engine) == "permission"
    assert engine.snapshot()["active_session"] == "asking"

    # Resolving the prompt on that session drops it back to working.
    engine.apply_event("tool-end", session_id="asking")
    assert state_of(engine) == "working"


def test_tool_error_keeps_working_by_default(engine):
    engine.apply_event("prompt-submit", session_id="s1")
    engine.apply_event("tool-error", session_id="s1")
    assert state_of(engine) == "working", "a failed tool call is still Claude working"


def test_a_transient_error_overlay_returns_to_the_base_state():
    config = copy.deepcopy(DEFAULTS)
    config["states"]["error"] = {
        "color": "#FF00FF", "effect": "flash", "count": 4, "period": 0.25, "priority": 90,
    }
    config["events"]["tool-error"] = {"state": "error"}
    engine = Engine(config)

    engine.apply_event("prompt-submit", session_id="s1")
    engine.apply_event("tool-error", session_id="s1")
    assert state_of(engine) == "error"

    engine.expire(time.time() + 2.0)
    assert state_of(engine) == "working", "an error must not lose the session's work"


def test_repeating_an_event_does_not_restart_the_animation(engine):
    engine.apply_event("prompt-submit", session_id="s1")
    started = engine.sessions["s1"].base_started
    time.sleep(0.01)
    engine.apply_event("tool-end", session_id="s1")
    assert engine.sessions["s1"].base_started == started


def test_out_of_order_events_are_dropped(engine):
    now = time.time()
    engine.apply_event("prompt-submit", session_id="s1", ts=now)
    # An async `stop` hook that lost the race must not win.
    engine.apply_event("stop", session_id="s1", ts=now - 5)
    assert state_of(engine) == "working"


def test_session_end_releases_the_light(engine):
    engine.apply_event("prompt-submit", session_id="s1")
    engine.apply_event("session-end", session_id="s1")
    assert engine.sessions == {}
    assert engine.frame() == (0, 0, 0)


def test_only_the_ending_session_is_released(engine):
    engine.apply_event("prompt-submit", session_id="s1")
    engine.apply_event("prompt-submit", session_id="s2")
    engine.apply_event("session-end", session_id="s1")
    assert list(engine.sessions) == ["s2"]
    assert state_of(engine) == "working"


def test_a_stuck_working_state_decays_to_idle(engine):
    """A killed Claude Code process never fires SessionEnd."""
    engine.apply_event("prompt-submit", session_id="s1")
    timeout = engine.states["working"].timeout
    engine.expire(time.time() + timeout + 1)
    assert state_of(engine) == "idle"


def test_a_silent_session_is_forgotten(engine):
    engine.apply_event("prompt-submit", session_id="s1")
    engine.expire(time.time() + engine.session_ttl + 1)
    assert engine.sessions == {}


def test_unknown_events_are_ignored_rather_than_fatal(engine):
    assert engine.apply_event("something-claude-adds-in-2027", session_id="s1") is False
    assert engine.sessions == {}


def test_unknown_states_are_rejected(engine):
    assert engine.apply_state("chartreuse", session_id="s1") is False


def test_pause_blanks_the_light_without_losing_state(engine):
    engine.apply_event("prompt-submit", session_id="s1")
    engine.paused = True
    assert engine.frame() == (0, 0, 0)
    engine.paused = False
    assert engine.frame() == (255, 0, 0)


def test_disabled_config_never_lights_up():
    config = copy.deepcopy(DEFAULTS)
    config["enabled"] = False
    engine = Engine(config)
    engine.apply_event("prompt-submit", session_id="s1")
    assert engine.frame() == (0, 0, 0)


def test_channel_order_is_applied_to_the_frame():
    config = copy.deepcopy(DEFAULTS)
    config["device"]["channel_order"] = "grb"
    engine = Engine(config)
    engine.apply_event("prompt-submit", session_id="s1")
    # Logical red becomes a payload whose green field carries the 255.
    assert engine.frame() == (0, 255, 0)


def test_brightness_scales_every_state():
    config = copy.deepcopy(DEFAULTS)
    config["render"]["brightness"] = 0.5
    engine = Engine(config)
    engine.apply_event("prompt-submit", session_id="s1")
    assert engine.frame() == (128, 0, 0)


def test_sleep_hint_is_long_for_static_states_and_short_for_animated():
    config = copy.deepcopy(DEFAULTS)
    engine = Engine(config)
    engine.apply_event("prompt-submit", session_id="s1")  # solid
    assert engine.sleep_hint() >= 1.0
    engine.apply_event("permission", session_id="s1")  # blinking
    assert engine.sleep_hint() == pytest.approx(1.0 / config["render"]["fps"])


def test_events_can_be_remapped_in_config():
    config = copy.deepcopy(DEFAULTS)
    config["events"]["prompt-submit"] = "permission"
    engine = Engine(config)
    engine.apply_event("prompt-submit", session_id="s1")
    assert state_of(engine) == "permission"
