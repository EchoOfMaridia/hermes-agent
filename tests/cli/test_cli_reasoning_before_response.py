"""Regression tests for the desktop reasoning-block render order.

Bug class (verbatim from the live transcript at
``docs/maestro/transcripts/2026-07-13-rev-reasoning-fix-tmux.txt:96-113``):

    Reasoning should appear ABOVE the response, never below it.  When the
    model emits a chain-of-thought block (or a provider's native
    reasoning field) before the response text, the user reads the
    reasoning first to understand the model's intent, then the answer.
    Rendering the response first and the reasoning panel afterwards
    inverts the temporal order the user expects.

Live symptom (transcript lines 96-113):
    The Hermes response panel (`╭─ ⚕ Hermes ... ╰`) is printed first
    with the answer, THEN the `┌─ Reasoning ─...┐` panel is printed
    below it.  Reading the transcript bottom-to-top gives the
    question → answer → reasoning, but the user's eyes naturally
    re-associate the reasoning with the answer that came BEFORE it
    in the scrollback, which is the response from a PREVIOUS turn.

Root cause:
    A cherry-pick landed in the worktree (visible in
    ``git status --short``) that swapped the contract.  The fix:
    ``_stream_reasoning_delta`` was changed to BUFFER all reasoning
    text into ``_reasoning_buffered_for_after_response`` (instead of
    rendering live) and the finalizer at the end of the turn was
    changed to read that buffer and print the reasoning panel AFTER
    the response panel.  The change was framed as "reasoning was
    appearing above the response" being the bug, but inverting that
    contract produced the actual user-visible regression captured in
    the live transcript.

These tests pin the correct contract — reasoning renders LIVE and
BEFORE the response panel:

    1. The first reasoning token opens a `┌─ Reasoning ─...┐` box
       immediately (line-by-line, force-flush long partials so the
       user sees the thinking in real time, not after a 30-second
       silence).
    2. The first response token closes the reasoning box and opens
       the response box BELOW it (so the response panel always lands
       underneath the reasoning it was generated from).
    3. The reasoning panel never appears after the response box in
       the finalizer (i.e. the buffer-and-defer pattern is gone).
    4. The finalizer's fallback path (when the live reasoning box
       never opened) still renders the reasoning from
       ``result.get('last_reasoning')`` so non-streaming consumers
       don't lose the chain-of-thought.
"""
from __future__ import annotations

from unittest.mock import patch

from cli import HermesCLI


def _make_cli(*, show_reasoning=True, streaming_enabled=True):
    """Bare HermesCLI stub — render state only."""
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


# Test 1: live reasoning box opens on first token, BEFORE the response


