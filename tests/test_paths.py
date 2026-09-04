"""Path resolution: the client and the daemon must always agree.

If ``buzerkostka.client`` ever computed a different socket path than the
daemon bound to, every hook event would vanish without a trace. Both now
share ``buzerkostka.paths``; these tests pin the behaviour that module
promises, including the two hazards it exists to absorb -- an unwritable
XDG state directory and an over-long ``sun_path``.
"""

import os
import stat

import pytest

from buzerkostka import client, config, paths


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("BUZERKOSTKA_STATE_DIR", "XDG_STATE_HOME", "XDG_CONFIG_HOME",
                 "BUZERKOSTKA_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TMPDIR", "/tmp")


# ------------------------------------------------------- client == config
def test_state_dir_agrees(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert client._state_dir() == config.state_dir()


def test_state_dir_env_override_agrees(monkeypatch, tmp_path):
    monkeypatch.setenv("BUZERKOSTKA_STATE_DIR", str(tmp_path / "custom"))
    assert client._state_dir() == config.state_dir()


def test_socket_path_agrees(monkeypatch, tmp_path):
    monkeypatch.setenv("BUZERKOSTKA_STATE_DIR", str(tmp_path))
    assert client._socket_path() == config.socket_path()


# ------------------------------------------------- unwritable state dir
@pytest.fixture
def root_owned_state_home(monkeypatch, tmp_path):
    """Reproduce ``~/.local/state`` owned by root, mode 755.

    A real machine picks this up from `sudo gem install` and friends. We
    cannot chown to root in a test, so drop the write bit instead --
    ``os.access`` sees the same thing.
    """
    state_home = tmp_path / "state"
    state_home.mkdir()
    state_home.chmod(stat.S_IRUSR | stat.S_IXUSR)  # r-x------
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    yield state_home
    state_home.chmod(0o700)  # let pytest clean up


def test_an_unwritable_state_home_does_not_raise(root_owned_state_home):
    """The bug: this used to escape as a PermissionError traceback."""
    directory = config.state_dir()
    assert os.path.isdir(directory)
    assert os.access(directory, os.W_OK)


def test_the_fallback_lands_in_the_temp_dir(root_owned_state_home):
    assert config.state_dir() == paths.fallback_state_dir()
    assert config.state_dir().startswith("/tmp/buzerkostka-")


def test_client_and_config_pick_the_same_fallback(root_owned_state_home):
    assert client._state_dir() == config.state_dir()
    assert client._socket_path() == config.socket_path()


def test_status_reports_the_reason(root_owned_state_home):
    directory, problem = config.state_dir_status()
    assert directory == paths.fallback_state_dir()
    assert str(root_owned_state_home) in problem
    assert "not writable" in problem


def test_status_is_quiet_when_everything_is_fine(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    directory, problem = config.state_dir_status()
    assert problem is None
    assert directory == paths.preferred_state_dir()


def test_the_fallback_is_private(root_owned_state_home):
    mode = stat.S_IMODE(os.stat(config.state_dir()).st_mode)
    assert mode & 0o077 == 0, "the socket lives here; keep other users out"


# ------------------------------------------------------ sun_path length
def test_socket_path_falls_back_when_it_would_be_too_long(monkeypatch, tmp_path):
    """sun_path is 104 bytes on macOS -- a deep state dir must still work."""
    deep = tmp_path.joinpath(*(["averylongdirectorynamesegment"] * 6))
    monkeypatch.setenv("BUZERKOSTKA_STATE_DIR", str(deep))

    path = config.socket_path()
    assert len(path.encode()) <= paths.MAX_SOCKET_PATH
    assert path.startswith("/tmp/buzerkostka-")
    assert client._socket_path() == path


def test_the_short_socket_name_is_stable_but_per_state_dir(monkeypatch, tmp_path):
    deep = tmp_path.joinpath(*(["averylongdirectorynamesegment"] * 6))

    monkeypatch.setenv("BUZERKOSTKA_STATE_DIR", str(deep))
    first = config.socket_path()
    assert config.socket_path() == first, "must be stable across calls"

    monkeypatch.setenv("BUZERKOSTKA_STATE_DIR", str(deep / "other"))
    assert config.socket_path() != first, "different state dirs must not collide"


# ----------------------------------------------------------------- misc
def test_config_path_honours_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config.config_path() == str(tmp_path / "buzerkostka" / "config.json")


def test_config_path_env_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("BUZERKOSTKA_CONFIG", str(tmp_path / "c.json"))
    assert config.config_path() == str(tmp_path / "c.json")


def test_repo_root_points_at_the_checkout():
    root = client._repo_root()
    assert os.path.exists(os.path.join(root, "bin", "buzerkostka-event"))
