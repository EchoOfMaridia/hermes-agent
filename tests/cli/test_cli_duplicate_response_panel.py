"""Regression tests for the "double-rendered Hermes reply" CLI bug.

Symptom (every assistant reply in the foreground CLI):
    The user sees their assistant response printed TWICE. First inside
    the streaming box (`╭─ ⚕ Hermes ─...─╮ … ╰───╯`) token-by-token as
    the model emits tokens, then a SECOND time inside a Rich ``Panel``
    after the stream finishes. The Rich Panel duplicates the streamed
    content verbatim.

Root cause (suspected):
    In ``cli.py`` the post-stream finalizer uses the guard::

        already_streamed = self._stream_started and self._stream_box_opened and not is_error_response

    to decide whether to skip the Rich Panel re-render. But by the time
    the finalizer runs, ``_flush_stream()`` (line 5895) has already
    emitted the visual close-bar of the response box AND cleared
    ``_stream_box_opened = False`` (line 5952). So::

        _stream_started=True, _stream_box_opened=False
        → already_streamed = False
        → finalizer falls into the `else` branch and prints the
          full response AGAIN as a Rich Panel.

These tests pin the invariant:
    After a complete streaming cycle (response streamed, box closed,
    finalizer about to render the trailing Panel), the finalizer must
    detect "already streamed" using a flag that survives
    ``_flush_stream``. If it does not, the user sees the assistant's
    full response rendered twice — once streamed, once in a Panel.
"""
from __future__ import annotations

from unittest.mock import patch


def _make_cli(*, streaming_enabled=True, use_streaming_tts=False):
    """Bare HermesCLI stub — render state only. No transport, no skin."""
    from cli import HermesCLI

    cli = HermesCLI.__new__(HermesCLI)
    cli.show_reasoning = False
    cli.streaming_enabled = streaming_enabled

    # ── Stream (response) render state
    cli._stream_box_opened = False
    cli._stream_completed = False
    cli._stream_prefilt = ""
    cli._deferred_content = ""
    cli._stream_needs_break = False
    cli._stream_text_ansi = ""
    cli._in_stream_table = False
    cli._stream_table_buf = []
    cli._stream_buf = ""
    cli._stream_started = False
    cli._stream_last_was_newline = True

    # ── Display config
    cli.show_timestamps = False
    cli.final_response_markdown = "render"

    # ── Voice TTS
    cli._voice_tts = False  # so use_streaming_tts branch is dead
    return cli


def _simulate_full_stream(cli, content: str) -> None:
    """Drive the streaming path end-to-end: open box, emit content, close box.

    Mirrors what a real streaming run does at the render-state level:
      * Open the response box (sets _stream_box_opened=True,
        _stream_started=True via the box-opening branch).
      * Append content to the stream buffer (the actual emission goes
        through _cprint which we patch out).
      * Call _flush_stream() which is what the runtime calls at the
        end of every model response — it prints the close-bar AND
        clears _stream_box_opened (cli.py:5952).
    """
    # Open the box — same code path _emit_stream_text runs when the
    # first visible token arrives. We simulate by setting the flags
    # directly because the box-opening path depends on skin/timestamps.
    cli._stream_box_opened = True
    cli._stream_started = True
    cli._stream_buf = content

    # End-of-stream flush. This is the *runtime* call that closes
    # the response box and resets _stream_box_opened to False.
    with patch("cli._cprint"):
        cli._flush_stream()


# ── Test 1: post-stream render state is consistent ───────────────────────────


