"""Tests that the bundled example workflows run end-to-end.

We import each example as a Python module, submit its @workflow body,
and verify it completes successfully. The examples are deliberately
self-contained (no LLM call) so they run in CI without an agent
bridge configured.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

from plugins.hermes_workflow.runtime import WorkflowRuntime


EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"


def _import_example(name: str):
    """Import an example script by file name and return its globals."""
    path = EXAMPLES_DIR / f"{name}.py"
    module_name = f"_hermes_wf_example_{name}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return vars(module)


async def _run_workflow(module_globals, runtime: WorkflowRuntime) -> dict:
    """Find the @workflow entrypoint and run it."""
    workflow_fn = next(
        v for v in module_globals.values()
        if callable(v) and hasattr(v, "__workflow_meta__")
    )
    run_id = await runtime.submit(workflow_fn, {})
    run = runtime.get_run(run_id)
    await run.task
    return {"run_id": run_id, "state": run.state.value,
            "completed_steps": list(run.completed_steps.keys())}


# ---------------------------------------------------------------------------
# simple_review — minimal one-step workflow
# ---------------------------------------------------------------------------

class TestSimpleReview:
    def test_runs_to_completion(self, tmp_path):
        async def _go():
            rt = WorkflowRuntime(journal_root=tmp_path / "wf")
            module = _import_example("simple_review")
            result = await _run_workflow(module, rt)
            assert result["state"] == "done"
            assert "greet" in result["completed_steps"]
        asyncio.run(_go())


# ---------------------------------------------------------------------------
# parallel_audit — five parallel audits + verifier-guarded summary
# ---------------------------------------------------------------------------

class TestParallelAudit:
    def test_runs_to_completion(self, tmp_path):
        async def _go():
            rt = WorkflowRuntime(journal_root=tmp_path / "wf")
            module = _import_example("parallel_audit")
            result = await _run_workflow(module, rt)
            assert result["state"] == "done"
            # Five audits + one summary.
            completed = result["completed_steps"]
            assert any("audit_" in s for s in completed)
            assert "summarize" in completed
        asyncio.run(_go())

    def test_audits_run_concurrently(self, tmp_path):
        """The five audit_* steps share the same start window when
        gather() is used. We verify by checking that the journal shows
        step_started events for all five audits within a tight time
        window."""
        import time
        from plugins.hermes_workflow.journal import Journal

        async def _go():
            rt = WorkflowRuntime(journal_root=tmp_path / "wf")
            module = _import_example("parallel_audit")
            workflow_fn = next(
                v for v in module.values()
                if callable(v) and hasattr(v, "__workflow_meta__")
            )
            run_id = await rt.submit(workflow_fn, {})
            run = rt.get_run(run_id)
            await run.task

            journal = Journal.replay(run_id, tmp_path / "wf")
            starts = sorted(
                e["ts"] for e in journal.events
                if e.get("kind") == "step_started"
                and "audit_" in e.get("step", "")
            )
            assert len(starts) == 5
            # All five should start within 100ms of each other.
            assert (starts[-1] - starts[0]) < 0.1
        asyncio.run(_go())


# ---------------------------------------------------------------------------
# retry_with_backoff — flaky step succeeds on third attempt
# ---------------------------------------------------------------------------

class TestRetryWithBackoff:
    def test_succeeds_after_two_failures(self, tmp_path):
        from plugins.hermes_workflow.journal import Journal

        async def _go():
            rt = WorkflowRuntime(journal_root=tmp_path / "wf")
            module = _import_example("retry_with_backoff")
            result = await _run_workflow(module, rt)
            assert result["state"] == "done"

            journal = Journal.replay(result["run_id"], tmp_path / "wf")
            # step_failed events for the failed attempts.
            failed_count = sum(
                1 for e in journal.events
                if e.get("kind") == "step_failed"
                and e.get("step") == "flaky_call"
            )
            assert failed_count >= 2
            # step_completed shows the third attempt succeeded.
            completed = sum(
                1 for e in journal.events
                if e.get("kind") == "step_completed"
                and e.get("step") == "flaky_call"
            )
            assert completed == 1
        asyncio.run(_go())