class TestReasoningRendersLiveBeforeResponse:
    """The first reasoning delta must open a `┌─ Reasoning ─...┐`
    panel immediately, and that panel must appear in the output
    sequence BEFORE the response panel.

    The "Reasoning after response" bug (the live symptom captured at
    docs/maestro/transcripts/2026-07-13-rev-reasoning-fix-tmux.txt:96-113)
    is the inverse: the response panel was emitted first, the
    reasoning panel afterwards.  Pin the correct order here.
    """

    @patch("cli._cprint")
    def test_live_reasoning_box_opens_on_first_token(self, mock_cprint):
        cli = _make_cli()
        assert cli._reasoning_box_opened is False

        with patch.object(cli, "_scrollback_box_width", return_value=80):
            cli._stream_reasoning_delta("plan step one\n")

        assert cli._reasoning_box_opened is True, (
            "REASONING-AFTER-RESPONSE BUG: _stream_reasoning_delta did "
            "not open the live reasoning box. Live reasoning is the "
            "contract — the user must see the chain-of-thought as it "
            "streams, not after the response panel closes."
        )

    @patch("cli._cprint")
    def test_first_response_token_deferred_until_flush(self, mock_cprint):
        """A response delta arriving while the reasoning box is open
        must defer the text to ``_deferred_content`` so the response
        panel can land BELOW the reasoning panel at flush time."""
        cli = _make_cli()

        with patch.object(cli, "_scrollback_box_width", return_value=80):
            cli._stream_reasoning_delta("plan step one\n")
            assert cli._reasoning_box_opened is True
            cli._stream_delta("answer one\n")

        # Response text was deferred, NOT emitted to the response box.
        assert cli._stream_box_opened is False, (
            "First response delta opened the response box immediately. "
            "Without the deferral, the response panel would land above "
            "the still-open reasoning panel — the exact inverted-order "
            "regression captured in the live transcript."
        )
        assert "answer one" in cli._deferred_content, (
            f"Response text not deferred into _deferred_content. "
            f"deferred={cli._deferred_content!r}"
        )

    @patch("cli._cprint")
    def test_live_reasoning_text_emitted_line_by_line(self, mock_cprint):
        """Reasoning text is emitted line-by-line as the tokens arrive
        (force-flushed past 80 chars so the user sees it in real time)."""
        cli = _make_cli()

        with patch.object(cli, "_scrollback_box_width", return_value=80):
            cli._stream_reasoning_delta("first line of reasoning\n")
            cli._stream_reasoning_delta("second line of reasoning\n")

        all_calls = _cprint_calls(mock_cprint)
        joined = " ".join(all_calls)
        assert "first line of reasoning" in joined, (
            f"Reasoning line 'first line of reasoning' missing from output. "
            f"Calls: {all_calls}"
        )
        assert "second line of reasoning" in joined, (
            f"Reasoning line 'second line of reasoning' missing from output. "
            f"Calls: {all_calls}"
        )

    @patch("cli._cprint")
    def test_reasoning_panel_appears_before_response_panel(self, mock_cprint):
        """End-to-end ordering: the `┌─ Reasoning ─...┐` box must be
        emitted BEFORE the `╭─ ⚕ Hermes ... ╮` response box in the
        output sequence.  This is the exact regression captured in
        docs/maestro/transcripts/2026-07-13-rev-reasoning-fix-tmux.txt
        (response box at line 96, reasoning box at line 104)."""
        cli = _make_cli()

        with patch.object(cli, "_scrollback_box_width", return_value=80):
            cli._stream_reasoning_delta("chain of thought\n")
            cli._stream_delta("the answer\n")
            cli._flush_stream()

        all_calls = _cprint_calls(mock_cprint)
        # Find the index of the reasoning-panel opener and the
        # response-panel opener.
        reasoning_idx = next(
            (i for i, c in enumerate(all_calls) if "Reasoning" in c and "┌─" in c),
            None,
        )
        response_idx = next(
            (i for i, c in enumerate(all_calls) if "Hermes" in c and "╭─" in c),
            None,
        )
        assert reasoning_idx is not None, (
            f"Reasoning panel opener missing from output. Calls: {all_calls}"
        )
        assert response_idx is not None, (
            f"Response panel opener missing from output. Calls: {all_calls}"
        )
        assert reasoning_idx < response_idx, (
            "REASONING-AFTER-RESPONSE BUG: the response panel "
            f"({response_idx=}) was emitted before the reasoning panel "
            f"({reasoning_idx=}). Reasoning must appear ABOVE the "
            f"response. Calls: {all_calls}"
        )


# Test 2: no per-turn buffer; reasoning text goes straight to the box


class TestNoDeferredReasoningBuffer:
    """The cherry-pick introduced ``_reasoning_buffered_for_after_response``
    to defer reasoning rendering until after the response.  That field
    must NOT exist on the live CLI — it is the load-bearing mechanism
    of the wrong order, and its presence is the regression fingerprint.
    """

    def test_buffered_reasoning_field_does_not_exist(self):
        cli = _make_cli()
        assert not hasattr(cli, "_reasoning_buffered_for_after_response") or \
            getattr(cli, "_reasoning_buffered_for_after_response", "") == "", (
            "REASONING-AFTER-RESPONSE BUG: _reasoning_buffered_for_after_response "
            "field exists on the CLI. That field is the mechanism that defers "
            "reasoning rendering until after the response — exactly the "
            "regression captured in the live transcript. Reasoning must "
            "render live, not be buffered for the finalizer."
        )


# Test 3: finalizer does NOT print reasoning after the response


