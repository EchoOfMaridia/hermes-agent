"""Unit tests for the extracted turn prologue (``agent/turn_context.py``).

These exercise ``build_turn_context`` against a lightweight fake agent to
confirm the prologue produces the right ``TurnContext`` and applies the
``agent`` side effects the loop relies on — without spinning up a real
``AIAgent`` or hitting any provider.
"""

from __future__ import annotations

import types
from unittest.mock import patch

import pytest

from agent.turn_context import TurnContext, build_turn_context


class _FakeTodoStore:
    def has_items(self):
        return True

    def _hydrate(self, *_a, **_k):
        pass


class _FakeGuardrails:
    def __init__(self):
        self.reset_called = False

    def reset_for_turn(self):
        self.reset_called = True


class _FakeAgent:
    """Minimal stand-in covering only what the prologue touches."""

    def __init__(self):
        self.session_id = "sess-1"
        self.model = "test/model"
        self.provider = "openrouter"
        self.base_url = "https://openrouter.ai/api/v1"
        self.api_key = "sk-x"
        self.api_mode = "chat_completions"
        self.platform = "cli"
        self.quiet_mode = True
        self.max_iterations = 90
        self.tools: list = []
        self.valid_tool_names: set = set()
        self.enabled_toolsets = None
        self.disabled_toolsets = None
        self._skip_mcp_refresh = False
        self.compression_enabled = False
        self.context_compressor: object = types.SimpleNamespace(
            protect_first_n=2, protect_last_n=2
        )
        self._cached_system_prompt = "SYSTEM"
        self._memory_store = None
        self._memory_manager = None
        self._memory_nudge_interval = 0
        self._turns_since_memory = 0
        self._user_turn_count = 0
        self._todo_store = _FakeTodoStore()
        self._tool_guardrails = _FakeGuardrails()
        self._compression_warning = None
        self._interrupt_requested = False
        self._memory_write_origin = "assistant_tool"
        self._stream_context_scrubber = None
        self._stream_think_scrubber = None
        # Attributes the prologue assigns; recorded for assertions.
        self._invalid_tool_retries = -1
        self._vision_supported = None
        self._persist_calls = 0

    # --- methods the prologue calls ---
    def _ensure_db_session(self):
        pass

    def _restore_primary_runtime(self):
        pass

    def _cleanup_dead_connections(self):
        return False

    def _emit_status(self, _msg):
        pass

    def _replay_compression_warning(self):
        pass

    def _hydrate_todo_store(self, *_a, **_k):
        pass

    def _safe_print(self, *_a, **_k):
        pass

    def _persist_session(self, *_a, **_k):
        self._persist_calls += 1


@pytest.fixture(autouse=True)
def _stub_runtime_main():
    """``build_turn_context`` calls ``auxiliary_client.set_runtime_main`` as a
    production side effect (telling aux tools the live main provider/model).
    That writes a module-level global these unit tests don't care about and
    which would otherwise leak into sibling tests (e.g. provider-parity
    resolution) when the per-test process isolation plugin is disabled. Stub
    it out so the prologue tests stay hermetic.
    """
    with patch("agent.auxiliary_client.set_runtime_main", lambda *a, **k: None):
        yield


def _build(agent, **overrides):
    kwargs = dict(
        agent=agent,
        user_message="hello",
        system_message=None,
        conversation_history=None,
        task_id=None,
        stream_callback=None,
        persist_user_message=None,
        restore_or_build_system_prompt=lambda *a, **k: None,
        install_safe_stdio=lambda: None,
        sanitize_surrogates=lambda s: s,
        summarize_user_message_for_log=lambda s: s,
        set_session_context=lambda _sid: None,
        set_current_write_origin=lambda _o: None,
        ra=lambda: types.SimpleNamespace(_set_interrupt=lambda *a, **k: None),
    )
    kwargs.update(overrides)
    return build_turn_context(**kwargs)


