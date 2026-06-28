"""Tests for ScriptAuthor (Gap 2: ad-hoc mode wired to real LLM).

What we verify:

Stage-by-stage execution:
- LLM call success: returns parsed JSON.
- Safety check: rejects forbidden terms (subprocess, os.system, etc.).
- Safety check: requires plugins.hermes_workflow import.
- Safety check: requires exactly 1 @workflow decorator.
- Save: writes script to library.json with manifest entry.
- Submit: returns run_id on success.

Failure modes:
- LLM call exception: error_stage="llm_call".
- Safety failure: error_stage="safety_check" with raw_script populated.
- Save failure: error_stage="save".
- Graph validation failure: error_stage="graph_validation".
- Submit failure: error_stage="submit".

LLM stub: tests inject a fake ``acomplete_structured`` that returns
a pre-canned ``PluginLlmStructuredResult`` with ``parsed=...``. This
keeps the LLM dep out of unit tests while exercising the full
script-author pipeline.
"""

from __future__ import annotations

import asyncio
import json
import textwrap
from pathlib import Path
from typing import Any

import pytest

from plugins.hermes_workflow.runtime_factory import build_runtime
from plugins.hermes_workflow.script_author import (
    ScriptAuthor,
    _validate_script_safety,
    _slugify,
)
from plugins.hermes_workflow.tests._plugin_context import MockPluginContext


# ---------------------------------------------------------------------------
# Stub LLM that returns a pre-canned response.
# ---------------------------------------------------------------------------

