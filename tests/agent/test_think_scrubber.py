"""Tests for StreamingThinkScrubber.

These tests lock in the contract the scrubber must satisfy so downstream
consumers (ACP, api_server, TTS, CLI, gateway) never see reasoning
blocks leaking through the stream_delta_callback.  The scenarios map
directly to the MiniMax-M2.7 / DeepSeek / Qwen3 streaming patterns that
break the older per-delta regex strip.
"""

from __future__ import annotations

import pytest

from agent.think_scrubber import StreamingThinkScrubber


def _drive(scrubber: StreamingThinkScrubber, deltas: list[str]) -> str:
    """Feed a sequence of deltas and return the concatenated visible output."""
    out = [scrubber.feed(d) for d in deltas]
    out.append(scrubber.flush())
    return "".join(out)


class TestClosedPairs:
    """Closed <tag>...</tag> pairs are always stripped, regardless of boundary."""

    def test_closed_pair_single_delta(self) -> None:
        s = StreamingThinkScrubber()
        assert _drive(s, ["<think>reasoning</think>Hello world"]) == "Hello world"

    def test_closed_pair_surrounded_by_content(self) -> None:
        s = StreamingThinkScrubber()
        assert _drive(s, ["Hello <think>note</think> world"]) == "Hello  world"

    @pytest.mark.parametrize(
        "tag",
        ["think", "thinking", "reasoning", "thought", "REASONING_SCRATCHPAD"],
    )
    def test_all_tag_variants(self, tag: str) -> None:
        s = StreamingThinkScrubber()
        delta = f"<{tag}>x</{tag}>Hello"
        assert _drive(s, [delta]) == "Hello"

    def test_case_insensitive_pair(self) -> None:
        s = StreamingThinkScrubber()
        assert _drive(s, ["<THINK>x</Think>Hello"]) == "Hello"


class TestUnterminatedOpen:
    """Unterminated open tag discards all subsequent content to end of stream."""

    def test_open_at_stream_start(self) -> None:
        s = StreamingThinkScrubber()
        assert _drive(s, ["<think>reasoning text with no close"]) == ""

    def test_open_after_newline(self) -> None:
        s = StreamingThinkScrubber()
        # 'Hello\n' is a block boundary for the <think> that follows
        assert _drive(s, ["Hello\n<think>reasoning"]) == "Hello\n"

    def test_open_after_newline_then_whitespace(self) -> None:
        s = StreamingThinkScrubber()
        assert _drive(s, ["Hello\n  <think>reasoning"]) == "Hello\n  "

    def test_prose_mentioning_tag_not_stripped(self) -> None:
        """Mid-line '<think>' in prose is preserved (no boundary)."""
        s = StreamingThinkScrubber()
        text = "Use the <think> element for reasoning"
        assert _drive(s, [text]) == text


class TestOrphanClose:
    """Orphan close tags (no prior open) are stripped without boundary check."""

    def test_orphan_close_alone(self) -> None:
        s = StreamingThinkScrubber()
        assert _drive(s, ["Hello</think>world"]) == "Helloworld"

    def test_orphan_close_with_trailing_space_consumed(self) -> None:
        """Matches _strip_think_blocks case 3 \\s* behaviour."""
        s = StreamingThinkScrubber()
        assert _drive(s, ["Hello</think> world"]) == "Helloworld"

    def test_multiple_orphan_closes(self) -> None:
        s = StreamingThinkScrubber()
        assert _drive(s, ["A</think>B</thinking>C"]) == "ABC"


class TestPartialTagsAcrossDeltas:
    """Partial tags at delta boundaries must be held back, not emitted raw."""

    def test_split_open_tag_held_back(self) -> None:
        """'<' arrives alone, 'think>' completes it on next delta."""
        s = StreamingThinkScrubber()
        # At stream start, last_emitted_ended_newline=True, so <think> at 0 is boundary
        assert (
            _drive(s, ["<", "think>reasoning</think>done"])
            == "done"
        )

    def test_split_open_tag_not_at_boundary(self) -> None:
        """Mid-line split '<' + 'think>X</think>' is a closed pair.

        Closed pairs are always stripped (matching
        ``_strip_think_blocks`` case 1), even without a block
        boundary — a closed pair is an intentional bounded construct.
        """
        s = StreamingThinkScrubber()
        out = _drive(s, ["word<", "think>prose</think>more"])
        assert out == "wordmore"

    def test_split_close_tag_held_back(self) -> None:
        """Close tag split across deltas still closes the block."""
        s = StreamingThinkScrubber()
        assert (
            _drive(s, ["<think>reasoning<", "/think>after"])
            == "after"
        )

    def test_split_close_tag_deep(self) -> None:
        """Close tag can be split anywhere."""
        s = StreamingThinkScrubber()
        assert (
            _drive(s, ["<think>reasoning</th", "ink>after"])
            == "after"
        )


