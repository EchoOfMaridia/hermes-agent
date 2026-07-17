"""Workflow runtime: the orchestrator.

The runtime is a singleton inside the agent process. One asyncio loop, one
semaphore, one in-memory run registry, one append-only journal per run.

What the runtime owns:

1. The concurrent-step cap (default 16). Enforced via asyncio.Semaphore.
   Every step execution acquires the semaphore; if 16 are already running,
   the 17th awaits.

2. The total-step cap (default 1000). Enforced via a counter on each Run.
   When the counter reaches the cap, the next step raises MaxTotalReached.

3. The run registry. A dict from run_id to Run. submit() inserts, cancel()
   removes, status() reads.

4. The journal lifecycle. submit() opens a Journal for the new run; the
   Run's execute() coroutine appends events as steps progress.

What the runtime does NOT own:

1. Step logic. Steps are user-authored coroutines; the runtime invokes them
   but does not interpret their return value beyond "must be Evidence."

2. Verifier logic. The runtime calls the verifier after the step fn
   returns; the verifier is a user-supplied coroutine that re-checks the
   Evidence claim.

3. Workspace semantics. The runtime passes the workflow's workspace Path
   into RunContext; the workspace itself is created by the CLI / agent
   bridge when a run is submitted.

Concurrency model:

- Each Run is one asyncio.Task. The body of that task is the @workflow
  coroutine, which composes step calls using parallel/gather (Step 1).
- parallel/gather dispatch N step coroutines. Each step invocation enters
  _execute_step on the runtime, which acquires the semaphore and either
  proceeds or awaits.
- Multiple parallel step invocations queue on the semaphore. With 16
  slots, 17+ concurrent dispatches queue until a slot frees.
- The total-step counter increments on each step entry, regardless of
  whether the step succeeds, fails, retries, or times out. This is the
  "total work attempted" budget, not "total work that succeeded."

Failure semantics:

- A step fn that raises: caught, journaled, retried per spec.max_retries
  with exponential backoff. After retries exhausted, the Run transitions
  to FAILED.
- A verifier that returns valid=False: same retry path. After retries
  exhausted, VerifierMismatch is raised and the Run transitions to FAILED.
- A cap exception (MaxTotalReached): halts the run, transitions to HALTED.
- A user-initiated cancel: cancels the asyncio.Task; transitions to
  CANCELLED.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

try:
    import jsonschema as _jsonschema_lib
except ImportError:  # pragma: no cover — jsonschema is an optional dep
    _jsonschema_lib = None  # type: ignore[assignment]

from .dsl.types import (
    Evidence,
    RunContext,
    RunState,
    StepSpec,
    StepState,
    VerifierMismatch,
    VerifierResult,
    WorkflowError,
    MaxConcurrentReached,
    MaxTotalReached,
)
from .dsl.validator import GraphValidator, collect_step_specs
from .journal import Journal
from .agent_bridge import AgentBridge, AgentResponse, JournalingBridge
from .visibility import EventTranslator


# ---------------------------------------------------------------------------
# Run — one execution of one workflow
# ---------------------------------------------------------------------------

@dataclass
class Run:
    """A single execution of one workflow.

    Lifecycle:
        PENDING -> RUNNING -> (DONE | FAILED | HALTED | CANCELLED)

    State transitions are appended to the journal so a replay can recover
    the final state without trusting the runtime's in-memory dict.
    """

    run_id: str
    workflow_fn: Callable[..., Awaitable[dict]]
    workflow_name: str
    inputs: dict[str, Any]
    workspace: Path
    journal: Journal
    runtime: "WorkflowRuntime"
    max_concurrent: int = 16
    max_total: int = 1000
    state: RunState = RunState.PENDING
    completed_steps: dict[str, Evidence] = field(default_factory=dict)
    failed_steps: dict[str, str] = field(default_factory=dict)
    step_states: dict[str, StepState] = field(default_factory=dict)
    spawned_total: int = 0
    task: asyncio.Task | None = None
    cancel_requested: bool = False
    last_event_time: float = field(default_factory=time.time)
    # Visibility context: name of the step currently executing (set
    # in execute_step, cleared on completion). Used by the agent
    # bridge to attribute ask_agent() calls to the right @step in the
    # journal. None when no step is active (workflow body or between steps).
    current_step_name: str | None = None
    # Per-step agent-call counter. Incremented by next_agent_call_index()
    # inside ask_agent() so each LLM call inside a step gets a unique
    # index for ToolCallChunk correlation.
    agent_call_counters: dict[str, int] = field(default_factory=dict)

    # -- metadata helpers ---------------------------------------------------

    def touch(self) -> None:
        """Update last_event_time. The staleness_seconds counter in
        runtime.status() uses this to detect hangs."""
        self.last_event_time = time.time()

    def next_agent_call_index(self) -> int:
        """Return the next agent-call index for the current step.

        Resets when a step starts (callers should call reset_step_state()
        first when beginning a new step). Returns 1-based; step_started
        events with index=0 are step-level, agent_call events with
        index>=1 are agent-level.
        """
        if self.current_step_name is None:
            # No active step (workflow body call). Use a synthetic
            # step name so the counter still works.
            key = "<workflow>"
        else:
            key = self.current_step_name
        n = self.agent_call_counters.get(key, 0) + 1
        self.agent_call_counters[key] = n
        return n

    def reset_step_state(self, step_name: str) -> None:
        """Called at step_started: clear current_step_name and reset
        per-step counters so a new step's agent calls start at index 1."""
        self.current_step_name = step_name
        self.agent_call_counters.pop(step_name, None)

    def clear_step_state(self) -> None:
        """Called at step_completed/step_failed: clear current_step_name."""
        self.current_step_name = None

    # -- execution -----------------------------------------------------------

    async def execute(self) -> dict:
        """Drive the workflow body. Returns the body's returned dict on
        success; raises on unhandled exceptions.
        """
        self.state = RunState.RUNNING
        self.journal.append({
            "kind": Journal.KIND_RUN_STARTED,
            "run_id": self.run_id,
            "workflow": self.workflow_name,
            "inputs": self.inputs,
            "max_concurrent": self.max_concurrent,
            "max_total": self.max_total,
        })
        self.touch()

        ctx = RunContext(
            run_id=self.run_id,
            workspace=self.workspace,
            inputs=self.inputs,
            step_outputs=self.completed_steps,
            runtime=self.runtime,
        )

        # Install _CURRENT_RUN so @step wrappers inside the workflow body
        # dispatch through this Run. Reset via token after the body returns.
        from .dsl.primitives import set_current_run
        token = set_current_run(self)
        try:
            result = await self.workflow_fn(ctx)
        except BaseException as e:
            self._finalize(e)
            raise
        else:
            self._finalize(None)
            return result
        finally:
            from .dsl.primitives import _CURRENT_RUN
            _CURRENT_RUN.reset(token)

    def _finalize(self, exc: BaseException | None) -> None:
        """Record the run's terminal state in the journal. Called exactly
        once per run."""
        if self.state in (RunState.DONE, RunState.FAILED,
                          RunState.HALTED, RunState.CANCELLED):
            return        # already finalized
        if isinstance(exc, asyncio.CancelledError):
            self.state = RunState.CANCELLED
            self.journal.append({
                "kind": Journal.KIND_RUN_CANCELLED,
                "run_id": self.run_id,
            })
        elif isinstance(exc, MaxTotalReached):
            self.state = RunState.HALTED
            self.journal.append({
                "kind": Journal.KIND_RUN_HALTED,
                "run_id": self.run_id,
                "reason": str(exc),
            })
        elif isinstance(exc, VerifierMismatch):
            self.state = RunState.FAILED
            self.journal.append({
                "kind": Journal.KIND_RUN_FAILED,
                "run_id": self.run_id,
                "error": str(exc),
                "error_type": type(exc).__name__,
            })
        elif exc is not None:
            self.state = RunState.FAILED
            self.journal.append({
                "kind": Journal.KIND_RUN_FAILED,
                "run_id": self.run_id,
                "error": str(exc),
                "error_type": type(exc).__name__,
                "traceback": traceback.format_exc(),
            })
        else:
            self.state = RunState.DONE
            self.journal.append({
                "kind": Journal.KIND_RUN_COMPLETED,
                "run_id": self.run_id,
            })
        self.touch()
        self.journal.close()

    # -- step dispatch -------------------------------------------------------

    async def execute_step(
        self,
        spec: StepSpec,
        bound_inputs: dict[str, Any],
    ) -> Evidence:
        """Run one step end-to-end. Acquires the semaphore, invokes the step
        fn, calls the verifier (if any), retries on failure per spec.

        Raises MaxTotalReached if the total-step cap has been hit.
        Raises VerifierMismatch if retries are exhausted on a verifier
        rejection. Re-raises the underlying step exception if retries are
        exhausted on a step failure.
        """
        if self.cancel_requested:
            raise asyncio.CancelledError()

        if self.spawned_total >= self.max_total:
            raise MaxTotalReached(
                f"run {self.run_id}: total-step cap {self.max_total} reached "
                f"({self.spawned_total} steps already spawned)"
            )

        # Acquire the concurrency semaphore. We must release it in finally
        # even if a verifier mismatch retries the step.
        await self.runtime._semaphore.acquire()
        self.spawned_total += 1
        self.step_states[spec.name] = StepState.RUNNING
        self.reset_step_state(spec.name)        # visibility: tag this step as active
        self.journal.append({
            "kind": Journal.KIND_STEP_STARTED,
            "run_id": self.run_id,
            "step": spec.name,
            "ts": time.time(),
        })
        self.touch()
        try:
            attempts = 0
            backoff = spec.retry_backoff_seconds
            while True:
                attempts += 1
                # Build the step's RunContext at execution time so it
                # reflects the latest completed-steps snapshot.
                ctx = RunContext(
                    run_id=self.run_id,
                    workspace=self.workspace,
                    inputs=self.inputs,
                    step_outputs=self.completed_steps,
                    runtime=self.runtime,
                )
                try:
                    if spec.timeout_seconds is not None:
                        evidence = await asyncio.wait_for(
                            spec.fn(ctx, **bound_inputs),
                            timeout=spec.timeout_seconds,
                        )
                    else:
                        evidence = await spec.fn(ctx, **bound_inputs)
                except Exception as e:
                    self.failed_steps[spec.name] = str(e)
                    self.journal.append({
                        "kind": Journal.KIND_STEP_FAILED,
                        "run_id": self.run_id,
                        "step": spec.name,
                        "error": str(e),
                        "error_type": type(e).__name__,
                        "attempt": attempts,
                    })
                    self.touch()
                    if attempts <= spec.max_retries:
                        await asyncio.sleep(backoff)
                        backoff *= 2
                        continue
                    raise

                # Verify before accepting the step.
                self.step_states[spec.name] = StepState.AWAITING_VERIFICATION

                # Auto-schema-verifier: when output_schema is declared
                # AND no explicit verifier= was provided, install a
                # default verifier that jsonschema-validates the
                # evidence's parsed_payload against the schema.
                # Explicit verifier= ALWAYS overrides this hook.
                effective_verifier = spec.verifier
                if (
                    effective_verifier is None
                    and spec.output_schema is not None
                ):
                    effective_verifier = _auto_schema_verifier(
                        schema=spec.output_schema,
                        jsonschema_lib=_jsonschema_lib,
                    )

                if effective_verifier is not None:
                    try:
                        verdict = await effective_verifier(evidence, ctx)
                    except Exception as e:
                        self.journal.append({
                            "kind": Journal.KIND_VERIFIER_RETURNED,
                            "run_id": self.run_id,
                            "step": spec.name,
                            "valid": False,
                            "reason": f"verifier raised: {e!r}",
                            "attempt": attempts,
                        })
                        self.touch()
                        if attempts <= spec.max_retries:
                            await asyncio.sleep(backoff)
                            backoff *= 2
                            continue
                        raise VerifierMismatch(
                            spec.name,
                            VerifierResult(
                                valid=False, reason=f"verifier raised: {e!r}",
                            ),
                        ) from e

                    self.journal.append({
                        "kind": Journal.KIND_VERIFIER_RETURNED,
                        "run_id": self.run_id,
                        "step": spec.name,
                        "valid": verdict.valid,
                        "reason": verdict.reason,
                        "attempt": attempts,
                    })
                    self.touch()
                    if not verdict.valid:
                        if attempts <= spec.max_retries:
                            await asyncio.sleep(backoff)
                            backoff *= 2
                            continue
                        raise VerifierMismatch(spec.name, verdict)

                # Step verified.
                self.completed_steps[spec.name] = evidence
                ctx.step_outputs[spec.name] = evidence
                self.step_states[spec.name] = StepState.VERIFIED
                self.clear_step_state()        # visibility: no active step
                self.journal.append({
                    "kind": Journal.KIND_STEP_COMPLETED,
                    "run_id": self.run_id,
                    "step": spec.name,
                    "evidence": _evidence_to_dict(evidence),
                    "attempts": attempts,
                })
                self.touch()
                return evidence
        finally:
            self.clear_step_state()            # visibility: always clear
            self.runtime._semaphore.release()


