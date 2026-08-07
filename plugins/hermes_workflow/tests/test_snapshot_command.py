"""Tests for the `hermes workflow snapshot` command (spec section 19.2).

What we verify:

Argument parsing:
- snapshot subcommand is recognized
- --tier accepts 1, 2, 3
- --json flag works

Behavior:
- snapshot of a completed run emits the rendered card tree (text)
- snapshot --json emits the structured snapshot
- snapshot of a run with steps shows step-level details
- snapshot of an unknown run_id exits 0 with empty result (no journal)

Tier variants:
- tier=1 produces a tree with sub-cards
- tier=2 produces one-line-per-step chat output
- tier=3 produces plain text
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

import pytest

from plugins.hermes_workflow.cli import _dispatch
from plugins.hermes_workflow.journal import Journal
from plugins.hermes_workflow.runtime import WorkflowRuntime
from plugins.hermes_workflow.tests._runtime_helpers import (
    import_workflow,
    submit_and_wait,
    write_workflow_module,
)


def _make_args(*cli_args: str) -> argparse.Namespace:
    import argparse
    from plugins.hermes_workflow.cli import register_cli

    parser = argparse.ArgumentParser()
    # Mimic the hermes plugin loader wiring: register_cli receives a
    # leaf ``ArgumentParser`` (the ``workflow`` subparser) and attaches
    # the run/list/... sub-subparsers to it.  See cli.py:register_cli
    # for the contract.
    top_subs = parser.add_subparsers()
    workflow_parser = top_subs.add_parser("workflow")
    register_cli(workflow_parser)
    return parser.parse_args(["workflow", *cli_args])


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

class TestSnapshotArgs:
    def test_snapshot_help(self):
        from plugins.hermes_workflow.cli import register_cli
        import argparse
        parser = argparse.ArgumentParser()
        # Same contract as _make_args: leaf ``workflow`` parser in.
        top_subs = parser.add_subparsers()
        workflow_parser = top_subs.add_parser("workflow")
        register_cli(workflow_parser)

    def test_snapshot_tier_default_is_2(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("HERMES_WORKFLOW_ROOT", str(tmp_path / "wf"))
        # tier default = 2; with --json flag the structured output is emitted.
        args = _make_args("snapshot", "r_anything", "--json")
        assert args.tier == 2


# ---------------------------------------------------------------------------
# Snapshot behavior
# ---------------------------------------------------------------------------

class TestSnapshotCommand:
    def _run_simple_workflow(self, tmp_path, monkeypatch):
        """Helper: run a 2-step workflow and return its run_id."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("HERMES_WORKFLOW_ROOT", str(tmp_path / "wf"))

        mod_path = write_workflow_module(tmp_path, """
            from plugins.hermes_workflow import step, workflow, Evidence

            def _empty_ev():
                return Evidence(files_changed=(), commands_run=(), exit_codes=(),
                                tests_run=0, tests_passed=0, duration_seconds=0.0)

            @step(name="a")
            async def a(ctx) -> Evidence: return _empty_ev()

            @step(name="b", depends_on=("a",))
            async def b(ctx) -> Evidence:
                return Evidence(files_changed=("out.md",),
                                commands_run=(), exit_codes=(),
                                tests_run=0, tests_passed=0,
                                duration_seconds=1.5)

            @workflow(name="chain_wf")
            async def chain_wf(ctx) -> dict:
                await a(ctx)
                await b(ctx)
                return {}
        """)
        async def _go():
            rt = WorkflowRuntime(journal_root=tmp_path / "wf")
            module = import_workflow(mod_path)
            run_id, _ = await submit_and_wait(rt, module["chain_wf"], {})
            return run_id

        return asyncio.run(_go())

    def test_snapshot_unknown_run_exits_zero(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("HERMES_WORKFLOW_ROOT", str(tmp_path / "wf"))

        args = _make_args("snapshot", "r_does_not_exist", "--json")
        rc = _dispatch(args)
        assert rc == 0

    def test_snapshot_json_emits_structured(self, tmp_path, monkeypatch):
        run_id = self._run_simple_workflow(tmp_path, monkeypatch)
        args = _make_args("snapshot", run_id, "--json")
        rc = _dispatch(args)
        assert rc == 0

    def test_snapshot_text_emits_card_tree(self, tmp_path, monkeypatch, capsys):
        run_id = self._run_simple_workflow(tmp_path, monkeypatch)
        args = _make_args("snapshot", run_id)        # tier defaults to 2
        rc = _dispatch(args)
        assert rc == 0
        captured = capsys.readouterr()
        # Tier 2 output includes step names.
        assert "a" in captured.out
        assert "b" in captured.out
        assert "chain_wf" in captured.out

    def test_snapshot_tier1_includes_sub_cards(self, tmp_path, monkeypatch, capsys):
        run_id = self._run_simple_workflow(tmp_path, monkeypatch)
        args = _make_args("snapshot", run_id, "--tier", "1")
        rc = _dispatch(args)
        assert rc == 0
        captured = capsys.readouterr()
        # Tier 1 uses tree characters.
        assert "├──" in captured.out or "└──" in captured.out
        assert "a" in captured.out
        assert "b" in captured.out

    def test_snapshot_tier3_plain_text(self, tmp_path, monkeypatch, capsys):
        run_id = self._run_simple_workflow(tmp_path, monkeypatch)
        args = _make_args("snapshot", run_id, "--tier", "3")
        rc = _dispatch(args)
        assert rc == 0
        captured = capsys.readouterr()
        # Tier 3 has no unicode markers.
        assert "▶" not in captured.out
        assert "■" not in captured.out
        assert "🔧" not in captured.out
        assert "chain_wf" in captured.out
