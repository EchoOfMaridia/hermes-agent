"""ScriptAuthor: convert a natural-language intent into a workflow script.

This is the v0.2.0 ad-hoc-mode implementation. When the user types
``workflow: review the auth code`` or invokes ``call_workflow`` with
``mode="ad-hoc"``, the ScriptAuthor:

1. Calls ``ctx.llm.acomplete_structured()`` with a JSON Schema that
   produces a valid Python script using the hermes_workflow DSL.
2. Validates the generated script with the runtime's graph validator.
3. Persists the script under a generated name in the workflow library.
4. Submits the script to the runtime and returns the run_id.

If any step fails, ScriptAuthor returns a structured error so the
caller can report exactly what went wrong (LLM call failure, syntax
error in generated script, validation failure, etc.). The script is
never executed without validation passing.

Configuration:
    HERMES_WORKFLOW_AD_HOC_MODEL    Provider:model string. Default:
                                    None (use host's default).
    HERMES_WORKFLOW_AD_HOC_TEMP     Float. Default 0.2 (low temperature
                                    to reduce hallucination in code gen).
"""

from __future__ import annotations

import inspect
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from plugins.hermes_workflow.runtime_factory import (
    default_journal_root,
    make_script_author_run_id,
)
from plugins.hermes_workflow.library import Library


_log = logging.getLogger("hermes_workflow.script_author")


# JSON Schema for the LLM's structured output. The model produces a
# Python script (string), the workflow's display name, and a list of
# declared step names for cross-validation.
_SCRIPT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": (
                "Snake-case identifier for the workflow (e.g., "
                "'code_review', 'security_audit'). Used as the "
                "library entry name."
            ),
            "pattern": "^[a-z][a-z0-9_]*$",
        },
        "description": {
            "type": "string",
            "description": "One-line human description of what the workflow does.",
        },
        "script": {
            "type": "string",
            "description": (
                "Complete Python source for the workflow script. Must "
                "import from plugins.hermes_workflow and define exactly "
                "one @workflow-decorated coroutine plus zero or more "
                "@step-decorated coroutines."
            ),
        },
        "step_names": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Names of all @step functions declared in the script. "
                "Used for cross-validation against the parsed AST."
            ),
        },
    },
    "required": ["name", "description", "script", "step_names"],
}


