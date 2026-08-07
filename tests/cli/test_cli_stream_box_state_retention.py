"""Regression tests for the "reverse reasoning" CLI render-state bug.

Symptom (intermittent, hermes TUI):
    After one turn closes its reasoning box + stream box, the next turn's
    reasoning chunks render into a fresh reasoning box, but the response
    box from the previous turn has not been reset. The result is a screen
    that visually inverts:
        - the response bubble contains text from the PRIOR turn (or the
          prior turn's tool-call reflection),
        - the reasoning pane below shows the CURRENT turn's planning.

    The reasoning is "future tense relative to" the response — exactly
    the "reverse reasoning" symptom.

Root cause (suspected): the CLI render state flags
(``_stream_box_opened``, ``_reasoning_box_opened``,
``_reasoning_shown_this_turn``, ``_reasoning_buf``, ``_stream_prefilt``,
``_deferred_content``) leak across turns. The key line is
``cli.py:5587``:

    if getattr(self, "_stream_box_opened", False): return

This guard silently drops reasoning that arrives while a response bubble
is open — by design, so reasoning never draws INSIDE an open response.
But the SAME guard becomes a bug if ``_stream_box_opened`` from turn N
is not cleared before turn N+1's reasoning begins: turn N+1 reasoning
is suppressed, the user sees turn N's response bubble above the new
turn's reasoning preview (only shown after the box is "closed"), and
the panels look inverted.

These tests pin the render-state invariants:
    1. The line 5587 guard fires exactly as expected.
    2. After a full reasoning + content cycle, all render state
       flags return to ``False`` / ``""`` so the next turn starts clean.
    3. The between-turn reset does NOT clear
       ``_reasoning_shown_this_turn`` (a footgun that would break
       "shown reasoning this turn" tracking across tool boundaries).

These tests stop short of driving ``_stream_delta`` end-to-end because
that path depends on ``show_timestamps`` / the skin engine / table-mode
state which is orthogonal to the bug class.
"""
from __future__ import annotations

from unittest.mock import patch


def _make_cli(*, show_reasoning=True, streaming_enabled=True):
    """Bare HermesCLI stub — render state only. No transport, no skin."""
    from cli import HermesCLI

    cli = HermesCLI.__new__(HermesCLI)
    cli.show_reasoning = show_reasoning
    cli.streaming_enabled = streaming_enabled

    # ── Reasoning render state
    cli._reasoning_box_opened = False
    cli._reasoning_shown_this_turn = False
    cli._reasoning_buf = ""

    # ── Stream (response) render state
    cli._stream_box_opened = False
    cli._stream_prefilt = ""
    cli._deferred_content = ""
    cli._stream_needs_break = False
    cli._stream_text_ansi = ""

    # ── Display config (needed by _flush_stream / _emit_stream_text)
    cli.show_timestamps = False
    cli.final_response_markdown = "render"
    cli._in_stream_table = False
    cli._stream_table_buf = []
    cli._stream_buf = ""
    cli._stream_started = False
    cli._stream_last_was_newline = True

    return cli


# ── Test 1: line 5587 guard fires correctly ──────────────────────────────────


class TestReasoningSuppressedWhenStreamBoxOpen:
    """The line-5587 guard: when the response box is open, reasoning
    deltas are silently suppressed. This is INTENTIONAL behavior; the
    bug is when the guard fires ACROSS turns because ``_stream_box_opened``
    was not reset between turns."""

    @patch("cli._cprint")
    def test_reasoning_dropped_while_response_box_open(self, mock_cprint):
        cli = _make_cli()
        cli._stream_box_opened = True  # response bubble is open

        # Reasoning arriving NOW must be silently suppressed.
        cli._stream_reasoning_delta("thinking about X")

        # Reasoning buffer untouched — guard dropped it.
        assert cli._reasoning_buf == "", (
            f"Reasoning leaked into an open response bubble. "
            f"_reasoning_buf={cli._reasoning_buf!r}"
        )
        assert cli._reasoning_box_opened is False, (
            "Reasoning box was opened INSIDE an open response bubble. "
            "cli.py:5587 guard should have suppressed this entirely."
        )


class TestReasoningEmittedWhenStreamBoxClosed:
    """When the response bubble has been closed, the very next
    reasoning delta opens a fresh reasoning panel."""

    @patch("cli._cprint")
    def test_reasoning_after_closed_response_box_opens_panel(self, mock_cprint):
        cli = _make_cli()
        cli._stream_box_opened = False  # pre-condition

        cli._stream_reasoning_delta("fresh thought\n")

        # Live reasoning contract: reasoning box opens immediately on
        # the first reasoning token so the user sees the chain-of-thought
        # render in real time, ABOVE the (not-yet-opened) response panel.
        assert cli._reasoning_box_opened is True, (
            "Reasoning box should open LIVE on the first reasoning token. "
            "The deferred-render path (the cherry-pick) hides the chain-of-"
            "thought until AFTER the response — that inverts the temporal "
            "order the user expects to read. See "
            "docs/maestro/transcripts/2026-07-13-rev-reasoning-fix-tmux.txt "
            "for the live symptom of that inversion."
        )
        assert cli._stream_box_opened is False


