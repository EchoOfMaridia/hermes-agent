"""Tests for the plugin entrypoint (Step 7-10 of the build plan).

What we verify:

register() surface registration:
- CLI subcommand "workflow" is registered with the expected help/description
- Eight slash commands are registered (run, list, inspect, status,
  snapshot, cancel, save, expand)
- Model tool "call_workflow" is registered with the JSON schema
- Gateway hook "pre_gateway_dispatch" is registered with the right signature

Model tool behavior:
- call_workflow with mode="library" loads from the library and submits
- call_workflow with mode="ad-hoc" returns the v0.2.0 stub error
- call_workflow with unknown workflow returns a structured error dict

Gateway reaction handler:
- "workflow: foo" rewrites event.text to the v0.2.0 stub guidance
- "/workflow run bar" passes through
- Plain text with no workflow keyword passes through

Library:
- save/load round-trip works
- list_names returns the saved entries
- loading an unknown name raises KeyError
"""

from __future__ import annotations

import asyncio
import json
import textwrap
from pathlib import Path

import pytest

from plugins.hermes_workflow.library import Library
from plugins.hermes_workflow.runtime_factory import build_runtime
from plugins.hermes_workflow.tests._plugin_context import MockPluginContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _register_plugin(tmp_path) -> MockPluginContext:
    """Build a runtime with a temp journal root and register the plugin
    against a fresh mock context. Patches default_journal_root so the
    runtime inside the entrypoint writes to tmp_path."""
    from plugins.hermes_workflow import register as plugin_register
    from plugins.hermes_workflow import runtime_factory

    runtime_factory.default_journal_root = lambda: tmp_path / "wf"
    ctx = MockPluginContext()
    plugin_register(ctx)
    return ctx


# ---------------------------------------------------------------------------
# register() surface registration
# ---------------------------------------------------------------------------

class TestRegisterSurfaces:
    def test_cli_command_registered(self, tmp_path):
        ctx = _register_plugin(tmp_path)
        assert "workflow" in ctx.cli_commands
        assert "hermes_workflow" in ctx.cli_commands["workflow"]["description"]

    def test_slash_command_registered_as_single_workflow_tokenized(
        self, tmp_path,
    ):
        """The plugin registers a SINGLE slash command named
        ``workflow`` whose handler tokenizes the first word of
        ``arg`` as a subcommand.

        Rationale (see plugins/hermes_workflow/slash.py: docstring
        at top of file):

            "Hermes's slash-command protocol parses a user-typed
            ``/foo bar baz`` into ``name='foo'``, ``arg='bar baz'``
            ... and the gateway then invokes
            ``plugin_handler(user_args)``. That means the plugin's
            natural unit of registration is ONE slash command per
            *namespace*, with the subcommand selected by the first
            token of arg."

        Earlier drafts of this test (the original author commit
        pre-tokenization) expected 8 separate ``workflow-*``
        registrations — that contract never matched the shipped
        host protocol. This test pins what the plugin actually
        does: one ``/workflow`` namespace command, with the
        eight documented subcommands routed inside the handler.
        See also ``test_eight_workflow_subcommands_routable_via_tokenized_dispatch``
        below, which pins each subcommand's behavior.
        """
        ctx = _register_plugin(tmp_path)
        # Exactly one slash command: the ``workflow`` namespace.
        assert set(ctx.slash_commands.keys()) == {"workflow"}
        cmd = ctx.slash_commands["workflow"]
        assert cmd["description"], "workflow command missing description"
        assert cmd["handler"] is not None
        assert callable(cmd["handler"])

    def test_eight_workflow_subcommands_routable_via_tokenized_dispatch(
        self, tmp_path,
    ):
        """The single ``/workflow`` command dispatches each of the
        eight documented subcommands (run, list, inspect, status,
        snapshot, cancel, save, expand) to its matching handler.

        We invoke the registered handler directly with raw arg
        strings and inspect what comes back. Subcommands that
        forward to the CLI machinery return whatever the CLI
        produced for that subcommand (often a "not found" error
        because there's no real file ``foo`` in the test env) —
        what matters here is the **routing** contract: every
        subcommand must reach its branch (no ``unknown workflow
        subcommand`` exception-in-string), no invocation may
        raise, and the response must be a string (not ``None``).
        Subcommands with built-in guidance (save, expand) get
        substring assertions on top of that routing contract.
        """
        ctx = _register_plugin(tmp_path)
        handler = ctx.slash_commands["workflow"]["handler"]

        # Routing-only cases: just confirm the subcommand reached
        # the right branch. We don't pin help strings because the
        # CLI subprocess path is sensitive to test-env state.
        routing_cases = [
            "run foo",
            "list",
            "inspect foo",
            "status",
            "snapshot foo --tier 1",
            "expand foo",
        ]
        for raw in routing_cases:
            response = handler(raw)
            assert response is not None, (
                f"/workflow {raw!r} returned None — dispatcher likely "
                f"missing the subcommand branch"
            )
            assert isinstance(response, str), (
                f"/workflow {raw!r} returned non-string response: "
                f"{type(response).__name__}"
            )
            assert "unknown workflow subcommand" not in response, (
                f"/workflow {raw!r} was not routed: {response[:200]!r}"
            )

        # Bespoke-stub cases: the dispatcher returns well-known
        # guidance strings that pin the save/expand implementations.
        assert "usage: /workflow cancel" in handler("cancel"), (
            "cancel must emit a usage string when called with no run_id"
        )
        assert "usage: /workflow save" in handler("save"), (
            "save must emit a usage string when called with no name"
        )
        assert handler("save x y z").startswith(
            "save is a v0.2.0 feature"
        ) or "usage:" in handler("save x y z"), (
            "save must reject >1 arg with either a usage hint or "
            "the v0.2.0 stub guidance"
        )

    def test_slash_commands_have_descriptions(self, tmp_path):
        ctx = _register_plugin(tmp_path)
        for name, cmd in ctx.slash_commands.items():
            assert cmd["description"], f"{name} missing description"
            assert cmd["handler"] is not None

    def test_model_tool_call_workflow_registered(self, tmp_path):
        ctx = _register_plugin(tmp_path)
        assert "call_workflow" in ctx.tools
        tool = ctx.tools["call_workflow"]
        assert tool["toolset"] == "workflow"
        assert tool["is_async"] is True
        schema = tool["schema"]
        assert "name" in schema["properties"]
        assert "inputs" in schema["properties"]
        assert "mode" in schema["properties"]

    def test_gateway_hook_registered(self, tmp_path):
        ctx = _register_plugin(tmp_path)
        assert "pre_gateway_dispatch" in ctx.hooks


