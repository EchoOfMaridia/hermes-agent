"""test_slash_feedback — RED/GREEN test for the /workflow slash command feedback.

Per the user's complaint: "the slash commands have no feedback at all
making them basically useless." This pins the contract that the slash
handler MUST return a chat-injectable string (never None) so the user
sees something when they type /workflow, /workflow help, or /workflow
create with an empty intent.

Manual runner (per hermes-workflow-author skill): runs with plain
``python3``, prints [OK]/[FAIL] per path.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


REPO_ROOT = Path("/home/cage/Desktop/Workspaces/HermesDesktop")
sys.path.insert(0, str(REPO_ROOT))

# Path-mock before importing the plugin.
import types
from unittest.mock import patch as _patch

from plugins.hermes_workflow import slash


def test_no_args_returns_help_text():
    """`/workflow` (no subcommand) returns the help text, not None."""
    runtime = types.SimpleNamespace(journal_root=None)
    result = slash._dispatch_workflow(runtime, "")
    assert result is not None, "slash returned None on empty intent"
    assert "workflow plugin commands" in result, f"unexpected: {result[:200]}"
    assert "/workflow run" in result and "/workflow list" in result
    print("[OK] no_args_returns_help_text")


def test_help_subcommand_returns_help():
    """`/workflow help` returns help text, not None."""
    runtime = types.SimpleNamespace(journal_root=None)
    result = slash._dispatch_workflow(runtime, "help")
    assert result is not None
    assert "workflow plugin commands" in result
    print("[OK] help_subcommand_returns_help")


def test_unknown_subcommand_returns_guidance():
    """`/workflow xyz` returns a 'unknown subcommand' message, not None."""
    runtime = types.SimpleNamespace(journal_root=None)
    result = slash._dispatch_workflow(runtime, "xyz")
    assert result is not None, "slash returned None on unknown subcommand"
    assert "unknown workflow subcommand" in result
    print("[OK] unknown_subcommand_returns_guidance")


def test_create_with_empty_intent_returns_usage_string():
    """`/workflow create` (no intent) returns a usage string with library
    entries, not None. The user wanted feedback — currently they get
    silence (return None → no chat injection)."""
    runtime = types.SimpleNamespace(journal_root=None)
    # No script_author → falls through to the manual-copy guidance path
    result = slash._dispatch_workflow(runtime, "create", script_author=None)
    # The guidance path returns an async generator; the chat dispatcher
    # consumes it. The FIRST yield of that generator is the feedback.
    if hasattr(result, "__aiter__"):
        import asyncio
        gen = result
        first = asyncio.run(_collect_first(gen))
        assert first is not None, "create-with-empty-intent returned empty stream"
        assert "create is a v0.2.0 feature" in first or "ScriptAuthor" in first, (
            f"unexpected: {first[:200]}"
        )
    else:
        # Plain string path (if the implementation changes later)
        assert result is not None
    print("[OK] create_with_empty_intent_returns_usage_string")


async def _collect_first(gen):
    async for chunk in gen:
        return chunk
    return None


def test_list_subcommand_returns_library_via_cli_capture():
    """`/workflow list` invokes the CLI's list subcommand and returns its
    output. Smoke test — confirms the dispatcher routes correctly."""
    runtime = types.SimpleNamespace(journal_root=None)
    # The CLI's `_cmd_list` reads from ~/.hermes/workflows/library.json.
    # Stub it to avoid hitting the real library.
    from plugins.hermes_workflow import cli as _cli
    with _patch.object(_cli, "_cmd_list", return_value=0) as _:
        # The CLI dispatcher prints to stdout; the slash handler
        # captures it. We just check the return is a non-None string.
        result = slash._dispatch_workflow(runtime, "list")
        # _run_cli_capture may return a sentinel "(exit 0)" or the captured
        # text. Either way it should not be None.
        assert result is not None, "list returned None"
    print(f"[OK] list_subcommand_returns_library_via_cli_capture — got: {result[:80]!r}")


if __name__ == "__main__":
    failed = 0
    for fn in [
        test_no_args_returns_help_text,
        test_help_subcommand_returns_help,
        test_unknown_subcommand_returns_guidance,
        test_create_with_empty_intent_returns_usage_string,
        test_list_subcommand_returns_library_via_cli_capture,
    ]:
        try:
            fn()
        except AssertionError as exc:
            failed += 1
            print(f"[FAIL] {fn.__name__}: {exc}")
        except Exception as exc:
            import traceback
            failed += 1
            print(f"[FAIL] {fn.__name__}: {type(exc).__name__}: {exc}")
            traceback.print_exc()
    if failed:
        print(f"\n[FAIL] {failed} slash feedback tests failed")
        sys.exit(1)
    print("\n[OK] all slash feedback tests passed")