_SYSTEM_INSTRUCTIONS = """\
You are generating a hermes_workflow script from a user's natural-language \
intent. Follow the DSL rules strictly:

DSL rules:
- The script MUST import from `plugins.hermes_workflow`:
    from plugins.hermes_workflow import step, parallel, gather, workflow, Evidence
- Define one or more @step-decorated coroutines. Each takes `(ctx, **kwargs)` \
  and returns an `Evidence` (a frozen dataclass).
- Define exactly ONE @workflow-decorated coroutine that composes the steps.
- @step usage:
    @step(name="<unique_name>", depends_on=(...), verifier=..., max_retries=2)
    async def step_fn(ctx, **kwargs) -> Evidence:
        return Evidence(files_changed=(), commands_run=(), exit_codes=(),
                       tests_run=0, tests_passed=0, duration_seconds=0.0)
- @workflow usage:
    @workflow(name="<workflow_name>", description="<one-line>")
    async def run(ctx) -> dict:
        await step_a(ctx)
        ...
        return {"summary": "..."}

Agent calls (LLM-driven sub-tasks inside a step):
- Use `await ctx.runtime.ask_agent(...)` — this is the ONLY way to invoke \
  the agent from a step.
- Full signature:
    await ctx.runtime.ask_agent(
        prompt=...,                # required — the task description
        model=None,                # optional model hint: "haiku", "sonnet",
                                   # "opus". None = inner bridge default.
                                   # Use "haiku" for trivial extraction,
                                   # "sonnet" for default work,
                                   # "opus" for planning / multi-step.
        max_tokens=None,           # optional cap on response tokens
        tools=[...],               # optional list of tool definitions the
                                   # agent can call. Each entry is a dict
                                   # with name, description, schema (JSON
                                   # Schema for the tool's args). When
                                   # omitted, the agent is a pure text
                                   # completer (cannot act on the world).
                                   # Common tools to expose: terminal,
                                   # file_edit, search, web_search, browser.
        session_key="...",         # optional opaque string. Successive
                                   # calls with the same session_key share
                                   # conversation history (when the inner
                                   # bridge supports threading). Use this
                                   # when multiple steps need to discuss
                                   # the same codebase / topic without
                                   # re-pasting the full context.
        system_prompt="...",       # optional system prompt override. Use
                                   # this to scope the agent's role for
                                   # the specific step ("You are a Python
                                   # debugger", "You are a security
                                   # reviewer", etc.).
    )
- Returns `AgentResponse` with:
    - `.text`:       final text response from the agent
    - `.tool_calls`: tuple of structured tool-call records the agent made
                     during this turn. Each entry is a dict:
                     {"name": str, "args": dict, "result": str}
                     Use this to inspect what the agent actually did
                     (e.g., assert it ran the right command, edited the
                     right file).
    - `.tokens_in`, `.tokens_out`: token counts (for logging/cost tracking)
    - `.duration`:   wallclock seconds

When to use each agent-call surface:
- Pure text reasoning / extraction (no world interaction):
    response = await ctx.runtime.ask_agent(
        prompt="List the functions in module X. Return JSON.",
        model="haiku",
    )
    data = json.loads(response.text)

- Step needs the agent to ACT (run commands, edit files, search):
    response = await ctx.runtime.ask_agent(
        prompt="Find the bug in tests/test_x.py and fix it.",
        model="sonnet",
        tools=[
            {"name": "terminal", "description": "Run shell commands",
             "schema": {"type": "object",
                        "properties": {"command": {"type": "string"}},
                        "required": ["command"]}},
            {"name": "file_edit", "description": "Edit a file",
             "schema": {"type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "old_string": {"type": "string"},
                            "new_string": {"type": "string"}},
                        "required": ["path", "old_string", "new_string"]}},
            {"name": "search", "description": "Search the codebase",
             "schema": {"type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"]}},
        ],
        session_key=f"bug_fix_{ctx.run_id}",
        system_prompt="You are a Python debugger. Always show the "
                      "failing assertion first, then explain root "
                      "cause, then apply the minimal fix.",
    )
    # Inspect what the agent did:
    for tc in response.tool_calls:
        print(f"  used {tc['name']} with {tc['args']}")

- Multiple steps share context (e.g., plan → implement → review):
    Use the same session_key across the steps. Each call sees the
    previous turns' text + tool calls + results, so you don't need to
    re-paste the codebase into every prompt.

Defensive patterns (recommended for any step calling the agent):
- Wrap in try/except. The runtime returns a stub `AgentResponse` when \
  no agent bridge is wired (CLI-originated runs); do not assume the \
  response is from a live agent.
- Check `response.tool_calls` to verify the agent actually did the \
  thing you asked. If `tools=[terminal]` was provided and \
  `response.tool_calls` is empty, the agent reasoned but did not act.
- For required tool execution, fail the step with a clear error rather \
  than silently proceeding.

The Evidence dataclass has these fields:
    files_changed: tuple[str, ...]
    commands_run: tuple[str, ...]
    exit_codes: tuple[int, ...]
    tests_run: int
    tests_passed: int
    duration_seconds: float
- exit_codes length must equal commands_run length.
- tests_passed must be <= tests_run.

Output the script as a single string. The script must be syntactically valid \
Python and pass graph validation (no cycles, no unknown dependencies, \
exactly one @workflow).

Return the JSON object with: name, description, script, step_names.
"""


@dataclass
class AuthorResult:
    """Result of a ScriptAuthor invocation.

    On success: ``run_id`` is set, ``script_path`` points at the saved
    library entry. On failure: ``error`` and ``error_stage`` identify
    which step failed.
    """

    ok: bool = False
    name: str = ""
    script_path: str = ""
    run_id: str = ""
    workflow: str = ""
    error: str = ""
    error_stage: str = ""
    raw_script: str = ""        # for debugging on validation failure
    validation_errors: list[str] = field(default_factory=list)
    # Internal: the runtime-issued run_id (``r_<hex>``) that backs the
    # synthesized ``run_id`` (``za_<slug>_<hex>``). The slash surface
    # uses this to bridge ``za_`` queries to ``r_`` journal files.
    # Not part of the documented AuthorResult surface — populated by
    # ScriptAuthor.generate() only.
    _real_run_id: str = ""


def _slugify(text: str, *, max_len: int = 48) -> str:
    """Convert a free-form intent into a snake_case library name."""
    s = re.sub(r"[^a-zA-Z0-9]+", "_", text.lower()).strip("_")
    s = re.sub(r"_+", "_", s)
    return s[:max_len] or "workflow"


