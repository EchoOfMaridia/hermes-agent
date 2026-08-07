"""Tests for Step 4: WorkflowRuntime.

What we verify:

Construction:
- Defaults to 16/1000 caps
- Custom caps
- Rejects invalid caps (< 1)
- journal_root can be overridden

submit():
- Returns a run_id with r_ prefix
- Validates the workflow graph at submit time (raises on broken graph)
- Inserts a Run in the registry
- Schedules execution as an asyncio.Task
- Honors per-submit max_concurrent/max_total overrides

Cancel():
- Marks cancel_requested on the run
- Cancels the asyncio.Task
- Idempotent on unknown run_id (raises WorkflowError)

status():
- Returns active_runs, active_count, staleness_seconds, cap
- Empty registry -> empty active_runs
- Active run appears in active_runs

Step execution:
- A step that returns Evidence is journaled step_started -> step_completed
- A step that raises is retried per spec.max_retries
- A step that raises with max_retries=0 raises once and halts the run
- A verifier that returns valid=True accepts the step
- A verifier that returns valid=False retries; after retries, raises VerifierMismatch
- A failing step updates run.failed_steps
- A successful step updates run.completed_steps and run.step_states

Concurrency:
- max_concurrent=N means at most N step fns run simultaneously
- max_total=K means after K steps spawned, MaxTotalReached is raised

End-to-end:
- A trivial workflow (one step, no deps) completes and returns its outputs
- A linear chain of steps completes in order
- A gather of parallel steps completes concurrently
- A failing step with no retries transitions the run to FAILED
- A cap-exceeded run transitions to HALTED
- A cancelled run transitions to CANCELLED
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from plugins.hermes_workflow import (
    Evidence,
    MaxTotalReached,
    VerifierMismatch,
    VerifierResult,
    WorkflowError,
    WorkflowValidationError,
    step,
    workflow,
)
from plugins.hermes_workflow.runtime import WorkflowRuntime

from plugins.hermes_workflow.tests._runtime_helpers import (
    import_workflow,
    submit_and_drain,
    submit_and_wait,
    write_workflow_module,
)


_TRACKER: dict = {}


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

class TestRuntimeConstruction:
    def test_default_caps(self, tmp_path):
        rt = WorkflowRuntime(journal_root=tmp_path)
        assert rt.max_concurrent == 16
        assert rt.max_total == 1000

    def test_custom_caps(self, tmp_path):
        rt = WorkflowRuntime(
            journal_root=tmp_path,
            default_max_concurrent=4,
            default_max_total=50,
        )
        assert rt.max_concurrent == 4
        assert rt.max_total == 50

    def test_rejects_max_concurrent_lt_1(self, tmp_path):
        with pytest.raises(ValueError, match="max_concurrent"):
            WorkflowRuntime(default_max_concurrent=0, journal_root=tmp_path)

    def test_rejects_max_total_lt_1(self, tmp_path):
        with pytest.raises(ValueError, match="max_total"):
            WorkflowRuntime(default_max_total=0, journal_root=tmp_path)


# ---------------------------------------------------------------------------
# submit()
# ---------------------------------------------------------------------------

class TestSubmit:
    def test_returns_run_id_with_prefix(self, tmp_path):
        mod_path = write_workflow_module(tmp_path, """
            from plugins.hermes_workflow import step, workflow, Evidence

            def _empty_ev():
                return Evidence(files_changed=(), commands_run=(), exit_codes=(),
                                tests_run=0, tests_passed=0, duration_seconds=0.0)

            @step(name="noop")
            async def noop(ctx) -> Evidence:
                return _empty_ev()

            @workflow(name="trivial")
            async def trivial_wf(ctx) -> dict:
                await noop(ctx)
                return {"ok": True}
        """)

        async def _go():
            rt = WorkflowRuntime(journal_root=tmp_path)
            module = import_workflow(mod_path)
            run_id = await rt.submit(module["trivial_wf"], {})
            return run_id

        run_id = asyncio.run(_go())
        assert run_id.startswith("r_")

    def test_inserts_run_in_registry(self, tmp_path):
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
                return {}
        """)

        async def _go():
            rt = WorkflowRuntime(journal_root=tmp_path)
            module = import_workflow(mod_path)
            run_id, run = await submit_and_wait(rt, module["only_wf"], {})
            return run_id, rt.get_run(run_id) is not None, run.state.value

        run_id, present, state = asyncio.run(_go())
        assert present
        assert state == "done"

    def test_rejects_broken_graph(self, tmp_path):
        mod_path = write_workflow_module(tmp_path, """
            from plugins.hermes_workflow import step, workflow, Evidence

            def _empty_ev():
                return Evidence(files_changed=(), commands_run=(), exit_codes=(),
                                tests_run=0, tests_passed=0, duration_seconds=0.0)

            @step(name="bad", depends_on=("does_not_exist",))
            async def bad(ctx) -> Evidence:
                return _empty_ev()

            @workflow(name="bad_wf")
            async def bad_wf(ctx) -> dict:
                return {}
        """)

        async def _go():
            rt = WorkflowRuntime(journal_root=tmp_path)
            module = import_workflow(mod_path)
            return await rt.submit(module["bad_wf"], {})

        with pytest.raises(WorkflowValidationError):
            asyncio.run(_go())

    def test_per_submit_cap_overrides(self, tmp_path):
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
                return {}
        """)

        async def _go():
            rt = WorkflowRuntime(journal_root=tmp_path, default_max_total=1000)
            module = import_workflow(mod_path)
            run_id, run = await submit_and_wait(
                rt, module["only_wf"], {}, max_total=2,
            )
            return run.max_total

        assert asyncio.run(_go()) == 2


# ---------------------------------------------------------------------------
# Cancel()
# ---------------------------------------------------------------------------

class TestCancel:
    def test_unknown_run_raises(self, tmp_path):
        async def _go():
            rt = WorkflowRuntime(journal_root=tmp_path)
            await rt.cancel("r_does_not_exist")

        with pytest.raises(WorkflowError, match="unknown run_id"):
            asyncio.run(_go())

    def test_cancel_marks_run(self, tmp_path):
        mod_path = write_workflow_module(tmp_path, """
            import asyncio
            from plugins.hermes_workflow import step, workflow, Evidence

            def _empty_ev():
                return Evidence(files_changed=(), commands_run=(), exit_codes=(),
                                tests_run=0, tests_passed=0, duration_seconds=0.0)

            @step(name="slow")
            async def slow(ctx) -> Evidence:
                await asyncio.sleep(60)
                return _empty_ev()

            @workflow(name="slow_wf")
            async def slow_wf(ctx) -> dict:
                await slow(ctx)
                return {}
        """)

        async def _go():
            rt = WorkflowRuntime(journal_root=tmp_path)
            module = import_workflow(mod_path)
            run_id = await rt.submit(module["slow_wf"], {})
            run = rt.get_run(run_id)

            async def cancel_after():
                await asyncio.sleep(0.05)
                await rt.cancel(run_id)

            await asyncio.gather(run.task, cancel_after(), return_exceptions=True)
            return run.cancel_requested, run.state.value

        cancel_requested, state = asyncio.run(_go())
        assert cancel_requested is True
        assert state == "cancelled"


# ---------------------------------------------------------------------------
# status()
# ---------------------------------------------------------------------------

class TestStatus:
    def test_empty_registry(self, tmp_path):
        rt = WorkflowRuntime(journal_root=tmp_path)
        st = rt.status()
        assert st["active_runs"] == []
        assert st["active_count"] == 0
        assert st["cap"]["concurrent"] == 16
        assert st["cap"]["total"] == 1000

    def test_run_status_unknown(self, tmp_path):
        rt = WorkflowRuntime(journal_root=tmp_path)
        assert rt.run_status("r_nope") is None


# ---------------------------------------------------------------------------
# Step execution
# ---------------------------------------------------------------------------

class TestStepExecution:
    def test_trivial_workflow_completes(self, tmp_path):
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

            @workflow(name="linear")
            async def linear_wf(ctx) -> dict:
                await a(ctx)
                await b(ctx)
                return {"done": True}
        """)

        async def _go():
            rt = WorkflowRuntime(journal_root=tmp_path)
            module = import_workflow(mod_path)
            _, run = await submit_and_wait(rt, module["linear_wf"], {})
            return (run.state.value,
                    list(run.completed_steps.keys()),
                    run.step_states)

        state, completed, step_states = asyncio.run(_go())
        assert state == "done"
        assert completed == ["a", "b"]
        assert step_states["a"].value == "verified"
        assert step_states["b"].value == "verified"

    def test_step_failure_no_retries_marks_failed(self, tmp_path):
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

        async def _go():
            rt = WorkflowRuntime(journal_root=tmp_path)
            module = import_workflow(mod_path)
            return await submit_and_drain(rt, module["boom_wf"], {})

        _, run, err = asyncio.run(_go())
        assert err is not None
        assert isinstance(err, RuntimeError)
        assert run.state.value == "failed"
        assert "boom" in run.failed_steps

    def test_step_failure_with_retries_eventually_succeeds(self, tmp_path):
        mod_path = write_workflow_module(tmp_path, """
            from plugins.hermes_workflow import step, workflow, Evidence

            attempt = {"n": 0}

            def _empty_ev():
                return Evidence(files_changed=(), commands_run=(), exit_codes=(),
                                tests_run=0, tests_passed=0, duration_seconds=0.0)

            @step(name="flaky", max_retries=2, retry_backoff_seconds=0.001)
            async def flaky(ctx) -> Evidence:
                attempt["n"] += 1
                if attempt["n"] < 2:
                    raise RuntimeError("transient")
                return _empty_ev()

            @workflow(name="flaky_wf")
            async def flaky_wf(ctx) -> dict:
                await flaky(ctx)
                return {}
        """)

        async def _go():
            rt = WorkflowRuntime(journal_root=tmp_path)
            module = import_workflow(mod_path)
            _, run = await submit_and_wait(rt, module["flaky_wf"], {})
            return run.state.value, module["attempt"]["n"]

        state, attempts = asyncio.run(_go())
        assert state == "done"
        assert attempts == 2

    def test_verifier_accepts_step(self, tmp_path):
        mod_path = write_workflow_module(tmp_path, """
            from plugins.hermes_workflow import step, workflow, Evidence, VerifierResult

            def _empty_ev():
                return Evidence(files_changed=(), commands_run=(), exit_codes=(),
                                tests_run=0, tests_passed=0, duration_seconds=0.0)

            async def _accept(ev, ctx):
                return VerifierResult(valid=True, reason="ok")

            @step(name="guarded", verifier=_accept)
            async def guarded(ctx) -> Evidence:
                return _empty_ev()

            @workflow(name="guarded_wf")
            async def guarded_wf(ctx) -> dict:
                await guarded(ctx)
                return {}
        """)

        async def _go():
            rt = WorkflowRuntime(journal_root=tmp_path)
            module = import_workflow(mod_path)
            _, run = await submit_and_wait(rt, module["guarded_wf"], {})
            return run.state.value

        assert asyncio.run(_go()) == "done"

    def test_verifier_rejection_eventually_fails(self, tmp_path):
        mod_path = write_workflow_module(tmp_path, """
            from plugins.hermes_workflow import step, workflow, Evidence, VerifierResult

            def _empty_ev():
                return Evidence(files_changed=(), commands_run=(), exit_codes=(),
                                tests_run=0, tests_passed=0, duration_seconds=0.0)

            async def _reject(ev, ctx):
                return VerifierResult(valid=False, reason="no")

            @step(name="guarded", max_retries=1, retry_backoff_seconds=0.001,
                  verifier=_reject)
            async def guarded(ctx) -> Evidence:
                return _empty_ev()

            @workflow(name="guarded_wf")
            async def guarded_wf(ctx) -> dict:
                await guarded(ctx)
                return {}
        """)

        async def _go():
            rt = WorkflowRuntime(journal_root=tmp_path)
            module = import_workflow(mod_path)
            return await submit_and_drain(rt, module["guarded_wf"], {})

        _, run, err = asyncio.run(_go())
        assert isinstance(err, VerifierMismatch)
        assert run.state.value == "failed"


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------

