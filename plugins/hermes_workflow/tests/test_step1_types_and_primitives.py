"""Tests for Step 1: DSL types and primitives.

What we verify here:

Evidence dataclass:
- Construct with valid fields succeeds
- tests_passed > tests_run raises ValueError
- exit_codes length != commands_run length raises ValueError
- Evidence is frozen (cannot mutate fields)
- Equality is by value (frozen=True)

StepSpec + @step decorator:
- @step attaches __workflow_step__ to the coroutine
- Spec carries name, depends_on, inputs_from, verifier, retry config
- @step with empty name raises ValueError
- @step with non-tuple depends_on raises TypeError
- @step with negative max_retries raises ValueError
- @step with non-positive timeout raises ValueError

parallel/gather:
- parallel() with no args raises ValueError
- gather() with no kwargs raises ValueError
- parallel() returns a coroutine function that, when called with (ctx, **kwargs),
  returns a coroutine yielding tuple[Evidence, ...]
- gather() returns a GatherHandle; calling it with (ctx, **kwargs) returns
  a coroutine yielding dict[name, Evidence]
- Order preserved in parallel(); keys preserved in gather()

@workflow decorator:
- @workflow attaches __workflow_meta__ to the coroutine
- Meta carries name, description, max_concurrent, max_total
- @workflow with empty name raises ValueError
- @workflow with max_concurrent < 1 raises ValueError
- @workflow with max_total < 1 raises ValueError

Error classes:
- WorkflowError is the base
- WorkflowValidationError subclasses WorkflowError
- CapExceeded subclasses WorkflowError
- MaxConcurrentReached and MaxTotalReached subclass CapExceeded
- VerifierMismatch carries step_name and verdict attributes
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from plugins.hermes_workflow import (
    CapExceeded,
    Evidence,
    MaxConcurrentReached,
    MaxTotalReached,
    VerifierMismatch,
    VerifierResult,
    WorkflowError,
    WorkflowValidationError,
    gather,
    parallel,
    step,
    workflow,
)
from plugins.hermes_workflow.dsl.primitives import GatherHandle
from plugins.hermes_workflow.dsl.types import StepSpec


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------

class TestEvidence:
    def test_valid_construction_succeeds(self):
        ev = Evidence(
            files_changed=("a.py",),
            commands_run=("git status",),
            exit_codes=(0,),
            tests_run=10,
            tests_passed=10,
            duration_seconds=1.5,
        )
        assert ev.files_changed == ("a.py",)
        assert ev.tests_passed == ev.tests_run

    def test_zero_tests_zero_passed_is_valid(self):
        ev = Evidence(
            files_changed=(),
            commands_run=(),
            exit_codes=(),
            tests_run=0,
            tests_passed=0,
            duration_seconds=0.0,
        )
        assert ev.tests_run == 0

    def test_tests_passed_greater_than_run_raises(self):
        with pytest.raises(ValueError, match="tests_passed"):
            Evidence(
                files_changed=(),
                commands_run=(),
                exit_codes=(),
                tests_run=5,
                tests_passed=10,
                duration_seconds=0.0,
            )

    def test_exit_codes_length_mismatch_raises(self):
        with pytest.raises(ValueError, match="exit_codes"):
            Evidence(
                files_changed=(),
                commands_run=("cmd1", "cmd2"),
                exit_codes=(0,),  # only one exit code for two commands
                tests_run=0,
                tests_passed=0,
                duration_seconds=0.0,
            )

    def test_frozen_cannot_mutate(self):
        ev = Evidence(
            files_changed=(),
            commands_run=(),
            exit_codes=(),
            tests_run=0,
            tests_passed=0,
            duration_seconds=0.0,
        )
        with pytest.raises((AttributeError, Exception)):
            ev.tests_run = 99        # type: ignore[misc]

    def test_equality_by_value(self):
        ev1 = Evidence(
            files_changed=("a.py",), commands_run=(), exit_codes=(),
            tests_run=0, tests_passed=0, duration_seconds=1.0,
        )
        ev2 = Evidence(
            files_changed=("a.py",), commands_run=(), exit_codes=(),
            tests_run=0, tests_passed=0, duration_seconds=1.0,
        )
        assert ev1 == ev2

    def test_hashable_for_frozen_set(self):
        ev = Evidence(
            files_changed=(), commands_run=(), exit_codes=(),
            tests_run=0, tests_passed=0, duration_seconds=0.0,
        )
        assert hash(ev) is not None
        s = {ev}
        assert ev in s


# ---------------------------------------------------------------------------
# @step
# ---------------------------------------------------------------------------

def _ev_empty() -> Evidence:
    return Evidence(
        files_changed=(), commands_run=(), exit_codes=(),
        tests_run=0, tests_passed=0, duration_seconds=0.0,
    )


class TestStepDecorator:
    def test_attaches_workflow_step_attribute(self):
        @step(name="my_step")
        async def my_step(ctx) -> Evidence:
            return _ev_empty()

        assert hasattr(my_step, "__workflow_step__")
        spec = my_step.__workflow_step__
        assert isinstance(spec, StepSpec)
        assert spec.name == "my_step"

    def test_spec_carries_dependencies_and_inputs(self):
        async def my_verifier(ev, ctx) -> VerifierResult:
            return VerifierResult(valid=True, reason="ok")

        @step(
            name="review_file",
            depends_on=("list_changed_files", "setup"),
            inputs_from={"paths": "list_changed_files", "config": "setup"},
            verifier=my_verifier,
            max_retries=3,
            retry_backoff_seconds=2.5,
            timeout_seconds=60.0,
        )
        async def review_file(ctx, paths, config) -> Evidence:
            return _ev_empty()

        spec = review_file.__workflow_step__
        assert spec.name == "review_file"
        assert spec.depends_on == ("list_changed_files", "setup")
        assert spec.inputs_from == {"paths": "list_changed_files",
                                     "config": "setup"}
        assert spec.verifier is my_verifier
        assert spec.max_retries == 3
        assert spec.retry_backoff_seconds == 2.5
        assert spec.timeout_seconds == 60.0

    def test_empty_name_raises(self):
        with pytest.raises(ValueError, match="non-empty name"):
            step(name="")

    def test_non_tuple_depends_on_raises(self):
        with pytest.raises(TypeError, match="depends_on"):
            step(name="x", depends_on=["not", "a", "tuple"])  # type: ignore[arg-type]

    def test_non_dict_inputs_from_raises(self):
        with pytest.raises(TypeError, match="inputs_from"):
            step(name="x", inputs_from="not a dict")  # type: ignore[arg-type]

    def test_negative_max_retries_raises(self):
        with pytest.raises(ValueError, match="max_retries"):
            step(name="x", max_retries=-1)

    def test_negative_retry_backoff_raises(self):
        with pytest.raises(ValueError, match="retry_backoff_seconds"):
            step(name="x", retry_backoff_seconds=-1.0)

    def test_zero_or_negative_timeout_raises(self):
        with pytest.raises(ValueError, match="timeout_seconds"):
            step(name="x", timeout_seconds=0)
        with pytest.raises(ValueError, match="timeout_seconds"):
            step(name="x", timeout_seconds=-1.0)

    def test_default_values(self):
        @step(name="minimal")
        async def minimal(ctx) -> Evidence:
            return _ev_empty()

        spec = minimal.__workflow_step__
        assert spec.depends_on == ()
        assert spec.inputs_from == {}
        assert spec.verifier is None
        assert spec.max_retries == 0
        assert spec.retry_backoff_seconds == 1.0
        assert spec.timeout_seconds is None

    def test_decorator_wraps_function(self):
        async def f(ctx):
            return _ev_empty()

        decorated = step(name="wraps")(f)
        # The decorator wraps; __wrapped__ carries the original.
        assert decorated is not f
        assert decorated.__wrapped__ is f
        assert decorated.__name__ == "f"


# ---------------------------------------------------------------------------
# parallel / gather
# ---------------------------------------------------------------------------

class TestParallelGather:
    def test_parallel_empty_raises(self):
        with pytest.raises(ValueError, match="at least one step"):
            parallel()

    def test_gather_empty_raises(self):
        with pytest.raises(ValueError, match="at least one named step"):
            gather()

    def test_parallel_returns_coroutine_function(self):
        async def step_a(ctx) -> Evidence:
            return _ev_empty()

        runner = parallel(step_a)
        # runner should be a coroutine function (callable returning a coroutine)
        assert asyncio.iscoroutinefunction(runner)

    def test_gather_returns_gather_handle(self):
        async def step_a(ctx) -> Evidence:
            return _ev_empty()

        handle = gather(a=step_a)
        assert isinstance(handle, GatherHandle)

    def test_parallel_dispatches_and_collects(self):
        # NOTE: we name these _step_a/_step_b to avoid shadowing the
        # imported `step` decorator.
        async def _step_a(ctx, **kwargs) -> Evidence:
            return Evidence(
                files_changed=("a.txt",), commands_run=(), exit_codes=(),
                tests_run=0, tests_passed=0, duration_seconds=0.1,
            )

        async def _step_b(ctx, **kwargs) -> Evidence:
            return Evidence(
                files_changed=("b.txt",), commands_run=(), exit_codes=(),
                tests_run=0, tests_passed=0, duration_seconds=0.1,
            )

        async def main():
            return await parallel(_step_a, _step_b)(None)

        a, b = asyncio.run(main())
        assert a.files_changed == ("a.txt",)
        assert b.files_changed == ("b.txt",)

    def test_gather_keys_match_input(self):
        async def _step_a(ctx, **kwargs) -> Evidence:
            return Evidence(
                files_changed=("a.txt",), commands_run=(), exit_codes=(),
                tests_run=0, tests_passed=0, duration_seconds=0.1,
            )

        async def _step_b(ctx, **kwargs) -> Evidence:
            return Evidence(
                files_changed=("b.txt",), commands_run=(), exit_codes=(),
                tests_run=0, tests_passed=0, duration_seconds=0.1,
            )

        async def main():
            return await gather(alpha=_step_a, beta=_step_b)(None)

        result = asyncio.run(main())
        assert set(result.keys()) == {"alpha", "beta"}
        assert result["alpha"].files_changed == ("a.txt",)
        assert result["beta"].files_changed == ("b.txt",)

    def test_parallel_preserves_declaration_order(self):
        async def _make_step(label: str):
            async def _step(ctx, **kwargs) -> Evidence:
                return Evidence(
                    files_changed=(label,), commands_run=(), exit_codes=(),
                    tests_run=0, tests_passed=0, duration_seconds=0.0,
                )
            return _step

        async def main():
            a = await _make_step("A")
            b = await _make_step("B")
            c = await _make_step("C")
            results = await parallel(a, b, c)(None)
            return [r.files_changed[0] for r in results]

        assert asyncio.run(main()) == ["A", "B", "C"]

    def test_parallel_passes_kwargs_to_each_step(self):
        received: list[Any] = []

        async def _step_a(ctx, **kwargs) -> Evidence:
            received.append(("a", kwargs))
            return _ev_empty()

        async def _step_b(ctx, **kwargs) -> Evidence:
            received.append(("b", kwargs))
            return _ev_empty()

        async def main():
            await parallel(_step_a, _step_b)(None, user_id=42)

        asyncio.run(main())
        assert received == [("a", {"user_id": 42}), ("b", {"user_id": 42})]

    def test_gather_passes_kwargs_to_each_step(self):
        received: list[Any] = []

        async def _step_a(ctx, **kwargs) -> Evidence:
            received.append(("a", kwargs))
            return _ev_empty()

        async def _step_b(ctx, **kwargs) -> Evidence:
            received.append(("b", kwargs))
            return _ev_empty()

        async def main():
            await gather(a=_step_a, b=_step_b)(None, paths=["x.py"])

        asyncio.run(main())
        assert received == [("a", {"paths": ["x.py"]}),
                              ("b", {"paths": ["x.py"]})]


# ---------------------------------------------------------------------------
# @workflow
# ---------------------------------------------------------------------------

class TestWorkflowDecorator:
    def test_attaches_workflow_meta(self):
        @workflow(name="my_workflow", description="does stuff")
        async def my_workflow(ctx) -> dict:
            return {}

        assert hasattr(my_workflow, "__workflow_meta__")
        meta = my_workflow.__workflow_meta__
        assert meta.name == "my_workflow"
        assert meta.description == "does stuff"
        assert meta.max_concurrent == 16
        assert meta.max_total == 1000

    def test_custom_caps(self):
        @workflow(name="custom", max_concurrent=4, max_total=100)
        async def custom(ctx) -> dict:
            return {}

        meta = custom.__workflow_meta__
        assert meta.max_concurrent == 4
        assert meta.max_total == 100

    def test_empty_name_raises(self):
        with pytest.raises(ValueError, match="non-empty name"):
            workflow(name="")

    def test_max_concurrent_lt_1_raises(self):
        with pytest.raises(ValueError, match="max_concurrent"):
            workflow(name="x", max_concurrent=0)

    def test_max_total_lt_1_raises(self):
        with pytest.raises(ValueError, match="max_total"):
            workflow(name="x", max_total=0)


# ---------------------------------------------------------------------------
# Error classes
# ---------------------------------------------------------------------------

class TestErrors:
    def test_workflow_error_is_base(self):
        assert issubclass(WorkflowValidationError, WorkflowError)
        assert issubclass(CapExceeded, WorkflowError)
        assert issubclass(VerifierMismatch, WorkflowError)

    def test_cap_subclasses(self):
        assert issubclass(MaxConcurrentReached, CapExceeded)
        assert issubclass(MaxTotalReached, CapExceeded)

    def test_verifier_mismatch_carries_step_name_and_verdict(self):
        verdict = VerifierResult(valid=False, reason="tests failed")
        err = VerifierMismatch(step_name="run_tests", verdict=verdict)
        assert err.step_name == "run_tests"
        assert err.verdict is verdict
        assert "run_tests" in str(err)
        assert "tests failed" in str(err)

    def test_verifier_mismatch_inherits_workflow_error(self):
        err = VerifierMismatch(
            step_name="x",
            verdict=VerifierResult(valid=False, reason="bad"),
        )
        assert isinstance(err, WorkflowError)


# ---------------------------------------------------------------------------
# VerifierResult
# ---------------------------------------------------------------------------

class TestVerifierResult:
    def test_valid_construction(self):
        v = VerifierResult(valid=True, reason="27/27 pass")
        assert v.valid is True
        assert v.reason == "27/27 pass"
        assert v.recheck_after_seconds is None

    def test_with_recheck_hint(self):
        v = VerifierResult(
            valid=True, reason="ok", recheck_after_seconds=60.0,
        )
        assert v.recheck_after_seconds == 60.0
