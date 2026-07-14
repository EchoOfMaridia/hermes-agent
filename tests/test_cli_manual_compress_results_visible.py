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

Implementation note (KISS): we construct ``HermesCLI`` via
``HermesCLI.__new__(HermesCLI)`` and inject the agent attributes the
test needs manually, instead of using ``tests.cli.test_cli_init``'s
``_make_cli()`` factory. ``_make_cli`` performs an ``importlib.reload``
under stubbed ``prompt_toolkit.*`` whose behaviour under per-file
subprocess isolation (``scripts/run_tests.sh``) is order-dependent on
whether ``cli`` resolves as a single-file module or a package — the
mocking it does to ``get_tool_definitions`` works on the single-file
form but raises ``AttributeError`` on the package form. Bypassing it
gives us a deterministic shell across pytest, the subprocess runner,
and any future test discovery. The actual fix and contract pin are
what matter here — not the fixture mechanics.
"""

from __future__ import annotations

import sys
from contextlib import nullcontext
from unittest.mock import patch

import pytest

from cli import HermesCLI


def _find_production_cli_module():
    """Find the production ``cli`` module regardless of import alias.

    Under ``scripts/run_tests.sh``'s per-file subprocess isolation
    ``cli`` may load as either a single-file module (``.../cli.py``)
    or a package (``.../cli/__init__.py``) depending on import order.
    The ``__name__ == "cli"`` filter rejects sibling modules (the
    test-side ``tests.cli`` package, ``hermes_cli.*``, and
    ``prompt_toolkit.filters.cli``).
    """
    for mod in sys.modules.values():
        f = getattr(mod, "__file__", None)
        if not f:
            continue
        if getattr(mod, "__name__", "") != "cli":
            continue
        if f.endswith("/cli.py") or f.endswith("/cli/__init__.py"):
            return mod
    raise AssertionError("could not locate the production cli module")


def _patch_cprint_on_prod_module(monkeypatch, capture: list[str]):
    """Patch ``_cprint`` on the live production ``cli`` module."""
    target = _find_production_cli_module()
    monkeypatch.setattr(target, "_cprint",
                        lambda text: capture.append(text))
    return target


class _DummyAgent:
    """Stand-in agent whose ``_compress_context`` returns a smaller result
    than the input so manual_compression_feedback reports a real change
    (not a noop)."""

    def __init__(self):
        self.compression_enabled = True
        self._cached_system_prompt = "FULL CACHED SYSTEM PROMPT SHOULD NOT BE NESTED"
        self.session_id = "new-session"
        self.calls = []
        self.flushed_calls = []

    def _compress_context(
        self,
        messages,
        system_message,
        *,
        approx_tokens=None,
        focus_topic=None,
        force=False,
    ):
        self.calls.append(
            {
                "messages": messages,
                "system_message": system_message,
                "approx_tokens": approx_tokens,
                "focus_topic": focus_topic,
                "force": force,
            }
        )
        return [{"role": "user", "content": "[CONTEXT SUMMARY]: compacted"}], "sp"

    def _flush_messages_to_session_db(self, messages, role):
        self.flushed_calls.append((list(messages), role))
        return None


_REAL_SUMMARY_PAYLOAD = {
    "noop": False,
    "headline": "Compressed: 4 → 1 messages",
    "token_line": "Approx request size: ~1234 → ~56 tokens",
    "note": "Note: fewer messages can still raise this estimate when...",
}


@pytest.fixture
def _cli(monkeypatch):
    """Build a HermesCLI ready to run _manual_compress, with mocks isolated."""
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
    cli.agent = _DummyAgent()
    cli.session_id = "old-session"
    cli._pending_title = "old title"
    cli._busy_command = lambda _message: nullcontext()
    return cli


def test_manual_compress_summary_uses_cprint_not_raw_print(_cli, monkeypatch):
    """The headline / token_line / note MUST be routed through ``_cprint``.

    Without this, ``patch_stdout`` swallows the lines and the user sees
    nothing — this is the exact regression "it's not showing the results
    of the compaction anymore".

    The bare-``print()`` path is allowed only for the
    ``Compression failed:`` exception path; ``_cprint`` is the contract
    for success-path user-facing summaries.
    """
    capture: list[str] = []
    _patch_cprint_on_prod_module(monkeypatch, capture)
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
    _patch_cprint_on_prod_module(monkeypatch, [])

    raw_prints: list = []

    def _capture_print(*args, **kwargs):
        raw_prints.append((args, kwargs))

    monkeypatch.setattr("builtins.print", _capture_print)
    _cli._manual_compress("/compress")

    rendered_strings = [
        str(a[0]) if a and not isinstance(a[0], tuple) else " ".join(str(x) for x in a[0])
        for a, _ in raw_prints
    ]
    full_output = "\n".join(rendered_strings)

    bad_targets = [
        label
        for label, payload in (
            ("headline", _REAL_SUMMARY_PAYLOAD["headline"]),
            ("token_line", _REAL_SUMMARY_PAYLOAD["token_line"]),
            ("note", _REAL_SUMMARY_PAYLOAD["note"]),
        )
        if payload in full_output
    ]
    assert not bad_targets, (
        "These user-facing summary fields were emitted via raw print() "
        f"instead of _cprint, which causes the /compress regression "
        f"(patch_stdout swallows them): {bad_targets}. "
        f"Full print() output was: {full_output!r}"
    )


def test_manual_compress_preview_uses_cprint(_cli, monkeypatch):
    """``/compress --preview`` and ``/compress here N`` already-success
    paths route their user-facing lines through print() — same bug class
    as the success summary, so pinned here together.
    """
    capture: list[str] = []
    _patch_cprint_on_prod_module(monkeypatch, capture)

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
