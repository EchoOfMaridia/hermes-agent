"""Regression test: /compress user-facing summary must be visible.

The user-visible feedback from ``/compress`` (headline, token_line, note)
must reach the user, even when the slash command runs while a
prompt_toolkit ``Application`` is active and ``patch_stdout`` is wrapping
sys.stdout.

``patch_stdout`` routes writes to sys.stdout through a ``StdoutProxy``
that swallows or garbles raw ``print()`` output into escape-stripped
dead text (see ``patch_stdout`` docs and the ``_cprint`` docstring at
``cli.py`` line ~2598, plus commit ``b94397fe7`` which routed
``/sessions`` and ``/history`` through ``_cprint`` to fix exactly this).

The bug being pinned: ``HermesCLI._manual_compress`` was printing the
"Compressing N messages..." preamble and the "✅ Compressed..." summary
lines via raw ``print(...)``. When a TUI session is attached (which is
the default), those lines disappeared from the user's screen — so the
user ran ``/compress`` and got no feedback that anything happened at
all, even though ``_compress_context`` ran successfully. The expected
fix: route every user-facing line in ``_manual_compress`` through
``_cprint(...)`` so prompt_toolkit's renderer paints it above the input
area instead of letting ``StdoutProxy`` swallow it.

This test pins that contract.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from tests.cli._helpers.manual_compress_shell import (
    build_test_shell,
    patch_cprint,
)


_REAL_SUMMARY_PAYLOAD = {
    "noop": False,
    "headline": "Compressed: 4 → 1 messages",
    "token_line": "Approx request size: ~1234 → ~56 tokens",
    "note": "Note: fewer messages can still raise this estimate when...",
}


@pytest.fixture
def _cli(monkeypatch):
    """Build a HermesCLI whose agent returns the canned summary payload
    so the assertions have a deterministic head/token/note to look for."""
    from cli import HermesCLI

    monkeypatch.setattr(
        "agent.manual_compression_feedback.summarize_manual_compression",
        lambda *args, **kwargs: _REAL_SUMMARY_PAYLOAD,
    )

    cli = HermesCLI.__new__(HermesCLI)
    cli.conversation_history = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
        {"role": "user", "content": "three"},
        {"role": "assistant", "content": "four"},
    ]
    # Same-identity session_id — skip the post-compress sync branch
    # by reusing the default agent from build_test_shell.
    from unittest.mock import MagicMock
    agent = MagicMock()
    agent.compression_enabled = True
    agent._cached_system_prompt = ""
    agent.tools = None
    agent.session_id = "new-session"
    agent._compress_context.return_value = (
        [{"role": "user", "content": "[summary]"}], "",
    )
    agent._flush_messages_to_session_db = lambda *a, **k: None
    cli.agent = agent
    cli.session_id = "new-session"
    cli._pending_title = "old title"
    # Bypass busy_command redraw noise.
    from contextlib import nullcontext
    cli._busy_command = lambda _message: nullcontext()
    return cli


def test_manual_compress_summary_uses_cprint_not_raw_print(_cli, monkeypatch):
    """The headline / token_line / note MUST be routed through ``_cprint``.

    Without this, ``patch_stdout`` swallows the lines and the user sees
    nothing — this is the exact regression "it's not showing the results
    of the compaction anymore".
    """
    capture: list[str] = []
    patch_cprint(monkeypatch, capture)

    with patch("agent.model_metadata.estimate_request_tokens_rough",
               return_value=1234):
        _cli._manual_compress("/compress")

    rendered = "\n".join(capture)
    assert _REAL_SUMMARY_PAYLOAD["headline"] in rendered, (
        "headline must reach the user via _cprint, not raw print(). "
        f"Got _cprint calls: {rendered!r}"
    )
    assert _REAL_SUMMARY_PAYLOAD["token_line"] in rendered
    assert _REAL_SUMMARY_PAYLOAD["note"] in rendered


def test_manual_compress_summary_not_emitted_via_raw_print(_cli, monkeypatch):
    """A successful /compress MUST NOT write the user-facing summary via
    bare ``print()`` — that path is swallowed by ``patch_stdout`` when a
    prompt_toolkit ``Application`` is running and is the underlying cause
    of the regression.
    """
    patch_cprint(monkeypatch, [])

    raw_prints: list = []

    def _capture_print(*args, **kwargs):
        raw_prints.append((args, kwargs))

    monkeypatch.setattr("builtins.print", _capture_print)

    with patch("agent.model_metadata.estimate_request_tokens_rough",
               return_value=1234):
        _cli._manual_compress("/compress")

    rendered_strings = [
        str(a[0]) if a and not isinstance(a[0], tuple)
        else " ".join(str(x) for x in a[0])
        for a, _ in raw_prints
    ]
    full_output = "\n".join(rendered_strings)

    bad = [
        label
        for label, payload in (
            ("headline", _REAL_SUMMARY_PAYLOAD["headline"]),
            ("token_line", _REAL_SUMMARY_PAYLOAD["token_line"]),
            ("note", _REAL_SUMMARY_PAYLOAD["note"]),
        )
        if payload in full_output
    ]
    assert not bad, (
        "These user-facing summary fields were emitted via raw print() "
        f"instead of _cprint, which causes the /compress regression "
        f"(patch_stdout swallows them): {bad}. "
        f"Full print() output was: {full_output!r}"
    )


def test_manual_compress_preview_uses_cprint(_cli, monkeypatch):
    """``/compress --preview`` returns BEFORE the LLM call (no token
    cost), so it's pure user feedback — failure to surface it makes
    the flag look broken to the user."""
    capture: list[str] = []
    patch_cprint(monkeypatch, capture)

    monkeypatch.setattr(
        "hermes_cli.partial_compress.summarize_compress_preview",
        lambda *args, **kwargs: {
            "lines": ["PREVIEW-LINE-WOULD-BE-RENDERED"],
        },
    )

    _cli._manual_compress("/compress --preview")
    rendered = "\n".join(capture)

    assert "PREVIEW-LINE-WOULD-BE-RENDERED" in rendered, (
        "preview path must surface its lines through _cprint so the "
        "patch_stdout regression doesn't hide them either. "
        f"Got _cprint calls: {rendered!r}"
    )
