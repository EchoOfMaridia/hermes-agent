"""Tests for the auto-schema-verifier installed on @step(output_schema=...) steps."""

from __future__ import annotations

import asyncio

from plugins.hermes_workflow.dsl.types import Evidence, RunContext
from plugins.hermes_workflow.runtime import _auto_schema_verifier


def _empty_evidence(parsed=None) -> Evidence:
    return Evidence(
        files_changed=(), commands_run=(), exit_codes=(),
        tests_run=0, tests_passed=0, duration_seconds=0.0,
        parsed_payload=parsed,
    )


def _fake_ctx() -> RunContext:
    from pathlib import Path
    return RunContext(
        run_id="r_test", workspace=Path("/tmp"),
        inputs={}, step_outputs={}, runtime=None,  # type: ignore[arg-type]
    )


def _try_import_jsonschema():
    try:
        import jsonschema
        return jsonschema
    except ImportError:
        return None


class TestAutoSchemaVerifier:
    def test_happy_path_returns_valid(self):
        js = _try_import_jsonschema()
        if js is None:
            return  # jsonschema not installed; skip
        schema = {
            "type": "object",
            "properties": {
                "intent": {"type": "string"},
                "confidence": {"type": "number"},
            },
            "required": ["intent", "confidence"],
        }
        verify = _auto_schema_verifier(schema, js)
        evidence = _empty_evidence(parsed={"intent": "refund", "confidence": 0.92})
        result = asyncio.run(verify(evidence, _fake_ctx()))
        assert result.valid is True
        assert "matches output_schema" in result.reason

    def test_sad_path_returns_invalid_with_path(self):
        js = _try_import_jsonschema()
        if js is None:
            return  # jsonschema not installed; skip
        schema = {
            "type": "object",
            "properties": {
                "intent": {"type": "string"},
                "confidence": {"type": "number"},
            },
            "required": ["intent", "confidence"],
        }
        verify = _auto_schema_verifier(schema, js)
        # Missing required "confidence" field.
        evidence = _empty_evidence(parsed={"intent": "refund"})
        result = asyncio.run(verify(evidence, _fake_ctx()))
        assert result.valid is False
        assert "does not match output_schema" in result.reason

    def test_missing_parsed_payload_returns_invalid_with_actionable_reason(self):
        js = _try_import_jsonschema()
        schema = {"type": "object"}
        verify = _auto_schema_verifier(schema, js)
        evidence = _empty_evidence(parsed=None)
        result = asyncio.run(verify(evidence, _fake_ctx()))
        assert result.valid is False
        assert "parsed_payload" in result.reason
        assert "ctx.runtime.parse_structured" in result.reason

    def test_jsonschema_missing_skips_validation_with_explanation(self):
        schema = {"type": "object"}
        # Pass None for the jsonschema module — simulates the package
        # not being installed.
        verify = _auto_schema_verifier(schema, None)
        evidence = _empty_evidence(parsed={"a": 1})
        result = asyncio.run(verify(evidence, _fake_ctx()))
        assert result.valid is True
        assert "jsonschema" in result.reason
        assert "skipped" in result.reason

    def test_extra_fields_rejected(self):
        """additionalProperties=false rejects unknown keys."""
        js = _try_import_jsonschema()
        if js is None:
            return
        schema = {
            "type": "object",
            "properties": {"a": {"type": "number"}},
            "additionalProperties": False,
        }
        verify = _auto_schema_verifier(schema, js)
        evidence = _empty_evidence(parsed={"a": 1, "extra_field": "nope"})
        result = asyncio.run(verify(evidence, _fake_ctx()))
        assert result.valid is False