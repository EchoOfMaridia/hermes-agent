"""Tests for WorkflowRuntime.parse_structured pass-through."""

from __future__ import annotations

from pathlib import Path

import pytest

from plugins.hermes_workflow.agent_bridge import AgentResponse
from plugins.hermes_workflow.dsl.types import StructuredOutputError
from plugins.hermes_workflow.runtime import WorkflowRuntime


def _rt() -> WorkflowRuntime:
    return WorkflowRuntime(
        journal_root=Path("/tmp/_test_rt_parse"),
        default_max_concurrent=2,
    )


class TestRuntimeParseStructured:
    def test_returns_parsed_when_bridge_already_parsed(self):
        rt = _rt()
        resp = AgentResponse(
            text='{"a": 1}',
            parsed={"a": 1},
            content_type="json",
        )
        assert rt.parse_structured(resp) == {"a": 1}

    def test_parses_text_when_no_parsed_field(self):
        rt = _rt()
        resp = AgentResponse(text='{"a": 2}', parsed=None, content_type="text")
        assert rt.parse_structured(resp) == {"a": 2}

    def test_validates_against_caller_supplied_schema(self):
        rt = _rt()
        resp = AgentResponse(
            text='{"a": 1}',
            parsed={"a": 1},
            content_type="json",
        )
        # Re-validate with a stricter schema (string-typed a) -> should fail.
        with pytest.raises(StructuredOutputError):
            rt.parse_structured(
                resp,
                schema={
                    "type": "object",
                    "properties": {"a": {"type": "string"}},
                    "required": ["a"],
                },
            )

    def test_returns_none_for_unparseable_text(self):
        rt = _rt()
        resp = AgentResponse(text="just prose, no JSON", parsed=None)
        assert rt.parse_structured(resp) is None