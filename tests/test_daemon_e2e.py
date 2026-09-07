"""End-to-end test: a real daemon process, a real socket, real hook calls.

Uses the ``console`` transport pointed at the daemon's log so the test can
assert on the colours that were actually published, without needing a
cube, a broker or a network.
"""

import json
import os
import subprocess
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUZERKOSTKA = os.path.join(ROOT, "bin", "buzerkostka")
EVENT = os.path.join(ROOT, "bin", "buzerkostka-event")


@pytest.fixture
def env(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "transport": "null",
        "render": {"fps": 30, "resend_interval_s": 0},
    }))
    environment = dict(os.environ)
    environment["BUZERKOSTKA_CONFIG"] = str(config)
    environment["BUZERKOSTKA_STATE_DIR"] = str(tmp_path / "state")
    return environment


def run(env, *args, stdin=None):
    return subprocess.run(
        [sys.executable, BUZERKOSTKA] + list(args),
        env=env, input=stdin, capture_output=True, text=True, timeout=30,
    )


def hook(env, event, session="s1", cwd="/tmp/project", transcript=None):
    """Invoke the hook client exactly the way Claude Code does."""
    payload = json.dumps({"session_id": session, "cwd": cwd,
                          "hook_event_name": "Test",
                          "transcript_path": transcript})
    return subprocess.run(
        [sys.executable, EVENT, "--event", event],
        env=env, input=payload, capture_output=True, text=True, timeout=30,
    )


def status(env):
    result = run(env, "status", "--json")
    return json.loads(result.stdout)


def wait_for_state(env, expected, timeout=5.0):
    deadline = time.time() + timeout
    seen = None
    while time.time() < deadline:
        seen = status(env).get("active_state")
        if seen == expected:
            return True
        time.sleep(0.05)
    pytest.fail("expected state %r, last saw %r" % (expected, seen))


@pytest.fixture
def daemon(env):
    run(env, "daemon")
    deadline = time.time() + 10
    while time.time() < deadline:
        if status(env).get("ok"):
            break
        time.sleep(0.1)
    else:  # pragma: no cover
        pytest.fail("daemon never came up")
    yield env
    run(env, "stop")


def test_status_before_the_daemon_exists(env):
    result = run(env, "status", "--json")
    assert json.loads(result.stdout)["ok"] is False


def test_a_hook_autostarts_the_daemon(env):
    """No manual `buzerkostka daemon` step should ever be needed."""
    result = hook(env, "prompt-submit")
    assert result.returncode == 0
    assert result.stdout == "", "hook stdout must stay empty for Claude Code"

    deadline = time.time() + 10
    while time.time() < deadline:
        if status(env).get("ok"):
            break
        time.sleep(0.1)
    else:  # pragma: no cover
        pytest.fail("the hook did not bring the daemon up")

    wait_for_state(env, "working")
    run(env, "stop")


def test_the_full_turn_cycle(daemon):
    hook(daemon, "session-start")
    wait_for_state(daemon, "idle")

    hook(daemon, "prompt-submit")
    wait_for_state(daemon, "working")

    hook(daemon, "permission")
    wait_for_state(daemon, "permission")

    hook(daemon, "tool-end")
    wait_for_state(daemon, "working")

    hook(daemon, "stop")
    wait_for_state(daemon, "idle")

    hook(daemon, "session-end")
    deadline = time.time() + 5
    while time.time() < deadline:
        if not status(daemon)["sessions"]:
            break
        time.sleep(0.05)
    assert status(daemon)["sessions"] == []


def test_two_sessions_are_ranked_by_urgency(daemon):
    hook(daemon, "prompt-submit", session="a", cwd="/tmp/alpha")
    hook(daemon, "permission", session="b", cwd="/tmp/beta")
    wait_for_state(daemon, "permission")

    snapshot = status(daemon)
    assert snapshot["active_session"] == "b"
    assert {s["label"] for s in snapshot["sessions"]} == {"alpha", "beta"}

    hook(daemon, "tool-end", session="b")
    wait_for_state(daemon, "working")


def test_frames_are_actually_published(daemon):
    before = status(daemon)["frames_sent"]
    hook(daemon, "permission")  # blinking, so frames keep coming
    time.sleep(0.5)
    assert status(daemon)["frames_sent"] > before


