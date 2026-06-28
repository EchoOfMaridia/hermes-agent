"""Gateway message-reaction handler.

Subscribes to incoming gateway messages and auto-invokes workflows
when the message matches one of two patterns:

    "workflow: <intent>"        -> ad-hoc generation (v0.2.0 stub)
    "/workflow <args>"           -> slash command passthrough

The handler is invoked via the pre_gateway_dispatch hook registered
in __init__.py. Hermes passes (event, gateway, session_store); our
hook inspects event.text and may:

  - Return None to pass through (no workflow match).
  - Return {"action": "rewrite", "text": "..."} to replace the message
    text (e.g., inject a workflow invocation).

For v0.1.0, the handler is read-only on the message stream: it
inspects text and returns None unless the pattern matches, in which
case it records the dispatch decision. The actual workflow invocation
happens via inject_message() (queued into the conversation) or directly
via runtime.submit() in a follow-up message.

v0.2.0 ad-hoc mode requires the script-author integration. Until that
ships, ``workflow: <intent>`` returns a v0.2.0 stub message.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from typing import Any, Callable


_log = logging.getLogger("hermes_workflow.gateway")

PATTERN_AD_HOC = "workflow:"
PATTERN_SLASH = "/workflow"


def build_gateway_handler(runtime: Any, script_author: Any | None = None
                              ) -> Callable[[Any, Any], Any]:
    """Build the pre_gateway_dispatch hook callable.

    Returns a function suitable for registering as
    ``ctx.register_hook("pre_gateway_dispatch", handler)``.

    When ``script_author`` is provided, the ``workflow: <intent>``
    pattern triggers ScriptAuthor.generate(); otherwise it returns
    a v0.2.0 stub guidance message.
    """

    async def hook(event: Any, kwargs: dict) -> dict | None:
        """Inspect an incoming message event. Return None to pass through,
        or a dict to influence flow per the spec in hermes_cli/plugins.py.
        """
        text = _extract_text(event)
        if not text:
            return None

        # Pattern 1: "workflow: <intent>" — ad-hoc generation.
        if text.lower().startswith(PATTERN_AD_HOC):
            intent = text[len(PATTERN_AD_HOC):].strip()
            return await _handle_ad_hoc(event, runtime, intent, kwargs,
                                          script_author)

        # Pattern 2: "/workflow <args>" — slash command passthrough.
        if text.lower().startswith(PATTERN_SLASH):
            args_str = text[len(PATTERN_SLASH):].strip()
            return await _handle_slash_passthrough(
                event, runtime, args_str, kwargs,
            )

        return None

    return hook


def _extract_text(event: Any) -> str:
    """Pull the text payload out of an event regardless of its shape."""
    if event is None:
        return ""
    if isinstance(event, str):
        return event
    for attr in ("text", "content", "message", "body"):
        if hasattr(event, attr):
            value = getattr(event, attr)
            if isinstance(value, str):
                return value
    return ""


async def _handle_ad_hoc(event: Any, runtime: Any, intent: str,
                           kwargs: dict,
                           script_author: Any | None) -> dict | None:
    """Handle 'workflow: <intent>' message.

    When script_author is provided, generate a workflow script from
    the intent via the LLM, save it to the library, submit it to the
    runtime, and rewrite the event with a status message.

    When script_author is None, return the v0.2.0 stub guidance.
    """
    if script_author is None:
        guidance = (
            f"📝 workflow: {intent}\n\n"
            "(ad-hoc generation requires ScriptAuthor; not available "
            "in this environment. Save your script to "
            "~/.hermes/workflows/<name>.py and use "
            "`/workflow run <path>`.)"
        )
        _log.info("gateway_handler: ad-hoc intent=%r (no ScriptAuthor)", intent)
        return {"action": "rewrite", "text": guidance}

    # Real ScriptAuthor path.
    try:
        result = await script_author.generate(
            intent=intent, runtime=runtime,
        )
    except Exception as e:
        _log.warning("ScriptAuthor.generate failed: %s", e)
        return {"action": "rewrite",
                "text": f"📝 workflow: {intent}\n\nerror: {e}"}

    if result.ok:
        text = (
            f"📝 workflow: {intent}\n\n"
            f"✅ generated {result.name!r}, run_id={result.run_id}\n"
            f"script saved at {result.script_path}\n\n"
            f"follow with `/workflow status {result.run_id}`"
        )
        return {"action": "rewrite", "text": text}
    else:
        # Failure: surface the error stage + script preview for debugging.
        preview = (result.raw_script[:300] if result.raw_script
                    else "(no script produced)")
        text = (
            f"📝 workflow: {intent}\n\n"
            f"❌ {result.error_stage}: {result.error}\n\n"
            f"script preview:\n```\n{preview}\n```"
        )
        return {"action": "rewrite", "text": text}


async def _handle_slash_passthrough(event: Any, runtime: Any, args_str: str,
                                       kwargs: dict) -> dict | None:
    """Handle '/workflow <args>' slash command from a gateway surface.

    We don't dispatch the workflow inline here — gateway message
    handlers must not block. Instead we rewrite the message text to
    be the canonical form, then let normal dispatch continue. The
    actual workflow runs in the same conversation via inject_message.

    For v0.1.0, the slash command format is preserved; the workflow
    is queued via the active CLI's _pending_input (via
    PluginContext.inject_message).
    """
    tokens = shlex.split(args_str) if args_str else []
    if not tokens:
        return None
    sub = tokens[0].lower()
    if sub in ("run", "list", "status", "snapshot", "cancel",
                "inspect", "save", "expand"):
        # Forward to the slash handler via inject_message.
        _log.info("gateway_handler: slash passthrough /workflow %s", args_str)
        return {"action": "allow"}
    return None
