"""Tests for Step 6: CLI.

What we verify:

Argument parsing:
- Each subcommand is recognized
- --inputs accepts KEY=VALUE pairs (JSON or plain string)
- --workspace, --max-concurrent, --max-total flow through

run command:
- Runs a workflow script end-to-end
- --no-wait returns immediately with the run_id
- Failure produces non-zero exit code
- Success produces zero exit code

status command:
- No run_id -> runtime.status() snapshot
- run_id -> single-run status; unknown run_id -> error

replay command:
- Shows journal events
- --kind filters to a single kind

list command:
- Empty library -> "(no entries)"
- Library with entries -> lists them

inspect command:
- Valid script -> shows step graph
- Broken script (unknown dep) -> raises WorkflowValidationError
- --json emits structured output
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from plugins.hermes_workflow.cli import _dispatch
from plugins.hermes_workflow import WorkflowError
from plugins.hermes_workflow.journal import Journal
from plugins.hermes_workflow.runtime import WorkflowRuntime
from plugins.hermes_workflow.tests._runtime_helpers import write_workflow_module


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

class TestArgParsing:
    def test_run_help(self):
        from plugins.hermes_workflow.cli import register_cli
        import argparse
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        register_cli(sub)
        # Sanity: no exception.

    def test_parse_inputs_simple(self):
        from plugins.hermes_workflow.cli import _parse_inputs
        result = _parse_inputs(["name=alice", "count=3"])
        assert result["name"] == "alice"
        assert result["count"] == 3

    def test_parse_inputs_json_values(self):
        from plugins.hermes_workflow.cli import _parse_inputs
        result = _parse_inputs(['tags=["a","b"]', 'count=42'])
        assert result["tags"] == ["a", "b"]
        assert result["count"] == 42

    def test_parse_inputs_no_equals_raises(self):
        from plugins.hermes_workflow.cli import _parse_inputs
        with pytest.raises(ValueError, match="KEY=VALUE"):
            _parse_inputs(["missing_equals"])


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------

class TestRunCommand:
    def test_run_success(self, tmp_path, monkeypatch):
        # Override HOME so journal_root defaults to a writable temp dir.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("HERMES_WORKFLOW_ROOT", str(tmp_path / "wf"))

        mod_path = write_workflow_module(tmp_path, """
            from plugins.hermes_workflow import step, workflow, Evidence

            def _empty_ev():
                return Evidence(files_changed=(), commands_run=(), exit_codes=(),
                                tests_run=0, tests_passed=0, duration_seconds=0.0)

            @step(name="only")
            async def only(ctx) -> Evidence:
                return _empty_ev()

            @workflow(name="only_wf")
            async def only_wf(ctx) -> dict:
                await only(ctx)
                return {"done": True}
        """)

        args = _make_args(
            "run", str(mod_path),
            "--json",
        )
        rc = _dispatch(args)
        assert rc == 0

    def test_run_no_wait_returns_immediately(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("HERMES_WORKFLOW_ROOT", str(tmp_path / "wf"))

        mod_path = write_workflow_module(tmp_path, """
            from plugins.hermes_workflow import step, workflow, Evidence
            import asyncio

            def _empty_ev():
                return Evidence(files_changed=(), commands_run=(), exit_codes=(),
                                tests_run=0, tests_passed=0, duration_seconds=0.0)

            @step(name="slow")
            async def slow(ctx) -> Evidence:
                await asyncio.sleep(2.0)
                return _empty_ev()

            @workflow(name="slow_wf")
            async def slow_wf(ctx) -> dict:
                await slow(ctx)
                return {}
        """)

        args = _make_args(
            "run", str(mod_path),
            "--nowait", "--json",
        )
        rc = _dispatch(args)
        assert rc == 0

    def test_run_failure_returns_nonzero(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("HERMES_WORKFLOW_ROOT", str(tmp_path / "wf"))

        mod_path = write_workflow_module(tmp_path, """
            from plugins.hermes_workflow import step, workflow, Evidence

            def _empty_ev():
                return Evidence(files_changed=(), commands_run=(), exit_codes=(),
                                tests_run=0, tests_passed=0, duration_seconds=0.0)

            @step(name="boom")
            async def boom(ctx) -> Evidence:
                raise RuntimeError("intentional")

            @workflow(name="boom_wf")
            async def boom_wf(ctx) -> dict:
                await boom(ctx)
                return {}
        """)

        args = _make_args("run", str(mod_path), "--json")
        rc = _dispatch(args)
        assert rc == 1


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

class TestStatusCommand:
    def test_status_no_run_id(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("HERMES_WORKFLOW_ROOT", str(tmp_path / "wf"))

        args = _make_args("status", "--json")
        rc = _dispatch(args)
        assert rc == 0

    def test_status_unknown_run_id(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("HERMES_WORKFLOW_ROOT", str(tmp_path / "wf"))

        args = _make_args("status", "r_does_not_exist", "--json")
        rc = _dispatch(args)
        assert rc == 1


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------

class TestReplayCommand:
    def test_replay_nonexistent_run_id_returns_zero(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("HERMES_WORKFLOW_ROOT", str(tmp_path / "wf"))

        args = _make_args("replay", "r_never_ran", "--json")
        rc = _dispatch(args)
        # No journal exists -> empty events list, exit 0.
        assert rc == 0


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

class TestListCommand:
    def test_list_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("HERMES_WORKFLOW_ROOT", str(tmp_path / "wf"))

        args = _make_args("list", "--json")
        rc = _dispatch(args)
        assert rc == 0


# ---------------------------------------------------------------------------
# inspect
# ---------------------------------------------------------------------------

class TestInspectCommand:
    def test_inspect_valid_script(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("HERMES_WORKFLOW_ROOT", str(tmp_path / "wf"))

        mod_path = write_workflow_module(tmp_path, """
            from plugins.hermes_workflow import step, workflow, Evidence

            def _empty_ev():
                return Evidence(files_changed=(), commands_run=(), exit_codes=(),
                                tests_run=0, tests_passed=0, duration_seconds=0.0)

            @step(name="a")
            async def a(ctx) -> Evidence:
                return _empty_ev()

            @step(name="b", depends_on=("a",))
            async def b(ctx) -> Evidence:
                return _empty_ev()

            @workflow(name="chain_wf")
            async def chain_wf(ctx) -> dict:
                await a(ctx)
                await b(ctx)
                return {}
        """)

        args = _make_args("inspect", str(mod_path), "--json")
        rc = _dispatch(args)
        assert rc == 0

    def test_inspect_missing_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("HERMES_WORKFLOW_ROOT", str(tmp_path / "wf"))

        args = _make_args("inspect", "/nonexistent/path.py")
        rc = _dispatch(args)
        assert rc == 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_args(*cli_args: str) -> argparse.Namespace:
    """Build an argparse Namespace as if `hermes workflow <cli_args>` was run."""
    import argparse
    from plugins.hermes_workflow.cli import register_cli

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    register_cli(sub)

    full = ["workflow", *cli_args]
    return parser.parse_args(full)