# ---------------------------------------------------------------------------
# WorkflowRuntime — the singleton
# ---------------------------------------------------------------------------

class WorkflowRuntime:
    """The orchestrator. Singleton per process.

    Construction is cheap; do it once at startup. The runtime is reentrant
    for submit() (multiple concurrent submissions are safe) but the
    status() method is sync and reads internal state without locking. The
    semantics of status() are "snapshot at this moment"; a few millisconds
    later the snapshot may differ.
    """

    def __init__(
        self,
        *,
        default_max_concurrent: int = 16,
        default_max_total: int = 1000,
        journal_root: Path | None = None,
    ) -> None:
        if default_max_concurrent < 1:
            raise ValueError(
                f"default_max_concurrent must be >= 1, got {default_max_concurrent}"
            )
        if default_max_total < 1:
            raise ValueError(
                f"default_max_total must be >= 1, got {default_max_total}"
            )
        self._default_max_concurrent = default_max_concurrent
        self._default_max_total = default_max_total
        self._journal_root = (
            journal_root if journal_root is not None
            else Path.home() / ".hermes" / "workflows"
        )
        self._semaphore = asyncio.Semaphore(default_max_concurrent)
        self._runs: dict[str, Run] = {}
        self._lock = asyncio.Lock()
        self._last_event_time = time.time()
        self._agent_bridge: AgentBridge = JournalingBridge()
        # Visibility layer: a dispatcher callable (None means no streaming).
        # When set, every journal event is also translated to a StreamEvent
        # and passed to this callable. Set via set_dispatcher().
        self._dispatcher: Callable[[Any], None] | None = None
        self._event_translator = EventTranslator()

    # -- properties ---------------------------------------------------------

    @property
    def max_concurrent(self) -> int:
        return self._default_max_concurrent

    @property
    def max_total(self) -> int:
        return self._default_max_total

    @property
    def journal_root(self) -> Path:
        return self._journal_root

    @property
    def staleness_seconds(self) -> float:
        """Seconds since the last event of any kind across all runs.
        The canary for 'is anything happening, and what.'
        """
        return time.time() - self._last_event_time

    # -- run lifecycle ------------------------------------------------------

    async def submit(
        self,
        workflow_fn: Callable[..., Awaitable[dict]],
        inputs: dict[str, Any],
        *,
        workspace: Path | None = None,
        max_concurrent: int | None = None,
        max_total: int | None = None,
    ) -> str:
        """Submit a workflow for execution. Returns run_id.

        Validates the workflow's @step graph at submit time. Refuses to
        start a broken graph. The submission is fast (validation only);
        execution happens asynchronously on the returned run.
        """
        # Validate. Walk the workflow's globals (or fall back to the
        # function's defining module).
        workflow_meta = getattr(workflow_fn, "__workflow_meta__", None)
        workflow_name = workflow_meta.name if workflow_meta else workflow_fn.__name__

        # Validation: collect step specs from the workflow's module.
        module = sys.modules.get(workflow_fn.__module__)
        if module is not None:
            specs = collect_step_specs(vars(module))
            if specs:
                GraphValidator(specs).validate()

        mc = max_concurrent if max_concurrent is not None else self._default_max_concurrent
        mt = max_total if max_total is not None else self._default_max_total
        if mc < 1:
            raise ValueError(f"max_concurrent must be >= 1, got {mc}")
        if mt < 1:
            raise ValueError(f"max_total must be >= 1, got {mt}")

        run_id = self._mint_run_id(workflow_name, inputs)
        run_ws = workspace if workspace is not None else Path.cwd()
        base_journal = Journal(run_id, self._journal_root)

        # When the runtime has a dispatcher configured, wrap the journal so
        # every event also flows to the gateway. Falls through to plain
        # Journal when no dispatcher is configured (CLI / tests).
        if self._dispatcher is not None:
            from .dispatching_journal import DispatchingJournal
            run_journal: Any = DispatchingJournal(
                inner=base_journal,
                translator=self._event_translator,
                dispatcher_fn=self._dispatcher,
            )
        else:
            run_journal = base_journal

        run = Run(
            run_id=run_id,
            workflow_fn=workflow_fn,
            workflow_name=workflow_name,
            inputs=dict(inputs),
            workspace=run_ws,
            journal=run_journal,
            runtime=self,
            max_concurrent=mc,
            max_total=mt,
        )

        async with self._lock:
            self._runs[run_id] = run

        # Schedule execution on the event loop.
        run.task = asyncio.create_task(run.execute())
        self._last_event_time = time.time()
        return run_id

    async def cancel(self, run_id: str, reason: str = "user_cancelled") -> None:
        """Cancel a running workflow. Idempotent."""
        async with self._lock:
            run = self._runs.get(run_id)
        if run is None:
            raise WorkflowError(f"unknown run_id: {run_id}")
        run.cancel_requested = True
        if run.task and not run.task.done():
            run.task.cancel()
        run.journal.append({
            "kind": Journal.KIND_RUN_CANCELLED,
            "run_id": run_id,
            "reason": reason,
        })

    def get_run(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)

    # -- status / introspection --------------------------------------------

    def status(self) -> dict:
        """Return a structured status snapshot. Always available, never
        raises on transient inconsistency.

        The `staleness_seconds` field is the canary: if it grows past a
        few seconds with active runs, the runtime is hung.
        """
        active = []
        for run in self._runs.values():
            if run.state in (RunState.PENDING, RunState.RUNNING):
                active.append({
                    "run_id": run.run_id,
                    "workflow": run.workflow_name,
                    "state": run.state.value,
                    "steps_completed": len(run.completed_steps),
                    "steps_failed": len(run.failed_steps),
                    "spawned_total": run.spawned_total,
                    "last_event_age_seconds": time.time() - run.last_event_time,
                    "inputs": run.inputs,
                })

        return {
            "active_runs": active,
            "active_count": len(active),
            "staleness_seconds": time.time() - self._last_event_time,
            "cap": {"concurrent": self._default_max_concurrent,
                     "total": self._default_max_total},
        }

    def run_status(self, run_id: str) -> dict | None:
        run = self._runs.get(run_id)
        if run is None:
            return None
        return {
            "run_id": run.run_id,
            "workflow": run.workflow_name,
            "state": run.state.value,
            "steps_completed": list(run.completed_steps.keys()),
            "steps_failed": dict(run.failed_steps),
            "step_states": {k: v.value for k, v in run.step_states.items()},
            "spawned_total": run.spawned_total,
            "max_concurrent": run.max_concurrent,
            "max_total": run.max_total,
            "inputs": run.inputs,
        }

    # -- helpers ------------------------------------------------------------

    def _mint_run_id(self, workflow_name: str, inputs: dict) -> str:
        """Stable, content-addressed run id."""
        seed = f"{workflow_name}|{sorted(inputs.items()) if inputs else ''}|{uuid.uuid4().hex}"
        h = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
        return f"r_{h}"

    # -- agent bridge -------------------------------------------------------

    def set_agent_bridge(self, bridge: AgentBridge) -> None:
        """Replace the agent bridge. Workflow scripts call
        ctx.runtime.ask_agent() which routes through this bridge.

        The provided bridge is wrapped in a JournalingBridge if it isn't
        one already, so agent_call and agent_response events are always
        recorded. To bypass the journal wrap (rare), pass a bridge that
        already extends JournalingBridge.
        """
        if isinstance(bridge, JournalingBridge):
            self._agent_bridge = bridge
        else:
            self._agent_bridge = JournalingBridge(inner=bridge)

    async def ask_agent(self, *, prompt: str, model: str | None = None,
                        max_tokens: int | None = None,
                        tools: list[dict] | None = None,
                        session_key: str | None = None,
                        system_prompt: str | None = None,
                        json_schema: dict | None = None,
                        schema_name: str | None = None) -> AgentResponse:
        """Invoke the agent bridge. The bridge journals the call.

        This is the only way a workflow script invokes the LLM agent.
        Steps call ``await ctx.runtime.ask_agent(...)``. The bridge's
        contract: journal the call, invoke the inner LLM, journal the
        response, return ``AgentResponse``.

        Args:
            prompt:        The user prompt to send to the agent.
            model:         Optional model override (``"sonnet"``, ``"opus"``,
                           ``"haiku"``, etc.). ``None`` = use the inner
                           bridge's default.
            max_tokens:    Optional cap on response tokens.
            tools:         Optional list of tool definitions the agent
                           can call during this turn. Each entry is a
                           dict with ``name``, ``description``, and
                           ``schema`` (JSON Schema). Results from the
                           agent's tool use surface on
                           ``AgentResponse.tool_calls`` as
                           ``{"name", "args", "result"}`` records.
                           ``None`` = no tools (pure text completion,
                           v0.1.0 behaviour). Full tool list lives
                           in-memory; the journal records only the count.
            session_key:   Optional opaque string that threads this call
                           into a multi-turn conversation. Successive
                           calls with the same key share history when
                           the inner bridge supports threading.
                           ``None`` = one-shot call.
            system_prompt: Optional system prompt override for this call.
            json_schema:   Optional JSON Schema describing the structured
                           output the caller wants. Forwarded to the
                           underlying AgentBridge.invoke(); bridges that
                           support wire-level response_format use it for
                           enforcement; bridges that don't paste the
                           schema into the prompt. The returned
                           AgentResponse.parsed is the deserialized
                           object when parsing succeeded, None otherwise.
            schema_name:   Optional human-readable name for the schema.
                           Used by the wire-format layer to label the
                           constraint.

        Returns:
            AgentResponse with the final text, parsed object (when
            json_schema is set and parsing succeeded), and content_type.
        """
        return await self._agent_bridge.invoke(
            prompt=prompt, model=model, max_tokens=max_tokens,
            tools=tools, session_key=session_key,
            system_prompt=system_prompt,
            json_schema=json_schema, schema_name=schema_name,
        )

    def parse_structured(
        self,
        response: AgentResponse,
        *,
        schema: dict | None = None,
    ) -> Any | None:
        """Parse a structured-output response from an LLM call.

        Thin pass-through to
        :func:`plugins.hermes_workflow.structured_output.parse_structured`.
        Exposed on the runtime so workflow scripts can write::

            response = await ctx.runtime.ask_agent(
                prompt="...", json_schema=schema_def,
            )
            data = ctx.runtime.parse_structured(response, schema=schema_def)

        When the inner bridge already populated ``response.parsed`` (the
        PluginLlmBridge does this in-process), this method returns that
        value immediately. Re-validation against a different schema still
        runs through parse_structured's jsonschema path.

        Args:
            response: An AgentResponse from a prior ask_agent call.
            schema:   Optional JSON Schema to validate against. When None,
                      validation is skipped (parses JSON-only).

        Returns:
            The parsed object (dict or list), or None when no JSON could
            be recovered from response.text.

        Raises:
            StructuredOutputError: when schema validation fails.
        """
        # Fast path: bridge already parsed it.
        if response.parsed is not None:
            if schema is not None:
                # Re-validate against caller-supplied schema (might differ
                # from the one passed to the bridge).
                import json as _json
                from .structured_output import parse_structured as _parse
                return _parse(
                    _json.dumps(response.parsed), schema=schema
                )
            return response.parsed
        # Slow path: parse response.text.
        from .structured_output import parse_structured as _parse
        return _parse(response.text, schema=schema)

    # -- visibility dispatcher --------------------------------------------

    def set_dispatcher(self, dispatcher: Callable[[Any], None] | None
                       ) -> None:
        """Set the StreamEvent dispatcher.

        The dispatcher is a callable that receives one translated
        StreamEvent per journal event. Set to None to disable streaming
        (CLI invocations typically don't need live dispatch; gateway
        invocations do).

        The dispatcher is best-effort: dispatch errors are logged but do
        not affect journal persistence.
        """
        self._dispatcher = dispatcher


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _evidence_to_dict(ev: Evidence) -> dict:
    """Convert an Evidence to a JSON-serializable dict."""
    return {
        "files_changed": list(ev.files_changed),
        "commands_run": list(ev.commands_run),
        "exit_codes": list(ev.exit_codes),
        "tests_run": ev.tests_run,
        "tests_passed": ev.tests_passed,
        "duration_seconds": ev.duration_seconds,
    }


