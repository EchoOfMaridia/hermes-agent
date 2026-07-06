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
from typing import Any

from plugins.hermes_workflow.runtime import WorkflowRuntime


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

    # Auto-wire the LLM bridge from the HERMES_WORKFLOW_AGENT_BRIDGE env
    # var. Without this, every ctx.runtime.ask_agent() call raises
    # ``NotImplementedError: no agent bridge configured``.
    #
    # Accepted values:
    #   "hermes-chat"   → subprocess bridge to `hermes chat -q ...`
    #   "stub"          → deterministic stub for tests / dry-runs
    #   "" / unset      → no bridge (v0.1.0 behaviour, ask_agent raises)
    #
    # Importing the bridge module is deferred so this factory doesn't
    # take a hard dependency on hermes-binary-availability for callers
    # that don't use the agent surface.
    try:
        from .hermes_chat_bridge import build_bridge_from_env
        bridge = build_bridge_from_env()
        if bridge is not None:
            runtime.set_agent_bridge(bridge)
    except RuntimeError as exc:
        # ``HermesChatBridge.__init__`` raises RuntimeError if the
        # ``hermes`` CLI is not on PATH. Surface this so the operator
        # notices and either installs the CLI or sets
        # HERMES_WORKFLOW_AGENT_BRIDGE=stub to fall back.
        import logging
        _log.warning(
            "agent bridge auto-wire failed: %s "
            "(set HERMES_WORKFLOW_AGENT_BRIDGE=stub to fall back)",
            exc,
        )

    return runtime
