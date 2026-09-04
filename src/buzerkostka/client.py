"""The hook-side client. Optimised for "cheap and never in the way".

This module is what Claude Code actually executes, potentially on every
tool call, so it has three hard rules:

1. **Import almost nothing.** Only stdlib C modules, ``buzerkostka.paths``
   (which itself imports nothing but ``os``), and ``subprocess`` solely on
   the cold-start path. Interpreter start-up dominates the cost and the
   hooks are registered ``async``, so the session never waits.
2. **Never write to stdout.** Claude Code parses hook stdout; anything we
   print risks being interpreted. Diagnostics go to stderr, and only when
   ``BUZERKOSTKA_DEBUG=1``.
3. **Never fail.** A cube that is unplugged, a broker that is down or a
   daemon that refuses to start must all result in exit status 0. A
   status light is not worth breaking someone's session over.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time

from .paths import socket_path as _socket_path, state_dir as _state_dir

CONNECT_TIMEOUT = 1.0
SPAWN_COOLDOWN_S = 10.0
SPAWN_WAIT_S = 2.0


def _debug(message: str) -> None:
    if os.environ.get("BUZERKOSTKA_DEBUG"):
        sys.stderr.write("buzerkostka: %s\n" % message)


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))


def _connect(path: str):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(CONNECT_TIMEOUT)
    sock.connect(path)
    return sock


def spawn_daemon() -> bool:
    """Start the daemon in the background, at most once per cooldown.

    Rate limiting matters: if the daemon cannot start (bad config, say),
    an unguarded client would fork a new one on every single tool call.
    """
    stamp = os.path.join(_state_dir(), "spawn.stamp")
    now = time.time()
    try:
        if now - os.path.getmtime(stamp) < SPAWN_COOLDOWN_S:
            _debug("daemon spawn suppressed (cooldown)")
            return False
    except OSError:
        pass

    try:
        with open(stamp, "w") as handle:
            handle.write(str(now))
    except OSError:
        pass

    import subprocess

    root = _repo_root()
    launcher = os.path.join(root, "bin", "buzerkostka")
    env = dict(os.environ)
    if os.path.exists(launcher):
        argv = [sys.executable, launcher, "daemon"]
    else:
        # Installed as a package rather than run from a clone.
        argv = [sys.executable, "-m", "buzerkostka", "daemon"]
        src = os.path.join(root, "src")
        if os.path.isdir(src):
            existing = env.get("PYTHONPATH")
            env["PYTHONPATH"] = src + (os.pathsep + existing if existing else "")

    try:
        subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
        _debug("spawned daemon: %s" % " ".join(argv))
        return True
    except OSError as exc:
        _debug("cannot spawn daemon: %s" % exc)
        return False


def request(payload: dict, autostart: bool = True, want_reply: bool = False):
    """Send one request to the daemon. Returns the reply dict or ``None``."""
    path = _socket_path()
    line = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")

    for attempt in (0, 1):
        sock = None
        try:
            sock = _connect(path)
            sock.sendall(line)
            if not want_reply:
                return None
            sock.shutdown(socket.SHUT_WR)
            chunks = []
            while True:
                chunk = sock.recv(8192)
                if not chunk:
                    break
                chunks.append(chunk)
                if b"\n" in chunk:
                    break
            raw = b"".join(chunks).strip()
            if not raw:
                return None
            return json.loads(raw.split(b"\n")[0].decode("utf-8"))
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            # No listener: either the daemon is not running, or the socket
            # file is stale after a crash.
            _debug("daemon not reachable (%s)" % exc)
            if attempt == 1 or not autostart:
                return None
            if not spawn_daemon():
                return None
            deadline = time.time() + SPAWN_WAIT_S
            while time.time() < deadline:
                if os.path.exists(path):
                    break
                time.sleep(0.05)
        except (OSError, ValueError) as exc:
            _debug("request failed: %s" % exc)
            return None
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
    return None


def read_hook_payload(timeout: float = 0.25) -> dict:
    """Parse the JSON Claude Code writes to our stdin.

    Guarded by ``select`` so an interactive invocation (or a future hook
    that sends nothing) can never make the hook hang.
    """
    if sys.stdin is None or sys.stdin.closed:
        return {}
    try:
        if sys.stdin.isatty():
            return {}
    except (OSError, ValueError):
        return {}
    try:
        import select

        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        if not ready:
            return {}
        raw = sys.stdin.read()
    except (OSError, ValueError) as exc:
        _debug("cannot read stdin: %s" % exc)
        return {}
    if not raw or not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        _debug("stdin is not JSON: %s" % exc)
        return {}
    return payload if isinstance(payload, dict) else {}


def session_label(payload: dict):
    """A short, human-friendly name for `buzerkostka status`."""
    cwd = payload.get("cwd")
    return os.path.basename(cwd.rstrip("/")) if cwd else None


def main(argv=None) -> int:
    """Entry point for ``bin/buzerkostka-event``."""
    argv = list(sys.argv[1:] if argv is None else argv)

    event = None
    explicit_session = None
    read_stdin = True

    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in ("--event", "-e") and index + 1 < len(argv):
            event = argv[index + 1]
            index += 2
        elif arg == "--session" and index + 1 < len(argv):
            explicit_session = argv[index + 1]
            index += 2
        elif arg == "--no-stdin":
            read_stdin = False
            index += 1
        elif arg in ("--help", "-h"):
            sys.stderr.write(
                "usage: buzerkostka-event --event EVENT [--session ID] [--no-stdin]\n"
            )
            return 0
        elif not arg.startswith("-") and event is None:
            event = arg  # positional form, handy for manual testing
            index += 1
        else:
            index += 1

    if not event:
        _debug("no event given")
        return 0

    if os.environ.get("BUZERKOSTKA_DISABLE"):
        _debug("disabled via BUZERKOSTKA_DISABLE")
        return 0

    payload = read_hook_payload() if read_stdin else {}
    session = explicit_session or payload.get("session_id") or "default"

    request(
        {
            "cmd": "event",
            "event": event,
            "session": session,
            "ts": time.time(),
            "cwd": payload.get("cwd"),
            "label": session_label(payload),
            # The daemon tails this to notice a denied permission prompt,
            # which Claude Code reports through no hook at all.
            "transcript": payload.get("transcript_path"),
        }
    )
    return 0
