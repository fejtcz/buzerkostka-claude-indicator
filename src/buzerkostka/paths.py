"""Where the socket, lock, log and config live.

This module imports nothing but ``os`` on purpose. Both the CLI and the
hot-path hook client need these paths, and they must agree exactly -- if
the client computed a different socket path than the daemon bound to,
every event would silently vanish. Sharing one implementation is the only
way to guarantee that.

Two hazards are handled here:

*An unusable state directory.*
    ``~/.local/state`` is sometimes owned by root -- ``sudo gem install``
    and a few other tools create it that way -- which makes the whole XDG
    path unwritable for the user. Rather than crash, fall back to a
    per-user directory in the temp dir. :func:`state_dir_status` reports
    when that happened so ``doctor`` can explain it.

*An over-long socket path.*
    ``sockaddr_un.sun_path`` is 104 bytes on macOS and 108 on Linux. A
    deep ``XDG_STATE_HOME``, a long home directory or a pytest ``tmp_path``
    will overflow it, so past a threshold we hash down to a short name.
"""

from __future__ import annotations

import os

CONFIG_ENV = "BUZERKOSTKA_CONFIG"
STATE_DIR_ENV = "BUZERKOSTKA_STATE_DIR"

#: Stay under the smaller of the two ``sun_path`` limits, with headroom.
MAX_SOCKET_PATH = 100


def _temp_root() -> str:
    return os.environ.get("TMPDIR") or "/tmp"


def preferred_state_dir() -> str:
    """The state directory we would use if nothing were in the way."""
    override = os.environ.get(STATE_DIR_ENV)
    if override:
        return os.path.expanduser(override)
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state"
    )
    return os.path.join(base, "buzerkostka")


def fallback_state_dir() -> str:
    """Used when :func:`preferred_state_dir` cannot be created or written."""
    try:
        uid = os.getuid()
    except AttributeError:  # pragma: no cover - non-POSIX
        uid = 0
    return os.path.join(_temp_root(), "buzerkostka-%d" % uid)


def _usable(path: str) -> bool:
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
    except OSError:
        return False
    return os.access(path, os.W_OK | os.X_OK)


def state_dir() -> str:
    """The directory to use, creating it if possible. Never raises.

    Deterministic: two processes with the same environment always pick the
    same directory, which is what keeps the client and the daemon talking
    to each other.
    """
    preferred = preferred_state_dir()
    if _usable(preferred):
        return preferred
    fallback = fallback_state_dir()
    _usable(fallback)  # best effort; the caller will report a real failure
    return fallback


def state_dir_status() -> tuple:
    """``(path, problem_or_None)`` -- for ``doctor`` to explain itself."""
    preferred = preferred_state_dir()
    if _usable(preferred):
        return (preferred, None)

    parent = os.path.dirname(preferred)
    if os.path.isdir(preferred) and not os.access(preferred, os.W_OK):
        reason = "%s exists but is not writable by you" % preferred
    elif os.path.isdir(parent) and not os.access(parent, os.W_OK):
        reason = "%s is not writable by you" % parent
    else:
        reason = "%s could not be created" % preferred
    return (state_dir(), reason)


def socket_path() -> str:
    """Where the daemon listens."""
    path = os.path.join(state_dir(), "daemon.sock")
    if len(path.encode("utf-8")) <= MAX_SOCKET_PATH:
        return path
    import hashlib

    digest = hashlib.sha1(path.encode("utf-8")).hexdigest()[:12]
    return os.path.join(_temp_root(), "buzerkostka-%s.sock" % digest)


def config_path() -> str:
    """The config file location, honouring ``$BUZERKOSTKA_CONFIG`` and XDG."""
    override = os.environ.get(CONFIG_ENV)
    if override:
        return os.path.expanduser(override)
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return os.path.join(base, "buzerkostka", "config.json")
