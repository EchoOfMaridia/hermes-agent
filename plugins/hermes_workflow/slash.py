"""Slash commands for the workflow plugin.

Hermes's slash-command protocol parses a user-typed ``/foo bar baz`` into
``name="foo"``, ``arg="bar baz"`` (``ui-tui/src/domain/slash.ts:6-10``)
and the gateway then invokes ``plugin_handler(user_args)``
(``gateway/run.py:8370``). That means the plugin's natural unit of
registration is ONE slash command per *namespace*, with the subcommand
selected by the first token of ``arg``.

We register ``/workflow`` as a single command. The handler tokenizes the
raw arg on whitespace, picks a subcommand, and dispatches to the
matching implementation. ``/workflow`` with no arg prints help; ``/workflow
help`` also prints help.

Available slash commands::

    /workflow run <script> [--inputs ...]
    /workflow list
    /workflow inspect <name>
    /workflow status [run_id]
    /workflow snapshot <run_id> [--tier 1|2|3]
    /workflow cancel <run_id>
    /workflow save <name>
    /workflow expand <run_id>    (returns the full card tree, tier 1)
    /workflow help

The handler returns a string suitable for direct display. Returning
``None`` silently no-ops.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import shlex
from typing import Any


# ---------------------------------------------------------------------------
# Slash-command registry (one entry: /workflow).
# ---------------------------------------------------------------------------

_WORKFLOW_COMMAND = "workflow"


def build_slash_handlers(runtime: Any) -> dict[str, dict]:
    """Build the dict of slash-command name -> {handler, description}.

    The plugin registers a single command (``workflow``) whose handler
    tokenizes the first word of ``arg`` as a subcommand and routes to
    the matching implementation. This matches the host's slash-command
    model (``name`` is one token, ``arg`` is the rest).
    """

    def _workflow_handler(raw: str) -> str | None:
        return _dispatch_workflow(runtime, raw)

    return {
        _WORKFLOW_COMMAND: {
            "handler": _workflow_handler,
            "description": (
                "Workflow plugin. Subcommands: run, list, inspect, "
                "status, snapshot, cancel, save, expand, help."
            ),
            "args_hint": "<subcommand> [args...]",
        },
    }


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

# Map of subcommand -> the CLI subcommand it forwards to. ``save`` and
# ``expand`` don't map 1:1 to CLI subcommands — they have bespoke logic.
_CLI_FORWARD = {
    "run": "run",
    "list": "list",
    "inspect": "inspect",
    "status": "status",
    "snapshot": "snapshot",
    "cancel": None,   # handled via runtime directly
    "save": None,     # bespoke stub
    "expand": None,   # bespoke: snapshot --tier 1
}

_HELP_TEXT = """\
workflow plugin commands:
  /workflow run <script> [--inputs k=v ...]   Run a workflow script by path
  /workflow list                              List saved workflows in the library
  /workflow inspect <name-or-script>          Show a script's step graph
  /workflow status [run_id]                   Show active runs, or one run's status
  /workflow snapshot <run_id> [--tier 1|2|3]  Render a run's card tree (default tier 2)
  /workflow cancel <run_id>                   Cancel a running workflow
  /workflow save <name>                       Save the most-recent run to the library (v0.2.0 stub)
  /workflow expand <run_id>                   Expand a run's card tree to tier 1 (full detail)
  /workflow help                              Show this help
"""


def _dispatch_workflow(runtime: Any, raw: str) -> str | None:
    """Route a /workflow invocation to the right subcommand."""
    tokens = shlex.split(raw) if raw and raw.strip() else []
    if not tokens or tokens[0] in {"help", "--help", "-h", "?"}:
        return _HELP_TEXT.rstrip()

    sub, rest = tokens[0], tokens[1:]

    # Bespoke handlers first (these don't have CLI subcommands) -------
    if sub == "cancel":
        return _cancel_via_runtime(runtime, rest)
    if sub == "save":
        return _save_stub(rest)
    if sub == "expand":
        # /workflow expand <run_id>  ==  /workflow snapshot <run_id> --tier 1
        tier_args = list(rest) + ["--tier", "1"]
        return _run_cli_capture("snapshot", " ".join(_quote(a) for a in tier_args))

    # Forward to CLI subcommand ----------------------------------------
    cli_subcommand = _CLI_FORWARD.get(sub)
    if cli_subcommand is None:
        return (
            f"unknown workflow subcommand: {sub!r}\n"
            f"type /workflow help for the list"
        )
    forward_arg = " ".join(_quote(a) for a in rest)
    return _run_cli_capture(cli_subcommand, forward_arg)


def _quote(token: str) -> str:
    """Quote a token for shell-style round-tripping if it contains spaces."""
    if any(c.isspace() for c in token):
        return shlex.quote(token)
    return token


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_cli_capture(subcommand: str, raw: str) -> str | None:
    """Invoke the CLI machinery synchronously, capture stdout, return string.

    The CLI's _dispatch prints to stdout; we redirect stdout into a
    StringIO buffer for the duration of the call and return the
    captured text. This lets slash-command handlers reuse the CLI
    implementation without duplicating it.
    """
    from plugins.hermes_workflow.cli import _dispatch_async, register_cli

    parser = argparse.ArgumentParser(prog="hermes workflow")
    sub = parser.add_subparsers()
    register_cli(sub)

    tokens = ["workflow", subcommand]
    if raw and raw.strip():
        tokens.extend(shlex.split(raw))

    try:
        args = parser.parse_args(tokens)
    except SystemExit:
        return f"invalid arguments: {raw!r}"

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            rc = asyncio.run(_dispatch_async(args))
        except Exception as e:
            return f"error: {e}"
    out = buf.getvalue().rstrip()
    return out or f"(exit {rc})"


def _cancel_via_runtime(runtime: Any, rest: list[str]) -> str | None:
    """Cancel a running workflow via the runtime directly."""
    if not rest:
        return "usage: /workflow cancel <run_id>"
    run_id = rest[0]
    try:
        asyncio.run(runtime.cancel(run_id))
    except Exception as e:
        return f"cancel failed: {e}"
    return f"cancelled {run_id}"


def _save_stub(rest: list[str]) -> str | None:
    """v0.2.0 save stub.

    Save-as-library requires the script-author integration (the LLM that
    generates Python from natural-language intent). Until that ships,
    /workflow save returns guidance on the manual copy-paste path.
    """
    if len(rest) != 1:
        return "usage: /workflow save <name>"
    return (
        "save is a v0.2.0 feature requiring the script-author integration. "
        "For now, copy your .py script into ~/.hermes/workflows/<name>.py "
        "and the CLI `hermes workflow run <path>` will find it."
    )