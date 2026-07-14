"""Tests for /compress <focus> — guided compression with focus topic.

Inspired by Claude Code's /compact <focus> feature.

Updated 2026-07-13: switched from raw ``print()`` + ``capsys`` to
``_cprint`` capture (the production fix routes user-facing output
through ``_cprint(...)``; bare ``print()`` writes get swallowed by
``patch_stdout``'s StdoutProxy when an Application is running).
Uses a shared ``build_test_shell`` factory instead of
``tests.cli.test_cli_init._make_cli`` — see helper docstring for why.
"""

from unittest.mock import MagicMock, patch

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


def test_focus_topic_extracted_and_passed(monkeypatch):
    """Focus topic is extracted from the command and passed to _compress_context."""
    history = _make_history()
    compressed = [history[0], history[-1]]
    cli, _ = build_test_shell(agent_response=(compressed, ""))
    cli.conversation_history = history

    cprinted: list[str] = []
    patch_cprint(monkeypatch, cprinted)

    def _estimate(messages):
        # The agent under test returns ``compressed`` while the
        # ``messages`` it gets back is the original (list-wrapped)
        # history; we recognise each form and return appropriate tokens.
        if messages == compressed:
            return 50
        return 100

    with patch("agent.model_metadata.estimate_messages_tokens_rough",
               side_effect=_estimate):
        cli._manual_compress("/compress database schema")

    rendered = "\n".join(cprinted)
    assert 'focus: "database schema"' in rendered

    cli.agent._compress_context.assert_called_once()
    call_kwargs = cli.agent._compress_context.call_args
    assert call_kwargs.kwargs.get("focus_topic") == "database schema"


def test_no_focus_topic_when_bare_command(monkeypatch):
    """When no focus topic is provided, None is passed."""
    history = _make_history()
    cli, _ = build_test_shell(agent_response=(list(history), ""))
    cli.conversation_history = history

    with patch("agent.model_metadata.estimate_messages_tokens_rough",
               return_value=100):
        cli._manual_compress("/compress")

    cli.agent._compress_context.assert_called_once()
    call_kwargs = cli.agent._compress_context.call_args
    assert call_kwargs.kwargs.get("focus_topic") is None


def test_empty_focus_after_command_treated_as_none(monkeypatch):
    """Trailing whitespace after /compress does not produce a focus topic."""
    history = _make_history()
    cli, _ = build_test_shell(agent_response=(list(history), ""))
    cli.conversation_history = history

    with patch("agent.model_metadata.estimate_messages_tokens_rough",
               return_value=100):
        cli._manual_compress("/compress   ")

    cli.agent._compress_context.assert_called_once()
    call_kwargs = cli.agent._compress_context.call_args
    assert call_kwargs.kwargs.get("focus_topic") is None


def test_focus_topic_printed_in_compression_banner(monkeypatch):
    """The focus topic shows in the compression progress banner."""
    history = _make_history()
    compressed = [history[0], history[-1]]
    cli, _ = build_test_shell(agent_response=(compressed, ""))
    cli.conversation_history = history

    cprinted: list[str] = []
    patch_cprint(monkeypatch, cprinted)

    with patch("agent.model_metadata.estimate_messages_tokens_rough",
               return_value=100):
        cli._manual_compress("/compress API endpoints")

    rendered = "\n".join(cprinted)
    assert 'focus: "API endpoints"' in rendered


def test_no_focus_prints_standard_banner(monkeypatch):
    """Without focus, the standard banner (no focus: line) is printed."""
    history = _make_history()
    compressed = [history[0], history[-1]]
    cli, _ = build_test_shell(agent_response=(compressed, ""))
    cli.conversation_history = history

    cprinted: list[str] = []
    patch_cprint(monkeypatch, cprinted)

    with patch("agent.model_metadata.estimate_messages_tokens_rough",
               return_value=100):
        cli._manual_compress("/compress")

    rendered = "\n".join(cprinted)
    assert "focus:" not in rendered
    assert "Compressing" in rendered