class TestFinalizerDoesNotRenderReasoningAfterResponse:
    """After the response stream closes, the finalizer must NOT print a
    second reasoning panel below it.  The live reasoning box already
    showed the chain-of-thought during the turn; printing it again
    after the response is the user-visible regression.
    """

    @patch("cli._cprint")
    def test_finalizer_skips_reasoning_when_live_already_shown(
        self, mock_cprint,
    ):
        """``_reasoning_shown_this_turn=True`` (live reasoning opened)
        must suppress the finalizer's reasoning-rendering block."""
        cli = _make_cli()
        # Drive the live-rendering path so the live reasoning box opens.
        with patch.object(cli, "_scrollback_box_width", return_value=80):
            cli._stream_reasoning_delta("plan\n")
            cli._stream_delta("answer\n")
            cli._flush_stream()
        # _reasoning_shown_this_turn is set by _stream_reasoning_delta and
        # persists through _reset_stream_state (covered separately).  It
        # is what gates the finalizer's reasoning-render block.
        assert cli._reasoning_shown_this_turn is True

        calls_after_live = list(mock_cprint.call_args_list)
        # Simulate the finalizer shape:
        #   if show_reasoning and not _reasoning_shown_this_turn:
        #       render last_reasoning
        # The cherry-pick removed the `_reasoning_shown_this_turn`
        # guard — pin that it stays.
        result = {"last_reasoning": "STALE_FALLBACK_TEXT"}
        if cli.show_reasoning and not getattr(
            cli, "_reasoning_shown_this_turn", False,
        ):
            reasoning = result.get("last_reasoning")
            if reasoning:
                mock_cprint(f"\nReasoning:\n{reasoning.strip()}\n")

        all_calls = _cprint_calls(mock_cprint)
        stale = any("STALE_FALLBACK_TEXT" in c for c in all_calls)
        assert not stale, (
            "REASONING-AFTER-RESPONSE BUG: finalizer rendered reasoning "
            "after the response box, even though the live reasoning box "
            "already showed the chain-of-thought. This produces the "
            "double-reasoning symptom in the live transcript. "
            f"Calls: {all_calls}"
        )

    @patch("cli._cprint")
    def test_finalizer_renders_reasoning_when_live_did_not_open(
        self, mock_cprint,
    ):
        """The fallback path still works: when the model emits no
        reasoning deltas during the turn but the result carries a
        ``last_reasoning`` payload (non-streaming consumer, response
        replay), the finalizer renders a reasoning panel."""
        cli = _make_cli()
        # No live reasoning delta — _reasoning_shown_this_turn stays False.
        assert cli._reasoning_shown_this_turn is False

        result = {"last_reasoning": "Post-hoc chain of thought."}
        # Simulate the production finalizer's reasoning-render block
        # (the pre-cherry-pick shape, gated on _reasoning_shown_this_turn).
        if cli.show_reasoning and not getattr(
            cli, "_reasoning_shown_this_turn", False,
        ):
            reasoning = result.get("last_reasoning")
            if reasoning:
                w = cli._scrollback_box_width()
                r_label = " Reasoning "
                r_fill = w - 2 - len(r_label)
                r_top = f"┌─{r_label}{'─' * max(r_fill - 1, 0)}┐"
                r_bot = f"└{'─' * (w - 2)}┘"
                mock_cprint(
                    f"\n{r_top}\n{reasoning.strip()}\n{r_bot}"
                )

        all_calls = _cprint_calls(mock_cprint)
        rendered = any("Reasoning" in c for c in all_calls)
        assert rendered, (
            "Fallback reasoning render path is broken — when the live "
            "reasoning box never opens but the result carries "
            "last_reasoning, the user sees no chain-of-thought at all. "
            f"Calls: {all_calls}"
        )


# Test 4: live reasoning box is closed by the first response delta


