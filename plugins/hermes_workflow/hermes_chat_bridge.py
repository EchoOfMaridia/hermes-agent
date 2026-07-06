"""HermesChatBridge — pluggable LLM bridge that shells out to `hermes chat`.

This bridge lets workflow scripts call ``ctx.runtime.ask_agent()`` and have
the prompt actually reach a live LLM, even though the default
``AgentBridge`` is a stub that raises ``NotImplementedError: no agent
bridge configured``.

The implementation spawns ``hermes chat -q <prompt> -Q -m <model>`` as a
subprocess and returns the captured stdout as the ``text`` field of an
``AgentResponse``. Tool calls aren't supported in this minimal bridge —
the workflow scripts that use it (e.g. ``agent_review``,
``agent_synthesize_chart``) only need a single text response.

Activation
----------

The bridge is wired automatically by ``runtime_factory.build_runtime``
when either of these env vars is set:

    HERMES_WORKFLOW_AGENT_BRIDGE=hermes-chat    (recommended)

or

    HERMES_WORKFLOW_AGENT_BRIDGE=stub           (uses a deterministic stub
                                                  that returns "AGENT_BRIDGE_DISABLED"
                                                  — useful for tests)

If the env var is unset or empty, no bridge is wired and ``ask_agent``
calls raise ``NotImplementedError`` (the original v0.1.0 behaviour).

Failure semantics
-----------------

If the subprocess exits non-zero or times out, the bridge raises
``RuntimeError`` so the workflow step fails loudly rather than silently
returning empty text. Timeouts default to 5 minutes (covers a typical
claude-sonnet 4 review of a 1-2 MB JSON file).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .agent_bridge import AgentBridge, AgentResponse


#: Default timeout for a single LLM call (seconds).
_DEFAULT_TIMEOUT_S = 300.0


def _find_hermes_binary() -> str | None:
    """Locate the ``hermes`` CLI on PATH. Returns None if not found."""
    return shutil.which("hermes")


class HermesChatBridge(AgentBridge):
    """Bridge that shells out to ``hermes chat -q <prompt> -Q``.

    The prompt is sent verbatim (after prepending the optional
    ``system_prompt``). The model's final response (no streaming, no
    banner) is returned as ``AgentResponse.text``.

    Args:
        default_model: Optional default model to use when the caller
                       doesn't supply one (e.g. ``"sonnet"``,
                       ``"anthropic/claude-sonnet-4"``). The env var
                       ``HERMES_WORKFLOW_AGENT_MODEL`` overrides this.
        timeout_s:     Hard timeout per call in seconds.
        extra_args:    Extra ``hermes chat`` flags injected on every
                       invocation. Useful for setting ``--toolsets``,
                       ``--provider``, etc.
    """

    def __init__(
        self,
        *,
        default_model: str | None = None,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        extra_args: tuple[str, ...] = (),
    ) -> None:
        self._hermes_bin = _find_hermes_binary()
        if self._hermes_bin is None:
            raise RuntimeError(
                "hermes CLI not found on PATH; install hermes-agent or set "
                "HERMES_WORKFLOW_AGENT_BRIDGE to 'stub' to disable the bridge"
            )
        self._default_model = (
            os.environ.get("HERMES_WORKFLOW_AGENT_MODEL") or default_model
        )
        self._timeout_s = timeout_s
        self._extra_args = extra_args

    async def invoke(
        self,
        *,
        prompt: str,
        model: str | None,
        max_tokens: int | None,
        tools: list[dict] | None = None,
        session_key: str | None = None,
        system_prompt: str | None = None,
    ) -> AgentResponse:
        """Run ``hermes chat -q <prompt>`` and return its response.

        ``tools``, ``session_key``, and ``max_tokens`` are accepted for
        signature compatibility with ``AgentBridge.invoke`` but are not
        honoured by this minimal bridge. The subprocess always runs in
        one-shot non-interactive mode (``-q`` + ``-Q``).
        """
        # Compose the full prompt: system_prompt (if any) + user prompt.
        full_prompt_parts: list[str] = []
        if system_prompt:
            full_prompt_parts.append(f"[system]\n{system_prompt}\n[/system]\n")
        full_prompt_parts.append(prompt)
        full_prompt = "\n".join(full_prompt_parts)

        # Build argv. -q sets the prompt, -Q suppresses banner/spinner
        # so we can capture stdout cleanly, -m picks the model.
        argv: list[str] = [self._hermes_bin, "chat", "-q", full_prompt, "-Q"]
        chosen_model = model or self._default_model
        if chosen_model:
            argv.extend(["-m", chosen_model])
        argv.extend(list(self._extra_args))

        start = time.time()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"failed to spawn hermes CLI: {exc}"
            ) from exc

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=self._timeout_s
            )
        except asyncio.TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise RuntimeError(
                f"hermes chat timed out after {self._timeout_s}s "
                f"(model={chosen_model}, prompt_chars={len(full_prompt)})"
            ) from exc

        duration = time.time() - start
        stdout = stdout_b.decode("utf-8", errors="replace").strip()
        stderr = stderr_b.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            raise RuntimeError(
                f"hermes chat exited {proc.returncode} "
                f"(model={chosen_model}, prompt_chars={len(full_prompt)}): "
                f"{stderr or '(no stderr)'}"
            )

        # Best-effort token accounting: real tokens aren't exposed by the
        # -q CLI; we report approximate values based on character counts.
        approx_in = len(full_prompt) // 4
        approx_out = len(stdout) // 4

        return AgentResponse(
            text=stdout,
            tool_calls=(),
            tokens_in=approx_in,
            tokens_out=approx_out,
            duration=duration,
        )


class StubBridge(AgentBridge):
    """Deterministic bridge for tests / dry-runs.

    Returns a fixed PASS-verdict markdown payload that includes all
    the anchor strings the verifier expects (long enough to satisfy
    the 80-char minimum on agent_response length). The exact text is
    shaped so the subsequent ``agent_synthesize_chart`` step still
    has something to work with — it just won't be a real review.

    Operators set ``HERMES_WORKFLOW_AGENT_BRIDGE=stub`` to skip LLM
    review (useful for CI dry-runs where token spend is undesirable).
    """

    _DEFAULT_REVIEW_TEXT = (
        "## Verdict\n\n"
        "PASS (stub bridge — no LLM review performed)\n\n"
        "## indirection_layer_verdict\n\n"
        "- PipelineHandle -> Pipeline: stub-confirmed\n"
        "- ContentHandle -> MultimodalContent: stub-confirmed\n"
        "- BinaryHandle -> BinaryContent: stub-confirmed\n"
        "- PipeHandle -> Pipe: stub-confirmed\n\n"
        "## false_positives\n\n"
        "no false positives\n\n"
        "## missed_members\n\n"
        "no missed members\n\n"
        "## module_attribution_corrections\n\n"
        "no corrections\n"
    )

    #: Markdown that contains every anchor the verifier expects. The
    #: ``agent_synthesize_chart`` step writes whatever the bridge
    #: returns to ``coverage_chart.md``; the verifier on that step
    #: grep-checks for these literal strings. Real LLM responses will
    #: include richer content; the stub just needs to satisfy the
    #: anchor check so dry-runs can complete end-to-end.
    _DEFAULT_CHART_TEXT = (
        "# TPipe ABI Module Audit — Coverage Chart\n\n"
        "(Stub bridge — no LLM synthesis performed)\n\n"
        "## Per-Module Coverage\n\n"
        "| Module | Symbol Coverage | Implementation Coverage |\n"
        "|--------|-----------------|--------------------------|\n"
        "| TPipe | see module_coverage.json | see module_coverage.json |\n\n"
        "## Per-Class Breakdown\n\n"
        "See per_module_symbols/<module>.json for the full per-class "
        "breakdown. The stub bridge does not render a real chart — "
        "set HERMES_WORKFLOW_AGENT_BRIDGE=hermes-chat for the full "
        "LLM-rendered output.\n\n"
        "## Implementation Coverage\n\n"
        "Implementation coverage is computed by joining the Kotlin "
        "public surface against the @CEntryPoint methods in "
        "TPipeBootstrap.java. See module_coverage.json for the "
        "current numbers.\n\n"
        "## Symbol Coverage\n\n"
        "Symbol coverage is computed by joining the Kotlin public "
        "surface against the declared symbols in tpipe-abi.h. See "
        "module_coverage.json for the current numbers.\n"
    )

    def __init__(self, response_text: str | None = None) -> None:
        self._response_text = response_text or self._DEFAULT_REVIEW_TEXT

    async def invoke(
        self,
        *,
        prompt: str,
        model: str | None,
        max_tokens: int | None,
        tools: list[dict] | None = None,
        session_key: str | None = None,
        system_prompt: str | None = None,
    ) -> AgentResponse:
        # Heuristic: the agent_synthesize_chart prompt explicitly asks
        # for a markdown chart with "Per-Module Coverage", "Per-Class
        # Breakdown", etc. We detect that and return the chart-shaped
        # response. Otherwise return the review-shaped response.
        if "Per-Module Coverage" in prompt or "rendering the final markdown chart" in prompt:
            response_text = self._DEFAULT_CHART_TEXT
        else:
            response_text = self._DEFAULT_REVIEW_TEXT
        # Allow the operator to override either default.
        if self._response_text != self._DEFAULT_REVIEW_TEXT:
            response_text = self._response_text
        return AgentResponse(
            text=response_text,
            tool_calls=(),
            tokens_in=len(prompt) // 4,
            tokens_out=len(response_text) // 4,
            duration=0.0,
        )


def build_bridge_from_env() -> AgentBridge | None:
    """Read HERMES_WORKFLOW_AGENT_BRIDGE and return the matching bridge.

    Returns None if the env var is unset (preserving v0.1.0 behaviour).

    Accepted values:
        "hermes-chat"   → HermesChatBridge
        "stub"          → StubBridge
        "" / unset      → None
        anything else   → None (unknown value, default to no bridge)
    """
    choice = os.environ.get("HERMES_WORKFLOW_AGENT_BRIDGE", "").strip().lower()
    if not choice:
        return None
    if choice in ("hermes-chat", "hermes_chat", "chat"):
        return HermesChatBridge()
    if choice == "stub":
        return StubBridge()
    return None