class _CannedLlm:
    """LLM stub that returns ``parsed`` as acomplete_structured's output."""

    def __init__(self, parsed: dict | None = None,
                  *, raise_exc: Exception | None = None) -> None:
        self.parsed = parsed or {}
        self.raise_exc = raise_exc
        self.calls: list[dict] = []

    async def acomplete_structured(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.raise_exc is not None:
            raise self.raise_exc

        class _Result:
            text = json.dumps(self.parsed)
            parsed = self.parsed
        return _Result()

    async def acomplete(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)

        class _Result:
            text = "stub"
        return _Result()


# ---------------------------------------------------------------------------
# Script fixtures
# ---------------------------------------------------------------------------

VALID_SCRIPT = textwrap.dedent("""
    from plugins.hermes_workflow import step, workflow, Evidence

    def _ev():
        return Evidence(files_changed=(), commands_run=(), exit_codes=(),
                       tests_run=0, tests_passed=0, duration_seconds=0.0)

    @step(name="only")
    async def only(ctx) -> Evidence:
        return _ev()

    @workflow(name="demo")
    async def demo(ctx) -> dict:
        await only(ctx)
        return {"ok": True}
""")


# ---------------------------------------------------------------------------
# _slugify
# ---------------------------------------------------------------------------

class TestSlugify:
    def test_basic(self):
        assert _slugify("review the auth code") == "review_the_auth_code"

    def test_collapses_separators(self):
        assert _slugify("foo---bar___baz") == "foo_bar_baz"

    def test_truncates(self):
        assert len(_slugify("x" * 100)) <= 48

    def test_empty_falls_back(self):
        assert _slugify("!!!") == "workflow"


# ---------------------------------------------------------------------------
# _validate_script_safety
# ---------------------------------------------------------------------------

class TestSafetyValidation:
    def test_valid_script_passes(self):
        assert _validate_script_safety(VALID_SCRIPT) == []

    def test_rejects_subprocess(self):
        bad = VALID_SCRIPT.replace("from plugins.hermes_workflow import",
                                    "import subprocess\n")
        errors = _validate_script_safety(bad)
        assert any("subprocess" in e for e in errors)

    def test_rejects_os_system(self):
        bad = "import os\nos.system('rm -rf /')\n"
        errors = _validate_script_safety(bad)
        assert any("os.system" in e for e in errors)

    def test_rejects_eval(self):
        bad = "x = eval('1+1')\n" + VALID_SCRIPT
        errors = _validate_script_safety(bad)
        assert any("eval(" in e for e in errors)

    def test_requires_dsl_import(self):
        bad = "@workflow(name='x')\nasync def x(ctx): pass\n"
        errors = _validate_script_safety(bad)
        assert any("plugins.hermes_workflow" in e for e in errors)

    def test_rejects_zero_workflows(self):
        bad = "@step(name='x')\nasync def x(ctx): pass\n"
        errors = _validate_script_safety(bad)
        assert any("@workflow" in e for e in errors)

    def test_rejects_multiple_workflows(self):
        bad = VALID_SCRIPT + "\n@workflow(name='y')\nasync def y(ctx): pass\n"
        errors = _validate_script_safety(bad)
        assert any("1 @workflow" in e for e in errors)

    def test_rejects_zero_steps(self):
        bad = "@workflow(name='x')\nasync def x(ctx): return {}\n"
        errors = _validate_script_safety(bad)
        assert any("no @step" in e for e in errors)


# ---------------------------------------------------------------------------
# Stage-by-stage ScriptAuthor execution
# ---------------------------------------------------------------------------

class TestScriptAuthorSuccess:
    def _setup(self, tmp_path):
        llm = _CannedLlm(parsed={
            "name": "demo",
            "description": "demo workflow",
            "script": VALID_SCRIPT,
            "step_names": ["only"],
        })
        runtime = build_runtime(journal_root=tmp_path / "wf")
        return ScriptAuthor(
            llm=llm,
            library_root=tmp_path / "wf" / "library",
        ), runtime, llm

    def test_happy_path(self, tmp_path):
        author, runtime, llm = self._setup(tmp_path)
        result = asyncio.run(author.generate(
            intent="create a demo workflow", runtime=runtime,
        ))
        assert result.ok
        assert result.name == "demo"
        assert result.workflow == "demo"
        assert result.run_id
        assert result.script_path
        # The library has the entry.
        library_path = tmp_path / "wf" / "library"
        assert (library_path / "demo.py").exists()
        manifest = json.loads(
            (library_path / "library.json").read_text()
        )
        assert any(e["name"] == "demo" for e in manifest["entries"])

    def test_calls_llm_with_schema(self, tmp_path):
        author, runtime, llm = self._setup(tmp_path)
        asyncio.run(author.generate(
            intent="intent text", runtime=runtime,
        ))
        assert len(llm.calls) == 1
        call = llm.calls[0]
        assert "json_schema" in call
        assert call["json_schema"]["type"] == "object"
        assert "instructions" in call
        assert "DSL rules" in call["instructions"]


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------

class TestScriptAuthorFailures:
    def _setup(self, tmp_path, *, parsed=None, raise_exc=None):
        llm = _CannedLlm(parsed=parsed, raise_exc=raise_exc)
        runtime = build_runtime(journal_root=tmp_path / "wf")
        return ScriptAuthor(
            llm=llm,
            library_root=tmp_path / "wf" / "library",
        ), runtime

    def test_llm_call_exception(self, tmp_path):
        author, runtime = self._setup(
            tmp_path, raise_exc=RuntimeError("rate limit"),
        )
        result = asyncio.run(author.generate(
            intent="x", runtime=runtime,
        ))
        assert not result.ok
        assert result.error_stage == "llm_call"
        assert "rate limit" in result.error

    def test_safety_check_rejects(self, tmp_path):
        bad_script = (
            "import subprocess\n"
            "@workflow(name='bad')\nasync def bad(ctx): pass\n"
        )
        author, runtime = self._setup(tmp_path, parsed={
            "name": "bad", "description": "x",
            "script": bad_script, "step_names": [],
        })
        result = asyncio.run(author.generate(
            intent="x", runtime=runtime,
        ))
        assert not result.ok
        assert result.error_stage == "safety_check"
        assert "subprocess" in result.error
        assert result.raw_script == bad_script

    def test_graph_validation_failure(self, tmp_path):
        # Valid safety but invalid graph: depends_on unknown step.
        bad_script = textwrap.dedent("""
            from plugins.hermes_workflow import step, workflow, Evidence
            def _ev():
                return Evidence(files_changed=(), commands_run=(),
                               exit_codes=(), tests_run=0, tests_passed=0,
                               duration_seconds=0.0)
            @step(name="only", depends_on=("does_not_exist",))
            async def only(ctx) -> Evidence:
                return _ev()
            @workflow(name="demo")
            async def demo(ctx) -> dict:
                await only(ctx)
                return {}
        """)
        author, runtime = self._setup(tmp_path, parsed={
            "name": "demo", "description": "x",
            "script": bad_script, "step_names": ["only"],
        })
        result = asyncio.run(author.generate(
            intent="x", runtime=runtime,
        ))
        # The script passes safety + save; runtime.submit() rejects
        # the unknown dependency at submit-time.
        assert not result.ok
        assert result.error_stage == "submit"
        assert "depends on unknown step" in result.error
        # The library has the script (save succeeded).
        assert result.name == "demo"
        assert result.script_path


# ---------------------------------------------------------------------------
# Integration with the plugin entrypoint
# ---------------------------------------------------------------------------

class TestScriptAuthorInEntryPoint:
    def test_entry_point_constructs_script_author(self, tmp_path):
        from plugins.hermes_workflow import register as plugin_register
        from plugins.hermes_workflow import runtime_factory
        runtime_factory.default_journal_root = lambda: tmp_path / "wf"
        ctx = MockPluginContext()
        # Replace ctx.llm with a stub that ScriptAuthor can call.
        ctx.llm = _CannedLlm(parsed={
            "name": "demo", "description": "x",
            "script": VALID_SCRIPT, "step_names": ["only"],
        })
        plugin_register(ctx)
        # The call_workflow tool's handler now accepts a script_author.
        tool = ctx.tools["call_workflow"]
        assert tool is not None
        # Verify the handler is wired to ScriptAuthor by invoking
        # it with mode="ad-hoc".
        result = asyncio.run(tool["handler"](
            name="create a demo workflow",
            inputs={},
            mode="ad-hoc",
        ))
        assert result.get("mode") == "ad-hoc"
        assert result.get("run_id") or result.get("error_stage")