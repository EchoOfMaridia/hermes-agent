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
from pathlib import Path
from typing import Any

from plugins.hermes_workflow.runtime import WorkflowRuntime


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

    return WorkflowRuntime(
        default_max_concurrent=max_concurrent,
        default_max_total=max_total,
        journal_root=journal_root or default_journal_root(),
    )
