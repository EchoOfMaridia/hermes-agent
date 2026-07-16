"""Tests for CLI manual compression messaging.

Updated 2026-07-16 (post-merge): the merged-in test file used `capsys` to
capture `_cprint` output, but our `_cprint` routes through prompt_toolkit
(whose mock doesn't write to stdout). Reverted to the existing
``patch_cprint`` helper from ``tests.cli._helpers.manual_compress_shell``
which captures `_cprint` calls directly — same approach our pre-merge
test file used (and the same approach the file was on before the merge
brought in upstream's capsys-based version).
"""

from unittest.mock import MagicMock, patch

from tests.cli._helpers.manual_compress_shell import (
    build_test_shell,
    patch_cprint,
)


def _make_history() -> list[dict[str, str]]:
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


def test_manual_compress_reports_aborted_summary_without_success_banner(monkeypatch):
    history = _make_history()
    cli, agent = build_test_shell(agent_response=(list(history), ""))
    cli.conversation_history = history
    cli.agent.session_id = cli.session_id
    agent.context_compressor._last_compress_aborted = True
    agent.context_compressor._last_summary_fallback_used = False
    agent.context_compressor._last_summary_error = (
        "Provider 'opencode-zen' is set in config.yaml but no API key was found."
    )

    cprinted: list[str] = []
    patch_cprint(monkeypatch, cprinted)

    with patch("agent.model_metadata.estimate_request_tokens_rough", return_value=100):
        cli._manual_compress()

    rendered = "\n".join(cprinted)
    assert "⚠️ Compression aborted: 4 messages preserved" in rendered
    assert "no messages were removed" in rendered
    assert "no API key was found" in rendered
    assert "✅ Compressed:" not in rendered


def test_manual_compress_explains_when_token_estimate_rises(monkeypatch):
    history = _make_history()
    compressed = [
        history[0],
        {"role": "assistant", "content": "Dense summary that still counts as more tokens."},
        history[-1],
    ]
    cli, _ = build_test_shell(agent_response=(compressed, ""))
    cli.conversation_history = history
    cli.agent.session_id = cli.session_id

    cprinted: list[str] = []
    patch_cprint(monkeypatch, cprinted)

    def _estimate(messages, **_kwargs):
        if messages == history:
            return 100
        if messages == compressed:
            return 120
        raise AssertionError(f"unexpected transcript: {messages!r}")

    with patch("agent.model_metadata.estimate_request_tokens_rough", side_effect=_estimate):
        cli._manual_compress()

    rendered = "\n".join(cprinted)
    assert "✅ Compressed: 4 → 3 messages" in rendered
    assert "Approx request size: ~100 → ~120 tokens" in rendered
    assert "denser summaries" in rendered


def test_manual_compress_syncs_session_id_after_split():
    """Regression for cli.session_id desync after /compress.

    _compress_context ends the parent session and creates a new child session,
    mutating agent.session_id. Without syncing, cli.session_id still points
    at the ended parent — causing /status, /resume, exit summary, and the
    next end_session() call (e.g. from /resume <id>) to target the wrong row.
    """
    cli, agent = build_test_shell()
    history = _make_history()
    old_id = cli.session_id
    new_child_id = "20260101_000000_child1"

    compressed = [
        {"role": "user", "content": "[summary]"},
        history[-1],
    ]
    cli.conversation_history = history
    # Simulate _compress_context mutating agent.session_id as a side effect.
    def _fake_compress(*args, **kwargs):
        agent.session_id = new_child_id
        return (compressed, "")
    agent._compress_context.side_effect = _fake_compress
    agent.session_id = old_id  # starts in sync
    cli._pending_title = "stale title"

    with patch("agent.model_metadata.estimate_request_tokens_rough", return_value=100):
        cli._manual_compress()

    # CLI session_id must now point at the continuation child, not the parent.
    assert cli.session_id == new_child_id
    assert cli.session_id != old_id
    # Pending title must be cleared — titles belong to the parent lineage and
    # get regenerated for the continuation.
    assert cli._pending_title is None


def test_manual_compress_flushes_compressed_history_to_child_session_db():
    """Manual /compress must persist the handoff in the continuation DB.

    _compress_context rotates the agent to a new child session and returns a
    compressed transcript whose first messages include the handoff summary. The
    CLI then replaces its in-memory conversation_history with that transcript.
    Because the child DB starts empty, the flush must start from offset 0 rather
    than treating the compressed history as already persisted.
    """
    cli, agent = build_test_shell()
    history = _make_history()
    old_id = cli.session_id
    new_child_id = "20260101_000000_child1"
    compressed = [
        {"role": "user", "content": "[CONTEXT COMPACTION — REFERENCE ONLY] compacted"},
        history[-1],
    ]
    cli.conversation_history = history
    agent.session_id = old_id

    def _fake_compress(*args, **kwargs):
        agent.session_id = new_child_id
        return (compressed, "")

    agent._compress_context.side_effect = _fake_compress

    with patch("agent.model_metadata.estimate_messages_tokens_rough", return_value=100):
        cli._manual_compress()

    agent._flush_messages_to_session_db.assert_called_once_with(compressed, None)


def test_manual_compress_does_not_flush_full_history_when_session_id_unchanged():
    cli, agent = build_test_shell(agent_response=(_make_history(), ""))
    history = _make_history()
    cli.conversation_history = history
    agent.session_id = cli.session_id

    with patch("agent.model_metadata.estimate_messages_tokens_rough", return_value=100):
        cli._manual_compress()

    agent._flush_messages_to_session_db.assert_not_called()


def test_manual_compress_no_sync_when_session_id_unchanged():
    """If compression is a no-op (agent.session_id didn't change), the CLI
    must NOT clear _pending_title or otherwise disturb session state.
    """
    cli, _ = build_test_shell(agent_response=(_make_history(), ""))
    history = _make_history()
    cli.conversation_history = history
    cli.agent.session_id = cli.session_id
    cli._pending_title = "keep me"

    with patch("agent.model_metadata.estimate_request_tokens_rough", return_value=100):
        cli._manual_compress()

    # No split → pending title untouched.
    assert cli._pending_title == "keep me"