class TestTheMiniMaxScenario:
    """The exact pattern run_agent per-delta regex strip breaks."""

    def test_minimax_split_open(self) -> None:
        """delta1='<think>', delta2='Let me check', delta3='</think>done'."""
        s = StreamingThinkScrubber()
        out = _drive(s, ["<think>", "Let me check their config", "</think>", "done"])
        assert out == "done"

    def test_minimax_split_open_with_trailing_content(self) -> None:
        """Reasoning then closes and hands off to final content."""
        s = StreamingThinkScrubber()
        out = _drive(
            s,
            [
                "<think>",
                "The user wants to know if thinking is on",
                "</think>",
                "\n\nshow_reasoning: false — thinking is OFF.",
            ],
        )
        assert out == "\n\nshow_reasoning: false — thinking is OFF."

    def test_minimax_unterminated_reasoning_at_end(self) -> None:
        """Unclosed reasoning at stream end is dropped entirely."""
        s = StreamingThinkScrubber()
        out = _drive(s, ["<think>", "The user wants", " to know something"])
        assert out == ""


class TestResetAndReentry:
    def test_reset_clears_in_block_state(self) -> None:
        s = StreamingThinkScrubber()
        s.feed("<think>hanging")
        assert s._in_block is True
        s.reset()
        assert s._in_block is False
        # After reset, a new turn works cleanly
        assert _drive(s, ["Hello world"]) == "Hello world"

    def test_reset_clears_buffered_partial_tag(self) -> None:
        s = StreamingThinkScrubber()
        s.feed("word<")
        assert s._buf == "<"
        s.reset()
        assert s._buf == ""
        assert _drive(s, ["fresh content"]) == "fresh content"


class TestFlushBehaviour:
    def test_flush_drops_unterminated_block(self) -> None:
        s = StreamingThinkScrubber()
        assert s.feed("<think>reasoning with no close") == ""
        assert s.flush() == ""

    def test_flush_emits_innocent_partial_tag_tail(self) -> None:
        """If held-back tail turned out not to be a real tag, emit it."""
        s = StreamingThinkScrubber()
        s.feed("word<")  # '<' could be a tag prefix
        # Stream ends with only '<' held back — emit it as prose.
        assert s.flush() == "<"

    def test_flush_on_empty_scrubber(self) -> None:
        s = StreamingThinkScrubber()
        assert s.flush() == ""


class TestRealisticStreaming:
    """Character-by-character streaming must work as well as larger chunks."""

    def test_char_by_char_closed_pair(self) -> None:
        s = StreamingThinkScrubber()
        deltas = list("<think>x</think>Hello world")
        assert _drive(s, deltas) == "Hello world"

    def test_char_by_char_orphan_close(self) -> None:
        s = StreamingThinkScrubber()
        deltas = list("Hello</think>world")
        assert _drive(s, deltas) == "Helloworld"

    def test_reasoning_then_real_response_first_word_preserved(self) -> None:
        """Regression: the first word of the final response must NOT be eaten.

        Stefan's screenshot bug — 'Let me check' was being rendered as
        ' me check'.  The scrubber must not consume any character of
        post-close content.
        """
        s = StreamingThinkScrubber()
        deltas = [
            "<think>",
            "User wants to know things",
            "</think>",
            "Let me check their config.",
        ]
        assert _drive(s, deltas) == "Let me check their config."

    def test_no_tag_passthrough_is_identical(self) -> None:
        """Streams without any reasoning tags pass through byte-for-byte."""
        s = StreamingThinkScrubber()
        deltas = ["Hello ", "world ", "how ", "are ", "you?"]
        assert _drive(s, deltas) == "Hello world how are you?"


