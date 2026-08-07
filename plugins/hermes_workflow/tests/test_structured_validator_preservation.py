"""Validator smoke test: output_schema survives collect_step_specs traversal."""

from __future__ import annotations

import pytest

from plugins.hermes_workflow.dsl.primitives import step
from plugins.hermes_workflow.dsl.types import Evidence, WorkflowValidationError
from plugins.hermes_workflow.dsl.validator import (
    collect_step_specs,
    collect_workflow_meta,
    GraphValidator,
)


def _empty_evidence() -> Evidence:
    return Evidence(
        files_changed=(), commands_run=(), exit_codes=(),
        tests_run=0, tests_passed=0, duration_seconds=0.0,
    )


# Use this module's real __name__ so the validator's
# attr_value.__module__ != module_globals["__name__"] filter accepts
# the steps defined in this test file.
_TEST_MODULE_NAME = __name__


class TestValidatorOutputSchemaPreserved:
    def test_collector_preserves_output_schema(self):
        schema = {"type": "object", "properties": {"a": {"type": "string"}}}

        @step(name="with_schema", output_schema=schema)
        async def with_schema(ctx) -> Evidence:
            return _empty_evidence()

        fake_globals = {
            "__name__": _TEST_MODULE_NAME,
            "with_schema": with_schema,
        }
        specs = collect_step_specs(fake_globals)
        assert "with_schema" in specs
        assert specs["with_schema"].output_schema == schema

    def test_collector_preserves_none_output_schema(self):
        @step(name="no_schema")
        async def no_schema(ctx) -> Evidence:
            return _empty_evidence()

        fake_globals = {
            "__name__": _TEST_MODULE_NAME,
            "no_schema": no_schema,
        }
        specs = collect_step_specs(fake_globals)
        assert specs["no_schema"].output_schema is None

    def test_collector_handles_mixed_schema_and_no_schema(self):
        schema = {"type": "object"}

        @step(name="structured", output_schema=schema)
        async def structured(ctx) -> Evidence:
            return _empty_evidence()

        @step(name="unstructured")
        async def unstructured(ctx) -> Evidence:
            return _empty_evidence()

        fake_globals = {
            "__name__": _TEST_MODULE_NAME,
            "structured": structured,
            "unstructured": unstructured,
        }
        specs = collect_step_specs(fake_globals)
        assert specs["structured"].output_schema == schema
        assert specs["unstructured"].output_schema is None