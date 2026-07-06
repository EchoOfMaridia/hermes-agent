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
import logging
import shlex
from typing import Any


_log = logging.getLogger("hermes_workflow.slash")


# ---------------------------------------------------------------------------
# Slash-command registry (one entry: /workflow).
# ---------------------------------------------------------------------------

_WORKFLOW_COMMAND = "workflow"


def build_slash_handlers(
    runtime: Any,
    script_author: Any | None = None,
) -> dict[str, dict]:
    """Build the dict of slash-command name -> {handler, description}.

    The plugin registers a single command (``workflow``) whose handler
    tokenizes the first word of ``arg`` as a subcommand and routes to
    the matching implementation. This matches the host's slash-command
    model (``name`` is one token, ``arg`` is the rest).

    ``script_author`` is threaded through from the plugin entrypoint so
    ``/workflow create <intent>`` can route to the LLM-driven ad-hoc
    authoring path (mirrors the gateway handler's contract in
    gateway_handler.py). Without it the ``create`` subcommand falls back
    to v0.2.0 manual-copy guidance (the same code path the CLI uses
    when no LLM is available).
    """
    def _workflow_handler(raw: str) -> str | None:
        return _dispatch_workflow(runtime, raw, script_author=script_author)

    return {
        _WORKFLOW_COMMAND: {
            "handler": _workflow_handler,
            "description": (
                "Workflow plugin. Subcommands: run, list, inspect, "
                "status, snapshot, cancel, save, expand, create, help."
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
  /workflow create <intent>                   Ad-hoc workflow from natural-language intent (LLM-driven, v0.2.0)
  /workflow help                              Show this help
"""


def _dispatch_workflow(
    runtime: Any,
    raw: str,
    script_author: Any | None = None,
) -> str | None:
    """Route a /workflow invocation to the right subcommand.

    ``script_author`` is plumbed in for the v0.2.0 ``create`` subcommand:
    if the user supplied a natural-language intent after ``/workflow create ...``
    we route to ``script_author`` (LLM-driven ad-hoc authoring) so the
    agent doesn't have to manually write Python. Falls back to the
    v0.2.0 save stub path (manual copy-paste) when ``script_author`` is
    ``None`` — the same behavior the CLI has when no LLM is available.
    """
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
        return _run_cli_capture(runtime, "snapshot",
                                 " ".join(_quote(a) for a in tier_args))
    if sub == "create":
        # v0.2.0 LLM ad-hoc authoring. Routes to ScriptAuthor.generate
        # when one is wired in (CLI without LLM, or stub context, falls
        # back to the manual-copy guidance via the helper).
        return _create_via_script_author(runtime, rest, script_author)

    # Forward to CLI subcommand ----------------------------------------
    cli_subcommand = _CLI_FORWARD.get(sub)
    if cli_subcommand is None:
        return (
            f"unknown workflow subcommand: {sub!r}\n"
            f"type /workflow help for the list"
        )
    forward_arg = " ".join(_quote(a) for a in rest)
    return _run_cli_capture(runtime, cli_subcommand, forward_arg)


def _quote(token: str) -> str:
    """Quote a token for shell-style round-tripping if it contains spaces."""
    if any(c.isspace() for c in token):
        return shlex.quote(token)
    return token


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_cli_capture(runtime: Any, subcommand: str, raw: str) -> str | None:
    """Invoke the CLI machinery synchronously, capture stdout, return string.

    The CLI's _dispatch prints to stdout; we redirect stdout into a
    StringIO buffer for the duration of the call and return the
    captured text. This lets slash-command handlers reuse the CLI
    implementation without duplicating it.

    The slash surface passes its own ``runtime`` (built with the
    slash-surface journal root). We forward that journal root to the
    CLI via ``HERMES_WORKFLOW_ROOT`` so that ``args_journal_root()``
    inside the CLI resolves to the SAME root the slash surface uses —
    otherwise /workflow run, /workflow inspect, /workflow status see
    a different filesystem than /workflow create wrote to.
    """
    import os as _os
    from plugins.hermes_workflow.cli import _dispatch_async, register_cli

    parser = argparse.ArgumentParser(prog="hermes workflow")
    register_cli(parser)

    tokens = [subcommand]
    if raw and raw.strip():
        tokens.extend(shlex.split(raw))

    # Forward the slash surface's journal root to the CLI by setting
    # HERMES_WORKFLOW_ROOT before invoking the dispatcher. Restore
    # the previous value (or unset) on the way out so we don't leak
    # the override into unrelated subprocesses.
    saved_root = _os.environ.get("HERMES_WORKFLOW_ROOT")
    rt_root = getattr(runtime, "journal_root", None)
    if rt_root is not None:
        _os.environ["HERMES_WORKFLOW_ROOT"] = str(rt_root)
    try:
        try:
            args = parser.parse_args(tokens)
        except SystemExit:
            return f"invalid arguments: {' '.join(tokens)!r}"

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                rc = asyncio.run(_dispatch_async(args))
            except Exception as e:
                return f"error: {e}"
        out = buf.getvalue().rstrip()
        return out or f"(exit {rc})"
    finally:
        if saved_root is None:
            _os.environ.pop("HERMES_WORKFLOW_ROOT", None)
        else:
            _os.environ["HERMES_WORKFLOW_ROOT"] = saved_root


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


def _create_via_script_author(
    runtime: Any,
    rest: list[str],
    script_author: Any | None,
) -> Any:
    """Ad-hoc authoring via ScriptAuthor.

    Mirrors the contract of ``gateway_handler._handle_ad_hoc``: when
    script_author is provided, generate a workflow script from the
    user's natural-language intent, save it to the library, submit it
    to the runtime, and report the run_id. When script_author is None,
    return v0.2.0 manual-copy guidance.

    Streaming behavior (Fix 1 + Fix 2 — 2026-06-30):

    This function returns an **async generator** that yields strings.
    Each yield lands as an incremental message on the chat surface
    (Discord / Telegram / iMessage / TUI / desktop), so the user sees
    LLM tokens as they arrive instead of staring at a spinner for 6+
    seconds. The first yield is a "starting" indicator; intermediate
    yields are the notifier-token deltas captured during generation;
    the final yield is the artifact card (success) or error message
    (failure). Coalescing into the artifact card is the gateway
    dispatcher's job — see ``gateway/run.py:9017`` for the branch
    logic that detects ``hasattr(result, "__aiter__")``.

    Backwards compatibility: callers that pass this through
    ``asyncio.run(_create_via_script_author(...))`` get the same
    final coalesced string as before (the dispatcher's last return
    is the artifact card). The new behavior only activates when the
    gateway dispatcher's async-iterator branch consumes the generator
    directly.

    Args:
        runtime:        The WorkflowRuntime singleton.
        rest:           Tokens after the ``create`` subcommand. The
                        intent is ``" ".join(rest)``.
        script_author:  Optional ScriptAuthor instance. When None,
                        fall back to manual-copy guidance.

    Returns:
        AsyncIterator[str] — yields in-progress deltas, then the
        final artifact card. Returns None (synchronously) when the
        intent is empty or script_author is unwired.
    """
    intent = " ".join(rest).strip()
    if not intent:
        return None  # empty intent — caller falls back to usage string

    if script_author is None:
        # Return a one-shot string via an async generator so the
        # dispatcher's branch logic handles this uniformly.
        async def _guidance():
            yield (
                "create is a v0.2.0 feature requiring the ScriptAuthor "
                "integration. Save your script to "
                "~/.hermes/workflows/<name>.py and use "
                "`/workflow run <path>`."
            )
        return _guidance()

    async def _stream():
        # Capture token deltas into a list as the LLM streams. The
        # notifier is wired BEFORE generate() starts so we don't miss
        # the early tokens. Chain the new capture onto the prior
        # notifier (don't replace it) so any externally-attached
        # notifier still sees the full event sequence — the slash
        # surface is additive, not destructive.
        captured: list[str] = []
        prev_notifier: Any = getattr(script_author, "notifier", None)

        def _notifier(kind: str, **payload: Any) -> None:
            if kind == "token":
                delta = payload.get("delta", "")
                if delta:
                    captured.append(delta)
            # Forward to prior notifier so externally-attached
            # observers (statusbar, journal) still see the full event
            # sequence. Wrap in try/except so a downstream failure
            # can't kill our token capture.
            if prev_notifier is not None:
                try:
                    prev_notifier(kind, **payload)
                except Exception:
                    pass

        script_author.notifier = _notifier

        # Yield a starting indicator so the user sees activity
        # immediately, before the first token arrives.
        yield f"🔨 generating workflow from: {intent!r}\n"

        try:
            # Run the async generate as a background task; flush
            # captured tokens as a parallel consumer. We use a task so
            # the generator can yield between captures.
            gen_task = asyncio.create_task(
                script_author.generate(intent=intent, runtime=runtime)
            )

            # Flush captured tokens until generate() completes.
            last_idx = 0
            while not gen_task.done():
                await asyncio.sleep(0)
                while last_idx < len(captured):
                    yield captured[last_idx]
                    last_idx += 1

            # Drain any remaining tokens after completion.
            while last_idx < len(captured):
                yield captured[last_idx]
                last_idx += 1

            result = gen_task.result()
        except Exception as e:
            yield (
                f"error invoking ScriptAuthor: {e}\n"
                f"(intent was: {intent!r})"
            )
            return
        finally:
            # Restore the prior notifier (defensive: don't leak our
            # capture closure if the dispatcher reuses the handler).
            try:
                script_author.notifier = prev_notifier
            except Exception:
                pass

        if result.ok:
            za_run_id = result.run_id
            # Surface script body inline so the user sees what was
            # generated (the inline-ground-truth pattern from Pitfall
            # #23 in hermes-workflow-author). Truncate to 1200 chars
            # with a follow-up pointer.
            body_preview = ""
            try:
                with open(result.script_path, "r", encoding="utf-8") as fh:
                    body_preview = fh.read(1200)
            except Exception:
                body_preview = ""
            # Emit the artifact-posted notifier event so the
            # streamer surfaces the posted file path and body
            # preview. No-op when script_author has no wired notifier
            # (the CLI shell path).
            notifier = getattr(script_author, "notifier", None)
            if notifier is not None:
                try:
                    notifier("artifact_posted", name=result.name,
                              path=result.script_path, run_id=za_run_id,
                              body_preview=body_preview)
                except Exception as _exc:
                    _log.warning("artifact_posted notifier raised: %s",
                                 _exc)
            yield (
                f"\n✅ generated {result.name!r}, run_id={za_run_id}\n"
                f"script saved at {result.script_path}\n\n"
                f"```python\n{body_preview}\n```\n\n"
                f"follow with `/workflow status {za_run_id}`"
            )
            return

        # Failure: surface error_stage + a raw_script preview so the
        # user can see what the LLM generated before the gate
        # rejected it.
        preview = (result.raw_script[:300] if result.raw_script else "")
        suffix = (
            f"\n\n--- generated script (first 300 chars) ---\n{preview}"
            if preview
            else ""
        )
        yield (
            f"ScriptAuthor failed at stage={result.error_stage!r}: "
            f"{result.error}{suffix}"
        )

    return _stream()


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