"""Tests for /compress here [N] — boundary-aware partial compression.

Verifies the CLI handler (_manual_compress) splits the history, compresses
only the head, and re-appends the verbatim tail. Inspired by Claude Code's
Rewind "Summarize up to here" action (v2.1.139, May 2026).

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


def _make_history() -> list[dict[str, str]]:
    """8 messages = 4 exchanges (the minimal length for ``here 2``)."""
    h: list[dict[str, str]] = []
    for i in range(4):
        h.append({"role": "user", "content": f"u{i}"})
        h.append({"role": "assistant", "content": f"a{i}"})
    return h


def test_compress_here_compresses_head_only(monkeypatch):
    """/compress here 2 passes only the head to _compress_context."""
    history = _make_history()
    summary = [{"role": "user", "content": "[summary of earlier turns]"}]
    cli, _ = build_test_shell(agent_response=(summary, ""))
    cli.conversation_history = history

    with patch("agent.model_metadata.estimate_request_tokens_rough",
               return_value=100):
        cli._manual_compress("/compress here 2")

    cli.agent._compress_context.assert_called_once()
    call = cli.agent._compress_context.call_args
    # Head = everything before the last 2 user-starts = first 4 messages.
    assert call.args[0] == history[:4]
    # focus_topic must be None in partial mode (modes are exclusive).
    assert call.kwargs.get("focus_topic") is None


def test_compress_here_reappends_verbatim_tail(monkeypatch):
    """The most recent exchanges are preserved verbatim after the summary."""
    history = _make_history()
    summary = [{"role": "assistant", "content": "[summary]"}]
    cli, _ = build_test_shell(agent_response=(summary, ""))
    cli.conversation_history = history

    with patch("agent.model_metadata.estimate_request_tokens_rough",
               return_value=100):
        cli._manual_compress("/compress here 2")

    # Result = compressed head + verbatim tail (last 2 exchanges).
    assert cli.conversation_history == summary + history[4:]
    # Tail boundary keeps role alternation valid (tail starts on user).
    assert history[4]["role"] == "user"
    # No consecutive same-role user/assistant messages anywhere.
    roles = [m["role"] for m in cli.conversation_history
             if m["role"] in ("user", "assistant")]
    assert all(roles[i] != roles[i + 1] for i in range(len(roles) - 1))


def test_compress_here_banner_mentions_summarizing_up_to_here(monkeypatch):
    cli, _ = build_test_shell(
        agent_response=([{"role": "user", "content": "[summary]"}], ""),
    )
    cli.conversation_history = _make_history()

    cprinted: list[str] = []
    patch_cprint(monkeypatch, cprinted)

    with patch("agent.model_metadata.estimate_request_tokens_rough",
               return_value=100):
        cli._manual_compress("/compress here")

    rendered = "\n".join(cprinted)
    assert "Summarizing up to here" in rendered
    assert "verbatim" in rendered


def test_bare_compress_still_full(monkeypatch):
    """/compress with no args compresses the whole history (full mode)."""
    history = _make_history()
    cli, _ = build_test_shell(agent_response=(list(history), ""))
    cli.conversation_history = history

    with patch("agent.model_metadata.estimate_request_tokens_rough",
               return_value=100):
        cli._manual_compress("/compress")

    call = cli.agent._compress_context.call_args
    # Full mode passes the entire history as the head.
    assert call.args[0] == history


def test_focus_still_works(monkeypatch):
    """/compress <focus> keeps the existing focus behavior."""
    history = _make_history()
    cli, _ = build_test_shell(agent_response=(list(history), ""))
    cli.conversation_history = history

    with patch("agent.model_metadata.estimate_request_tokens_rough",
               return_value=100):
        cli._manual_compress("/compress database schema")

    call = cli.agent._compress_context.call_args
    assert call.args[0] == history
    assert call.kwargs.get("focus_topic") == "database schema"
