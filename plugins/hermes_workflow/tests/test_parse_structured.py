"""Tests for parse_structured — TPipe extractJson minimum port."""

from __future__ import annotations

import pytest

from plugins.hermes_workflow.dsl.types import StructuredOutputError
from plugins.hermes_workflow.structured_output import parse_structured


# ---------------------------------------------------------------------------
# Fast path: clean JSON
# ---------------------------------------------------------------------------


class TestParseCleanJson:
    def test_parse_clean_json_object(self):
        result = parse_structured('{"a": 1, "b": "two"}')
        assert result == {"a": 1, "b": "two"}

    def test_parse_clean_json_array(self):
        result = parse_structured("[1, 2, 3]")
        assert result == [1, 2, 3]

    def test_parse_clean_nested(self):
        result = parse_structured('{"items": [{"id": 1}, {"id": 2}]}')
        assert result == {"items": [{"id": 1}, {"id": 2}]}

    def test_parse_empty_string_returns_none(self):
        assert parse_structured("") is None

    def test_parse_whitespace_only_returns_none(self):
        assert parse_structured("   \n\t  ") is None


# ---------------------------------------------------------------------------
# Code fence stripping
# ---------------------------------------------------------------------------


class TestParseStripsFences:
    def test_parse_strips_json_fence(self):
        text = '```json\n{"a": 1}\n```'
        assert parse_structured(text) == {"a": 1}

    def test_parse_strips_bare_fence(self):
        text = '```\n{"a": 1}\n```'
        assert parse_structured(text) == {"a": 1}

    def test_parse_strips_fence_with_language(self):
        text = '```python\n{"a": 1}\n```'
        assert parse_structured(text) == {"a": 1}

    def test_parse_strips_think_tags(self):
        text = '<think>The user wants a refund analysis.</think>{"intent": "refund"}'
        assert parse_structured(text) == {"intent": "refund"}


# ---------------------------------------------------------------------------
# Brace-walking recovery
# ---------------------------------------------------------------------------


class TestParseRecoversFromProse:
    def test_recovers_prose_wrapped_object(self):
        text = 'Here is the result: {"score": 0.92, "label": "positive"} — done.'
        assert parse_structured(text) == {"score": 0.92, "label": "positive"}

    def test_recovers_prose_wrapped_array(self):
        text = "The top three items are: [1, 2, 3]"
        assert parse_structured(text) == [1, 2, 3]

    def test_returns_none_for_pure_prose(self):
        assert parse_structured(
            "This is just text, no JSON here."
        ) is None

    def test_handles_truncated_json_without_raising(self):
        text = '{"a": 1, "b": 2'  # no closing brace
        try:
            result = parse_structured(text)
            assert result is None or isinstance(result, dict)
        except Exception as exc:
            pytest.fail(
                f"parse_structured should not raise on truncated JSON: {exc}"
            )


# ---------------------------------------------------------------------------
# Schema validation (when jsonschema is installed)
# ---------------------------------------------------------------------------


class TestParseSchemaValidation:
    def test_validates_against_schema_when_provided(self):
        schema = {
            "type": "object",
            "properties": {"a": {"type": "number"}},
            "required": ["a"],
        }
        result = parse_structured('{"a": 1}', schema=schema)
        assert result == {"a": 1}

    def test_validation_failure_raises(self):
        from plugins.hermes_workflow.dsl.types import StructuredOutputError
        schema = {
            "type": "object",
            "properties": {"a": {"type": "number"}},
            "required": ["a"],
        }
        with pytest.raises(StructuredOutputError):
            parse_structured('{"a": "not a number"}', schema=schema)

    def test_no_schema_returns_object_without_validation(self):
        # Without schema, JSON-only parsing — type mismatches are
        # silently accepted as raw values.
        result = parse_structured('{"a": "string instead of number"}')
        assert result == {"a": "string instead of number"}

    def test_validation_error_carries_validation_path(self):
        from plugins.hermes_workflow.dsl.types import StructuredOutputError
        schema = {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"score": {"type": "number"}},
                    },
                },
            },
        }
        with pytest.raises(StructuredOutputError) as exc_info:
            parse_structured(
                '{"items": [{"score": "bad"}]}',
                schema=schema,
            )
        assert exc_info.value.validation_path  # non-empty list of path elements


# ---------------------------------------------------------------------------
# Bare-word reconstruction (last resort)
# ---------------------------------------------------------------------------


class TestParseBareWordRecovery:
    def test_reconstructs_simple_object(self):
        text = 'Response: "intent": "refund", "confidence": 0.92'
        result = parse_structured(text)
        assert result == {"intent": "refund", "confidence": 0.92}