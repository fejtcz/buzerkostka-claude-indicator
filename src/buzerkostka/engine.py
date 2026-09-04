"""Per-session state tracking and aggregation into a single colour.

There is one cube but there can be many Claude Code sessions -- several
terminal tabs, a couple of IDE windows, maybe a session on another
machine pointed at the same broker. The engine keeps one record per
session and resolves them down to one frame using state priority, so the
thing that most needs your attention is what you see.

Each session holds:

``base``
    What the session is persistently doing (``idle``, ``working``,
    ``permission``, ...).

``overlay``
    An optional transient state (``done``, ``error``) that plays for a
    fixed duration and then reveals ``base`` again.

Two safety nets matter in practice. A Claude Code process that is killed
never fires ``SessionEnd``, so every state may declare a ``timeout``
after which it decays to ``idle``; and a session that has been silent for
``daemon.session_ttl_s`` is forgotten entirely. Without those, one
crashed session leaves the cube stuck on red forever.
"""

from __future__ import annotations

import time

from .states import build_states, frame_for_transport, next_frame_delay, render

#: Applying this state to a session means "I no longer want the light".
#: The session is dropped; when the last one goes, the cube goes dark.
RELEASE_STATE = "off"

DECAY_STATE = "idle"


def _transcript_size(path) -> int:
    from .watch import size_of

    return size_of(path) or 0


class Session:
    __slots__ = (
        "id",
        "base",
        "base_started",
        "overlay",
        "overlay_started",
        "last_seen",
        "last_ts",
        "cwd",
        "label",
        "events",
        "transcript",
        "transcript_pos",
    )

    def __init__(self, session_id: str, now: float):
        self.id = session_id
        self.base = DECAY_STATE
        self.base_started = now
        self.overlay = None
        self.overlay_started = 0.0
        self.last_seen = now
        self.last_ts = 0.0
        self.cwd = None
        self.label = None
        self.events = 0
        # Where the transcript watcher has read up to. See watch.py: a
        # denied permission prompt fires no hook, so the transcript is
        # the only place that tells us the human said no.
        self.transcript = None
        self.transcript_pos = 0

    def effective(self) -> tuple:
        """The state name currently in charge and when it started."""
        if self.overlay is not None:
            return (self.overlay, self.overlay_started)
        return (self.base, self.base_started)


