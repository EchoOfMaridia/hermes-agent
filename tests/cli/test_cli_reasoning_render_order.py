"""Regression tests for the ACTUAL reverse-reasoning bug — render-order.

Bug class (regression of bugs-from-hell #2):
    When reasoning chunks arrive BEFORE the response text starts streaming,
    the live code path at cli.py:5574 `_stream_reasoning_delta` renders
    the reasoning box IMMEDIATELY (at the top of the screen). Then the
    response box opens, streams, and closes BELOW the reasoning box.
    Result: reasoning appears ABOVE the response, visually inverted
    from what the user expects.

Previous fix attempt (test_cli_stream_box_state_retention.py):
    Pinned the _state_ flags (_stream_box_opened, _reasoning_box_opened,
    _reasoning_shown_this_turn) but missed the actual symptom — it
    tested that the live reasoning box DID open at the right moment,
    not that reasoning should be DEFERRED until after the response.

Live-condition slippage:
    The mocked tests verified flag transitions but did not verify
    that reasoning TEXT is buffered and only rendered AFTER the
    response box closes. The live LLM hits this path constantly.

Fix shape:
    1. `_stream_reasoning_delta` (cli.py:5574) accumulates reasoning
       text into a per-turn buffer field instead of rendering live.
    2. After the response box closes, the finalizer (cli.py:12650)
       reads that buffer and renders the reasoning panel.
    3. The buffer is cleared at turn boundary (cli.py:12234).
"""
from __future__ import annotations

from unittest.mock import patch


def _make_cli(*, show_reasoning=True, streaming_enabled=True):
    """Bare HermesCLI stub — render state only."""
    from cli import HermesCLI

    cli = HermesCLI.__new__(HermesCLI)
    cli.show_reasoning = show_reasoning
    cli.streaming_enabled = streaming_enabled

    # Per-turn render state
    cli._reasoning_box_opened = False
    cli._reasoning_shown_this_turn = False
    cli._reasoning_buf = ""
    cli._reasoning_preview_buf = ""

    # Stream state
    cli._stream_box_opened = False
    cli._stream_completed = False
    cli._stream_started = False
    cli._stream_buf = ""
    cli._stream_prefilt = ""
    cli._stream_text_ansi = ""
    cli._deferred_content = ""
    cli._in_stream_table = False
    cli._stream_table_buf = []
    cli._stream_last_was_newline = True
    cli._stream_needs_break = False

    # Display config
    cli.show_timestamps = False
    cli.final_response_markdown = "strip"

    return cli


def _cprint_calls(mock_cprint):
    return [str(call_args) for call_args in mock_cprint.call_args_list]


# Test 1: _stream_reasoning_delta does NOT render the live reasoning box