# ── Test 2: between-turn reset invariants ─────────────────────────────────────


class TestCrossTurnStreamBoxReset:
    """After a turn completes (reasoning emitted, response opened,
    response closed), the snapshot at the START of the next turn must
    have ``_stream_box_opened = False`` and ``_reasoning_box_opened
    = False``. If either flag is True at turn-start, the very next
    reasoning or content delta is dropped/silenced and the panels
    get out of sync — the screenshot's "reverse reasoning" symptom."""

    @patch("cli._cprint")
    def test_turn_n_complete_state_resets_between_turns(self, mock_cprint):
        cli = _make_cli()
        cli._stream_reasoning_delta("turn N: thinking")
        # Manually set the post-content state without driving _stream_delta
        # (which depends on skin/timestamps/table-mode).
        cli._stream_box_opened = True
        # Simulate end-of-turn flush: close both boxes.
        cli._close_reasoning_box()
        cli._reasoning_box_opened = False
        cli._stream_box_opened = False
        cli._reasoning_buf = ""

        # Snapshot for the next turn.
        state_at_start_of_n1 = {
            "_stream_box_opened": cli._stream_box_opened,
            "_reasoning_box_opened": cli._reasoning_box_opened,
            "_reasoning_buf": cli._reasoning_buf,
            "_stream_prefilt": cli._stream_prefilt,
            "_deferred_content": cli._deferred_content,
        }

        assert state_at_start_of_n1 == {
            "_stream_box_opened": False,
            "_reasoning_box_opened": False,
            "_reasoning_buf": "",
            "_stream_prefilt": "",
            "_deferred_content": "",
        }, (
            f"REVERSE-REASONING BUG (state leak): turn-N complete state "
            f"leaked into the start of turn N+1. snapshot={state_at_start_of_n1!r}. "
            f"If _stream_box_opened leaks True, turn N+1 reasoning at "
            f"cli.py:5587 will be silently suppressed and the user will "
            f"see turn N's response bubble above turn N+1's reasoning "
            f"panel — the exact 'reverse reasoning' symptom."
        )


class TestFlushStreamResetsStreamBoxFlag:
    """``_flush_stream`` (line 5895) prints the close-bar of the response
    box but does NOT reset ``_stream_box_opened``. The flag reset is
    the responsibility of ``_reset_stream_state`` (a sibling called only
    when ``_stream_delta(None)`` fires). Between the visual close and
    the next user turn (which calls ``_reset_stream_state`` at line
    12226), any reasoning delta is silently dropped by the line-5587
    guard. Pin: ``_flush_stream`` itself must also clear
    ``_stream_box_opened`` so the visual close and the flag reset
    happen together."""

    @patch("cli._cprint")
    def test_flush_stream_clears_stream_box_flag(self, mock_cprint):
        cli = _make_cli()

        # Simulate a response stream that opened and partially filled
        # the response box.
        cli._stream_box_opened = True
        cli._stream_buf = "partial response line"

        # Flush at end-of-stream.
        cli._flush_stream()

        # The visual close was already emitted; the flag must also be
        # clear so the next reasoning delta is not silently dropped at
        # cli.py:5587.
        assert cli._stream_box_opened is False, (
            f"REVERSE-REASONING BUG (close-without-reset): "
            f"_flush_stream printed the response-box close-bar but "
            f"_stream_box_opened stayed True. The next reasoning delta "
            f"will be silently dropped at cli.py:5587 because the flag "
            f"is True even though no response bubble is visible. "
            f"_stream_box_opened={cli._stream_box_opened!r}"
        )


class TestReasoningShownFlagSurvivesResetStreamState:
    """Companion invariant: ``_reasoning_shown_this_turn`` must NOT be
    cleared by an intermediate stream-state reset (a tool boundary
    inside the same turn). Otherwise the "did we show reasoning this
    turn?" tracking is reset across tool boundaries. The reset is
    correct, but it must NOT touch this flag."""

    @patch("cli._cprint")
    def test_shown_flag_persists_through_reset_stream_state(self, mock_cprint):
        cli = _make_cli()
        cli._stream_reasoning_delta("thinking...")
        assert cli._reasoning_shown_this_turn is True

        # An intermediate turn-boundary reset (tool boundary) must keep
        # the flag so subsequent rendering decisions still know we
        # already showed reasoning this turn.
        cli._reset_stream_state()

        assert cli._reasoning_shown_this_turn is True, (
            "REVERSE-REASONING BUG (footgun): _reset_stream_state cleared "
            "_reasoning_shown_this_turn. This flag tracks whether the turn "
            "ever emitted reasoning — clearing it across tool boundaries "
            "would cause the turn-end summary to mis-classify."
        )
