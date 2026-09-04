"""The resident process: one connection, one render loop, one socket.

Why a daemon at all? The cube's firmware can only be told "be exactly
this colour"; every effect has to be produced by publishing a stream of
frames. A Claude Code hook is a process that lives for milliseconds, so
it cannot animate anything -- and it certainly should not pay for a fresh
TCP+MQTT handshake on every tool call. The daemon holds the connection
and the animation state; hooks just post events at it.
"""

from __future__ import annotations

import errno
import json
import os
import signal
import socket
import sys
import threading
import time

from . import config as config_module
from . import watch
from .engine import Engine
from .transport import ReconnectingTransport, TransportError, build_transport

PROTOCOL_VERSION = 1


class SingleInstanceLock:
    """An advisory ``flock`` so two daemons never fight over the cube."""

    def __init__(self, path: str):
        self.path = path
        self._fd = None

    def acquire(self) -> bool:
        import fcntl

        self._fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self._fd)
            self._fd = None
            return False
        os.truncate(self._fd, 0)
        os.write(self._fd, ("%d\n" % os.getpid()).encode("ascii"))
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            os.close(self._fd)
        finally:
            self._fd = None

    def holder_pid(self):
        try:
            with open(self.path, "r", encoding="ascii") as handle:
                return int(handle.read().strip())
        except (OSError, ValueError):
            return None


class Logger:
    def __init__(self, path: str, max_bytes: int = 262144, echo: bool = False):
        self.path = path
        self.echo = echo
        try:
            if os.path.exists(path) and os.path.getsize(path) > max_bytes:
                os.replace(path, path + ".1")
        except OSError:
            pass
        try:
            self._handle = open(path, "a", encoding="utf-8")
        except OSError:
            self._handle = None

    def __call__(self, message: str) -> None:
        line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), message)
        if self._handle is not None:
            try:
                self._handle.write(line + "\n")
                self._handle.flush()
            except OSError:
                pass
        if self.echo:
            sys.stderr.write(line + "\n")
            sys.stderr.flush()

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None


