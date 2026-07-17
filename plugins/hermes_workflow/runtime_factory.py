"""Runtime factory: a single WorkflowRuntime singleton per hermes process.

The plugin's register() entrypoint calls build_runtime() once. The
returned WorkflowRuntime is shared by the CLI, slash commands, model
tool, and gateway handler — every surface dispatches through the same
runtime, the same journal, the same caps.

We deliberately do NOT use a module-level singleton. Tests construct
their own; production code goes through build_runtime() so the
construction is explicit and dependency-injectable.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
from pathlib import Path
from typing import TYPE_CHECKING, Any

from plugins.hermes_workflow.runtime import WorkflowRuntime

if TYPE_CHECKING:
    from plugins.hermes_workflow.agent_bridge import AgentBridge


# Module-level slot for capturing ctx.llm at register() time. The
# runtime factory reads this slot via get_captured_plugin_llm() so
# build_runtime() can wire the in-process PluginLlmBridge when the
# plugin is loaded inside an active Hermes session. Single-element
# list used as a mutable slot (idiomatic Python pre-3.10 alternative
# to nonlocal over a module-level binding).
_captured_plugin_llm: list = []


def _capture_plugin_llm(llm) -> None:
    """Store ctx.llm so build_runtime can later read it via
    get_captured_plugin_llm()."""
    if _captured_plugin_llm:
        _captured_plugin_llm[0] = llm
    else:
        _captured_plugin_llm.append(llm)


def get_captured_plugin_llm():
    """Return whatever ctx.llm was captured at register() time."""
    return _captured_plugin_llm[0] if _captured_plugin_llm else None


def _slugify_runtime_id(text: str, *, max_len: int = 32) -> str:
    """Convert *text* to a snake_case slug suitable for a run_id fragment.

    Mirrors ``script_author._slugify`` without taking a hard dependency
    on it (the script_author module imports ``runtime_factory``).
    """
    s = re.sub(r"[^a-zA-Z0-9]+", "_", text.lower()).strip("_")
    s = re.sub(r"_+", "_", s)
    s = re.sub(r"[^a-z0-9_]", "", s)
    return s[:max_len] or "workflow"


def make_script_author_run_id(name: str) -> str:
    """Build a synthesized run_id for an externally-posted artifact.

    Format: ``za_<slug>_<hex>`` where ``za`` stands for
    "ScriptAuthor-z-A" (the script-author invocation of ad-hoc mode),
    ``<slug>`` is the workflow name slugified, and ``<hex>`` is
    ``secrets.token_hex(4)`` (8 hex chars).

    These ids never overlap with the runtime's own ``run_<hex>``
    namespace — every script-author-posted artifact begins with
    ``za_``, which the runtime's id scheme does not produce.
    """
    return f"za_{_slugify_runtime_id(name)}_{secrets.token_hex(4)}"


_log = logging.getLogger("hermes_workflow.runtime_factory")


def default_journal_root() -> Path:
    """Where journals live on disk.

    Honors ``HERMES_WORKFLOW_ROOT``; falls back to ``~/.hermes/workflows``.
    The fallback is created on first write, not on import.
    """
    env = os.environ.get("HERMES_WORKFLOW_ROOT")
    if env:
        return Path(env)
    return Path.home() / ".hermes" / "workflows"


def build_bridge_from_env_with_llm(*, llm=None) -> "AgentBridge | None":
    """Build the LLM bridge with the in-process PluginLlmBridge preferred.

    Activation precedence (highest first):
        1. ``HERMES_WORKFLOW_AGENT_BRIDGE=hermes-chat`` → subprocess
           bridge to ``hermes chat -q ...``. Operator opt-in wins.
        2. ``llm is not None`` → PluginLlmBridge (in-process,
           structured-output capable via wire-level response_format).
           Auto-fallback when the env var is unset.
        3. ``HERMES_WORKFLOW_AGENT_BRIDGE=stub`` (or unset, or
           unrecognized) → StubBridge (deterministic, no network).

    Args:
        llm: The PluginLlm facade (``ctx.llm``) when the plugin is
             running inside an active Hermes session. ``None`` when the
             plugin is loaded standalone (CLI direct, tests, etc.).

    Returns:
        An AgentBridge instance. Never ``None`` — at minimum returns a
        StubBridge so ask_agent calls succeed deterministically.
    """
    from .plugin_llm_bridge import PluginLlmBridge

    choice = os.environ.get("HERMES_WORKFLOW_AGENT_BRIDGE", "").strip().lower()
    # Explicit operator choice wins over auto-detection. We route
    # "stub" through the same code path as the env-var default since
    # both produce a StubBridge.
    if choice in ("hermes-chat", "hermes_chat", "chat"):
        from .hermes_chat_bridge import build_bridge_from_env
        return build_bridge_from_env()
    if choice == "stub":
        # Operator explicitly asked for stub — do not auto-fallback to
        # PluginLlmBridge even when ctx.llm is reachable.
        from .hermes_chat_bridge import StubBridge
        return StubBridge()

    # In-process PluginLlmBridge when ctx.llm is reachable (and the
    # env var is unset or unrecognized).
    if llm is not None:
        _log.info("auto-wiring PluginLlmBridge (ctx.llm is reachable)")
        return PluginLlmBridge(llm=llm)

    # Fall back to env-var default (stub if unset).
    from .hermes_chat_bridge import build_bridge_from_env
    return build_bridge_from_env()


def build_runtime(*, journal_root: Path | None = None,
                   max_concurrent: int = 16,
                   max_total: int = 1000) -> WorkflowRuntime:
    """Construct a WorkflowRuntime with the given configuration.

    Caps default to 16/1000 matching the spec (and Claude Code's
    Dynamic Workflows caps, so the mental model transfers). Override
    via ``HERMES_WORKFLOW_MAX_CONCURRENT`` / ``HERMES_WORKFLOW_MAX_TOTAL``
    environment variables for deployments that need different ceilings.
    """
    mc_env = os.environ.get("HERMES_WORKFLOW_MAX_CONCURRENT")
    mt_env = os.environ.get("HERMES_WORKFLOW_MAX_TOTAL")
    if mc_env:
        try:
            max_concurrent = int(mc_env)
        except ValueError:
            _log.warning("invalid HERMES_WORKFLOW_MAX_CONCURRENT=%r; using default",
                          mc_env)
    if mt_env:
        try:
            max_total = int(mt_env)
        except ValueError:
            _log.warning("invalid HERMES_WORKFLOW_MAX_TOTAL=%r; using default",
                          mt_env)

    runtime = WorkflowRuntime(
        default_max_concurrent=max_concurrent,
        default_max_total=max_total,
        journal_root=journal_root or default_journal_root(),
    )

    # Auto-wire the LLM bridge with the in-process PluginLlmBridge
    # preferred when ctx.llm is reachable. Without this, every
    # ctx.runtime.ask_agent() call raises ``NotImplementedError: no
    # agent bridge configured``.
    #
    # Activation precedence:
    #   1. HERMES_WORKFLOW_AGENT_BRIDGE=hermes-chat → subprocess
    #      bridge (operator opt-in for cross-machine workflows).
    #   2. ctx.llm is reachable → PluginLlmBridge (in-process,
    #      structured-output capable, wire-format response_format).
    #   3. HERMES_WORKFLOW_AGENT_BRIDGE=stub (or unset) → StubBridge.
    #
    # ctx.llm is supplied via _capture_plugin_llm() at register() time.
    try:
        llm = get_captured_plugin_llm()
        bridge = build_bridge_from_env_with_llm(llm=llm)
        if bridge is not None:
            runtime.set_agent_bridge(bridge)
    except RuntimeError as exc:
        # ``HermesChatBridge.__init__`` raises RuntimeError if the
        # ``hermes`` CLI is not on PATH. Surface this so the operator
        # notices and either installs the CLI or sets
        # HERMES_WORKFLOW_AGENT_BRIDGE=stub to fall back.
        _log.warning(
            "agent bridge auto-wire failed: %s "
            "(set HERMES_WORKFLOW_AGENT_BRIDGE=stub to fall back)",
            exc,
        )

    return runtime