# ---------------------------------------------------------------------------
# Model tool behavior
# ---------------------------------------------------------------------------

class TestModelTool:
    def _setup_library(self, tmp_path) -> Path:
        lib_dir = tmp_path / "lib"
        lib_dir.mkdir()
        script = lib_dir / "demo.py"
        script.write_text(textwrap.dedent("""
            from plugins.hermes_workflow import step, workflow, Evidence

            def _empty_ev():
                return Evidence(files_changed=(), commands_run=(),
                                exit_codes=(), tests_run=0,
                                tests_passed=0, duration_seconds=0.0)

            @step(name="only")
            async def only(ctx) -> Evidence: return _empty_ev()

            @workflow(name="demo_wf")
            async def demo_wf(ctx) -> dict:
                await only(ctx)
                return {}
        """))
        (lib_dir / "library.json").write_text(json.dumps({
            "version": 1,
            "entries": [{"name": "demo",
                          "description": "demo wf",
                          "path": "demo.py",
                          "created_at": "2026-06-27T00:00:00Z"}],
        }))
        return lib_dir

    def test_library_mode_loads_and_submits(self, tmp_path):
        from plugins.hermes_workflow.tool import build_tool_handler
        import plugins.hermes_workflow.tool as tool_mod

        lib_dir = self._setup_library(tmp_path)
        # The tool handler imported `default_journal_root` at import time;
        # patch the binding in `tool` module's namespace.
        original = tool_mod.default_journal_root
        tool_mod.default_journal_root = lambda: lib_dir
        try:
            runtime = build_runtime(journal_root=tmp_path / "wf")
            handler = build_tool_handler(runtime)
            result = asyncio.run(handler(name="demo", inputs={},
                                           mode="library"))
        finally:
            tool_mod.default_journal_root = original
        assert "run_id" in result
        assert result["status"] == "submitted"

    def test_ad_hoc_mode_returns_v020_stub(self, tmp_path):
        from plugins.hermes_workflow.tool import build_tool_handler
        runtime = build_runtime(journal_root=tmp_path / "wf")
        handler = build_tool_handler(runtime)
        result = asyncio.run(handler(name="summarize the auth code",
                                       inputs={"path": "auth.py"},
                                       mode="ad-hoc"))
        assert "error" in result
        assert "v0.2.0" in result["error"]

    def test_unknown_workflow_returns_structured_error(self, tmp_path):
        from plugins.hermes_workflow.tool import build_tool_handler
        runtime = build_runtime(journal_root=tmp_path / "wf")
        handler = build_tool_handler(runtime)
        result = asyncio.run(handler(name="does_not_exist", inputs={},
                                       mode="library"))
        assert "error" in result
        assert "available" in result


# ---------------------------------------------------------------------------
# Gateway reaction handler
# ---------------------------------------------------------------------------