def test_a_solid_state_stops_publishing(daemon):
    """Static colours must not spam the broker at the frame rate."""
    hook(daemon, "prompt-submit")
    wait_for_state(daemon, "working")
    time.sleep(0.3)
    settled = status(daemon)["frames_sent"]
    time.sleep(1.0)
    assert status(daemon)["frames_sent"] == settled


def test_pause_and_resume(daemon):
    hook(daemon, "prompt-submit")
    wait_for_state(daemon, "working")

    run(daemon, "pause")
    time.sleep(0.3)
    assert status(daemon)["paused"] is True
    assert status(daemon)["last_color"] == [0, 0, 0]

    run(daemon, "resume")
    time.sleep(0.3)
    assert status(daemon)["last_color"] == [255, 0, 0]


def test_manual_colour_override(daemon):
    run(daemon, "color", "#123456")
    time.sleep(0.3)
    assert status(daemon)["last_color"] == [18, 52, 86]

    # The next real event takes the light back.
    hook(daemon, "prompt-submit")
    time.sleep(0.3)
    assert status(daemon)["last_color"] == [255, 0, 0]


def test_reload_picks_up_a_changed_config(daemon):
    hook(daemon, "prompt-submit")
    wait_for_state(daemon, "working")

    path = daemon["BUZERKOSTKA_CONFIG"]
    with open(path) as handle:
        config = json.load(handle)
    config["states"] = {"working": {"color": "#00FF00", "effect": "solid",
                                    "priority": 30}}
    with open(path, "w") as handle:
        json.dump(config, handle)

    assert run(daemon, "reload").returncode == 0
    time.sleep(0.3)
    assert status(daemon)["last_color"] == [0, 255, 0]


def test_a_second_daemon_refuses_to_start(daemon):
    result = subprocess.run(
        [sys.executable, BUZERKOSTKA, "daemon", "--foreground"],
        env=daemon, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 3
    assert "already running" in result.stderr


def test_unknown_events_do_not_break_the_hook(daemon):
    result = hook(daemon, "some-future-event")
    assert result.returncode == 0
    assert status(daemon)["sessions"] == []


def test_the_hook_survives_a_dead_daemon(env):
    """Cube unplugged, broker down, daemon crashed: never fail the session."""
    env = dict(env)
    env["BUZERKOSTKA_STATE_DIR"] = os.path.join(env["BUZERKOSTKA_STATE_DIR"], "nested")
    env["BUZERKOSTKA_CONFIG"] = "/nonexistent/config.json"
    result = hook(env, "prompt-submit")
    assert result.returncode == 0


def test_disable_switch_short_circuits_the_hook(daemon):
    env = dict(daemon)
    env["BUZERKOSTKA_DISABLE"] = "1"
    hook(env, "prompt-submit")
    time.sleep(0.3)
    assert status(daemon)["sessions"] == []


def test_denying_a_permission_prompt_stops_the_blinking(daemon, tmp_path):
    """Claude Code fires no hook when you answer "No" -- not PostToolUse,
    not Stop, not even PermissionDenied (that one is auto-mode's). The
    daemon has to notice through the transcript, or the cube blinks
    orange at a session that is quietly waiting for its next prompt."""
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"type":"user","message":{"content":"go"}}\n')

    hook(daemon, "permission", transcript=str(transcript))
    wait_for_state(daemon, "permission")

    # What Claude Code appends when the human presses No.
    with open(transcript, "a") as handle:
        handle.write('{"type":"user","message":{"content":[{"type":"text",'
                     '"text":"[Request interrupted by user for tool use]"}]}}\n')

    wait_for_state(daemon, "idle")


def test_a_transcript_that_only_grows_leaves_the_state_alone(daemon, tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("{}\n")

    hook(daemon, "permission", transcript=str(transcript))
    wait_for_state(daemon, "permission")

    with open(transcript, "a") as handle:
        handle.write('{"type":"assistant","message":{"content":"thinking"}}\n')

    time.sleep(1.5)  # comfortably more than one watch interval
    assert status(daemon)["active_state"] == "permission"