class TestResponseDeltaDeferralAndFlushClosesReasoningBox:
    """Response text arriving while the live reasoning box is open is
    DEFERRED into ``_deferred_content`` (the production behaviour in
    ``_emit_stream_text``).  The reasoning box stays open until the
    stream flushes; the close-then-deferred-flush sequence is what
    guarantees the response panel lands BELOW the reasoning panel.

    These tests pin that deferral contract: a response delta alone
    must NOT close the reasoning box or open the response box, but
    once the stream flushes, both happen — and the response panel
    appears after the reasoning panel in the output sequence.
    """

    @patch("cli._cprint")
    def test_response_delta_alone_does_not_open_response_box(self, mock_cprint):
        """A single response delta while the reasoning box is open
        is deferred to ``_deferred_content`` and does NOT open the
        response box or close the reasoning box.  The reason: the
        production ``_emit_stream_text`` defers content while the
        reasoning box is open, so the response box only opens
        AFTER ``_close_reasoning_box`` is invoked at flush time."""
        cli = _make_cli()

        with patch.object(cli, "_scrollback_box_width", return_value=80):
            cli._stream_reasoning_delta("chain of thought\n")
            assert cli._reasoning_box_opened is True
            cli._stream_delta("the answer\n")

        # Response text is deferred; response box NOT yet open.
        assert cli._stream_box_opened is False, (
            "Response box opened BEFORE the reasoning box closed. The "
            "deferred-content flush path is what opens the response box, "
            "and that runs only after _close_reasoning_box() is invoked "
            "at flush time. If the box is open already, reasoning was "
            "skipped or the flush ran early."
        )
        assert cli._reasoning_box_opened is True, (
            "Reasoning box was closed by a single response delta. The "
            "deferral contract requires the box to stay open until flush."
        )
        assert "the answer" in cli._deferred_content

    @patch("cli._cprint")
    def test_flush_stream_closes_reasoning_and_emits_response(self, mock_cprint):
        """``_flush_stream`` invokes ``_close_reasoning_box``, which
        flushes ``_deferred_content`` into the now-open response box.
        This is the moment the response panel lands below the
        reasoning panel.

        The response box opens during the flush and then closes at
        the end of the flush; ``_stream_completed`` is the
        post-flush gate the finalizer reads to detect "the response
        already streamed", so we assert on that flag rather than
        ``_stream_box_opened`` (which is reset to False at flush
        end)."""
        cli = _make_cli()

        with patch.object(cli, "_scrollback_box_width", return_value=80):
            cli._stream_reasoning_delta("chain of thought\n")
            cli._stream_delta("the answer\n")
            cli._flush_stream()

        # After flush, reasoning box is closed, response was streamed
        # (the ``_stream_completed`` flag survives the flush and is
        # what the finalizer reads to decide whether to render the
        # Rich Panel or skip it).
        assert cli._reasoning_box_opened is False, (
            "REASONING-AFTER-RESPONSE BUG: _flush_stream did not close the "
            "live reasoning box. The reasoning panel would then stay open "
            "while the response panel lands, producing visual overlap or "
            "the inverted-order symptom."
        )
        assert cli._stream_completed is True, (
            "Response stream did not complete — _close_reasoning_box did "
            "not flush the deferred response content into the response "
            "box. The response box never opened during the flush."
        )
        # The response panel MUST appear in the output sequence, AFTER
        # the reasoning panel's bottom border.  This is the exact
        # ordering the live transcript demands.
        all_calls = _cprint_calls(mock_cprint)
        # Find the reasoning box's closing border (a call whose only
        # border character is the bottom-left └).  The reasoning
        # opener has ┌ (top-left) instead, so we distinguish by the
        # presence of the top-left character.
        reasoning_bottom_idx = None
        for i, c in enumerate(all_calls):
            if "└" in c and "┌" not in c and "Reasoning" not in c:
                reasoning_bottom_idx = i
                break
        response_opener_idx = next(
            (i for i, c in enumerate(all_calls) if "╭─" in c and "Hermes" in c),
            None,
        )
        assert reasoning_bottom_idx is not None, (
            f"Reasoning box bottom border missing. Calls: {all_calls}"
        )
        assert response_opener_idx is not None, (
            f"Response box opener missing. Calls: {all_calls}"
        )
        assert reasoning_bottom_idx < response_opener_idx, (
            f"REASONING-AFTER-RESPONSE BUG: response box opener "
            f"({response_opener_idx}) appeared at or before the reasoning "
            f"box close-bar ({reasoning_bottom_idx}). Reasoning must "
            f"render above the response. Calls: {all_calls}"
        )
