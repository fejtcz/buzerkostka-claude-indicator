"""Tests for config loading, merging, env overrides and validation."""

import json

import pytest

from buzerkostka import config as config_module


def test_defaults_are_self_consistent():
    assert config_module.validate(
        {**config_module.DEFAULTS, "transport": "console"}
    ) == []


def test_missing_file_falls_back_to_defaults(tmp_path):
    config = config_module.load(str(tmp_path / "nope.json"))
    assert config["transport"] == "mqtt"
    assert config["states"]["working"]["color"] == "#FF0000"


def test_user_config_is_merged_not_replaced(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "mqtt": {"host": "broker.lan"},
        "states": {"working": {"color": "#0000FF"}},
    }))
    config = config_module.load(str(path))

    assert config["mqtt"]["host"] == "broker.lan"
    assert config["mqtt"]["port"] == 1883, "untouched keys keep their default"
    assert config["states"]["working"]["color"] == "#0000FF"
    assert config["states"]["working"]["effect"] == "solid", "partial state override"
    assert "idle" in config["states"], "other states survive"


def test_environment_overrides_the_file(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"mqtt": {"host": "from-file"}}))
    config = config_module.load(str(path), environ={
        "BUZERKOSTKA_MQTT_HOST": "from-env",
        "BUZERKOSTKA_MQTT_PORT": "8883",
        "BUZERKOSTKA_MQTT_TLS": "true",
        "BUZERKOSTKA_BRIGHTNESS": "0.4",
    })
    assert config["mqtt"]["host"] == "from-env"
    assert config["mqtt"]["port"] == 8883
    assert config["mqtt"]["tls"] is True
    assert config["render"]["brightness"] == 0.4


def test_empty_environment_values_are_ignored(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"mqtt": {"host": "from-file"}}))
    config = config_module.load(str(path), environ={"BUZERKOSTKA_MQTT_HOST": ""})
    assert config["mqtt"]["host"] == "from-file"


def test_bad_environment_value_is_reported(tmp_path):
    with pytest.raises(config_module.ConfigError):
        config_module.load(str(tmp_path / "x.json"),
                           environ={"BUZERKOSTKA_MQTT_PORT": "not-a-port"})


def test_broken_json_is_reported_with_the_path(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{ not json")
    with pytest.raises(config_module.ConfigError) as excinfo:
        config_module.load(str(path))
    assert str(path) in str(excinfo.value)


def test_save_round_trips_and_drops_internal_keys(tmp_path):
    path = str(tmp_path / "sub" / "config.json")
    config = config_module.load(path)
    config["mqtt"]["host"] = "broker.lan"
    config_module.save(config, path)

    with open(path) as handle:
        saved = json.load(handle)
    assert "_path" not in saved
    assert saved["mqtt"]["host"] == "broker.lan"


def test_save_keeps_credentials_private(tmp_path):
    import os
    import stat

    path = str(tmp_path / "config.json")
    config_module.save({"mqtt": {"password": "hunter2"}}, path)
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600


@pytest.mark.parametrize("broken,expected", [
    ({"transport": "carrier-pigeon"}, "transport"),
    ({"transport": "mqtt", "mqtt": {"host": ""}}, "mqtt.host"),
    ({"device": {"channel_order": "xyz"}}, "channel_order"),
    ({"render": {"fps": 500}}, "render.fps"),
    ({"render": {"brightness": 5}}, "render.brightness"),
    ({"states": {"idle": {"effect": "strobe"}}}, "effect"),
    ({"states": {"idle": {"color": "#zzz"}}}, "color"),
    ({"events": {"stop": "nonexistent-state"}}, "unknown state"),
])
def test_validation_catches_common_mistakes(broken, expected):
    config = config_module.deep_merge(config_module.DEFAULTS, broken)
    config.setdefault("mqtt", {}).setdefault("host", "x")
    config.setdefault("device", {}).setdefault("topic", "t")
    problems = config_module.validate(config)
    assert any(expected in problem for problem in problems), problems


def test_deep_merge_does_not_mutate_the_base():
    base = {"a": {"b": 1}}
    config_module.deep_merge(base, {"a": {"b": 2}})
    assert base == {"a": {"b": 1}}


def test_state_dir_respects_the_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("BUZERKOSTKA_STATE_DIR", str(tmp_path / "state"))
    assert config_module.state_dir() == str(tmp_path / "state")
    # The socket path itself is covered by tests/test_paths.py, which also
    # exercises the sun_path length fallback.
    assert config_module.socket_path().endswith(".sock")
