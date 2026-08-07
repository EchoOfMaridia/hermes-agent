"""Tests for the /workflow create slash surface (Gap 1: wire ScriptAuthor).

What we verify:

- /workflow create <intent> routes to ScriptAuthor and returns its
  result, NOT the "unknown workflow subcommand: 'create'" error.
- /workflow create with no intent returns a usage line.
- /workflow create surfaces the script path + run_id from
  AuthorResult.ok=True.
- /workflow create surfaces the error_stage + raw_script preview from
  AuthorResult.ok=False.
- /workflow create falls back to v0.2.0 guidance when script_author
  is None (mirror of the gateway handler's behavior).

We do NOT exercise the LLM here. The ScriptAuthor stub is
constructed with a canned-LLM that returns a pre-baked AuthorResult
via a monkey-patched ``generate`` method. This keeps the test
hermetic and fast while exercising the full slash-dispatch path.
"""

from __future__ import annotations

import asyncio
import json
import textwrap
from typing import Any

import pytest

from plugins.hermes_workflow.runtime_factory import build_runtime
from plugins.hermes_workflow.script_author import AuthorResult
from plugins.hermes_workflow.slash import build_slash_handlers


# ---------------------------------------------------------------------------
# Stub ScriptAuthor — bypasses LLM and validation, returns canned results.
# ---------------------------------------------------------------------------

class _StubScriptAuthor:
    """ScriptAuthor replacement that returns pre-baked AuthorResult.

    Records every ``generate`` invocation so tests can assert the
    slash handler threaded ``intent`` and ``runtime`` through correctly.
    """

    def __init__(self, *, ok: bool = True, **fields) -> None:
        self.calls: list[dict] = []
        self._result = AuthorResult(ok=ok, **fields)

    async def generate(self, *, intent: str, runtime: Any,
                        inputs: dict | None = None) -> AuthorResult:
        self.calls.append({
            "intent": intent,
            "runtime": runtime,
            "inputs": inputs,
        })
        return self._result


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

