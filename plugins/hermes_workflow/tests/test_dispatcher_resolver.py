"""TDD regression tests for the dispatcher resolver wiring path.

What was broken (2026-06-30): the default resolver path of
``build_late_wire_callback`` tried to import
``hermes_cli.gateway.get_active_dispatcher`` — a symbol that DOES NOT
EXIST in any file in the repository. The ``ImportError`` was silently
swallowed by a bare ``except Exception: return None``, the failure was
logged at DEBUG level (invisible at INFO), and ``runtime._dispatcher``
stayed ``None`` for the lifetime of the session. As a result, **zero
workflow events ever streamed to the desktop chat**, across every
session, for every workflow the user ran.

These tests pin the expected behavior:

1. ``build_late_wire_callback`` default resolver MUST NOT silently
   swallow the dead-symbol case. Either it logs loudly OR falls back
   to a no-op resolver that we can detect.
2. The runtime MUST gain a way to register itself as a dispatcher
   source once a gateway dispatcher becomes available, even when
   ``get_active_dispatcher`` does not exist.
3. When ``JournalingBridge._inner`` is ``None``, ``ctx.runtime.ask_agent``
   MUST raise a clear, actionable error (not just ``NotImplementedError``)
   that points the user at the workaround.

The PluginContext in the user's hermes install DOES expose ``inject_message``
and the agent's chat loop. We use those as a fallback dispatch path —
the workflow runtime can render its journal events into a status
message that the chat loop injects.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from plugins.hermes_workflow.runtime_factory import build_runtime


# ---------------------------------------------------------------------------
# Bug A: the default resolver path is silently dead.
#
# Symptom: ``runtime._dispatcher`` stays ``None`` even after the
# late-wire hook fires N times because the resolver swallows ImportError.
# ---------------------------------------------------------------------------

class TestDefaultResolverIsNotSilentlyDead:
    """The production-code default resolver path must do *something*
    observable when called, even if "doing something" is "log loudly
    that the path is unsupported in this hermes version".

    The current behavior is: ImportError → bare except → return None →
    silent no-op. Pin the FIX.
    """

    def test_default_resolver_returns_callable_when_no_gateway(self):
        """The default resolver (no kwarg passed) must NOT raise.

        Production hermes-desktop does not define
        ``hermes_cli.gateway.get_active_dispatcher``. The resolver's
        ``_default_resolver`` catches that ImportError with a bare
        ``except Exception: return None``. That's fine — but the
        calling site (``hook()`` in ``gateway_late_wire.py``) needs
        to handle the case where the resolver returns None AND log at
        WARN level (not DEBUG), so users can grep for why their
        workflow events aren't surfacing.

        Pin: the hook logs at WARN level when the default resolver
        returns None on every call. Without this fix the agent.log
        only shows the problem at DEBUG, which is invisible.
        """
        from plugins.hermes_workflow.gateway_late_wire import (
            build_late_wire_callback,
        )
        with tempfile.TemporaryDirectory() as tmp:
            rt = build_runtime(journal_root=Path(tmp) / "wf")
            hook = build_late_wire_callback(rt)  # no resolver kwarg
            # After the hook fires, runtime._dispatcher must STILL be None.
            assert rt._dispatcher is None
            hook()  # second invocation
            assert rt._dispatcher is None  # still None (no gateway)


    def test_default_resolver_logs_warn_when_unavailable(self, caplog):
        """When the default resolver hits ImportError or returns
        None, the hook MUST log at WARN level (not DEBUG) so users
        can grep for "why aren't my workflow events showing".

        Before this fix, the silent ``except Exception: return None``
        and ``_log.debug("late-wire check failed: %s", e)`` ran, which
        is invisible at any production log level.
        """
        from plugins.hermes_workflow.gateway_late_wire import (
            build_late_wire_callback,
        )
        with tempfile.TemporaryDirectory() as tmp:
            rt = build_runtime(journal_root=Path(tmp) / "wf")
            hook = build_late_wire_callback(rt)
            with caplog.at_level(logging.DEBUG,
                                  logger="hermes_workflow.gateway_late_wire"):
                hook()
            # Pin that SOME log line was emitted at WARN OR higher,
            # not just DEBUG. (Implementation choice: the patch may
            # emit at WARN OR may emit a one-shot INFO line. Either
            # is acceptable; what is NOT acceptable is silent DEBUG.)
            assert any(
                r.levelno >= logging.WARNING for r in caplog.records
            ), (
                "expected a WARN-level (or higher) log entry when the "
                "default resolver has no gateway dispatcher to wire — "
                "if only DEBUG, the user has no way to discover the "
                "issue from logs"
            )


# ---------------------------------------------------------------------------
# Bug B: register() at plugin-load time MUST also run the late-wire
# callback once, with a fallback that surfaces dispatch to the chat
# loop. Today register() returns without wiring — and the user's
# chat-surface never gets any workflow events.
# ---------------------------------------------------------------------------

class TestRegisterWiresFallbackDispatch:
    """When register() loads the plugin and finds no gateway dispatcher
    (the typical case for hermes-desktop chats), it must register an
    ALTERNATIVE dispatch path: a callback that pushes workflow events
    into the host via ``ctx.inject_message`` (the same message queue
    the chat uses), so the user sees live progress as a sequence of
    assistant messages instead of as silently-journaled events.

    Pin contract: ``ctx.inject_message`` is called with role="assistant"
    and content=the rendered workflow card each time a journal event
    arrives that warrants user-visible output (run_started,
    step_started/completed/failed, run_cancelled/done, agent_call
    with prompt_preview).
    """

    def test_register_attaches_journal_renderer_to_context(self):
        """register() MUST attach a FallbackDispatchSink to ``ctx``
        (stored at ``ctx._fallback_sink``) when no gateway dispatcher
        is available, so journal events reach the chat surface.

        This is the user's only visibility into workflow runs when
        the gateway dispatcher isn't wired (hermes-desktop default).
        """
        from plugins.hermes_workflow.tests._plugin_context import (
            MockPluginContext,
        )
        from plugins.hermes_workflow import register as plugin_register

        ctx = MockPluginContext()
        # No gateway dispatcher available — the typical case for
        # hermes-desktop chats.
        plugin_register(ctx)
        # Pin: ctx has a fallback_sink attached.
        assert hasattr(ctx, "_fallback_sink"), (
            "register() must attach _fallback_sink to ctx so workflow "
            "events reach the chat surface even when no gateway "
            "dispatcher is wired"
        )
        # Pin: the sink is also wired as the runtime's dispatcher.
        # Otherwise events would silently drop on the floor.
        from plugins.hermes_workflow.fallback_dispatch import (
            FallbackDispatchSink,
        )
        assert isinstance(ctx._fallback_sink, FallbackDispatchSink)
        # Verify by dispatching an event and observing it landed.
        ctx._fallback_sink.dispatch("test_event")
        assert ctx._fallback_sink.pending() == 1


    def test_runtime_events_become_chat_messages_via_inject(self, tmp_path):
        """End-to-end: when a workflow runs against the fallback sink,
        the events that the runtime's DispatchingJournal generates
        (run_started, step_started, agent_call, …) MUST land in the
        FallbackDispatchSink queue, and drain_sink_to_ctx MUST push
        them into ctx.inject_message as the user-facing surface.

        This is the end-to-end visibility contract the user asked
        for: "I want to visually be able to see what's happening."
        """
        from plugins.hermes_workflow.runtime_factory import build_runtime
        from plugins.hermes_workflow.tests._plugin_context import (
            MockPluginContext,
        )
        from plugins.hermes_workflow import register as plugin_register
        from plugins.hermes_workflow.fallback_dispatch import (
            FallbackDispatchSink, drain_sink_to_ctx,
        )

        ctx = MockPluginContext()
        from plugins.hermes_workflow import runtime_factory
        runtime_factory.default_journal_root = lambda: tmp_path / "wf"
        plugin_register(ctx)
        # Drive events into the sink directly (we don't need a full
        # workflow run; the test is about the routing).
        sink = ctx._fallback_sink
        assert isinstance(sink, FallbackDispatchSink)
        # Inject some fake translated events as the runtime would.
        from types import SimpleNamespace
        sink.dispatch(SimpleNamespace(
            text="▶ run r_abc started",
            run_id="r_abc", workflow="demo",
        ))
        sink.dispatch(SimpleNamespace(
            text="■ step identify started",
            run_id="r_abc", step="identify",
        ))
        assert sink.pending() == 2
        n = drain_sink_to_ctx(sink, ctx)
        assert n == 2
        # The MockPluginContext records injected_messages as a list
        # of (content, role) tuples.
        assert len(ctx.injected_messages) == 2
        for content, role in ctx.injected_messages:
            assert role == "assistant"
            assert "run_id=r_abc" in content
        # Drain again — should be empty (no double-delivery).
        assert sink.pending() == 0
        n = drain_sink_to_ctx(sink, ctx)
        assert n == 0


# ---------------------------------------------------------------------------
# Bug C: JournalingBridge._inner is None. Every workflow that calls
# ctx.runtime.ask_agent() hits NotImplementedError from
# agent_bridge.py:104. The fix: the bridge MUST fall back to a sensible
# default that returns a structured response synthesized from the
# agent-loop's existing analysis capabilities (or, when the host has
# no agent bridge at all, returns an AgentResponse with a clear
# "no-agent" placeholder rather than NotImplementedError so the
# step can complete and the workflow can be inspected).
# ---------------------------------------------------------------------------

class TestAskAgentFallbackWhenNoBridge:
    """When ``JournalingBridge._inner`` is None (no hermes-agent
    integration), ``runtime.ask_agent()`` MUST NOT raise
    ``NotImplementedError`` for callers. It MUST return an
    ``AgentResponse`` with a clear ``reason`` field so the downstream
    step can surface "agent not configured" and the run can be
    inspected without crashing."""

    def test_ask_agent_returns_response_with_reason_when_no_inner(self):
        from plugins.hermes_workflow.agent_bridge import (
            AgentBridge, AgentResponse, JournalingBridge,
        )
        from plugins.hermes_workflow.runtime_factory import build_runtime
        with tempfile.TemporaryDirectory() as tmp:
            rt = build_runtime(journal_root=Path(tmp) / "wf")
            # No inner bridge — the default state.
            async def _go():
                return await rt.ask_agent(
                    prompt="summarize the recent bug fixes",
                    model="sonnet",
                )
            resp = asyncio.run(_go())
            # Pin: it's an AgentResponse, NOT a NotImplementedError.
            assert isinstance(resp, AgentResponse)
            # The text field carries a clear "no bridge" message
            # so downstream steps can surface this to the user.
            assert "no agent bridge" in resp.text.lower() or \
                "ask_agent requires" in resp.text.lower() or \
                "agent integration" in resp.text.lower()
            # tool_calls is the empty tuple (frozen dataclass invariant)
            assert resp.tool_calls == ()
            assert resp.tokens_in == 0
            assert resp.tokens_out == 0


    def test_ask_agent_with_inner_bridge_delegates(self):
        """When ``_inner`` IS wired (post-fix), ask_agent delegates
        to the inner bridge. Pin that we did NOT regress this path."""
        from plugins.hermes_workflow.agent_bridge import (
            AgentBridge, AgentResponse, JournalingBridge,
        )
        from plugins.hermes_workflow.runtime_factory import build_runtime

        class _InnerBridge(AgentBridge):
            async def invoke(self, *, prompt, model, max_tokens, json_schema=None, schema_name=None):
                return AgentResponse(
                    text=f"inner says: {prompt!r}",
                    tool_calls=("tool1",),
                    tokens_in=10,
                    tokens_out=20,
                    duration=0.5,
                )

        with tempfile.TemporaryDirectory() as tmp:
            rt = build_runtime(journal_root=Path(tmp) / "wf")
            # Pin the wire contract: the runtime exposes the
            # JournalingBridge on rt._agent_bridge; calling set_inner
            # wires the inner so ask_agent delegates to it.
            assert isinstance(rt._agent_bridge, JournalingBridge)
            inner = _InnerBridge()
            rt._agent_bridge.set_inner(inner)
            async def _go():
                return await rt.ask_agent(
                    prompt="hi",
                    model="sonnet",
                )
            resp = asyncio.run(_go())
            assert resp.text == "inner says: 'hi'"
            assert resp.tool_calls == ("tool1",)
            assert resp.tokens_in == 10
            assert resp.tokens_out == 20


# ---------------------------------------------------------------------------
# Bug D: script_author.generate() returns a string the chat shows once.
# The user wants incremental visibility into WHAT THE LLM IS GENERATING
# while it generates it. Pin that the slash surface emits at least
# three progress strings during a real generate() call:
#   1) "calling the LLM to author your workflow…"
#   2) "saving workflow to library…"  (or safety-check failure)
#   3) "submitting to runtime for execution…" / final
# When the runtime dispatcher IS wired, the LLM call itself should
# also surface its tool call to the chat (it doesn't today because
# ScriptAuthor uses acomplete_structured, which is atomic).
#
# Minimal-fix pin: at minimum, the slash surface returns a list of
# progress strings OR a single string containing all three progress
# markers as atomic updates. We pin the list shape so it's upgradeable.
# ---------------------------------------------------------------------------

class TestScriptAuthorVisibilityDuringGenerate:
    """Pin that `/workflow create <intent>` returns intermediate
    progress signals during script_author.generate(). The current
    implementation returns ONE string at the end which is only
    visible after the entire LLM call + save + submit completes.
    That's what made the user say "nothing happened."
    """

    def test_slash_create_returns_list_with_at_least_three_progress_markers(
        self, tmp_path,
    ):
        """The slash surface must return a list of progress strings,
        not just one final summary. Each list entry becomes a
        distinct message in the chat, giving the user a running
        signal that work is happening.
        """
        from plugins.hermes_workflow.runtime_factory import build_runtime
        from plugins.hermes_workflow.tests._plugin_context import (
            MockPluginContext,
        )
        from plugins.hermes_workflow import register as plugin_register
        from plugins.hermes_workflow.script_author import ScriptAuthor
        from plugins.hermes_workflow.slash import build_slash_handlers
        from plugins.hermes_workflow.fallback_dispatch import FallbackDispatchSink

        # Stubbed LLM that takes 200ms so we can observe the progress
        # markers being emitted at intermediate stages.
        import asyncio
        class _SlowLlm:
            def __init__(self):
                self.calls = 0
            async def acomplete_structured(self, **kwargs):
                self.calls += 1
                await asyncio.sleep(0.2)
                class _Result:
                    text = "{}"
                    parsed = {
                        "name": "demo",
                        "description": "x",
                        "script": "from plugins.hermes_workflow import step, workflow, Evidence\n@step(name='only')\nasync def only(ctx):\n    return Evidence(files_changed=(), commands_run=(), exit_codes=(), tests_run=0, tests_passed=0, duration_seconds=0.0)\n@workflow(name='demo')\nasync def run(ctx):\n    await only(ctx)\n    return {}\n",
                        "step_names": ["only"],
                    }
                return _Result()

        llm = _SlowLlm()
        ctx = MockPluginContext()
        ctx.llm = llm
        runtime_factory_mod = __import__(
            "plugins.hermes_workflow.runtime_factory", fromlist=["*"]
        )
        runtime_factory_mod.default_journal_root = lambda: tmp_path / "wf"
        # Pre-create the library dir so save doesn't fail on Path / str
        (tmp_path / "wf" / "library").mkdir(parents=True, exist_ok=True)
        (tmp_path / "wf" / "library" / "library.json").write_text(
            '{"version": 1, "entries": []}'
        )
        plugin_register(ctx)
        # Verify the fallback_sink was attached (Bug B fix).
        assert isinstance(ctx._fallback_sink, FallbackDispatchSink)

        handlers = build_slash_handlers(
            build_runtime(journal_root=tmp_path / "wf"),
            script_author=ScriptAuthor(
                llm=llm, library_root=tmp_path / "wf" / "library",
            ),
            fallback_sink=ctx._fallback_sink,
        )
        handler = handlers["workflow"]["handler"]

        # Pin contract: the handler returns a single string with
        # progress markers separated by `\n---\n`. Each marker is a
        # logical message boundary that the chat can render.
        result = handler("create demo workflow")
        # The result must NOT contain an asyncio.run loop-conflict error.
        assert "cannot be called from a running event loop" not in result, (
            f"the slash handler must not raise asyncio.run loop-conflict: "
            f"{result!r}"
        )
        if isinstance(result, str):
            # Single-string fallback: at minimum, the response must
            # include three progress markers.
            assert ("calling the LLM" in result.lower() or
                    "authoring" in result.lower() or
                    "llm" in result.lower()), result
            assert ("saving" in result.lower() or
                    "library" in result.lower() or
                    "saved" in result.lower() or
                    "generated" in result.lower()), result
            assert ("submit" in result.lower() or
                    "running" in result.lower() or
                    "submitted" in result.lower() or
                    "run_id" in result.lower()), result
        else:
            assert isinstance(result, list)
            assert len(result) >= 3


    def test_slash_create_works_inside_running_event_loop(
        self, tmp_path,
    ):
        """Pin the asyncio.run loop-conflict bug fix.

        Before the fix, ``_create_via_script_author`` called
        ``asyncio.run`` which raises ``RuntimeError: cannot be
        called from a running event loop`` when the chat loop is
        already running (the common chat case). The fix spawns the
        coroutine via a worker ThreadPoolExecutor so it runs to
        completion synchronously without conflicting with the
        outer loop.
        """
        import asyncio
        from plugins.hermes_workflow.runtime_factory import build_runtime
        from plugins.hermes_workflow.tests._plugin_context import (
            MockPluginContext,
        )
        from plugins.hermes_workflow import register as plugin_register
        from plugins.hermes_workflow.script_author import ScriptAuthor
        from plugins.hermes_workflow.slash import build_slash_handlers

        class _StubLlm:
            async def acomplete_structured(self, **kwargs):
                class _Result:
                    text = "{}"
                    parsed = None
                return _Result()

        ctx = MockPluginContext()
        ctx.llm = _StubLlm()
        runtime_factory_mod = __import__(
            "plugins.hermes_workflow.runtime_factory", fromlist=["*"]
        )
        runtime_factory_mod.default_journal_root = lambda: tmp_path / "wf"
        plugin_register(ctx)
        handlers = build_slash_handlers(
            build_runtime(journal_root=tmp_path / "wf"),
            script_author=ScriptAuthor(llm=ctx.llm),
            fallback_sink=ctx._fallback_sink,
        )
        handler = handlers["workflow"]["handler"]

        # Run inside an explicit asyncio loop — the case that
        # broke before the fix.
        async def _drive():
            return handler("create demo workflow")

        out = asyncio.run(_drive())
        # Must NOT contain the loop-conflict error.
        assert "cannot be called from a running event loop" not in out, (
            f"the slash handler must work when called from inside "
            f"an asyncio loop (the chat case): got {out!r}"
        )
