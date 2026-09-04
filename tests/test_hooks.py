"""Tests for generating and merging the Claude Code hook configuration."""

import json
import pathlib

import pytest

from buzerkostka import hooks


def write(path, data):
    pathlib.Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")


def read(path):
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


@pytest.fixture
def settings(tmp_path, monkeypatch):
    """Redirect every scope at a throwaway file.

    Without this a test run would rewrite the developer's real
    ``~/.claude/settings.json``.
    """
    path = tmp_path / "settings.json"
    monkeypatch.setattr(hooks, "settings_path", lambda *a, **k: str(path))
    return path


def test_tiers_are_cumulative():
    minimal = {spec[2] for spec in hooks.specs_for_tier("minimal")}
    standard = {spec[2] for spec in hooks.specs_for_tier("standard")}
    verbose = {spec[2] for spec in hooks.specs_for_tier("verbose")}
    assert minimal < standard < verbose


def test_minimal_tier_includes_post_tool_use():
    """Without it the cube would stay orange after you approve a tool."""
    events = [spec[0] for spec in hooks.specs_for_tier("minimal")]
    assert "PostToolUse" in events


def test_every_hook_event_maps_to_a_configured_event():
    from buzerkostka.config import DEFAULTS

    for _claude_event, _matcher, our_event, _tier in hooks.HOOK_SPECS:
        assert our_event in DEFAULTS["events"], our_event


def test_build_hooks_marks_everything_async_by_default():
    built = hooks.build_hooks("/opt/buzerkostka-event", "standard")
    for groups in built.values():
        for group in groups:
            for handler in group["hooks"]:
                assert handler["async"] is True
                assert handler["type"] == "command"


def test_no_async_flag_omits_the_key():
    built = hooks.build_hooks("/opt/buzerkostka-event", "minimal", use_async=False)
    handler = built["Stop"][0]["hooks"][0]
    assert "async" not in handler


def test_notification_hooks_carry_distinct_matchers():
    built = hooks.build_hooks("/opt/buzerkostka-event", "standard")
    matchers = {group["matcher"] for group in built["Notification"]}
    assert matchers == {"permission_prompt", "idle_prompt", "elicitation_dialog"}


def test_paths_with_spaces_are_quoted():
    built = hooks.build_hooks("/Users/a b/bin/buzerkostka-event", "minimal")
    command = built["Stop"][0]["hooks"][0]["command"]
    assert command.startswith("'/Users/a b/bin/buzerkostka-event'")


def test_install_preserves_unrelated_settings_and_foreign_hooks(settings):
    write(settings, {"model": "opus", "hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": "/usr/bin/say done"}]}]}})

    report = hooks.install("/opt/buzerkostka-event", scope="user", tier="minimal")
    assert report["added"] == len(hooks.specs_for_tier("minimal"))

    result = read(settings)
    assert result["model"] == "opus", "unrelated settings must survive"
    stop_commands = [h["command"] for g in result["hooks"]["Stop"] for h in g["hooks"]]
    assert "/usr/bin/say done" in stop_commands, "foreign hooks must survive"
    assert any("buzerkostka-event" in c for c in stop_commands)


def test_install_is_idempotent(settings):
    first = hooks.install("/opt/buzerkostka-event", tier="standard")
    second = hooks.install("/opt/buzerkostka-event", tier="standard")

    assert second["replaced"] == first["added"]
    assert second["added"] == first["added"]

    total = sum(len(g) for g in read(settings)["hooks"].values())
    assert total == first["added"], "reinstalling must not duplicate hooks"


def test_switching_tier_replaces_rather_than_accumulates(settings):
    hooks.install("/opt/buzerkostka-event", tier="verbose")
    report = hooks.install("/opt/buzerkostka-event", tier="minimal")

    total = sum(len(g) for g in read(settings)["hooks"].values())
    assert total == report["added"] == len(hooks.specs_for_tier("minimal"))


def test_uninstall_removes_only_our_hooks(settings):
    write(settings, {"hooks": {"Stop": [
        {"hooks": [{"type": "command", "command": "/usr/bin/say done"}]}
    ]}})

    hooks.install("/opt/buzerkostka-event", tier="standard")
    report = hooks.uninstall()

    assert report["removed"] > 0
    result = read(settings)
    assert result["hooks"] == {
        "Stop": [{"hooks": [{"type": "command", "command": "/usr/bin/say done"}]}]
    }


def test_uninstall_drops_the_hooks_key_when_it_empties(settings):
    write(settings, {"model": "opus"})

    hooks.install("/opt/buzerkostka-event", tier="minimal")
    hooks.uninstall()

    assert read(settings) == {"model": "opus"}


def test_install_makes_a_backup(settings):
    write(settings, {"model": "opus"})

    report = hooks.install("/opt/buzerkostka-event", tier="minimal")
    assert report["backup"] and read(report["backup"]) == {"model": "opus"}


def test_dry_run_touches_nothing(settings):
    write(settings, {"model": "opus"})

    hooks.install("/opt/buzerkostka-event", tier="minimal", dry_run=True)
    assert read(settings) == {"model": "opus"}


def test_plugin_hooks_use_the_plugin_root_variable():
    built = hooks.plugin_hooks("standard")["hooks"]
    command = built["Stop"][0]["hooks"][0]["command"]
    assert command.startswith('"${CLAUDE_PLUGIN_ROOT}"/bin/buzerkostka-event')


def test_unknown_tier_is_rejected():
    with pytest.raises(ValueError):
        hooks.specs_for_tier("loud")
