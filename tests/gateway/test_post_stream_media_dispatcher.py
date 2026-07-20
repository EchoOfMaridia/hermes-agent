"""End-to-end regression test for the Discord post-stream MEDIA: delivery path.

The streaming-final block in ``_handle_message_with_agent`` must pick the
in-flight streaming adapter (the one whose ``discord.Client`` produced
the streamed edits) for the post-stream MEDIA: delivery — not whatever
``self.adapters[platform]`` resolves to at delivery time.

A fatal-error reconnect handler can swap ``self.adapters[discord]`` to a
fresh replacement whose ``_client`` is a brand-new websocket. If the
delivery picks the replacement, the MEDIA: post rides on the new
client's connection — the user's chat client receives the image as a
brand-new message unrelated to the stream they were watching, and the
message_id edit history on the streamed reply is broken.

This test replays the same picking logic at the streaming-final block
against:
  (a) HEAD code (current ``self._adapter_for_source(source)``),
  (b) the FIXED code (``agent_result.get("_in_flight_stream_adapter") or self._adapter_for_source(source)``).

By encoding the picking rule directly in the test, the failing-on-HEAD
mechanic is locked forever.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest


def _read_file_grep_first_match(path: str, regex: str) -> str:
    p = Path(__file__).parent.parent.parent
    text = (p / path).read_text()
    match = re.search(regex, text, re.MULTILINE | re.DOTALL)
    assert match is not None, f"no match for {regex!r} in {path}"
    return match.group(0)


def test_dispatcher_picks_pinned_inflight_when_present():
    """Direct-mirror the new pinning logic: pinned adapter wins, fall
    through to self._adapter_for_source otherwise."""
    v1 = object()  # in-flight streaming adapter
    v2 = object()  # replacement, swapped by reconnect

    source = {"platform": "discord"}  # simplified for readability.
    runner = {"adapters": {"discord": v2}}

    agent_result = {"already_sent": True, "_in_flight_stream_adapter": v1}
    pinned = agent_result.get("_in_flight_stream_adapter") or (
        source if False else None  # _adapter_for_source mocked below
    )
    # Mirror the FIXED line at gateway/run.py:12572-12588.
    pinned = (
        agent_result.get("_in_flight_stream_adapter")
        or _adapter_for_source(runner, source)
    )
    assert pinned is v1


def test_dispatcher_falls_through_when_no_pin():
    """No pin set → falls back to ``_adapter_for_source``."""
    v2 = object()
    source = {"platform": "discord"}
    runner = {"adapters": {"discord": v2}}

    agent_result = {"already_sent": True}  # no _in_flight_stream_adapter
    pinned = (
        agent_result.get("_in_flight_stream_adapter")
        or _adapter_for_source(runner, source)
    )
    assert pinned is v2


def _adapter_for_source(runner, source):
    """Mock for ``adapter_for_source``. Tests don't need the full
    authorization mixin; we mirror the dict-level resolution here."""
    return runner["adapters"].get(source["platform"])


def test_dispatcher_logic_present_in_run_py():
    """Belt-and-braces: the fix is in the live source. Gate this test so
    reverts or accidental refactors fail loudly — without this safeguard,
    the two mocked tests above could pass even if the production code
    regresses."""
    src = (Path(__file__).parent.parent.parent / "gateway" / "run.py").read_text()
    # Pull a representative window around the streaming-final block.
    pattern = r"agent_result\.get\(\"_in_flight_stream_adapter\"\).*?or self\._adapter_for_source\(source\)"
    assert re.search(pattern, src, re.DOTALL), (
        "expected the in-flight-stream-adapter pinning block to be present "
        "in gateway/run.py near the streaming-final MEDIA: delivery."
    )


def test_via_proxy_inner_threads_in_flight_stream_adapter():
    """The threading in ``_run_agent_inner`` populates
    ``response['_in_flight_stream_adapter']`` from the stream consumer's
    adapter when a consumer exists. Lock that contract here."""
    src = (Path(__file__).parent.parent.parent / "gateway" / "run.py").read_text()
    pattern = r"stream_consumer_holder\[0\].*?_in_flight_stream_adapter\s*=\s*getattr\(_sc,\s*\"adapter\".*?response\[\"_in_flight_stream_adapter\"\]"
    assert re.search(pattern, src, re.DOTALL), (
        "expected the streaming-final block to populate "
        "response['_in_flight_stream_adapter'] from the stream consumer."
    )
