"""The transcript watcher -- our stand-in for the hook Claude Code lacks.

Answering "No" to a permission prompt fires no hook at all (verified
against Claude Code 2.1.259 with a logger on all 33 events), so the cube
would blink orange at an idle session forever. ``watch.scan`` is what
notices instead.
"""

import os

import pytest

from buzerkostka import watch
from buzerkostka.config import DEFAULTS
from buzerkostka.engine import Engine

MARKER = '{"type":"user","message":{"content":[{"type":"text",' \
         '"text":"[Request interrupted by user for tool use]"}]}}\n'


@pytest.fixture
def transcript(tmp_path):
    path = tmp_path / "session.jsonl"
    path.write_text('{"type":"user","message":{"content":"hello"}}\n')
    return str(path)


def test_a_missing_file_is_not_an_error():
    assert watch.scan("/nowhere/at/all.jsonl", 0) == (0, False)
    assert watch.size_of(None) is None


def test_nothing_appended_means_nothing_happened(transcript):
    size = os.path.getsize(transcript)
    assert watch.scan(transcript, size) == (size, False)


def test_the_interrupt_marker_is_found_once(transcript):
    size = os.path.getsize(transcript)
    with open(transcript, "a") as handle:
        handle.write(MARKER)

    position, interrupted = watch.scan(transcript, size)
    assert interrupted is True
    assert position == os.path.getsize(transcript)

    # Reading on from there must not report the same interruption twice,
    # or the cube would be dragged back to idle every second.
    assert watch.scan(transcript, position) == (position, False)


def test_merely_talking_about_an_interruption_is_not_one(transcript):
    """A transcript is full of prose that quotes the marker -- a command
    you ran, a file you edited, this test file. Only a user record whose
    own content is the marker counts."""
    size = os.path.getsize(transcript)
    quoted = "[Request interrupted by user for tool use]"
    with open(transcript, "a") as handle:
        handle.write('{"type":"assistant","message":{"content":[{"type":"text",'
                     '"text":"you will see %s in the log"}]}}\n' % quoted)
        handle.write('{"type":"user","message":{"content":[{"type":"tool_result",'
                     '"content":"%s"}]}}\n' % quoted)

    position, interrupted = watch.scan(transcript, size)
    assert interrupted is False
    assert position == os.path.getsize(transcript)


def test_ordinary_traffic_does_not_trip_it(transcript):
    size = os.path.getsize(transcript)
    with open(transcript, "a") as handle:
        handle.write('{"type":"assistant","message":{"content":"working"}}\n')
    position, interrupted = watch.scan(transcript, size)
    assert interrupted is False
    assert position > size


def test_a_half_written_line_is_left_for_the_next_poll(transcript):
    size = os.path.getsize(transcript)
    with open(transcript, "a") as handle:
        handle.write(MARKER.rstrip("\n")[:40])  # no newline yet

    assert watch.scan(transcript, size) == (size, False)

    with open(transcript, "a") as handle:
        handle.write(MARKER[40:])
    position, interrupted = watch.scan(transcript, size)
    assert interrupted is True
    assert position == os.path.getsize(transcript)


def test_a_truncated_file_re_anchors_instead_of_replaying(transcript):
    """A resumed session may write a shorter file; old interruptions in it
    must not be re-fired."""
    with open(transcript, "a") as handle:
        handle.write(MARKER)
    size = os.path.getsize(transcript)

    with open(transcript, "w") as handle:
        handle.write("{}\n")
    position, interrupted = watch.scan(transcript, size)
    assert interrupted is False
    assert position == os.path.getsize(transcript)


def test_only_the_tail_is_read_when_a_session_has_been_busy(transcript):
    with open(transcript, "a") as handle:
        handle.write('{"filler":"%s"}\n' % ("x" * 5000))
        handle.write(MARKER)
    position, interrupted = watch.scan(transcript, 0, max_bytes=1024)
    assert interrupted is True
    assert position == os.path.getsize(transcript)


# ----------------------------------------------------------------------
# Engine side
# ----------------------------------------------------------------------
def test_the_engine_anchors_at_the_current_end_of_the_transcript(transcript):
    """History written before we ever heard of the session is not ours to
    react to."""
    engine = Engine(DEFAULTS)
    engine.apply_event("permission", session_id="s1", transcript=transcript)

    session = engine.sessions["s1"]
    assert session.transcript == transcript
    assert session.transcript_pos == os.path.getsize(transcript)


def test_the_interrupted_event_takes_a_session_back_to_idle():
    engine = Engine(DEFAULTS)
    engine.apply_event("permission", session_id="s1")
    assert engine.winner()[1].name == "permission"

    assert engine.apply_event("interrupted", session_id="s1") is True
    assert engine.winner()[1].name == "idle"