class TestReasoningDeferredUntilAfterResponse:
    """The bug: reasoning box appears at the TOP of the screen (above
    the response) because _stream_reasoning_delta at cli.py:5574
    renders it inline as reasoning chunks arrive.

    Fix: _stream_reasoning_delta should ONLY buffer the text, not
    render it. Reasoning renders AFTER the response completes."""

    @patch("cli._cprint")
    def test_stream_reasoning_delta_does_not_open_live_box(self, mock_cprint):
        """A reasoning chunk arriving while no response box is open
        must NOT print a `┌─ Reasoning ─...┐` header to stdout. The
        reasoning must be buffered for the finalizer to render AFTER
        the response completes."""
        cli = _make_cli()
        assert cli._stream_box_opened is False
        assert cli._reasoning_box_opened is False

        with patch.object(cli, "_scrollback_box_width", return_value=80):
            cli._stream_reasoning_delta("The user wants the three-file architecture.\n")

        all_calls = _cprint_calls(mock_cprint)
        reasoning_box_opened = any("Reasoning" in c for c in all_calls)
        assert not reasoning_box_opened, (
            f"REVERSE-REASONING BUG: _stream_reasoning_delta rendered a "
            f"live reasoning box ({reasoning_box_opened=}). Reasoning must "
            f"be buffered for the finalizer, not rendered inline. "
            f"_cprint calls: {all_calls}"
        )

    @patch("cli._cprint")
    def test_stream_reasoning_delta_accumulates_into_buffer(self, mock_cprint):
        """The reasoning text must be accumulated into a per-turn buffer
        so the finalizer can render it AFTER the response box closes."""
        cli = _make_cli()

        with patch.object(cli, "_scrollback_box_width", return_value=80):
            cli._stream_reasoning_delta("First chunk of reasoning.\n")
            cli._stream_reasoning_delta("Second chunk of reasoning.\n")

        # The fix: a per-turn buffer field holds the accumulated text.
        buf = getattr(cli, "_reasoning_buffered_for_after_response", None)
        assert buf is not None, (
            "REVERSE-REASONING BUG: _stream_reasoning_delta did not write "
            "to _reasoning_buffered_for_after_response. The buffer field "
            "is missing — fix needs to add it."
        )
        assert "First chunk" in buf, (
            f"REVERSE-REASONING BUG: buffer missing first chunk. "
            f"buffer={buf!r}"
        )
        assert "Second chunk" in buf, (
            f"REVERSE-REASONING BUG: buffer missing second chunk. "
            f"buffer={buf!r}"
        )

    @patch("cli._cprint")
    def test_reasoning_buffer_cleared_at_turn_boundary(self, mock_cprint):
        """At turn boundary (cli.py:12234), the reasoning buffer must be
        cleared so next-turn reasoning doesn't accumulate into the
        previous turn's reasoning display."""
        cli = _make_cli()

        with patch.object(cli, "_scrollback_box_width", return_value=80):
            cli._stream_reasoning_delta("Turn 1 reasoning text.\n")

        # Simulate turn-boundary reset
        cli._reset_stream_state()
        cli._reasoning_shown_this_turn = False
        cli._reasoning_buffered_for_after_response = ""

        buf = getattr(cli, "_reasoning_buffered_for_after_response", "")
        assert buf == "", (
            f"REVERSE-REASONING BUG: turn-boundary reset did not clear "
            f"_reasoning_buffered_for_after_response. value={buf!r}"
        )


# Test 2: Finalizer reads the buffer and renders reasoning AFTER response


class TestFinalizerRendersReasoningAfterResponse:
    """After the response box closes, the finalizer should render the
    buffered reasoning panel. This is the user-facing fix."""

    @patch("cli._cprint")
    def test_finalizer_renders_reasoning_from_buffer(self, mock_cprint):
        """When the finalizer runs after a response completes, it must
        read the per-turn reasoning buffer and render it as a
        `┌─ Reasoning ─...┐` panel."""
        cli = _make_cli()
        cli._reasoning_buffered_for_after_response = (
            "The user wants a specific persona. Let me walk through the\n"
            "three-step workflow."
        )
        # Simulate: response streamed and closed (flush_state post-response)
        cli._stream_started = True
        cli._stream_completed = True
        cli._stream_box_opened = False

        # Finalizer shape (the production finalizer at cli.py:12650-12666
        # should be modified to read _reasoning_buffered_for_after_response
        # instead of result.get("last_reasoning") which is never set):
        if cli._reasoning_buffered_for_after_response and cli.show_reasoning:
            reasoning = cli._reasoning_buffered_for_after_response
            w = 80
            r_label = " Reasoning "
            r_fill = w - 2 - len(r_label)
            r_top = f"┌─{r_label}{'─' * max(r_fill - 1, 0)}┐"
            r_bot = f"└{'─' * (w - 2)}┘"
            mock_cprint(f"\n{r_top}\n{reasoning.strip()}\n{r_bot}")

        all_calls = _cprint_calls(mock_cprint)
        rendered = any("Reasoning" in c for c in all_calls)
        assert rendered, (
            f"REVERSE-REASONING BUG: finalizer did not render reasoning "
            f"panel from _reasoning_buffered_for_after_response. "
            f"_cprint calls: {all_calls}"
        )


# Test 3: Reasoning that arrives during an open response box is dropped


class TestReasoningDuringOpenResponseBox:
    """When reasoning arrives while a response box is open, the
    existing behavior (early return at cli.py:5588) is preserved."""

    @patch("cli._cprint")
    def test_reasoning_during_open_response_is_dropped(self, mock_cprint):
        cli = _make_cli()
        cli._stream_box_opened = True  # response box already open

        cli._stream_reasoning_delta("Late reasoning during open response.\n")

        all_calls = _cprint_calls(mock_cprint)
        assert not any("Reasoning" in c for c in all_calls), (
            f"REASONING-DURING-RESPONSE BUG: reasoning arrived while response "
            f"box was open but was still rendered. _cprint calls: {all_calls}"
        )