"""Tests for CLI manual compression messaging.

Updated 2026-07-13: switched from raw ``print()`` + ``capsys`` to
``_cprint`` capture because the regression fix routes the user-facing
summary through ``_cprint(...)`` (to survive ``patch_stdout``'s
StdoutProxy under an active prompt_toolkit ``Application``). Uses a
shared ``build_test_shell`` factory instead of
``tests.cli.test_cli_init._make_cli`` because the latter's reload
under stubbed ``prompt_toolkit.*`` is fragile under
``scripts/run_tests.sh``'s per-file subprocess isolation.
"""

from unittest.mock import MagicMock, patch

import pytest

from tests.cli._helpers.manual_compress_shell import (
    build_test_shell,
    patch_cprint,
)


def _make_history():
    return [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
        {"role": "user", "content": "three"},
        {"role": "assistant", "content": "four"},
    ]


def test_manual_compress_reports_noop_without_success_banner(monkeypatch):
    history = _make_history()
    cli, _ = build_test_shell(agent_response=(list(history), ""))
    cli.conversation_history = history
    # Same-identity session_id — skip the post-compress sync branch.
    cli.agent.session_id = cli.session_id

    cprinted: list[str] = []
    patch_cprint(monkeypatch, cprinted)

    def _estimate(messages, **_kwargs):
        assert messages == list(history)
        return 100

    with patch("agent.model_metadata.estimate_request_tokens_rough",
               side_effect=_estimate):
        cli._manual_compress()

    rendered = "\n".join(cprinted)
    assert "No changes from compression" in rendered
    assert "✅ Compressed" not in rendered
    assert "Approx request size: ~100 tokens (unchanged)" in rendered


def test_manual_compress_explains_when_token_estimate_rises(monkeypatch):
    cli, _ = build_test_shell()
    history = _make_history()
    compressed = [
        history[0],
        {"role": "assistant",
         "content": "Dense summary that still counts as more tokens."},
        history[-1],
    ]
    cli.conversation_history = history
    cli.agent._compress_context.return_value = (compressed, "")
    cli.agent.session_id = cli.session_id  # no-op: no split

    cprinted: list[str] = []
    patch_cprint(monkeypatch, cprinted)

    def _estimate(messages, **_kwargs):
        if messages == history:
            return 100
        if messages == compressed:
            return 120
        raise AssertionError(f"unexpected transcript: {messages!r}")

    with patch("agent.model_metadata.estimate_request_tokens_rough",
               side_effect=_estimate):
        cli._manual_compress()

    rendered = "\n".join(cprinted)
    assert "✅ Compressed: 4 → 3 messages" in rendered
    assert "Approx request size: ~100 → ~120 tokens" in rendered
    assert "denser summaries" in rendered