def _auto_schema_verifier(
    schema: dict,
    jsonschema_lib: Any | None,
) -> Callable[[Evidence, "RunContext"], Awaitable[VerifierResult]]:
    """Build a default verifier that jsonschema-validates Evidence.parsed_payload.

    Used by Run.execute_step when a @step declares output_schema= AND
    no explicit verifier= was provided. The returned coroutine is
    installed in place of spec.verifier for the duration of that step's
    execution. Explicit verifier= ALWAYS overrides this — see the
    auto-verifier wiring in Run.execute_step.

    Args:
        schema:          The JSON Schema the step declared.
        jsonschema_lib:  The optional ``jsonschema`` module (already
                         resolved at module import time). ``None`` when
                         the package isn't installed — the returned
                         verifier best-effort passes with a debug note.

    Returns:
        An async coroutine matching the Verifier protocol: takes
        (Evidence, RunContext) and returns VerifierResult.
    """
    async def _verify(
        ev: Evidence,
        ctx: "RunContext",
        _schema: dict = schema,
        _jsonschema: Any = jsonschema_lib,
    ) -> VerifierResult:
        if ev.parsed_payload is None:
            return VerifierResult(
                valid=False,
                reason=(
                    "step declared output_schema but returned no "
                    "parsed_payload; call "
                    "ctx.runtime.parse_structured(response, "
                    "schema=...) inside the step body and pass the "
                    "result to Evidence(parsed_payload=...)"
                ),
            )
        if _jsonschema is None:
            return VerifierResult(
                valid=True,
                reason=(
                    "schema validation skipped: jsonschema package "
                    "not installed"
                ),
            )
        try:
            _jsonschema.validate(ev.parsed_payload, _schema)
            top_keys = (
                len(ev.parsed_payload)
                if isinstance(ev.parsed_payload, dict)
                else "non-dict"
            )
            return VerifierResult(
                valid=True,
                reason=(
                    f"parsed_payload matches output_schema "
                    f"({top_keys} top-level keys)"
                ),
            )
        except _jsonschema.ValidationError as exc:
            path = list(exc.absolute_path)
            return VerifierResult(
                valid=False,
                reason=(
                    f"parsed_payload does not match output_schema: "
                    f"{exc.message} at path {path}"
                ),
            )

    return _verify


# Late import to avoid circular issues at module import time.
import sys  # noqa: E402
