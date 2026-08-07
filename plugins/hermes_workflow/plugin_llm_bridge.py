"""PluginLlmBridge — in-process bridge that wraps ctx.llm.acomplete_structured.

This is the load-bearing bridge for structured-output enforcement. When
the workflow plugin runs inside an active Hermes session (i.e.
``ctx.llm`` is reachable), the runtime factory auto-wires this bridge
FIRST. It:

1. Calls ``ctx.llm.acomplete_structured()`` with the supplied
   ``json_schema`` and ``schema_name``. The host (``agent/plugin_llm.py``)
   handles wire-level ``response_format`` enforcement, system-prompt
   injection, and post-parse schema validation when ``jsonschema`` is
   installed.
2. Translates ``PluginLlmStructuredResult`` -> ``AgentResponse``, copying
   ``parsed`` and ``content_type`` so downstream workflow code can do
   ``response.parsed["intent"]`` directly.

Activation: ``runtime_factory.build_runtime()`` prefers this bridge when
``ctx.llm`` is reachable. Operators can force the subprocess bridge with
``HERMES_WORKFLOW_AGENT_BRIDGE=hermes-chat`` or the stub with
``HERMES_WORKFLOW_AGENT_BRIDGE=stub``.

Failure semantics: when ``ctx.llm.acomplete_structured`` raises, the
bridge re-raises so the workflow step fails loudly. When parsing inside
the host fails (parsed is None despite json_schema being set), the bridge
still returns an AgentResponse — the caller can re-parse via
``ctx.runtime.parse_structured(response, schema=...)`` to retry with
the same or a different schema.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .agent_bridge import AgentBridge, AgentResponse


_log = logging.getLogger(__name__)


class PluginLlmBridge(AgentBridge):
    """Bridge that calls ctx.llm.acomplete_structured in-process.

    Args:
        llm: A PluginLlm facade (or any object exposing the same
             ``acomplete_structured(**kwargs)`` async method). Falls
             back to ``acomplete`` for plain-text calls when no
             ``json_schema`` was supplied.
    """

    def __init__(self, llm: Any) -> None:
        self._llm = llm

    async def invoke(
        self,
        *,
        prompt: str,
        model: str | None,
        max_tokens: int | None,
        tools: list[dict] | None = None,
        session_key: str | None = None,
        system_prompt: str | None = None,
        json_schema: dict | None = None,
        schema_name: str | None = None,
    ) -> AgentResponse:
        """Call ctx.llm.acomplete_structured and translate the result.

        ``tools``, ``session_key``, ``max_tokens``, and ``system_prompt``
        are accepted for signature compatibility with
        ``AgentBridge.invoke``. The structured-output path doesn't
        surface tools to the LLM as callable tools — the LLM is asked
        to produce JSON, not to act on the world.
        """
        if json_schema is not None or schema_name is not None:
            return await self._invoke_structured(
                prompt=prompt,
                model=model,
                max_tokens=max_tokens,
                system_prompt=system_prompt,
                json_schema=json_schema,
                schema_name=schema_name,
            )
        return await self._invoke_text(
            prompt=prompt,
            model=model,
            max_tokens=max_tokens,
            system_prompt=system_prompt,
        )

    async def _invoke_structured(
        self,
        *,
        prompt: str,
        model: str | None,
        max_tokens: int | None,
        system_prompt: str | None,
        json_schema: dict | None,
        schema_name: str | None,
    ) -> AgentResponse:
        # PluginLlmStructuredResult requires non-empty instructions and
        # at least one input block. Compose a single text input from the
        # prompt.
        try:
            from agent.plugin_llm import PluginLlmTextInput
        except ImportError as exc:  # pragma: no cover — host import failure
            raise RuntimeError(
                "PluginLlmBridge requires the host's agent.plugin_llm "
                "module (ctx.llm unavailable in this environment)"
            ) from exc

        inputs: list[Any] = [PluginLlmTextInput(text=prompt)]

        kwargs: dict[str, Any] = dict(
            instructions=prompt,
            input=inputs,
            json_schema=json_schema,
            json_mode=json_schema is not None,
            schema_name=schema_name,
            model=model,
            max_tokens=max_tokens,
            purpose="hermes_workflow.step",
        )
        if system_prompt:
            kwargs["system_prompt"] = system_prompt

        result = await self._llm.acomplete_structured(**kwargs)
        return AgentResponse(
            text=result.text,
            tool_calls=(),
            tokens_in=result.usage.input_tokens,
            tokens_out=result.usage.output_tokens,
            duration=0.0,
            parsed=result.parsed,
            content_type=result.content_type,
        )

    async def _invoke_text(
        self,
        *,
        prompt: str,
        model: str | None,
        max_tokens: int | None,
        system_prompt: str | None,
    ) -> AgentResponse:
        # Plain-text path: prefer async acomplete when the facade
        # exposes it; fall back to a thread-pool call to .complete for
        # callers that only expose the synchronous entrypoint.
        messages: list[dict[str, Any]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        acomplete = getattr(self._llm, "acomplete", None)
        if acomplete is not None:
            result = await acomplete(
                messages,
                model=model,
                max_tokens=max_tokens,
                purpose="hermes_workflow.step",
            )
            return AgentResponse(
                text=result.text,
                tool_calls=(),
                tokens_in=result.usage.input_tokens,
                tokens_out=result.usage.output_tokens,
                duration=0.0,
                parsed=None,
                content_type="text",
            )

        complete = getattr(self._llm, "complete", None)
        if complete is None:
            raise RuntimeError(
                "PluginLlmBridge requires llm.acomplete, llm.complete, "
                "or llm.acomplete_structured"
            )

        result = await asyncio.to_thread(
            complete,
            messages,
            model=model,
            max_tokens=max_tokens,
            purpose="hermes_workflow.step",
        )
        return AgentResponse(
            text=result.text,
            tool_calls=(),
            tokens_in=result.usage.input_tokens,
            tokens_out=result.usage.output_tokens,
            duration=0.0,
            parsed=None,
            content_type="text",
        )