def test_returns_turn_context_with_user_message_appended():
    agent = _FakeAgent()
    ctx = _build(agent)
    assert isinstance(ctx, TurnContext)
    assert ctx.user_message == "hello"
    # The user turn was appended and indexed.
    assert ctx.messages[-1] == {"role": "user", "content": "hello"}
    assert ctx.current_turn_user_idx == len(ctx.messages) - 1
    assert ctx.active_system_prompt == "SYSTEM"


def test_applies_agent_side_effects():
    agent = _FakeAgent()
    _build(agent)
    # Retry counters reset, guardrails reset, vision re-armed, turn counted.
    assert agent._invalid_tool_retries == 0
    assert agent._tool_guardrails.reset_called is True
    assert agent._vision_supported is True
    assert agent._user_turn_count == 1
    # Crash-resilience persistence fired once.
    assert agent._persist_calls == 1
    # task/turn ids assigned on the agent.
    assert agent._current_task_id
    assert agent._current_turn_id


def test_task_id_passthrough():
    agent = _FakeAgent()
    ctx = _build(agent, task_id="fixed-task")
    assert ctx.effective_task_id == "fixed-task"
    assert agent._current_task_id == "fixed-task"


def test_persist_user_message_becomes_original():
    agent = _FakeAgent()
    ctx = _build(agent, user_message="api-prefixed", persist_user_message="clean")
    # original_user_message tracks the clean persist override.
    assert ctx.original_user_message == "clean"
    # but the appended user turn carries the full (sanitized) message.
    assert ctx.messages[-1]["content"] == "api-prefixed"


def test_memory_nudge_fires_at_interval():
    agent = _FakeAgent()
    agent._memory_nudge_interval = 1
    agent.valid_tool_names = {"memory"}
    agent._memory_store = object()
    ctx = _build(agent)
    assert ctx.should_review_memory is True
    assert agent._turns_since_memory == 0  # reset after firing


def test_no_review_when_memory_disabled():
    agent = _FakeAgent()
    ctx = _build(agent)
    assert ctx.should_review_memory is False


# ── Between-turns MCP refresh (cache-safe late-binding) ──────────────────────
#
# A slow MCP server that connects after the agent's build-time tool snapshot
# must become callable by the user's NEXT turn — without mutating an in-flight
# turn's cached request prefix. The prologue is exactly that boundary, so the
# refresh hook lives here. These assert the contract (R1/R2/R6 in the spec),
# not timing permutations.


def test_between_turns_refresh_adds_late_tool_when_servers_registered():
    """R1: a tool that registered since build lands in this turn's snapshot."""
    agent = _FakeAgent()

    new_def = {"type": "function", "function": {"name": "mcp_x_tool", "description": "", "parameters": {}}}

    import model_tools
    with patch("tools.mcp_tool.has_registered_mcp_tools", return_value=True), \
         patch.object(model_tools, "get_tool_definitions", return_value=[new_def]):
        _build(agent)

    assert "mcp_x_tool" in agent.valid_tool_names
    assert any(t["function"]["name"] == "mcp_x_tool" for t in agent.tools)


def test_between_turns_refresh_skipped_when_no_servers():
    """R6: the common case (no MCP servers) never walks the registry."""
    agent = _FakeAgent()
    import model_tools

    with patch("tools.mcp_tool.has_registered_mcp_tools", return_value=False), \
         patch.object(model_tools, "get_tool_definitions") as gtd:
        _build(agent)

    gtd.assert_not_called()


def test_between_turns_refresh_skipped_when_skip_flag_set():
    """Internal forks (background_review) set _skip_mcp_refresh to keep tools[]
    byte-identical to the parent for cache parity — the hook must honor it even
    when MCP servers are registered."""
    agent = _FakeAgent()
    agent._skip_mcp_refresh = True
    import model_tools

    with patch("tools.mcp_tool.has_registered_mcp_tools", return_value=True), \
         patch.object(model_tools, "get_tool_definitions") as gtd:
        _build(agent)

    gtd.assert_not_called()


