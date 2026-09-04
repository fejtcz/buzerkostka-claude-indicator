"""Buzerkostka <-> Claude Code status indicator.

A tiny, dependency-free toolkit that turns a Pajenicko BUZERKOSTKA
(ESP32 + single WS2812 RGB LED, controlled over MQTT or HTTP) into a
physical status light for Claude Code sessions.

Architecture
------------
The cube firmware only understands "show this exact RGB colour" -- it has
no built-in effects. Anything animated (breathing, blinking) therefore has
to be rendered host side, frame by frame. That rules out doing the work
inside a Claude Code hook, because hooks are short-lived processes.

So the package is split in two:

``buzerkostka.daemon``
    A long-lived process. Owns the single MQTT/HTTP connection, keeps the
    per-session state machine, renders animation frames and publishes
    colours. Listens on a Unix domain socket.

``buzerkostka.client``
    A deliberately tiny module used by Claude Code hooks. It reads the
    hook payload from stdin, sends one datagram-ish line to the daemon and
    exits. It starts the daemon on demand and never fails loudly, so a
    broken cube can never break a Claude Code session.
"""

__version__ = "1.0.0"

__all__ = ["__version__"]