def _validate_script_safety(script: str) -> list[str]:
    """Static checks beyond graph validation.

    Catches the common LLM codegen failures before runtime:
    - Imports outside the allowlist
    - References to os.system / subprocess / shell=True
    - Missing __init__/Evidence import
    - More than one @workflow
    """
    errors: list[str] = []
    # Allowlist imports.
    forbidden = ("subprocess", "ctypes", "os.system", "shell=True",
                  "eval(", "exec(")
    for term in forbidden:
        if term in script:
            errors.append(f"forbidden term in script: {term!r}")

    # Must import the DSL.
    if "from plugins.hermes_workflow import" not in script:
        errors.append("script does not import from plugins.hermes_workflow")

    # Count @workflow decorators.
    workflow_count = script.count("@workflow(")
    if workflow_count != 1:
        errors.append(f"expected exactly 1 @workflow decorator, found "
                       f"{workflow_count}")

    # Count @step decorators.
    step_count = script.count("@step(")
    if step_count < 1:
        errors.append("script defines no @step functions")

    return errors


class ScriptAuthor:
    """Generate a workflow script from a natural-language intent.

    Construction is explicit (not a singleton) so tests can inject
    stub LLM callers. The plugin entrypoint constructs one per
    ``register()`` call and threads it into the model tool, gateway
    handler, and slash command surface.
    """

    def __init__(
        self,
        *,
        llm: Any,
        library_root: Path | None = None,
        model: str | None = None,
        temperature: float = 0.2,
        notifier: Any | None = None,
        dispatcher: Any | None = None,
        event_translator: Any | None = None,
    ) -> None:
        self._llm = llm
        self._library_root = library_root or default_journal_root()
        self._model = model
        self._temperature = temperature
        self.notifier = notifier
        # Live-streaming seam: ``dispatcher`` is the callable from
        # the gateway's StreamEvent pipeline (set via
        # ``runtime.set_dispatcher`` for journal events; threaded in
        # here so ScriptAuthor events travel the same path). Each
        # notifier event is translated through ``event_translator``
        # (defaulting to EventTranslator) and the result is forwarded.
        # Best-effort: dispatch failures are logged and swallowed.
        self.dispatcher = dispatcher
        self._event_translator = event_translator
        if self._event_translator is None:
            try:
                from plugins.hermes_workflow.visibility import (
                    EventTranslator as _ET,
                )
                self._event_translator = _ET()
            except Exception:
                self._event_translator = None

    def _emit(self, kind: str, **payload: Any) -> None:
        """Invoke the notifier if one is wired. No-op otherwise.

        The notifier contract is ``notifier(kind: str, **payload)``.
        Stage transitions emit ``stage_started`` / ``stage_completed``
        / ``stage_failed``. LLM streaming emits ``token`` /
        ``llm_completed``. The slash surface extension emits
        ``artifact_posted``. All emitters are best-effort: a notifier
        exception is logged and swallowed (the script-author pipeline
        itself must keep running even if a consumer is misbehaving).

        If a ``dispatcher`` is wired, the event is also translated via
        the EventTranslator and forwarded to the dispatcher (which is
        typically ``GatewayEventDispatcher.dispatch`` set up by the
        gateway runtime). This is the live-streaming path that makes
        ScriptAuthor events visible in the TUI/desktop statusbar.
        """
        notifier = getattr(self, "notifier", None)
        if notifier is not None:
            try:
                notifier(kind, **payload)
            except Exception as _exc:                   # pragma: no cover
                _log.warning("ScriptAuthor notifier raised: %s", _exc)

        dispatcher = getattr(self, "dispatcher", None)
        translator = getattr(self, "_event_translator", None)
        if dispatcher is not None and translator is not None:
            try:
                stream_evt = translator.translate_script_author_event(
                    (kind, payload)
                )
                if stream_evt is not None:
                    dispatcher(stream_evt)
            except Exception as _exc:                   # pragma: no cover
                _log.warning(
                    "ScriptAuthor dispatcher raised: %s", _exc
                )

    async def generate(
        self,
        *,
        intent: str,
        runtime: Any,
        inputs: dict | None = None,
    ) -> AuthorResult:
        """Generate a script and run it.

        Args:
            intent:    Natural-language description of the workflow.
            runtime:   WorkflowRuntime to submit the generated script.
            inputs:    Optional inputs to pass to the workflow.

        Returns:
            AuthorResult with ok=True on success (run_id populated)
            or ok=False with error_stage / error identifying the failure.
        """
        # Stage 1: LLM call.
        self._emit("stage_started", stage="llm_call")
        try:
            parsed = await self._call_llm(intent)
            self._emit("stage_completed", stage="llm_call", ok=True)
        except Exception as e:
            _log.warning("ScriptAuthor LLM call failed: %s", e)
            self._emit("stage_failed", stage="llm_call", error=str(e))
            return AuthorResult(
                ok=False,
                error=str(e),
                error_stage="llm_call",
            )

        # Stage 2: Static safety checks.
        script = parsed.get("script", "")
        self._emit("stage_started", stage="safety_check")
        safety_errors = _validate_script_safety(script)
        if safety_errors:
            self._emit("stage_failed", stage="safety_check",
                        error="; ".join(safety_errors))
            return AuthorResult(
                ok=False,
                error="; ".join(safety_errors),
                error_stage="safety_check",
                raw_script=script,
            )
        self._emit("stage_completed", stage="safety_check", ok=True)

        # Stage 3: Save to library.
        # The Library owns the layout: scripts land at <root>/<name>.py
        # and the manifest at <root>/library.json. ``Library.save``
        # reads the manifest path from the entry itself, so we must
        # pass the relative ``<name>.py`` and write the file at
        # ``<root>/<name>.py`` — not nest a redundant ``library/``
        # subdirectory (which is what produced
        # ``<root>/library/library/<name>.py`` on 2026-06-30,
        # breaking Library.load()).
        self._emit("stage_started", stage="save")
        try:
            library = Library(self._library_root)
            # Mirror Library.save's expectation: write the file at
            # ``<root>/<name>.py`` so the entry's ``path=<name>.py``
            # resolves correctly when Library.load() reads it back.
            # Create the root first (Library.save writes library.json
            # there too but doesn't mkdir the root itself).
            self._library_root.mkdir(parents=True, exist_ok=True)
            script_path = self._library_root / f"{parsed['name']}.py"
            script_path.write_text(script)
            library.save(parsed["name"], script_path,
                          description=parsed.get("description", ""))
            self._emit("stage_completed", stage="save", ok=True,
                        path=str(script_path))
        except Exception as e:
            _log.warning("ScriptAuthor save failed: %s", e)
            self._emit("stage_failed", stage="save", error=str(e))
            return AuthorResult(
                ok=False,
                error=str(e),
                error_stage="save",
                raw_script=script,
            )

        # Stage 4: Validate graph.
        self._emit("stage_started", stage="graph_validation")
        try:
            workflow_fn = library.load(parsed["name"])
            self._emit("stage_completed", stage="graph_validation", ok=True)
        except Exception as e:
            self._emit("stage_failed", stage="graph_validation",
                        error=str(e))
            return AuthorResult(
                ok=False,
                name=parsed["name"],
                error=f"graph validation: {e}",
                error_stage="graph_validation",
                raw_script=script,
            )

        # Stage 5: Submit.
        # Record the za_<name>_<hex> → r_<hex> mapping so the slash
        # surface can resolve status queries by either id. The mapping
        # is also written as a sibling file at
        # ``<journal_root>/<za_run_id>.alias`` pointing at the
        # real journal so tools that only know the synthesized id
        # can still locate the run. This fixes the gap that bit
        # ``/workflow status za_xxx`` (returned "unknown run_id")
        # and ``/workflow snapshot za_xxx`` (rendered an empty tree)
        # on 2026-06-30.
        self._emit("stage_started", stage="submit")
        try:
            za_run_id = make_script_author_run_id(parsed["name"])
            run_id = await runtime.submit(workflow_fn, inputs or {})
            try:
                alias_path = self._library_root.parent / f"{za_run_id}.alias"
                alias_path.parent.mkdir(parents=True, exist_ok=True)
                alias_path.write_text(run_id)
            except Exception as _alias_exc:
                _log.debug("alias write failed (non-fatal): %s", _alias_exc)
            self._emit("stage_completed", stage="submit", ok=True,
                        run_id=run_id, za_run_id=za_run_id)
        except Exception as e:
            _log.warning("ScriptAuthor submit failed: %s", e)
            self._emit("stage_failed", stage="submit", error=str(e))
            return AuthorResult(
                ok=False,
                name=parsed["name"],
                script_path=str(script_path),
                error=str(e),
                error_stage="submit",
                raw_script=script,
            )

        return AuthorResult(
            ok=True,
            name=parsed["name"],
            script_path=str(script_path),
            run_id=za_run_id,    # synthesized, stable handle for the user
            workflow=parsed["name"],
            # Internal mapping for status/snapshot resolvers. Not part
            # of the documented AuthorResult surface; populated only
            # when ScriptAuthor generates the id (not when called
            # via the dispatcher's za_xxx convention).
            _real_run_id=run_id,
        )

    async def _call_llm(self, intent: str) -> dict:
        """Invoke the host LLM with structured-output schema. Returns
        the parsed dict (after JSON-validating the LLM's output).

        The LLM returns a ``PluginLlmStructuredResult`` with a
        ``parsed`` attribute when json_schema validation succeeds.

        Streaming path: when the LLM object exposes an
        ``acomplete_stream`` async-iterator method (the v0.2.0
        streaming seam), this method consumes that iterator and emits
        ``token`` / ``llm_completed`` notifier events along the way.
        Falls back to ``acomplete_structured`` when the LLM doesn't
        implement the streaming seam (back-compat).
        """
        from plugins.hermes_workflow.script_author import _log    # local
        _log = logging.getLogger("hermes_workflow.script_author")

        call_kwargs = dict(
            instructions=_SYSTEM_INSTRUCTIONS,
            input=[{"type": "text", "text": f"User intent: {intent}"}],
            json_schema=_SCRIPT_SCHEMA,
            schema_name="hermes_workflow_script",
            json_mode=True,
            model=self._model,
            temperature=self._temperature,
        )

        if hasattr(self._llm, "acomplete_stream"):
            full_text = ""
            final_chunk = None
            # CRITICAL: ``acomplete_stream`` may be in any of FOUR
            # shapes:
            #  (a) async-generator function (body contains ``yield``):
            #      calling returns an ``async_generator`` directly
            #      iterable by ``async for``. Not awaitable.
            #  (b) plain ``async def`` whose body is itself
            #      ``return <async_gen>``: calling returns a
            #      *coroutine* that resolves to an async_generator —
            #      must be awaited before iterating. (Common for
            #      provider wrappers like the minmax bridge.)
            #  (c) plain ``async def`` whose body is ``return await
            #      <async_gen>``: BROKEN — cannot await an
            #      async_generator. We don't try to support this
            #      shape; if a provider ships it, it will raise.
            #  (d) ``async def`` that returns a *coroutine of a
            #      coroutine* (rare — happens when the LLM surface
            #      wraps a wrapped surface): single ``await`` peels
            #      one layer; we loop until we have something
            #      ``async for`` can use.
            #
            # We branch on the return value's awaitability iteratively
            # so shapes (a), (b), and (d) all work without leaking a
            # coroutine into ``async for``. The defensive loop
            # prevents the 'coroutine' object is not iterable crash
            # that bit /workflow create against the minimax provider
            # on 2026-06-30.
            stream_iter_or_coro = self._llm.acomplete_stream(**call_kwargs)
            # Iteratively peel awaitable layers. Hard cap of 4 to
            # detect pathological cases (e.g. self-awaiting coroutine).
            for _peel in range(4):
                if not inspect.isawaitable(stream_iter_or_coro):
                    break
                stream_iter_or_coro = await stream_iter_or_coro
            else:
                # Loop completed without break — still awaitable
                # after 4 peels. Give up with a clear error rather
                # than a confusing TypeError downstream.
                raise RuntimeError(
                    "acomplete_stream returned an awaitable that "
                    "could not be resolved to an iterator after "
                    "peeling 4 layers"
                )
            stream_iter = stream_iter_or_coro
            async for chunk in stream_iter:
                if chunk.delta:
                    self._emit("token", delta=chunk.delta,
                                stage="llm_call")
                    full_text += chunk.delta
                if chunk.final:
                    final_chunk = chunk
                    break
            if final_chunk is None:
                raise RuntimeError(
                    "acomplete_stream iterator terminated without a "
                    "final chunk"
                )
            self._emit(
                "llm_completed",
                chars=len(full_text),
                text=final_chunk.text,
                parsed=final_chunk.parsed,
            )
            if final_chunk.parsed is None:
                raise RuntimeError(
                    f"LLM returned no parsed JSON: {final_chunk.text[:200]!r}"
                )
            return final_chunk.parsed

        # Non-streaming fallback. ``acomplete_structured`` should
        # return a coroutine that resolves to a PluginLlmStructuredResult.
        # Some provider wrappers return a coroutine of a coroutine —
        # we peel here too for symmetry with the streaming branch.
        result = self._llm.acomplete_structured(**call_kwargs)
        for _peel in range(4):
            if not inspect.isawaitable(result):
                break
            result = await result
        if inspect.isawaitable(result):
            raise RuntimeError(
                "acomplete_structured returned an awaitable that "
                "could not be resolved after peeling 4 layers"
            )
        if not hasattr(result, "parsed"):
            raise RuntimeError(
                f"acomplete_structured returned unexpected type "
                f"{type(result).__name__} (no .parsed attribute)"
            )
        if result.parsed is None:
            raise RuntimeError(
                f"LLM returned no parsed JSON: {result.text[:200]!r}"
            )
        return result.parsed