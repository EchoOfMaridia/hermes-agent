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
        text:         Final text response from the agent.
        tool_calls:   Tuple of structured tool-call records invoked during
                      the response. Each entry is a dict with keys:
                        - ``name``:   str — tool name (e.g., "terminal",
                                      "file_edit", "search")
                        - ``args``:   dict — arguments passed to the tool
                        - ``result``: str — result returned by the tool
                                      (may be empty string for tools that
                                      produce no output)
                      Legacy v0.1.0 callers stored ``tool_calls`` as a
                      tuple of strings (just names). The new contract is
                      ``tuple[dict, ...]`` — downstream consumers that read
                      ``r.tool_calls[0]["name"]`` will fail loudly on the
                      legacy shape, which is the intended migration signal.
        tokens_in:    Input tokens consumed.
        tokens_out:   Output tokens produced.
        duration:     Wall-clock seconds.
        parsed:       Optional parsed structured-output object. Populated
                      when the bridge was called with json_schema= and the
                      response was successfully parsed against the schema.
                      None when no schema was requested OR parsing failed
                      (in which case the failure is journaled but the
                      bridge does not raise — the workflow author can
                      still inspect r.text directly). Use
                      ``ctx.runtime.parse_structured(r, schema=...)`` to
                      retry the parse with a different schema.
        content_type: "json" when parsed is set (parse succeeded);
                      "text" otherwise. Mirrors
                      ``PluginLlmStructuredResult.content_type`` so callers
                      have a single uniform shape.
    """

    text: str
    tool_calls: tuple[dict, ...] = ()
    tokens_in: int = 0
    tokens_out: int = 0
    duration: float = 0.0
    parsed: Any | None = None
    content_type: str = "text"


class AgentBridge:
    """Pluggable bridge between the workflow runtime and the hermes agent.

    Subclass and override `invoke` to plug in a real agent. The default
    implementation records the call in the journal and raises
    NotImplementedError, so test code can exercise the journal path
    without needing a live agent.
    """

    async def invoke(self, *, prompt: str, model: str | None,
                     max_tokens: int | None,
                     tools: list[dict] | None = None,
                     session_key: str | None = None,
                     system_prompt: str | None = None,
                     json_schema: dict | None = None,
                     schema_name: str | None = None) -> AgentResponse:
        """Invoke the agent. Override in subclasses.

        Args:
            prompt:        The user prompt to send to the agent.
            model:         Optional model override (e.g., "sonnet", "opus").
                           None = use the runtime default.
            max_tokens:    Optional cap on response tokens.
            tools:         Optional list of tool definitions to expose
                           to the agent during this call. Each entry is a
                           dict with ``name``, ``description``, and
                           ``schema`` (JSON Schema for the tool's args).
                           When provided, the agent can call these tools
                           and the resulting tool_calls appear on the
                           returned ``AgentResponse.tool_calls`` as
                           ``{"name", "args", "result"}`` records.
                           None (the default) means no tools — the agent
                           is a pure text-completer, matching v0.1.0
                           behaviour. The list lives in-memory only;
                           the journal records just ``tools_count``.
            session_key:   Optional opaque string that ties this call
                           to a multi-turn conversation. Successive
                           calls with the same ``session_key`` share
                           message history (when the inner bridge
                           supports it). None = one-shot call, no
                           threading.
            system_prompt: Optional system prompt override for this
                           call. None = use the inner bridge default.
            json_schema:   Optional JSON Schema describing the structured
                           output the caller wants. When provided, the
                           bridge SHOULD request JSON-mode or
                           json_schema-mode from the provider (wire-level
                           enforcement when supported), AND SHOULD
                           populate the returned ``AgentResponse.parsed``
                           with the deserialized object. Bridges that
                           cannot do wire-level enforcement (e.g. the
                           subprocess ``HermesChatBridge``) MUST paste
                           the schema into the prompt as a fallback.
                           None = unstructured text (v0.1.0 behaviour).
            schema_name:   Optional human-readable name for the schema.
                           Used by the wire-format layer to label the
                           schema constraint. Defaults to None.

        Returns:
            AgentResponse with the final text, parsed object (when
            json_schema is set and parsing succeeded), content_type
            ("json" or "text"), and metadata.

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
                     max_tokens: int | None,
                     tools: list[dict] | None = None,
                     session_key: str | None = None,
                     system_prompt: str | None = None,
                     json_schema: dict | None = None,
                     schema_name: str | None = None) -> AgentResponse:
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
        # - tools_count: number of tool definitions the agent had
        #   access to during this call. The full tool schemas live
        #   in-memory only (forwarded to the inner bridge) so the
        #   journal stays compact; live surfaces (desktop subagent
        #   windows, gateway stream, terminal output) get the full
        #   list from the in-memory AgentResponse on response.
        # - session_key: optional threading key that ties this call
        #   to a multi-turn conversation. Verifiers use it to
        #   correlate "step 3 was the 4th turn in session X."
        # - system_prompt_chars: privacy-preserving length. The
        #   preview is NOT journaled even when opt-in is set —
        #   system prompts are operational, not user data, but the
        #   full text is rarely needed for verification.
        # - schema_name + has_json_schema: structured-output contract
        #   for this call. Verifiers use these to correlate the
        #   agent_response's parsed_shape against the requested
        #   schema. has_json_schema is True iff json_schema is set;
        #   schema_name may be None when the caller didn't supply one.
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
                "tools_count": len(tools) if tools else 0,
                "session_key": session_key,
                "system_prompt_chars": len(system_prompt) if system_prompt else 0,
                "schema_name": schema_name,
                "has_json_schema": json_schema is not None,
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
            tools=tools, session_key=session_key,
            system_prompt=system_prompt,
            json_schema=json_schema, schema_name=schema_name,
        )

        # Record the agent_response event. Tool calls are journaled as
        # structured records (name/args/result_chars) rather than just
        # names — verifiers and replay tools need the args+result to
        # reconstruct what the agent actually did. The ``result_chars``
        # field is the length only; the full ``result`` text is kept
        # in-memory on AgentResponse.tool_calls for live surfaces but
        # not journaled (results can be large — e.g. ``pytest`` output).
        if run is not None:
            tool_calls_records = []
            for tc in (response.tool_calls or ()):
                if isinstance(tc, dict):
                    # New structured shape.
                    tool_calls_records.append({
                        "name": tc.get("name", ""),
                        "args": tc.get("args", {}),
                        "result_chars": len(tc.get("result", "")),
                    })
                else:
                    # Legacy shape (just a name string). Preserve as
                    # a minimal record so old callers don't break the
                    # journal write.
                    tool_calls_records.append({
                        "name": str(tc),
                        "args": {},
                        "result_chars": 0,
                    })
            # parsed_shape captures the SHAPE of the structured-output
            # payload — dict keys, "<list>" sentinel for arrays, or
            # the type name for scalars. Never values (the journal
            # stays privacy-preserving and small).
            parsed_shape: list[str] = []
            if response.parsed is not None:
                if isinstance(response.parsed, dict):
                    parsed_shape = list(response.parsed.keys())
                elif isinstance(response.parsed, list):
                    parsed_shape = ["<list>"]
                else:
                    parsed_shape = [type(response.parsed).__name__]
            run.journal.append({
                "kind": Journal.KIND_AGENT_RESPONSE,
                "run_id": run.run_id,
                "step": run.current_step_name,
                "call_index": call_index,
                "tool_calls": tool_calls_records,
                "tokens_in": response.tokens_in,
                "tokens_out": response.tokens_out,
                "duration": time.time() - start,
                "text_chars": len(response.text),
                "text_preview": _prompt_preview(response.text),
                "content_type": response.content_type,
                "parsed_shape": parsed_shape,
            })
            run.touch()

        return response