class Engine:
    def __init__(self, config: dict):
        self.reload(config)
        self.sessions = {}
        self.paused = False
        self._last_frame = None

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------
    def reload(self, config: dict) -> None:
        """Swap in a new config without losing session state."""
        self.config = config
        self.states = build_states(config.get("states"))
        self.events = config.get("events") or {}
        render_config = config.get("render") or {}
        self.fps = int(render_config.get("fps", 20))
        self.brightness = float(render_config.get("brightness", 1.0))
        self.gamma = float(render_config.get("gamma", 2.0))
        device = config.get("device") or {}
        self.channel_order = device.get("channel_order") or "rgb"
        daemon = config.get("daemon") or {}
        self.session_ttl = float(daemon.get("session_ttl_s", 28800))
        self.enabled = bool(config.get("enabled", True))
        self._last_frame = None  # force a re-publish after a reload

    # ------------------------------------------------------------------
    # Input
    # ------------------------------------------------------------------
    def resolve_event(self, event: str):
        """Map a hook event name to ``(state_name, base_name)``.

        Returns ``None`` for unknown events so a future Claude Code
        release can add hooks without breaking an older config.
        """
        mapping = self.events.get(event)
        if mapping is None:
            return None
        if isinstance(mapping, str):
            return (mapping, mapping)
        state = mapping.get("state")
        base = mapping.get("base")
        if state is None and base is None:
            return None
        return (state, base)

    def apply_event(self, event: str, session_id=None, ts=None, cwd=None, label=None,
                    transcript=None) -> bool:
        """Feed one hook event in. Returns True if the light may need updating."""
        resolved = self.resolve_event(event)
        if resolved is None:
            return False
        state_name, base_name = resolved
        return self.apply_state(
            state_name, base_name, session_id=session_id, ts=ts, cwd=cwd, label=label,
            transcript=transcript,
        )

    def apply_state(
        self, state_name, base_name=None, session_id=None, ts=None, cwd=None, label=None,
        transcript=None
    ) -> bool:
        now = time.time()
        session_id = session_id or "default"

        for name in (state_name, base_name):
            if name is not None and name not in self.states and name != RELEASE_STATE:
                return False

        if state_name == RELEASE_STATE or base_name == RELEASE_STATE:
            return self.release(session_id)

        session = self.sessions.get(session_id)
        if session is None:
            session = Session(session_id, now)
            self.sessions[session_id] = session

        # Hooks may run asynchronously, so two events can arrive out of
        # order. Timestamps come from the same machine, so a strictly
        # older event is safe to drop.
        if ts is not None:
            if ts < session.last_ts:
                return False
            session.last_ts = ts

        session.last_seen = now
        session.events += 1
        if cwd:
            session.cwd = cwd
        if label:
            session.label = label
        if transcript and transcript != session.transcript:
            # Start reading where the file is now; everything before this
            # point is history we must not react to.
            session.transcript = transcript
            session.transcript_pos = _transcript_size(transcript)

        if base_name is not None and base_name != session.base:
            session.base = base_name
            session.base_started = now

        if state_name is not None:
            state = self.states[state_name]
            if state.transient:
                session.overlay = state_name
                session.overlay_started = now
            else:
                session.overlay = None
                # Re-asserting the same state (a run of PostToolUse events,
                # say) deliberately leaves base_started alone, so the
                # animation phase does not restart.
                if state_name != session.base:
                    session.base = state_name
                    session.base_started = now
        return True

    def release(self, session_id: str) -> bool:
        """Forget a session. Returns True if it existed."""
        return self.sessions.pop(session_id, None) is not None

    def clear(self) -> None:
        self.sessions.clear()

    # ------------------------------------------------------------------
    # Time-driven bookkeeping
    # ------------------------------------------------------------------
    def expire(self, now=None) -> bool:
        """Retire finished overlays, decayed states and dead sessions."""
        now = time.time() if now is None else now
        changed = False

        for session_id in list(self.sessions):
            session = self.sessions[session_id]

            if now - session.last_seen > self.session_ttl:
                del self.sessions[session_id]
                changed = True
                continue

            if session.overlay is not None:
                state = self.states.get(session.overlay)
                duration = state.duration if state else 0.0
                if duration is None or now - session.overlay_started >= duration:
                    session.overlay = None
                    changed = True

            base = self.states.get(session.base)
            if base is not None and base.timeout is not None:
                if now - session.base_started >= base.timeout:
                    session.base = DECAY_STATE
                    session.base_started = now
                    changed = True

        return changed

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------
    def winner(self):
        """The session whose state is currently shown, or ``None``."""
        best = None
        best_key = None
        for session in self.sessions.values():
            name, started = session.effective()
            state = self.states.get(name)
            if state is None:
                continue
            # Highest priority wins; the most recently entered state
            # breaks ties so a second session going red is visible.
            key = (state.priority, started)
            if best_key is None or key > best_key:
                best_key = key
                best = (session, state, started)
        return best

    def frame(self, now=None):
        """The colour to publish right now, already device-ordered."""
        now = time.time() if now is None else now
        if not self.enabled or self.paused:
            return (0, 0, 0)

        current = self.winner()
        if current is None:
            return (0, 0, 0)

        _session, state, started = current
        rgb = render(state, now - started, self.gamma)
        if rgb is None:
            # A transient state finished between expire() and frame().
            rgb = (0, 0, 0)
        return frame_for_transport(rgb, self.brightness, self.channel_order)

    def sleep_hint(self, now=None) -> float:
        """Seconds the render loop may sleep before anything changes."""
        now = time.time() if now is None else now
        current = self.winner()
        if current is None or not self.enabled or self.paused:
            return 1.0
        _session, state, started = current
        delay = next_frame_delay(state, self.fps)
        if state.transient and state.duration is not None:
            remaining = (started + state.duration) - now
            if 0.0 < remaining < delay:
                return max(0.005, remaining)
        return delay

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    def snapshot(self, now=None) -> dict:
        now = time.time() if now is None else now
        current = self.winner()
        sessions = []
        for session in sorted(self.sessions.values(), key=lambda s: s.last_seen):
            name, started = session.effective()
            sessions.append(
                {
                    "session": session.id,
                    "state": name,
                    "base": session.base,
                    "overlay": session.overlay,
                    "for_s": round(now - started, 2),
                    "idle_s": round(now - session.last_seen, 2),
                    "events": session.events,
                    "cwd": session.cwd,
                    "label": session.label,
                }
            )
        return {
            "enabled": self.enabled,
            "paused": self.paused,
            "active_state": current[1].name if current else None,
            "active_session": current[0].id if current else None,
            "sessions": sessions,
            "brightness": self.brightness,
            "fps": self.fps,
            "channel_order": self.channel_order,
        }