def test_between_turns_refresh_no_churn_when_unchanged():
    """R2: an unchanged tool set leaves the snapshot object identity intact
    (no needless swap → nothing for the next request prefix to diff against)."""
    agent = _FakeAgent()
    same = [{"type": "function", "function": {"name": "a", "description": "", "parameters": {}}}]
    agent.tools = same
    agent.valid_tool_names = {"a"}

    import model_tools
    with patch("tools.mcp_tool.has_registered_mcp_tools", return_value=True), \
         patch.object(
             model_tools, "get_tool_definitions",
             return_value=[{"type": "function", "function": {"name": "a", "description": "", "parameters": {}}}],
         ):
        _build(agent)

    assert agent.tools is same  # not replaced → no churn


# ── Preflight rough-estimate must be clamped to context_length ────────────
#
# Bug screenshot evidence (desktop statusbar reading 3.1M/1.0M on a small
# session): the preflight branch at agent/turn_context.py:310-314 was
# assigning ``_compressor.last_prompt_tokens = _preflight_tokens`` with no
# upper bound. The rough estimate is derived from
# ``len(system_prompt) // 4 + len(tools) // 4 + message chars // 4`` and can
# grow well past the model context window with 50+ tools enabled. That value
# then propagated to the desktop statusbar via
# ``tui_gateway._get_usage()`` (``context_used = last_prompt_tokens``) and
# rendered as "3.1M/1.0M (100%)" — wildly wrong.
#
# The seed-to-display intent (PR #413) is preserved: preflight IS allowed
# to revise ``last_prompt_tokens`` upward when it's a better signal than the
# stale provider value. The fix is the clamp: rough estimates never exceed
# the model's actual context window. Tests pin both halves of the contract
# — clamped upward revision is allowed, unbounded upward revision is not.


class _FakeCompressor:
    """Compressor double exposing exactly the fields the preflight branch
    reads. ``should_compress`` / ``should_defer_preflight_to_real_usage`` are
    monkey-patched per-test so the rough estimate can be controlled without
    touching the real ContextCompressor.
    """

    def __init__(self, last_prompt_tokens=0, context_length=1_000_000):
        self.protect_first_n = 2
        self.protect_last_n = 2
        self.last_prompt_tokens = last_prompt_tokens
        self.last_real_prompt_tokens = last_prompt_tokens
        self.threshold_tokens = 85_000
        self.context_length = context_length
        self.compress_calls = 0
        self.should_compress_calls: list = []

    def should_defer_preflight_to_real_usage(self, _tokens):
        return False

    def should_compress(self, _tokens):
        return False  # default: never compress; tests that need to compress override

    def _compress_marker(self):
        # Marker method to detect "did preflight call into us?" — not used by
        # build_turn_context, just here for test instrumentation.
        self.compress_calls += 1


def _build_with_compressor(agent, conversation_history):
    """Wrap _build so the compressor is wired up and the preflight branch
    is actually entered. Returns the resulting TurnContext."""
    return _build(
        agent,
        conversation_history=conversation_history,
    )


def test_preflight_clamps_rough_estimate_to_context_length():
    """Regression for the 3.1M/1.0M statusbar bug: a rough estimate that
    exceeds the model's context window must be clamped to that ceiling
    before being assigned to ``last_prompt_tokens``."""
    agent = _FakeAgent()
    agent.compression_enabled = True
    # 1M context window — same model the bug screenshot was using.
    agent.context_compressor = _FakeCompressor(
        last_prompt_tokens=50_000, context_length=1_000_000
    )
    agent.tools = [{"type": "function", "function": {"name": "huge"}}] * 80  # 80 tools
    agent._cached_system_prompt = "X" * 200_000  # 200KB system prompt

    history = [
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": "y"},
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": "y"},
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": "y"},
    ]

    # Patch estimate_request_tokens_rough to return the bug-screenshot
    # value: 3.1M tokens, exceeding the 1M context window.
    with patch(
        "agent.turn_context.estimate_request_tokens_rough",
        return_value=3_100_000,
    ):
        _build_with_compressor(agent, history)

    # The fix: rough estimate is clamped to context_length (1M), so the
    # value seeded into last_prompt_tokens is bounded — the desktop
    # statusbar can no longer display "3.1M/1.0M (310%)".
    assert agent.context_compressor.last_prompt_tokens == 1_000_000, (
        f"preflight must clamp rough estimate to context_length; "
        f"expected 1_000_000 (context window), got {agent.context_compressor.last_prompt_tokens}"
    )


