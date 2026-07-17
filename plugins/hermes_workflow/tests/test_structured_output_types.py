"""Tests for the type-level changes that introduce structured-output enforcement.

These tests pin the public surface that workflow authors see:

- StepSpec gains an optional ``output_schema`` (JSON Schema) field.
- Evidence gains an optional ``parsed_payload`` (parsed structured object).
- @step accepts ``output_schema=`` as a kwarg and forwards it to StepSpec.
- StructuredOutputError is importable from the plugin root.
"""

from __future__ import annotations

import pytest

from plugins.hermes_workflow.dsl.primitives import step
from plugins.hermes_workflow.dsl.types import (
    Evidence,
    StepSpec,
)


# ---------------------------------------------------------------------------
# StepSpec.output_schema
# ---------------------------------------------------------------------------


class TestStepSpecOutputSchema:
    def test_step_spec_accepts_output_schema(self):
        async def fn(ctx):
            pass

        spec = StepSpec(
            name="classify",
            fn=fn,
            verifier=None,
            depends_on=(),
            inputs_from={},
            max_retries=0,
            retry_backoff_seconds=1.0,
            timeout_seconds=None,
            output_schema={
                "type": "object",
                "properties": {"label": {"type": "string"}},
            },
        )
        assert spec.output_schema == {
            "type": "object",
            "properties": {"label": {"type": "string"}},
        }

    def test_step_spec_defaults_output_schema_to_none(self):
        async def fn(ctx):
            pass

        spec = StepSpec(
            name="plain",
            fn=fn,
            verifier=None,
            depends_on=(),
            inputs_from={},
            max_retries=0,
            retry_backoff_seconds=1.0,
            timeout_seconds=None,
        )
        assert spec.output_schema is None


# ---------------------------------------------------------------------------
# Evidence.parsed_payload
# ---------------------------------------------------------------------------


class TestEvidenceParsedPayload:
    def test_evidence_carries_parsed_payload(self):
        ev = Evidence(
            files_changed=(),
            commands_run=(),
            exit_codes=(),
            tests_run=0,
            tests_passed=0,
            duration_seconds=0.0,
            parsed_payload={"intent": "refund", "confidence": 0.92},
        )
        assert ev.parsed_payload == {"intent": "refund", "confidence": 0.92}

    def test_evidence_defaults_parsed_payload_to_none(self):
        ev = Evidence(
            files_changed=(),
            commands_run=(),
            exit_codes=(),
            tests_run=0,
            tests_passed=0,
            duration_seconds=0.0,
        )
        assert ev.parsed_payload is None

    def test_evidence_parsed_payload_can_be_list(self):
        ev = Evidence(
            files_changed=(),
            commands_run=(),
            exit_codes=(),
            tests_run=0,
            tests_passed=0,
            duration_seconds=0.0,
            parsed_payload=[{"id": 1}, {"id": 2}],
        )
        assert ev.parsed_payload == [{"id": 1}, {"id": 2}]


# ---------------------------------------------------------------------------
# @step decorator accepts output_schema=
# ---------------------------------------------------------------------------


class TestStepDecoratorOutputSchema:
    def test_step_decorator_accepts_output_schema(self):
        schema = {"type": "object", "properties": {"score": {"type": "number"}}}

        @step(name="score_doc", output_schema=schema)
        async def score_doc(ctx) -> Evidence:
            return _empty_evidence()

        assert score_doc.__workflow_step__.output_schema == schema

    def test_step_decorator_defaults_output_schema_to_none(self):
        @step(name="plain_step")
        async def plain_step(ctx) -> Evidence:
            return _empty_evidence()

        assert plain_step.__workflow_step__.output_schema is None

    def test_step_decorator_rejects_non_dict_output_schema(self):
        with pytest.raises(TypeError):
            @step(name="bad_schema", output_schema="not a dict")
            async def bad_schema(ctx) -> Evidence:
                return _empty_evidence()

    def test_step_decorator_accepts_explicit_none_output_schema(self):
        @step(name="explicit_none", output_schema=None)
        async def explicit_none(ctx) -> Evidence:
            return _empty_evidence()

        assert explicit_none.__workflow_step__.output_schema is None


# ---------------------------------------------------------------------------
# Re-exports
# ---------------------------------------------------------------------------


class TestReExports:
    def test_structured_output_error_exported(self):
        from plugins.hermes_workflow import StructuredOutputError

        assert StructuredOutputError is not None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _empty_evidence() -> Evidence:
    return Evidence(
        files_changed=(),
        commands_run=(),
        exit_codes=(),
        tests_run=0,
        tests_passed=0,
        duration_seconds=0.0,
    )