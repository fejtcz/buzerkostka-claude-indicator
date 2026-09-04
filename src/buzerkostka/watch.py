"""Noticing the two things Claude Code's hooks never tell us.

Answering **No** to a permission prompt fires no hook at all. The tool
never runs, so there is no ``PostToolUse``; the turn is abandoned, so
there is no ``Stop``; and ``PermissionDenied`` turns out to be reserved
for auto-mode's classifier, not for a human pressing "No". Pressing Esc
to interrupt a running turn is the same story. Verified against Claude
Code 2.1.259 by registering a logger on all 33 hook events: a denied
Bash call produces ``PreToolUse`` and ``PermissionRequest``, and then
silence -- which is exactly how the cube ends up blinking orange at an
idle session.

The transcript does record it. Both cases append a *user* record whose
content is the text ``[Request interrupted by user...]``, so the daemon
tails the transcript of every session it knows about and watches for
that. Tailing is deliberately dumb -- a size check, then a read of what
was appended -- because it runs once a second for every live session.

The check is structural rather than a substring match, because a
transcript is full of text that merely *mentions* interruptions: a
command you ran, a file you edited, this very docstring. Only a user
record whose own content is the marker counts.
"""

from __future__ import annotations

import json
import os

#: Substrings that mean "this turn was cut short by the human". Kept as a
#: prefix match so both ``[Request interrupted by user]`` and
#: ``[Request interrupted by user for tool use]`` are caught.
INTERRUPT_MARKERS = (b"[Request interrupted by user",)

#: Never read more than this in one go. An interruption is always the
#: newest thing in the file, so when a session has been busy we can skip
#: straight to the tail instead of paging through a huge tool result.
MAX_TAIL_BYTES = 256 * 1024


def size_of(path):
    """``os.path.getsize`` that answers ``None`` instead of raising."""
    if not path:
        return None
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def scan(path: str, offset: int, max_bytes: int = MAX_TAIL_BYTES):
    """Read what was appended to ``path`` since ``offset``.

    Returns ``(new_offset, interrupted)``. The offset only ever advances
    to the end of the last *complete* line, so a marker that is still
    being written is seen on the next poll rather than split in half and
    missed.
    """
    size = size_of(path)
    if size is None:
        return (offset, False)
    if size < offset:
        # Truncated, rotated or a resumed session writing a fresh file.
        # Re-anchoring at the end is the safe choice: replaying history
        # would resurrect every interruption the session ever had.
        return (size, False)
    if size == offset:
        return (offset, False)

    start = offset
    if size - start > max_bytes:
        start = size - max_bytes

    try:
        with open(path, "rb") as handle:
            handle.seek(start)
            chunk = handle.read(size - start)
    except OSError:
        return (offset, False)

    end = chunk.rfind(b"\n")
    if end < 0:
        # No complete line yet. Wait for one, unless the unterminated
        # remainder has grown past the cap and would be skipped anyway.
        if size - start < max_bytes:
            return (offset, False)
        end = len(chunk) - 1

    complete = chunk[: end + 1]
    new_offset = start + end + 1
    return (new_offset, _contains_interruption(complete))


def _contains_interruption(blob: bytes) -> bool:
    for line in blob.split(b"\n"):
        # The substring test is only a cheap filter; the transcript is
        # full of prose that quotes the marker without being one.
        if not any(marker in line for marker in INTERRUPT_MARKERS):
            continue
        if is_interrupt_record(line):
            return True
    return False


def is_interrupt_record(line: bytes) -> bool:
    """Is this transcript line Claude Code recording "the human said no"?

    The shape is ``{"type": "user", "message": {"content": [{"type":
    "text", "text": "[Request interrupted by user for tool use]"}]}}``.
    Anything else -- an assistant message discussing interruptions, a
    tool result that happens to print one -- is not it.
    """
    try:
        record = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        return False
    if not isinstance(record, dict) or record.get("type") != "user":
        return False
    content = (record.get("message") or {}).get("content")
    if isinstance(content, str):
        blocks = [{"type": "text", "text": content}]
    elif isinstance(content, list):
        blocks = content
    else:
        return False
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text")
        if not isinstance(text, str):
            continue
        stripped = text.strip().encode("utf-8", "replace")
        if any(stripped.startswith(marker) for marker in INTERRUPT_MARKERS):
            return True
    return False