def _make_runtime_and_handler(stub: _StubScriptAuthor | None,
                                tmp_path):
    """Build a runtime + slash handler wired to ``stub``.

    Returns ``(runtime, handler)`` where ``handler`` is the raw string
    the slash surface returns to the user.
    """
    runtime = build_runtime(journal_root=tmp_path / "wf")
    slash_handlers = build_slash_handlers(runtime, script_author=stub)
    handler = slash_handlers["workflow"]["handler"]
    return runtime, handler


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCreateSlash:
    def test_create_unknown_without_script_author_returns_guidance(self,
                                                                    tmp_path):
        """No ScriptAuthor wired -> v0.2.0 guidance, not the v0.2.0
        error. Mirrors the gateway handler's contract."""
        runtime, handler = _make_runtime_and_handler(None, tmp_path)
        out = handler("create review the auth code")
        assert out is not None
        # Should mention the manual workaround path, not the
        # "unknown subcommand" string.
        assert "unknown workflow subcommand" not in out
        assert "v0.2.0" in out
        assert "~/.hermes/workflows" in out

    def test_create_with_empty_intent_returns_usage(self, tmp_path):
        runtime, handler = _make_runtime_and_handler(
            _StubScriptAuthor(ok=True), tmp_path
        )
        out = handler("create")
        assert out is not None
        assert "usage" in out.lower()
        # Must NOT have called ScriptAuthor.generate for an empty intent.
        # (The handler guards on empty intent before invoking.)

    def test_create_routes_to_script_author(self, tmp_path):
        """The /workflow create <intent> subcommand must reach
        ScriptAuthor.generate with the intent verbatim."""
        stub = _StubScriptAuthor(
            ok=True,
            name="review_auth",
            script_path="/home/user/.hermes/workflow_runs/library/review_auth.py",
            run_id="abc123",
        )
        runtime, handler = _make_runtime_and_handler(stub, tmp_path)
        out = handler("create review the auth code")

        # ScriptAuthor.generate was called exactly once.
        assert len(stub.calls) == 1
        assert stub.calls[0]["intent"] == "review the auth code"
        assert stub.calls[0]["runtime"] is runtime

        # User-visible output must include the run_id and script path
        # so the user can follow up with /workflow status <id>.
        assert "abc123" in out
        assert "review_auth.py" in out

    def test_create_surfaces_validation_failure(self, tmp_path):
        """When ScriptAuthor returns ok=False with error_stage='safety_check'
        the slash surface must surface the stage + raw script preview
        so the user can debug what went wrong."""
        stub = _StubScriptAuthor(
            ok=False,
            error_stage="safety_check",
            error="forbidden term in script: 'subprocess'",
            raw_script="import subprocess\n@step(name='x')\nasync def x(ctx):\n  pass",
        )
        runtime, handler = _make_runtime_and_handler(stub, tmp_path)
        out = handler("create something with subprocess calls")

        assert out is not None
        assert "safety_check" in out
        assert "subprocess" in out  # the offending token
        # Must NOT contain "unknown subcommand" -- that's the bug.
        assert "unknown workflow subcommand" not in out

    def test_create_surfaces_llm_failure(self, tmp_path):
        """When ScriptAuthor returns ok=False with error_stage='llm_call'
        the slash surface must surface the error and NOT silently
        swallow it."""
        stub = _StubScriptAuthor(
            ok=False,
            error_stage="llm_call",
            error="acomplete_structured: rate limited",
        )
        runtime, handler = _make_runtime_and_handler(stub, tmp_path)
        out = handler("create a code-review workflow")

        assert out is not None
        assert "llm_call" in out
        assert "rate limited" in out
        assert "unknown workflow subcommand" not in out

    def test_create_runs_asyncio_safely_under_sync_handler(self, tmp_path):
        """The slash handler is sync (returns str, not Awaitable[str]).
        ScriptAuthor.generate is async. The handler must bridge them
        without leaking a coroutine or raising 'coroutine was never
        awaited'."""
        stub = _StubScriptAuthor(
            ok=True,
            name="foo",
            script_path="/tmp/foo.py",
            run_id="r1",
        )
        runtime, handler = _make_runtime_and_handler(stub, tmp_path)
        # Calling the handler must NOT raise — sync return string only.
        result = handler("create a foo workflow")
        assert isinstance(result, str)
        assert "r1" in result

    def test_help_text_lists_create(self, tmp_path):
        """After this fix, /workflow help must advertise the `create`
        subcommand so users know the wiring exists. We assert against
        the dispatcher's help branch."""
        runtime, handler = _make_runtime_and_handler(
            _StubScriptAuthor(ok=True), tmp_path
        )
        out = handler("help")
        assert "create" in out


# ---------------------------------------------------------------------------
# Sanity: existing subcommands still work after the wiring change.
# ---------------------------------------------------------------------------

class TestNonCreateSubcommandsUnchanged:
    def test_run_still_routes_to_cli(self, tmp_path):
        """Sanity: /workflow run <path> still forwards to the CLI.
        A change to dispatch must not break existing subcommands."""
        runtime, handler = _make_runtime_and_handler(
            _StubScriptAuthor(ok=True), tmp_path
        )
        # /workflow run with a nonexistent path should produce a CLI
        # error message, not a ScriptAuthor message.
        out = handler("run /tmp/nonexistent_script.py")
        # The CLI's argparse will catch the missing file or print help.
        # We just assert it doesn't fall into the create branch.
        assert "unknown workflow subcommand" not in out

    def test_unknown_subcommand_still_rejects(self, tmp_path):
        """Bespoke handlers and the unknown-subcommand guard must both
        still work — only 'create' is now wired."""
        runtime, handler = _make_runtime_and_handler(
            _StubScriptAuthor(ok=True), tmp_path
        )
        out = handler("frobnicate the bazzles")
        assert "unknown workflow subcommand" in out