class Daemon:
    def __init__(self, config: dict, foreground: bool = False):
        self.config = config
        self.foreground = foreground
        self.state_dir = config_module.state_dir()
        self.socket_path = config_module.socket_path()
        self.log = Logger(
            os.path.join(self.state_dir, "daemon.log"),
            int((config.get("daemon") or {}).get("log_max_bytes", 262144)),
            echo=foreground,
        )
        self.engine = Engine(config)
        self.lock = SingleInstanceLock(os.path.join(self.state_dir, "daemon.lock"))

        self._transport = None
        self._transport_error = None
        self._server = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._mutex = threading.Lock()
        self._last_sent = None
        self._last_sent_at = 0.0
        self._override = None  # (rgb, expires_at) from `buzerkostka color`
        self._watched_at = 0.0  # last transcript poll
        self.started_at = time.time()
        self.frames_sent = 0
        self._idle_since = None

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------
    def _ensure_transport(self):
        if self._transport is not None:
            return self._transport
        try:
            inner = build_transport(self.config)
        except TransportError as exc:
            if str(exc) != self._transport_error:
                self._transport_error = str(exc)
                self.log("transport unavailable: %s" % exc)
            return None
        self._transport_error = None
        self._transport = ReconnectingTransport(inner)
        self.log("transport ready: %s" % inner.describe())
        return self._transport

    def _drop_transport(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None
        self._last_sent = None

    # ------------------------------------------------------------------
    # Socket server
    # ------------------------------------------------------------------
    def _bind_socket(self) -> None:
        # A socket file left behind by a crash would make every client
        # think the daemon is up. The flock we already hold proves it is
        # not, so removing it here is safe.
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(self.socket_path)
        os.chmod(self.socket_path, 0o600)
        server.listen(64)
        server.settimeout(0.5)
        self._server = server

    def _serve_forever(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError as exc:
                if exc.errno in (errno.EBADF, errno.EINVAL) or self._stop.is_set():
                    return
                self.log("accept failed: %s" % exc)
                continue
            try:
                self._handle_connection(conn)
            except Exception as exc:  # never let a client kill the daemon
                self.log("client error: %s" % exc)
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def _handle_connection(self, conn) -> None:
        conn.settimeout(2.0)
        chunks = []
        while True:
            try:
                chunk = conn.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            chunks.append(chunk)
            if b"\n" in chunk:
                break

        raw = b"".join(chunks).strip()
        if not raw:
            return

        replies = []
        for line in raw.split(b"\n"):
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                replies.append({"ok": False, "error": "bad request: %s" % exc})
                continue
            replies.append(self._dispatch(request))

        try:
            for reply in replies:
                conn.sendall(
                    (json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8")
                )
        except OSError:
            # Hook clients fire and forget; a closed pipe here is normal.
            pass

    def _dispatch(self, request: dict) -> dict:
        command = request.get("cmd") or "event"

        if command == "ping":
            return {"ok": True, "pong": True, "pid": os.getpid(), "version": PROTOCOL_VERSION}

        if command == "event":
            event = request.get("event")
            if not event:
                return {"ok": False, "error": "missing 'event'"}
            with self._mutex:
                self._override = None
                changed = self.engine.apply_event(
                    event,
                    session_id=request.get("session"),
                    ts=request.get("ts"),
                    cwd=request.get("cwd"),
                    label=request.get("label"),
                    transcript=request.get("transcript"),
                )
            if changed:
                self._wake.set()
            return {"ok": True, "applied": changed, "event": event}

        if command == "state":
            name = request.get("state")
            if not name:
                return {"ok": False, "error": "missing 'state'"}
            with self._mutex:
                self._override = None
                changed = self.engine.apply_state(
                    name,
                    base_name=request.get("base"),
                    session_id=request.get("session") or "manual",
                )
            if changed:
                self._wake.set()
            if not changed:
                return {"ok": False, "error": "unknown state %r" % name}
            return {"ok": True, "state": name}

        if command == "color":
            try:
                rgb = (int(request["r"]), int(request["g"]), int(request["b"]))
            except (KeyError, TypeError, ValueError):
                return {"ok": False, "error": "color needs integer r, g, b"}
            hold = float(request.get("hold") or 0)
            with self._mutex:
                self._override = (rgb, time.time() + hold if hold > 0 else None)
            self._wake.set()
            return {"ok": True, "color": rgb, "hold": hold}

        if command == "status":
            with self._mutex:
                snapshot = self.engine.snapshot()
            transport = self._transport
            snapshot.update(
                {
                    "ok": True,
                    "pid": os.getpid(),
                    "uptime_s": round(time.time() - self.started_at, 1),
                    "frames_sent": self.frames_sent,
                    "last_color": self._last_sent,
                    "override": self._override[0] if self._override else None,
                    "config_path": self.config.get("_path"),
                    "socket": self.socket_path,
                    "transport": {
                        "target": transport.describe() if transport else None,
                        "healthy": transport.healthy if transport else False,
                        "failures": transport.failures if transport else 0,
                        "error": (transport.last_error if transport else None)
                        or self._transport_error,
                    },
                }
            )
            return snapshot

        if command in ("pause", "resume"):
            with self._mutex:
                self.engine.paused = command == "pause"
            self._wake.set()
            return {"ok": True, "paused": command == "pause"}

        if command == "clear":
            with self._mutex:
                self.engine.clear()
                self._override = None
            self._wake.set()
            return {"ok": True}

        if command == "reload":
            try:
                fresh = config_module.load()
            except config_module.ConfigError as exc:
                return {"ok": False, "error": str(exc)}
            with self._mutex:
                self.config = fresh
                self.engine.reload(fresh)
                self._drop_transport()
                self._override = None
            self._wake.set()
            self.log("configuration reloaded")
            return {"ok": True, "reloaded": True}

        if command == "stop":
            self.log("stop requested by client")
            self._stop.set()
            self._wake.set()
            return {"ok": True, "stopping": True}

        return {"ok": False, "error": "unknown command %r" % command}

    # ------------------------------------------------------------------
    # Render loop
    # ------------------------------------------------------------------
    def _publish(self, rgb) -> None:
        transport = self._ensure_transport()
        if transport is None:
            return

        render_config = self.config.get("render") or {}
        min_interval = float(render_config.get("min_interval_ms", 40)) / 1000.0
        resend_interval = float(render_config.get("resend_interval_s", 30))
        now = time.time()

        unchanged = rgb == self._last_sent
        # Re-publishing a static colour now and then means a cube that
        # rebooted, dropped off Wi-Fi or missed a QoS 0 packet heals
        # itself instead of sitting on a lie.
        stale = resend_interval > 0 and (now - self._last_sent_at) >= resend_interval
        if unchanged and not stale and not transport.dirty:
            return
        if not unchanged and (now - self._last_sent_at) < min_interval:
            return

        # A frame dropped while the transport cools off must not be
        # counted, or `status` would claim to be driving a cube it cannot
        # reach. `dirty` stays set, so it is re-sent once we recover.
        if transport.send(*rgb):
            self.frames_sent += 1
        self._last_sent = rgb
        self._last_sent_at = now

    def _poll_transcripts(self, now: float) -> bool:
        """Catch the interruptions Claude Code fires no hook for.

        Deliberately outside ``self._mutex`` for the file I/O: the render
        loop and the IPC thread both want that lock, and a slow disk must
        not stall either.
        """
        daemon_config = self.config.get("daemon") or {}
        if not daemon_config.get("watch_transcript", True):
            return False
        interval = float(daemon_config.get("watch_interval_s", 1.0))
        if now - self._watched_at < interval:
            return False
        self._watched_at = now

        with self._mutex:
            targets = [
                (session.id, session.transcript, session.transcript_pos)
                for session in self.engine.sessions.values()
                if session.transcript
            ]

        changed = False
        for session_id, path, position in targets:
            new_position, interrupted = watch.scan(path, position)
            if new_position == position and not interrupted:
                continue
            with self._mutex:
                session = self.engine.sessions.get(session_id)
                if session is None or session.transcript != path:
                    continue  # the session moved on while we were reading
                session.transcript_pos = new_position
                if interrupted and self.engine.apply_event(
                    "interrupted", session_id=session_id
                ):
                    changed = True
            if interrupted:
                self.log("session %s interrupted by user (transcript)" % session_id)
        return changed

    def _render_once(self) -> float:
        now = time.time()
        self._poll_transcripts(now)
        with self._mutex:
            self.engine.expire(now)
            override = self._override
            if override is not None:
                rgb, expires = override
                if expires is not None and now >= expires:
                    self._override = None
                    override = None
            if override is not None:
                rgb = override[0]
                delay = 0.25
            else:
                rgb = self.engine.frame(now)
                delay = self.engine.sleep_hint(now)

        self._publish(rgb)

        transport = self._transport
        if transport is not None:
            transport.tick()
            if not transport.healthy:
                delay = min(delay, 1.0)
        return delay

    def _check_idle_exit(self) -> bool:
        limit = float((self.config.get("daemon") or {}).get("exit_after_idle_s", 0))
        if limit <= 0:
            return False
        with self._mutex:
            has_sessions = bool(self.engine.sessions)
        if has_sessions:
            self._idle_since = None
            return False
        since = self._idle_since
        if since is None:
            self._idle_since = time.time()
            return False
        return time.time() - since >= limit

    def run(self) -> int:
        if not self.lock.acquire():
            pid = self.lock.holder_pid()
            sys.stderr.write(
                "buzerkostka: daemon already running%s\n"
                % (" (pid %d)" % pid if pid else "")
            )
            return 3

        def on_signal(signum, _frame):
            self.log("signal %d received" % signum)
            self._stop.set()
            self._wake.set()

        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            try:
                signal.signal(signum, on_signal)
            except (OSError, ValueError):  # pragma: no cover
                pass

        try:
            self._bind_socket()
        except OSError as exc:
            message = "cannot bind %s: %s" % (self.socket_path, exc)
            # Detached daemons have no stderr, so the log is the only
            # place this will ever be seen.
            self.log(message)
            sys.stderr.write("buzerkostka: %s\n" % message)
            self.lock.release()
            return 4

        self.log(
            "daemon started pid=%d socket=%s transport=%s"
            % (os.getpid(), self.socket_path, self.config.get("transport"))
        )

        server_thread = threading.Thread(
            target=self._serve_forever, name="buzerkostka-ipc", daemon=True
        )
        server_thread.start()

        try:
            while not self._stop.is_set():
                try:
                    delay = self._render_once()
                except Exception as exc:  # keep the loop alive no matter what
                    self.log("render error: %s" % exc)
                    delay = 1.0
                if self._check_idle_exit():
                    self.log("exiting after idle period")
                    break
                self._wake.wait(delay)
                self._wake.clear()
        finally:
            self._shutdown()
        return 0

    def _shutdown(self) -> None:
        self._stop.set()
        if (self.config.get("render") or {}).get("off_on_exit", True):
            try:
                transport = self._ensure_transport()
                if transport is not None:
                    transport.send(0, 0, 0)
            except Exception:
                pass
        if self._transport is not None:
            self._transport.close()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
        try:
            if os.path.exists(self.socket_path):
                os.unlink(self.socket_path)
        except OSError:
            pass
        self.log("daemon stopped")
        self.log.close()
        self.lock.release()


def run(config: dict, foreground: bool = False) -> int:
    return Daemon(config, foreground=foreground).run()
