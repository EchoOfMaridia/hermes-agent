"""Agent bridge.

The runtime exposes a single LLM-facing surface: `ask_agent`. Workflow
scripts call `await ctx.runtime.ask_agent(prompt=..., model=...)` to invoke
an agent. The actual agent invocation is plugged in here.

In v0.1.0 the bridge is a stub that records the call in the journal and
raises NotImplementedError. The hermes-agent integration is a follow-on
that replaces the stub body with the actual hermes agent invocation.

What v0.1.0 ships:

1. `AgentResponse` — typed result returned by ask_agent. Carries the
   final text, the prompt that produced it, the model used, and the list
   of tool calls the agent made (for verifier correlation).

2. `ask_agent` — a coroutine method on WorkflowRuntime. Calls into the
   bridge; the bridge journals the call and (for now) raises
   NotImplementedError. When the integration lands, the raise is replaced
   with a real hermes-agent call.

3. The journal contract: every ask_agent call MUST be journaled with both
   an `agent_call` event (prompt + model) and an `agent_response` event
   (response + tool_calls + tokens). Verifiers correlate the Evidence
   claim against this trail.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any


def _prompt_preview(text: str, *, max_chars: int | None = None) -> str | None:
    """Return a privacy-aware preview of *text*, or None if disabled.

    The default is None (no preview persisted). Set
    HERMES_WORKFLOW_PROMPT_PREVIEW_CHARS in the environment to a positive
    integer to opt in to a leading-char preview. Setting it to 0 disables
    previews explicitly (same effect as None but recorded for clarity).

    The journal persists the prompt_chars length regardless; the preview
    is the only place prompt content touches the journal.
    """
    if max_chars is None:
        env = os.environ.get("HERMES_WORKFLOW_PROMPT_PREVIEW_CHARS", "").strip()
        if not env:
            return None
        try:
            max_chars = int(env)
        except ValueError:
            return None
    if max_chars <= 0:
        return None
    return text[:max_chars]


@dataclass
class AgentResponse:
    """Result of an ask_agent call. Returned to the workflow step.

    Attributes:
        text:       Final text response from the agent.
        tool_calls: Tuple of tool names invoked during the response.
        tokens_in:  Input tokens consumed.
        tokens_out: Output tokens produced.
        duration:   Wall-clock seconds.
    """

    text: str
    tool_calls: tuple[str, ...] = ()
    tokens_in: int = 0
    tokens_out: int = 0
    duration: float = 0.0


class AgentBridge:
    """Pluggable bridge between the workflow runtime and the hermes agent.

    Subclass and override `invoke` to plug in a real agent. The default
    implementation records the call in the journal and raises
    NotImplementedError, so test code can exercise the journal path
    without needing a live agent.
    """

    async def invoke(self, *, prompt: str, model: str | None,
                     max_tokens: int | None) -> AgentResponse:
        """Invoke the agent. Override in subclasses.

        Args:
            prompt:     The user prompt to send to the agent.
            model:      Optional model override (e.g., "sonnet", "opus").
                        None = use the runtime default.
            max_tokens: Optional cap on response tokens.

        Returns:
            AgentResponse with the final text and metadata.

        Raises:
            NotImplementedError: by default; subclasses override.
        """
        raise NotImplementedError(
            "AgentBridge.invoke must be overridden by a subclass. "
            "v0.1.0 ships the journal contract; the hermes-agent "
            "integration is a follow-on."
        )


class JournalingBridge(AgentBridge):
    """Default bridge used by WorkflowRuntime. Wraps another bridge and
    journals every call.

    The wrapped bridge is responsible for the actual LLM call. The
    JournalingBridge records the agent_call and agent_response events
    into the active Run's journal so verifiers can correlate Evidence
    claims with the actual tool-call trail.
    """

    def __init__(self, inner: AgentBridge | None = None) -> None:
        self._inner = inner

    @property
    def inner(self) -> AgentBridge | None:
        return self._inner

    def set_inner(self, inner: AgentBridge) -> None:
        """Replace the wrapped bridge (e.g., after hermes-agent integration
        is wired)."""
        self._inner = inner

    async def invoke(self, *, prompt: str, model: str | None,
                     max_tokens: int | None) -> AgentResponse:
        from .dsl.primitives import get_current_run
        from .journal import Journal

        run = get_current_run()
        start = time.time()

        # Record the agent_call event. We journal:
        # - step + call_index: needed for the visibility layer to
        #   attribute each LLM call to the right @step in the tree.
        # - prompt_chars: privacy-preserving summary (length only).
        # - prompt_preview: optional first N chars, opt-in via
        #   HERMES_WORKFLOW_PROMPT_PREVIEW_CHARS env var. Defaults to
        #   None (no preview) — prompts can contain sensitive data.
        if run is not None:
            call_index = run.next_agent_call_index()
            run.journal.append({
                "kind": Journal.KIND_AGENT_CALL,
                "run_id": run.run_id,
                "step": run.current_step_name,
                "call_index": call_index,
                "prompt_chars": len(prompt),
                "prompt_preview": _prompt_preview(prompt),
                "model": model,
                "max_tokens": max_tokens,
            })
            run.touch()

        # Invoke the inner bridge. If None, raise the default stub error.
        if self._inner is None:
            raise NotImplementedError(
                "no agent bridge configured; workflow scripts calling "
                "ctx.runtime.ask_agent() need the hermes-agent integration"
            )

        response = await self._inner.invoke(
            prompt=prompt, model=model, max_tokens=max_tokens,
        )

        # Record the agent_response event.
        if run is not None:
            run.journal.append({
                "kind": Journal.KIND_AGENT_RESPONSE,
                "run_id": run.run_id,
                "step": run.current_step_name,
                "call_index": call_index,
                "tool_calls": list(response.tool_calls),
                "tokens_in": response.tokens_in,
                "tokens_out": response.tokens_out,
                "duration": time.time() - start,
                "text_chars": len(response.text),
                "text_preview": _prompt_preview(response.text),
            })
            run.touch()

        return response