def test_preflight_revises_upward_when_estimate_under_context_length():
    """The seed-to-display intent (PR #413): preflight IS allowed to revise
    ``last_prompt_tokens`` upward when the rough estimate is below the
    context ceiling. This keeps the statusbar in sync when compression
    no-ops but the loaded history is genuinely oversized."""
    agent = _FakeAgent()
    agent.compression_enabled = True
    agent.context_compressor = _FakeCompressor(
        last_prompt_tokens=50_000, context_length=200_000
    )
    agent.tools = [{"type": "function", "function": {"name": "huge"}}] * 80
    agent._cached_system_prompt = "X" * 200_000

    history = [
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": "y"},
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": "y"},
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": "y"},
    ]

    # 144_669 is above the stale 50_000 but below the 200_000 ceiling —
    # the legitimate preflight revision path that PR #413 introduced.
    with patch(
        "agent.turn_context.estimate_request_tokens_rough",
        return_value=144_669,
    ):
        _build_with_compressor(agent, history)

    assert agent.context_compressor.last_prompt_tokens == 144_669, (
        f"preflight must revise upward when estimate is under ceiling; "
        f"expected 144_669, got {agent.context_compressor.last_prompt_tokens}"
    )


def test_preflight_does_not_revise_downward():
    """A smaller estimate must not clobber a larger tracked value
    (``test_preflight_seed_only_revises_upward`` from PR #413)."""
    agent = _FakeAgent()
    agent.compression_enabled = True
    agent.context_compressor = _FakeCompressor(
        last_prompt_tokens=160_000, context_length=200_000
    )
    agent.tools = [{"type": "function", "function": {"name": "huge"}}] * 80
    agent._cached_system_prompt = "X" * 200_000

    history = [
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": "y"},
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": "y"},
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": "y"},
    ]

    with patch(
        "agent.turn_context.estimate_request_tokens_rough",
        return_value=144_669,
    ):
        _build_with_compressor(agent, history)

    assert agent.context_compressor.last_prompt_tokens == 160_000, (
        f"preflight must not revise downward; "
        f"expected 160_000 (stale larger value), got {agent.context_compressor.last_prompt_tokens}"
    )


def test_preflight_still_calls_should_compress_with_rough_estimate():
    """Sanity: the fix only clamps the over-write; the rough estimate MUST
    still drive the compression decision (the legitimate purpose of
    preflight)."""
    agent = _FakeAgent()
    agent.compression_enabled = True

    compressor = _FakeCompressor(
        last_prompt_tokens=50_000, context_length=1_000_000
    )
    original_should_compress = compressor.should_compress

    def tracking_should_compress(tokens):
        compressor.should_compress_calls.append(tokens)
        return False  # don't actually compress in this test

    compressor.should_compress = tracking_should_compress
    agent.context_compressor = compressor
    agent.tools = [{"type": "function", "function": {"name": "huge"}}] * 80
    agent._cached_system_prompt = "X" * 200_000

    history = [
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": "y"},
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": "y"},
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": "y"},
    ]

    with patch(
        "agent.turn_context.estimate_request_tokens_rough",
        return_value=3_100_000,
    ):
        _build_with_compressor(agent, history)

    assert compressor.should_compress_calls, "preflight must still call should_compress with the rough estimate"
    assert compressor.should_compress_calls[0] == 3_100_000
    # And last_prompt_tokens is clamped to context_length, not 3.1M.
    assert compressor.last_prompt_tokens == 1_000_000