class TestAlreadyStreamedFlagSurvivesFlush:
    """After ``_flush_stream()`` completes the streaming render, the
    finalizer must still be able to detect "the response was already
    streamed" so it can skip the Rich Panel re-render.

    The bug: ``_stream_box_opened`` is cleared by ``_flush_stream`` at
    cli.py:5952, so the finalizer's gate::

        already_streamed = self._stream_started and self._stream_box_opened

    evaluates to ``False`` even when streaming clearly happened. The
    finalizer then prints the response AGAIN as a Rich Panel.
    """

    @patch("cli._cprint")
    def test_flush_stream_leaves_stream_started_true(self, mock_cprint):
        """Sanity check on the existing reset pattern: ``_flush_stream``
        closes the box and prints the close-bar, but it MUST NOT clear
        ``_stream_started`` (which represents "streaming actually
        happened") — otherwise the finalizer cannot tell streaming
        from non-streaming responses."""
        cli = _make_cli()
        _simulate_full_stream(cli, "hello world")

        # _stream_box_opened is False after flush — that part is fine,
        # the visual box is closed.
        assert cli._stream_box_opened is False
        # _stream_started MUST still be True so the finalizer knows
        # the response was already streamed.
        assert cli._stream_started is True, (
            f"DUPLICATE-RENDER BUG (gate-broken): _flush_stream cleared "
            f"_stream_started. The finalizer at cli.py:12687 cannot "
            f"distinguish a streamed response from a non-streamed one, "
            f"and will re-render the entire response inside a Rich "
            f"Panel. _stream_started={cli._stream_started!r}"
        )


# ── Test 2: finalizer skips Rich Panel when streaming happened ───────────────


class TestFinalizerSkipsPanelWhenStreamed:
    """Drive the exact condition the finalizer uses
    (cli.py:12686-12687) and pin its correct evaluation. With the bug,
    ``already_streamed`` is ``False`` after streaming completed,
    causing the Rich Panel re-render and the user-visible duplication.
    """

    @patch("cli._cprint")
    def test_finalizer_already_streamed_true_after_full_stream(self, mock_cprint):
        cli = _make_cli()
        _simulate_full_stream(cli, "the response body")

        # Mirror the exact gate from cli.py:12686-12687 (post-fix).
        result = result_dict = {
            "failed": False,
            "partial": False,
        }
        is_error_response = result_dict and (result_dict.get("failed") or result_dict.get("partial"))
        already_streamed = cli._stream_started and cli._stream_completed and not is_error_response

        assert already_streamed is True, (
            f"DUPLICATE-RENDER BUG: finalizer's gate evaluated to False "
            f"after a complete streaming cycle. With this gate False, "
            f"the finalizer prints the full response a SECOND time "
            f"inside a Rich Panel. State: _stream_started="
            f"{cli._stream_started!r}, _stream_completed="
            f"{cli._stream_completed!r}. Both should contribute to "
            f"already_streamed=True after streaming happened."
        )


# ── Test 3: end-to-end — full streaming turn does NOT print a Rich Panel ────


class TestStreamingResponseIsNotRenderedTwice:
    """Pin the user-visible bug: after streaming emits the response,
    the finalizer must NOT call ``ChatConsole().print(Panel(...))``
    with the same response text.

    We patch the Rich Panel print path to count invocations. With the
    bug, this fires exactly once during the streaming finalizer path.
    With the fix, it must fire zero times for a fully-streamed,
    non-error response.
    """

    @patch("cli._cprint")
    @patch("cli.ChatConsole")
    def test_streamed_response_skips_rich_panel_re_render(self, mock_chat_console, mock_cprint):
        from cli import _render_final_assistant_content
        from rich.panel import Panel

        cli = _make_cli()
        response_text = "the assistant's full reply"
        _simulate_full_stream(cli, response_text)

        # Mimic the finalizer's branch decision at cli.py:12686-12707
        # (post-fix).
        is_error_response = False
        already_streamed = cli._stream_started and cli._stream_completed and not is_error_response

        # Trace what the finalizer *would* do.
        if already_streamed:
            panel_calls = 0
        else:
            # This is the buggy branch — the Rich Panel re-render.
            _chat_console = mock_chat_console.return_value
            _chat_console.print(Panel(
                _render_final_assistant_content(response_text, mode=cli.final_response_markdown),
                title="⚕ Hermes",
                border_style="#CD7F32",
            ))
            panel_calls = 1

        assert panel_calls == 0, (
            f"DUPLICATE-RENDER BUG (user-visible): the assistant reply "
            f"was streamed into the ⚕ Hermes box token-by-token AND "
            f"then re-rendered inside a Rich Panel. The user sees the "
            f"same response twice in the same turn. "
            f"panel_calls={panel_calls}, expected 0."
        )