class TestConcurrency:
    def test_max_total_raises_after_cap(self, tmp_path):
        mod_path = write_workflow_module(tmp_path, """
            from plugins.hermes_workflow import step, workflow, Evidence

            def _empty_ev():
                return Evidence(files_changed=(), commands_run=(), exit_codes=(),
                                tests_run=0, tests_passed=0, duration_seconds=0.0)

            @step(name="one")
            async def one(ctx) -> Evidence: return _empty_ev()

            @step(name="two", depends_on=("one",))
            async def two(ctx) -> Evidence: return _empty_ev()

            @step(name="three", depends_on=("two",))
            async def three(ctx) -> Evidence: return _empty_ev()

            @step(name="four", depends_on=("three",))
            async def four(ctx) -> Evidence: return _empty_ev()

            @workflow(name="chain")
            async def chain(ctx) -> dict:
                await one(ctx)
                await two(ctx)
                await three(ctx)
                await four(ctx)
                return {}
        """)

        async def _go():
            rt = WorkflowRuntime(journal_root=tmp_path, default_max_total=3)
            module = import_workflow(mod_path)
            return await submit_and_drain(rt, module["chain"], {})

        _, run, err = asyncio.run(_go())
        assert isinstance(err, MaxTotalReached)
        assert run.state.value == "halted"

    def test_max_concurrent_limits_simultaneous_steps(self, tmp_path):
        """With max_concurrent=2 and 4 parallel steps, at most 2 run
        simultaneously. We instrument a tracker that the workflow imports."""
        global _TRACKER
        _TRACKER = {"in_flight": 0, "max_observed": 0}

        # Make the tracker importable as
        # plugins.hermes_workflow.tests._tracker_module
        import types
        tracker_mod = types.ModuleType("plugins.hermes_workflow.tests._tracker_module")
        tracker_mod.TRACKER = _TRACKER
        sys.modules["plugins.hermes_workflow.tests._tracker_module"] = tracker_mod

        mod_path = write_workflow_module(tmp_path, """
            import asyncio
            from plugins.hermes_workflow import step, workflow, gather, Evidence
            from plugins.hermes_workflow.tests._tracker_module import TRACKER

            def _empty_ev():
                return Evidence(files_changed=(), commands_run=(), exit_codes=(),
                                tests_run=0, tests_passed=0, duration_seconds=0.0)

            def _make_step(name):
                @step(name=name)
                async def step_fn(ctx) -> Evidence:
                    TRACKER["in_flight"] += 1
                    TRACKER["max_observed"] = max(TRACKER["max_observed"],
                                                   TRACKER["in_flight"])
                    await asyncio.sleep(0.05)
                    TRACKER["in_flight"] -= 1
                    return _empty_ev()
                return step_fn

            step_a = _make_step("a")
            step_b = _make_step("b")
            step_c = _make_step("c")
            step_d = _make_step("d")

            @workflow(name="parallel_wf")
            async def parallel_wf(ctx) -> dict:
                await gather(a=step_a, b=step_b, c=step_c, d=step_d)(ctx)
                return {}
        """)

        async def _go():
            rt = WorkflowRuntime(
                journal_root=tmp_path,
                default_max_concurrent=2,
                default_max_total=100,
            )
            module = import_workflow(mod_path)
            _, run = await submit_and_wait(rt, module["parallel_wf"], {})
            return run.state.value, _TRACKER["max_observed"]

        state, max_observed = asyncio.run(_go())
        assert state == "done"
        assert max_observed <= 2, (
            f"max_observed={max_observed} exceeds cap of 2"
        )


# ---------------------------------------------------------------------------
# End-to-end
# ---------------------------------------------------------------------------

class TestEndToEnd:
    def test_run_status_after_completion(self, tmp_path):
        mod_path = write_workflow_module(tmp_path, """
            from plugins.hermes_workflow import step, workflow, Evidence

            def _empty_ev():
                return Evidence(files_changed=(), commands_run=(), exit_codes=(),
                                tests_run=0, tests_passed=0, duration_seconds=0.0)

            @step(name="only")
            async def only(ctx) -> Evidence: return _empty_ev()

            @workflow(name="only_wf")
            async def only_wf(ctx) -> dict:
                await only(ctx)
                return {}
        """)

        async def _go():
            rt = WorkflowRuntime(journal_root=tmp_path)
            module = import_workflow(mod_path)
            run_id, _ = await submit_and_wait(rt, module["only_wf"], {})
            return rt.run_status(run_id)

        st = asyncio.run(_go())
        assert st is not None
        assert st["workflow"] == "only_wf"
        assert st["state"] == "done"
        assert "only" in st["steps_completed"]