class TestReasoningExtractionCallback:
    """Reasoning extracted from closed ``<tag>…</tag>`` blocks must reach the callback.

    Without this callback the scrubber silently discards ``<think>…</think>``
    inner content — which is fine for non-reasoning providers, but for
    providers that emit reasoning INLINE in the streamed ``content`` field
    (MiniMax-M3 on api.minimax.io/v1 with ``thinking: {type: adaptive}``)
    the chain-of-thought disappears before the reasoning channel ever
    fires.  These tests lock in the callback contract so the
    desktop/CLI/TUI reasoning surface keeps working.
    """

    def test_closed_pair_fires_callback_with_inner_content(self) -> None:
        extracted: list[str] = []
        s = StreamingThinkScrubber(on_reasoning_extracted=extracted.append)
        s.feed("<think>let me think</think>Hello")
        assert extracted == ["let me think"]
        assert s.flush() == ""  # flush() of finished block emits nothing

    def test_all_tag_variants_extract_inner(self) -> None:
        for tag in ("think", "thinking", "reasoning", "thought", "REASONING_SCRATCHPAD"):
            extracted: list[str] = []
            s = StreamingThinkScrubber(on_reasoning_extracted=extracted.append)
            visible = s.feed(f"<{tag}>chain of thought</{tag}>visible text")
            assert extracted == ["chain of thought"], tag
            assert visible == "visible text", tag
            assert s.flush() == "", tag

    def test_multiple_blocks_each_fire_callback(self) -> None:
        extracted: list[str] = []
        s = StreamingThinkScrubber(on_reasoning_extracted=extracted.append)
        visible = s.feed("<think>first</think> middle <think>second</think> end")
        assert extracted == ["first", "second"]
        assert visible == " middle  end"
        assert s.flush() == ""

    def test_mid_prose_closed_pair_extracts(self) -> None:
        """Closed pairs anywhere in the buffer extract, no boundary check."""
        extracted: list[str] = []
        s = StreamingThinkScrubber(on_reasoning_extracted=extracted.append)
        visible = s.feed("Hello<think>reasoning here</think> world")
        assert extracted == ["reasoning here"]
        assert visible == "Hello world"

    def test_case_insensitive_closed_pair_extracts(self) -> None:
        extracted: list[str] = []
        s = StreamingThinkScrubber(on_reasoning_extracted=extracted.append)
        visible = s.feed("<THINK>mixed case</Think>hello")
        assert extracted == ["mixed case"]
        assert visible == "hello"

    def test_split_across_deltas_closed_pair_extracts_complete(self) -> None:
        """Char-by-char streaming of a closed pair must deliver the FULL inner text."""
        extracted: list[str] = []
        s = StreamingThinkScrubber(on_reasoning_extracted=extracted.append)
        s.feed("<")
        s.feed("thin")
        s.feed("k>the reasoning</thin")
        s.feed("k>visible")
        assert extracted == ["the reasoning"]
        assert s.flush() == ""  # no held-back tail once block resolves

    def test_unterminated_block_does_not_fire_callback(self) -> None:
        """Open tag without a matching close tag is dropped, NOT extracted.

        Unterminated reasoning is partial and unreliable; leaking it
        through the reasoning channel is worse than losing it.
        """
        extracted: list[str] = []
        s = StreamingThinkScrubber(on_reasoning_extracted=extracted.append)
        s.feed("<think>partial reasoning without close")
        assert extracted == []
        assert s.flush() == ""

    def test_no_callback_registered_does_not_break_visible_text(self) -> None:
        """Backward-compat: scrubber constructed without callback still works."""
        s = StreamingThinkScrubber()
        assert _drive(s, ["<think>reasoning</think>hello"]) == "hello"

    def test_callback_exception_does_not_break_visible_stream(self) -> None:
        """A misbehaving callback must not corrupt the state machine."""
        def bad_callback(_text: str) -> None:
            raise RuntimeError("consumer bug")

        s = StreamingThinkScrubber(on_reasoning_extracted=bad_callback)
        # Visible text must still come through cleanly.
        assert _drive(s, ["<think>hello</think>world"]) == "world"

    def test_set_reasoning_extracted_callback_installs_late(self) -> None:
        """The callback can be installed after construction via setter."""
        extracted: list[str] = []
        s = StreamingThinkScrubber()
        # No callback yet — first feed swallows the reasoning but the
        # visible "A" still surfaces.
        first_visible = s.feed("<think>first</think>A")
        assert extracted == []
        assert first_visible == "A"
        # Install callback mid-stream — second feed routes extraction.
        s.set_reasoning_extracted_callback(extracted.append)
        second_visible = s.feed("<think>second</think>B")
        assert extracted == ["second"]
        assert second_visible == "B"
        # Neither feed leaves a held-back partial-tag tail, so flush is empty.
        assert s.flush() == ""

    def test_reset_preserves_callback(self) -> None:
        """reset() clears per-turn state but keeps the callback wired."""
        extracted: list[str] = []
        s = StreamingThinkScrubber(on_reasoning_extracted=extracted.append)
        s.feed("<think>turn one</think>hi")
        s.reset()
        s.feed("<think>turn two</think>bye")
        assert extracted == ["turn one", "turn two"]

    def test_minimax_three_chunk_split_extraction(self) -> None:
        """Reproduces the MiniMax-M3 wire shape: open tag, body, close tag as separate deltas.

        The MiniMax API returns reasoning in three separate SSE chunks:
        ``"<think>"`` then ``"reasoning body"`` then ``"</think>\\n\\n4"``.
        The reasoning callback must fire once with the full body, and the
        trailing ``\\n\\n4`` text must surface as visible stream output.
        """
        extracted: list[str] = []
        s = StreamingThinkScrubber(on_reasoning_extracted=extracted.append)
        s.feed("<think>")
        assert extracted == []
        s.feed("The user is asking a simple math question.")
        assert extracted == []
        # The third chunk contains the close tag AT THE START — the
        # post-close visible text surfaces from THIS feed() call, not
        # from flush().
        third_visible = s.feed("</think>\n\n4")
        assert extracted == ["The user is asking a simple math question."]
        assert third_visible == "\n\n4"
        assert s.flush() == ""