class TestGatewayHandler:
    def test_workflow_colon_prefix_rewrites_event(self, tmp_path):
        from plugins.hermes_workflow.gateway_handler import (
            build_gateway_handler,
        )
        runtime = build_runtime(journal_root=tmp_path / "wf")
        # Without a ScriptAuthor, the handler returns a stub message
        # explaining the requirement.
        handler = build_gateway_handler(runtime)
        event = _SimpleMessageEvent("workflow: review this PR")
        result = asyncio.run(handler(event, {}))
        assert result is not None
        assert result["action"] == "rewrite"
        assert "ScriptAuthor" in result["text"]

    def test_workflow_colon_prefix_with_script_author(self, tmp_path):
        """When ScriptAuthor is provided, the handler delegates to it."""
        import textwrap as tw
        from plugins.hermes_workflow.gateway_handler import (
            build_gateway_handler,
        )
        from plugins.hermes_workflow.script_author import ScriptAuthor

        valid_script = tw.dedent("""
            from plugins.hermes_workflow import step, workflow, Evidence
            def _ev():
                return Evidence(files_changed=(), commands_run=(),
                               exit_codes=(), tests_run=0, tests_passed=0,
                               duration_seconds=0.0)
            @step(name="only")
            async def only(ctx) -> Evidence: return _ev()
            @workflow(name="x")
            async def x(ctx) -> dict:
                await only(ctx)
                return {}
        """)

        class _CannedLlm:
            async def acomplete_structured(self, **kw):
                class _R:
                    parsed = {"name": "x", "description": "x",
                              "script": valid_script,
                              "step_names": ["only"]}
                return _R()

        llm = _CannedLlm()
        runtime = build_runtime(journal_root=tmp_path / "wf")
        author = ScriptAuthor(llm=llm, library_root=tmp_path / "wf" / "library")
        handler = build_gateway_handler(runtime, script_author=author)
        event = _SimpleMessageEvent("workflow: do something")
        result = asyncio.run(handler(event, {}))
        assert result is not None
        assert result["action"] == "rewrite"
        # ScriptAuthor path produces a status message with run_id.
        assert "run_id" in result["text"] or "error_stage" in result["text"]

    def test_slash_command_prefix_passes_through(self, tmp_path):
        from plugins.hermes_workflow.gateway_handler import (
            build_gateway_handler,
        )
        runtime = build_runtime(journal_root=tmp_path / "wf")
        handler = build_gateway_handler(runtime)
        event = _SimpleMessageEvent("/workflow run script.py")
        result = asyncio.run(handler(event, {}))
        assert result is None or result.get("action") == "allow"

    def test_plain_text_passes_through(self, tmp_path):
        from plugins.hermes_workflow.gateway_handler import (
            build_gateway_handler,
        )
        runtime = build_runtime(journal_root=tmp_path / "wf")
        handler = build_gateway_handler(runtime)
        event = _SimpleMessageEvent("hello there")
        result = asyncio.run(handler(event, {}))
        assert result is None

    def test_empty_message_passes_through(self, tmp_path):
        from plugins.hermes_workflow.gateway_handler import (
            build_gateway_handler,
        )
        runtime = build_runtime(journal_root=tmp_path / "wf")
        handler = build_gateway_handler(runtime)
        event = _SimpleMessageEvent("")
        result = asyncio.run(handler(event, {}))
        assert result is None


class _SimpleMessageEvent:
    """Minimal mock for hermes's gateway MessageEvent."""

    def __init__(self, text: str) -> None:
        self.text = text


# ---------------------------------------------------------------------------
# Library
# ---------------------------------------------------------------------------

class TestLibrary:
    def test_save_and_load(self, tmp_path):
        lib = Library(tmp_path / "lib")
        script = tmp_path / "demo.py"
        script.write_text(textwrap.dedent("""
            from plugins.hermes_workflow import step, workflow, Evidence

            def _empty_ev():
                return Evidence(files_changed=(), commands_run=(),
                                exit_codes=(), tests_run=0,
                                tests_passed=0, duration_seconds=0.0)

            @step(name="only")
            async def only(ctx) -> Evidence: return _empty_ev()

            @workflow(name="my_wf")
            async def my_wf(ctx) -> dict:
                await only(ctx)
                return {}
        """))
        result = lib.save("my_wf", script, description="test wf")
        assert result["name"] == "my_wf"
        assert lib.has("my_wf")
        loaded = lib.load("my_wf")
        assert callable(loaded)
        assert hasattr(loaded, "__workflow_meta__")

    def test_list_names(self, tmp_path):
        lib = Library(tmp_path / "lib")
        assert lib.list_names() == []
        script = tmp_path / "demo.py"
        script.write_text("from plugins.hermes_workflow import workflow\n")
        lib.save("a", script)
        lib.save("b", script)
        assert set(lib.list_names()) == {"a", "b"}

    def test_load_unknown_raises_keyerror(self, tmp_path):
        lib = Library(tmp_path / "lib")
        with pytest.raises(KeyError):
            lib.load("nope")

    def test_load_missing_script_raises_filenotfound(self, tmp_path):
        lib = Library(tmp_path / "lib")
        lib.root.mkdir(parents=True, exist_ok=True)
        lib.manifest_path.write_text(json.dumps({
            "version": 1,
            "entries": [{"name": "broken",
                          "description": "x",
                          "path": "missing.py",
                          "created_at": "2026-06-27T00:00:00Z"}],
        }))
        with pytest.raises(FileNotFoundError):
            lib.load("broken")
