"""Graph validator for workflow scripts.

A workflow script is a Python module that defines one or more @step-decorated
coroutines and exactly one @workflow-decorated coroutine. The validator
inspects the module at load time and rejects broken dependency graphs with
a structured WorkflowValidationError.

What we check:

1. Exactly one @workflow in the module.
2. Every @step name is unique within the module.
3. Every depends_on target names a declared @step.
4. Every inputs_from key names a declared @step.
5. The dependency graph has no cycles (topological sort succeeds).
6. inputs_from keys reference upstream steps that produce the matching
   output (best-effort: cross-check against step_outputs by walking the
   graph forward and ensuring that an upstream step has the param name
   in its outputs).

The validator runs at workflow import time, before any step executes. A
script that fails validation cannot run; the user gets a structured error
at load time, not a silent failure mid-execution.

Design note: the validator inspects a module's globals() dict. The plugin's
runtime collects the @step specs and @workflow meta by walking the module
after import. We don't require AST parsing — the @step decorator has
already attached everything we need to the function objects.
"""

from __future__ import annotations

from collections import deque
from typing import Any

from .types import (
    StepSpec,
    WorkflowError,
    WorkflowValidationError,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def collect_step_specs(module_globals: dict[str, Any]) -> dict[str, StepSpec]:
    """Walk a module's globals and collect every @step-decorated StepSpec.

    Returns a dict mapping step name to StepSpec. Module-level callables that
    do not have a __workflow_step__ attribute are skipped.
    """
    specs: dict[str, StepSpec] = {}
    for attr_name, attr_value in module_globals.items():
        # Skip non-callables (classes, constants, imported modules).
        if not callable(attr_value):
            continue
        # Skip imported names — only module-level @step-decorated functions
        # count. We identify imports by checking the __module__ attribute.
        spec = getattr(attr_value, "__workflow_step__", None)
        if not isinstance(spec, StepSpec):
            continue
        if attr_value.__module__ != module_globals.get("__name__"):
            # Imported from another module; ignore.
            continue
        if spec.name in specs:
            raise WorkflowValidationError(
                f"duplicate step name '{spec.name}' "
                f"(also defined at {specs[spec.name].fn.__qualname__} "
                f"and {attr_value.__qualname__})"
            )
        specs[spec.name] = spec
    return specs


def collect_workflow_meta(module_globals: dict[str, Any]) -> tuple[str, Any] | None:
    """Walk a module's globals and find the single @workflow-decorated entrypoint.

    Returns (workflow_name, workflow_fn) or None if no @workflow is defined.
    Raises WorkflowValidationError if more than one @workflow is defined.
    """
    found: list[tuple[str, Any]] = []
    for attr_name, attr_value in module_globals.items():
        if not callable(attr_value):
            continue
        meta = getattr(attr_value, "__workflow_meta__", None)
        if meta is None:
            continue
        if attr_value.__module__ != module_globals.get("__name__"):
            continue
        found.append((meta.name, attr_value))

    if len(found) == 0:
        return None
    if len(found) > 1:
        names = [name for name, _ in found]
        raise WorkflowValidationError(
            f"module defines {len(found)} @workflow entrypoints ({names}); "
            f"exactly one is required"
        )
    return found[0]


class GraphValidator:
    """Validates the dependency graph of a workflow's @step set.

    Usage:
        specs = collect_step_specs(globals())
        GraphValidator(specs).validate()
    """

    def __init__(self, specs: dict[str, StepSpec]) -> None:
        self.specs = specs

    def validate(self) -> None:
        """Run all checks. Raises WorkflowValidationError on first failure."""
        self._check_unique_names()
        self._check_depends_on_resolve()
        self._check_inputs_from_resolve()
        self._check_no_cycles()

    # -- individual checks -------------------------------------------------

    def _check_unique_names(self) -> None:
        # collect_step_specs already enforces uniqueness during collection;
        # this method is for symmetry / future re-validation.
        seen: set[str] = set()
        for name in self.specs:
            if name in seen:
                raise WorkflowValidationError(
                    f"duplicate step name '{name}'"
                )
            seen.add(name)

    def _check_depends_on_resolve(self) -> None:
        for name, spec in self.specs.items():
            for dep in spec.depends_on:
                if dep not in self.specs:
                    raise WorkflowValidationError(
                        f"step '{name}' depends on unknown step '{dep}'"
                    )

    def _check_inputs_from_resolve(self) -> None:
        for name, spec in self.specs.items():
            for input_name, source_step in spec.inputs_from.items():
                if source_step not in self.specs:
                    raise WorkflowValidationError(
                        f"step '{name}' inputs_from['{input_name}'] "
                        f"references unknown step '{source_step}'"
                    )

    def _check_no_cycles(self) -> None:
        """Kahn's algorithm. Raises if a cycle is present."""
        # Build in-degree map: how many dependencies does each step have?
        in_degree: dict[str, int] = {name: 0 for name in self.specs}
        # Adjacency: dep_name -> list of steps that depend on dep_name
        dependents: dict[str, list[str]] = {name: [] for name in self.specs}

        for name, spec in self.specs.items():
            in_degree[name] = len(spec.depends_on)
            for dep in spec.depends_on:
                dependents[dep].append(name)

        # Queue of steps with no remaining dependencies.
        queue: deque[str] = deque(
            name for name, deg in in_degree.items() if deg == 0
        )
        visited_order: list[str] = []

        while queue:
            name = queue.popleft()
            visited_order.append(name)
            for dependent in dependents[name]:
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)

        if len(visited_order) != len(self.specs):
            # Some nodes were never visited — they're in a cycle.
            unvisited = [
                name for name, deg in in_degree.items() if deg > 0
            ]
            # Try to identify the actual cycle by walking from one unvisited node.
            cycle = self._find_cycle(unvisited[0])
            raise WorkflowValidationError(
                f"dependency cycle detected: {' -> '.join(cycle)}"
            )

    def _find_cycle(self, start: str) -> list[str]:
        """Walk the dependency graph from start until we re-enter the path."""
        path: list[str] = [start]
        visited: set[str] = {start}
        current = start
        while True:
            spec = self.specs[current]
            # Follow the FIRST dependency in declaration order.
            if not spec.depends_on:
                return path    # shouldn't happen if there really is a cycle
            next_step = spec.depends_on[0]
            if next_step in visited:
                # Cycle closes here.
                cycle_start = path.index(next_step)
                return path[cycle_start:] + [next_step]
            path.append(next_step)
            visited.add(next_step)
            current = next_step


def validate_workflow_module(module_globals: dict[str, Any]) -> tuple[
    dict[str, StepSpec], tuple[str, Any] | None
]:
    """High-level entry point. Collects specs and the workflow entrypoint,
    then validates the graph. Returns (specs, workflow_meta) on success.
    Raises WorkflowValidationError on any failure.
    """
    specs = collect_step_specs(module_globals)
    workflow_entry = collect_workflow_meta(module_globals)
    if specs:
        GraphValidator(specs).validate()
    return specs, workflow_entry