class TestDrainExtractedReasoning:
    """``drain_extracted_reasoning`` returns AND clears the per-feed list.

    The streaming accumulator (``chat_completion_helpers.py``) calls this
    after every ``feed()`` so it can re-merge the extracted reasoning into
    ``reasoning_parts`` for the post-turn ``msg["reasoning_content"]``
    field.  Without the drain the accumulator would either miss the
    extraction (callback-only path doesn't return the text) or double-merge
    (if feed() also returned the extraction).
    """

    def test_drain_returns_extracted_text(self) -> None:
        s = StreamingThinkScrubber()
        s.feed("<think>reasoning here</think>visible")
        drained = s.drain_extracted_reasoning()
        assert drained == ["reasoning here"]

    def test_drain_clears_after_return(self) -> None:
        s = StreamingThinkScrubber()
        s.feed("<think>reasoning here</think>visible")
        s.drain_extracted_reasoning()
        # Second drain returns empty.
        assert s.drain_extracted_reasoning() == []

    def test_drain_resets_per_feed(self) -> None:
        """A second feed() resets the per-feed list so the caller only sees the latest extraction."""
        s = StreamingThinkScrubber()
        s.feed("<think>first</think>A")
        s.drain_extracted_reasoning()  # consume "first"
        # No tags in second feed — drain returns empty.
        s.feed("B")
        assert s.drain_extracted_reasoning() == []

    def test_drain_returns_multiple_chunks(self) -> None:
        """Multiple closed pairs in one feed produce multiple chunks."""
        s = StreamingThinkScrubber()
        s.feed("<think>a</think> middle <think>b</think> end")
        assert s.drain_extracted_reasoning() == ["a", "b"]

    def test_drain_empty_when_no_extraction(self) -> None:
        s = StreamingThinkScrubber()
        s.feed("no reasoning tags here")
        assert s.drain_extracted_reasoning() == []

    def test_drain_works_without_callback(self) -> None:
        """Drain returns the extracted text regardless of whether a callback is installed.

        The callback path is for live consumers (desktop/CLI/TUI
        ``reasoning.delta`` events).  The drain path is for the
        post-turn ``reasoning_content`` assembly.  Both must work
        independently.
        """
        s = StreamingThinkScrubber()  # no callback
        s.feed("<think>reasoning here</think>visible")
        assert s.drain_extracted_reasoning() == ["reasoning here"]
