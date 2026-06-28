"""Tests for Step 2: graph validator.

What we verify:

collect_step_specs:
- Picks up @step-decorated callables
- Skips non-callables (constants, classes, modules)
- Skips imported @step-decorated callables
- Raises on duplicate step names

collect_workflow_meta:
- Returns (name, fn) for a single @workflow
- Returns None when no @workflow is defined
- Raises on multiple @workflows

GraphValidator:
- Unique step names check
- depends_on must reference a declared @step
- inputs_from must reference a declared @step
- Cycle detection (Kahn's algorithm)
- Cycle message includes the cycle path
- Linear chain validates
- Diamond dependency validates
- Multiple roots validate

validate_workflow_module:
- Top-level helper that runs all checks
- Raises on the first error
"""

from __future__ import annotations

import pytest

from plugins.hermes_workflow import (
    Evidence,
    VerifierResult,
    WorkflowValidationError,
    step,
    workflow,
)
from plugins.hermes_workflow.dsl.validator import (
    GraphValidator,
    collect_step_specs,
    collect_workflow_meta,
    validate_workflow_module,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _empty_ev() -> Evidence:
    return Evidence(
        files_changed=(), commands_run=(), exit_codes=(),
        tests_run=0, tests_passed=0, duration_seconds=0.0,
    )


def _make_module_dict(*funcs, name: str = "test_module") -> dict:
    """Build a fake module __dict__ from a sequence of @step/@workflow funcs.

    Sets each function's __module__ to `name` so the validator's module
    check succeeds (validators filter imported step/decorators by
    __module__).
    """
    d: dict = {"__name__": name}
    for f in funcs:
        try:
            f.__module__ = name
        except (AttributeError, TypeError):
            pass
        d[f.__name__] = f
    return d


# ---------------------------------------------------------------------------
# collect_step_specs
# ---------------------------------------------------------------------------

class TestCollectStepSpecs:
    def test_picks_up_step_decorated(self):
        @step(name="alpha")
        async def alpha(ctx) -> Evidence:
            return _empty_ev()

        specs = collect_step_specs(_make_module_dict(alpha))
        assert "alpha" in specs

    def test_skips_non_callables(self):
        @step(name="alpha")
        async def alpha(ctx) -> Evidence:
            return _empty_ev()

        SOME_CONSTANT = 42
        d = _make_module_dict(alpha)
        d["SOME_CONSTANT"] = SOME_CONSTANT
        specs = collect_step_specs(d)
        assert "alpha" in specs
        assert "SOME_CONSTANT" not in specs

    def test_skips_imported_steps(self):
        @step(name="imported")
        async def imported(ctx) -> Evidence:
            return _empty_ev()
        imported.__module__ = "other_module"        # simulate `from other import imported`

        @step(name="local")
        async def local(ctx) -> Evidence:
            return _empty_ev()
        local.__module__ = "test_module"

        d = {"__name__": "test_module",
             "imported": imported, "local": local}
        specs = collect_step_specs(d)
        assert "local" in specs
        assert "imported" not in specs

    def test_duplicate_name_raises(self):
        @step(name="dup")
        async def a(ctx) -> Evidence:
            return _empty_ev()
        a.__module__ = "test_module"

        @step(name="dup")
        async def b(ctx) -> Evidence:
            return _empty_ev()
        b.__module__ = "test_module"

        with pytest.raises(WorkflowValidationError, match="duplicate"):
            collect_step_specs(_make_module_dict(a, b))


# ---------------------------------------------------------------------------
# collect_workflow_meta
# ---------------------------------------------------------------------------

class TestCollectWorkflowMeta:
    def test_returns_none_when_no_workflow(self):
        @step(name="only_a_step")
        async def only_a_step(ctx) -> Evidence:
            return _empty_ev()

        result = collect_workflow_meta(_make_module_dict(only_a_step))
        assert result is None

    def test_finds_single_workflow(self):
        @workflow(name="the_one")
        async def the_one(ctx) -> dict:
            return {}

        result = collect_workflow_meta(_make_module_dict(the_one))
        assert result is not None
        name, fn = result
        assert name == "the_one"
        assert fn is the_one

    def test_multiple_workflows_raises(self):
        @workflow(name="first")
        async def first(ctx) -> dict:
            return {}

        @workflow(name="second")
        async def second(ctx) -> dict:
            return {}

        with pytest.raises(WorkflowValidationError, match="2 @workflow"):
            collect_workflow_meta(_make_module_dict(first, second))


# ---------------------------------------------------------------------------
# GraphValidator
# ---------------------------------------------------------------------------

class TestGraphValidatorUniqueNames:
    def test_unique_names_passes(self):
        specs = {}
        for name in ("a", "b", "c"):
            @step(name=name)
            async def fn(ctx) -> Evidence:
                return _empty_ev()
            fn.__module__ = "test_module"
            specs[name] = fn.__workflow_step__

        GraphValidator(specs).validate()    # should not raise


class TestGraphValidatorDependsOn:
    def test_depends_on_resolves(self):
        @step(name="upstream")
        async def upstream(ctx) -> Evidence:
            return _empty_ev()
        upstream.__module__ = "test_module"

        @step(name="downstream", depends_on=("upstream",))
        async def downstream(ctx) -> Evidence:
            return _empty_ev()
        downstream.__module__ = "test_module"

        specs = {
            "upstream": upstream.__workflow_step__,
            "downstream": downstream.__workflow_step__,
        }
        GraphValidator(specs).validate()    # ok

    def test_unknown_dependency_raises(self):
        @step(name="lonely")
        async def lonely(ctx) -> Evidence:
            return _empty_ev()
        lonely.__module__ = "test_module"

        specs = {"lonely": lonely.__workflow_step__}
        # Manually craft a spec with bogus dep to test the validator path
        @step(name="bogus_dep", depends_on=("does_not_exist",))
        async def bogus_dep(ctx) -> Evidence:
            return _empty_ev()
        bogus_dep.__module__ = "test_module"
        specs["bogus_dep"] = bogus_dep.__workflow_step__

        with pytest.raises(WorkflowValidationError, match="unknown step"):
            GraphValidator(specs).validate()


class TestGraphValidatorInputsFrom:
    def test_inputs_from_resolves(self):
        @step(name="upstream")
        async def upstream(ctx) -> Evidence:
            return _empty_ev()
        upstream.__module__ = "test_module"

        @step(name="consumer", depends_on=("upstream",),
              inputs_from={"data": "upstream"})
        async def consumer(ctx, data) -> Evidence:
            return _empty_ev()
        consumer.__module__ = "test_module"

        specs = {
            "upstream": upstream.__workflow_step__,
            "consumer": consumer.__workflow_step__,
        }
        GraphValidator(specs).validate()    # ok

    def test_unknown_inputs_from_source_raises(self):
        @step(name="consumer", depends_on=(),
              inputs_from={"data": "missing"})
        async def consumer(ctx, data) -> Evidence:
            return _empty_ev()
        consumer.__module__ = "test_module"

        with pytest.raises(WorkflowValidationError, match="unknown step"):
            GraphValidator({"consumer": consumer.__workflow_step__}).validate()


class TestGraphValidatorCycles:
    def test_linear_chain_validates(self):
        @step(name="a")
        async def a(ctx) -> Evidence:
            return _empty_ev()
        a.__module__ = "test_module"

        @step(name="b", depends_on=("a",))
        async def b(ctx) -> Evidence:
            return _empty_ev()
        b.__module__ = "test_module"

        @step(name="c", depends_on=("b",))
        async def c(ctx) -> Evidence:
            return _empty_ev()
        c.__module__ = "test_module"

        specs = {
            "a": a.__workflow_step__,
            "b": b.__workflow_step__,
            "c": c.__workflow_step__,
        }
        GraphValidator(specs).validate()    # ok

    def test_diamond_validates(self):
        @step(name="root")
        async def root(ctx) -> Evidence:
            return _empty_ev()
        root.__module__ = "test_module"

        @step(name="left", depends_on=("root",))
        async def left(ctx) -> Evidence:
            return _empty_ev()
        left.__module__ = "test_module"

        @step(name="right", depends_on=("root",))
        async def right(ctx) -> Evidence:
            return _empty_ev()
        right.__module__ = "test_module"

        @step(name="merge", depends_on=("left", "right"))
        async def merge(ctx) -> Evidence:
            return _empty_ev()
        merge.__module__ = "test_module"

        specs = {
            "root": root.__workflow_step__,
            "left": left.__workflow_step__,
            "right": right.__workflow_step__,
            "merge": merge.__workflow_step__,
        }
        GraphValidator(specs).validate()    # ok

    def test_two_node_cycle_detected(self):
        @step(name="alpha", depends_on=("beta",))
        async def alpha(ctx) -> Evidence:
            return _empty_ev()
        alpha.__module__ = "test_module"

        @step(name="beta", depends_on=("alpha",))
        async def beta(ctx) -> Evidence:
            return _empty_ev()
        beta.__module__ = "test_module"

        specs = {
            "alpha": alpha.__workflow_step__,
            "beta": beta.__workflow_step__,
        }
        with pytest.raises(WorkflowValidationError, match="cycle"):
            GraphValidator(specs).validate()

    def test_three_node_cycle_detected(self):
        @step(name="x", depends_on=("z",))
        async def x(ctx) -> Evidence:
            return _empty_ev()
        x.__module__ = "test_module"

        @step(name="y", depends_on=("x",))
        async def y(ctx) -> Evidence:
            return _empty_ev()
        y.__module__ = "test_module"

        @step(name="z", depends_on=("y",))
        async def z(ctx) -> Evidence:
            return _empty_ev()
        z.__module__ = "test_module"

        specs = {
            "x": x.__workflow_step__,
            "y": y.__workflow_step__,
            "z": z.__workflow_step__,
        }
        with pytest.raises(WorkflowValidationError, match="cycle"):
            GraphValidator(specs).validate()

    def test_self_loop_detected(self):
        @step(name="loop", depends_on=("loop",))
        async def loop(ctx) -> Evidence:
            return _empty_ev()
        loop.__module__ = "test_module"

        specs = {"loop": loop.__workflow_step__}
        with pytest.raises(WorkflowValidationError, match="cycle"):
            GraphValidator(specs).validate()

    def test_cycle_message_includes_path(self):
        @step(name="n1", depends_on=("n3",))
        async def n1(ctx) -> Evidence:
            return _empty_ev()
        n1.__module__ = "test_module"

        @step(name="n2", depends_on=("n1",))
        async def n2(ctx) -> Evidence:
            return _empty_ev()
        n2.__module__ = "test_module"

        @step(name="n3", depends_on=("n2",))
        async def n3(ctx) -> Evidence:
            return _empty_ev()
        n3.__module__ = "test_module"

        specs = {
            "n1": n1.__workflow_step__,
            "n2": n2.__workflow_step__,
            "n3": n3.__workflow_step__,
        }
        with pytest.raises(WorkflowValidationError) as exc_info:
            GraphValidator(specs).validate()
        msg = str(exc_info.value)
        # All three nodes must appear in the cycle description.
        assert "n1" in msg and "n2" in msg and "n3" in msg


class TestGraphValidatorMultiRoot:
    def test_multiple_independent_roots_validate(self):
        @step(name="root_a")
        async def root_a(ctx) -> Evidence:
            return _empty_ev()
        root_a.__module__ = "test_module"

        @step(name="root_b")
        async def root_b(ctx) -> Evidence:
            return _empty_ev()
        root_b.__module__ = "test_module"

        @step(name="child_a", depends_on=("root_a",))
        async def child_a(ctx) -> Evidence:
            return _empty_ev()
        child_a.__module__ = "test_module"

        @step(name="child_b", depends_on=("root_b",))
        async def child_b(ctx) -> Evidence:
            return _empty_ev()
        child_b.__module__ = "test_module"

        specs = {
            "root_a": root_a.__workflow_step__,
            "root_b": root_b.__workflow_step__,
            "child_a": child_a.__workflow_step__,
            "child_b": child_b.__workflow_step__,
        }
        GraphValidator(specs).validate()    # ok


# ---------------------------------------------------------------------------
# validate_workflow_module (high-level helper)
# ---------------------------------------------------------------------------

class TestValidateWorkflowModule:
    def test_valid_module_returns_specs_and_workflow(self):
        @step(name="step_one")
        async def step_one(ctx) -> Evidence:
            return _empty_ev()
        step_one.__module__ = "test_module"

        @step(name="step_two", depends_on=("step_one",))
        async def step_two(ctx) -> Evidence:
            return _empty_ev()
        step_two.__module__ = "test_module"

        @workflow(name="my_wf")
        async def my_wf(ctx) -> dict:
            return {}

        specs, workflow_entry = validate_workflow_module(
            _make_module_dict(step_one, step_two, my_wf)
        )
        assert "step_one" in specs
        assert "step_two" in specs
        assert workflow_entry is not None
        assert workflow_entry[0] == "my_wf"

    def test_invalid_module_raises(self):
        @step(name="lonely", depends_on=("ghost",))
        async def lonely(ctx) -> Evidence:
            return _empty_ev()
        lonely.__module__ = "test_module"

        @workflow(name="wf")
        async def wf(ctx) -> dict:
            return {}

        with pytest.raises(WorkflowValidationError):
            validate_workflow_module(_make_module_dict(lonely, wf))

    def test_module_with_only_steps_no_workflow_is_allowed(self):
        # Modules that expose only @step coroutines (no @workflow) are valid
        # library imports. The validator returns empty workflow_entry.
        @step(name="lone")
        async def lone(ctx) -> Evidence:
            return _empty_ev()
        lone.__module__ = "test_module"

        specs, workflow_entry = validate_workflow_module(
            _make_module_dict(lone)
        )
        assert "lone" in specs
        assert workflow_entry is None
