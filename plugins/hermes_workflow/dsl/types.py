"""Core types for the hermes_workflow DSL.

Every workflow script imports these types via `from hermes_workflow import ...`
(see plugins/hermes_workflow/dsl/__init__.py).

Design notes:
- All frozen dataclasses so the runtime can hash, compare, and reuse safely.
- Evidence is the structured claim a step makes about its work. The runtime
  refuses to mark a step DONE without a valid Evidence object.
- Verifier is a coroutine that re-checks an Evidence claim before the
  runtime advances. It is the contract.
- RunContext is passed to every step and verifier. It carries runtime
  metadata (run_id, workspace, inputs, accumulated step outputs, runtime ref).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from hermes_workflow.runtime import WorkflowRuntime


# ---------------------------------------------------------------------------
# State enums
# ---------------------------------------------------------------------------

class StepState(Enum):
    """Per-step state machine.

    PENDING             - declared in the script, not yet ready to run
    READY               - all declared dependencies have completed
    RUNNING             - the runtime has claimed it, sema acquired, fn is awaited
    AWAITING_VERIFICATION - step fn returned Evidence; verifier is now running
    VERIFIED            - verifier returned valid=True
    FAILED              - step fn raised, verifier returned valid=False, or
                          retries exhausted
    BLOCKED             - reserved for future use (manual review gate)
    """

    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    AWAITING_VERIFICATION = "awaiting_verification"
    VERIFIED = "verified"
    FAILED = "failed"
    BLOCKED = "blocked"


class RunState(Enum):
    """Per-run state machine.

    PENDING                  - registered with runtime, not yet started
    RUNNING                  - workflow body coroutine is active
    AWAITING_VERIFICATION    - one or more steps are awaiting verifier verdict
    DONE                     - workflow body returned normally
    HALTED                   - cap exceeded; partial run journaled
    FAILED                   - unhandled exception in workflow body
    CANCELLED                - user invoked runtime.cancel()
    """

    PENDING = "pending"
    RUNNING = "running"
    AWAITING_VERIFICATION = "awaiting_verification"
    DONE = "done"
    HALTED = "halted"
    FAILED = "failed"
    CANCELLED = "cancelled"


# ---------------------------------------------------------------------------
# Evidence — the structured claim a step makes about its work
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Evidence:
    """Structured claim about what a step did.

    Every step fn returns one of these. The runtime refuses to mark a step
    VERIFIED unless the (optional) verifier accepts the Evidence.

    Attributes:
        files_changed:    Tuple of paths (relative to ctx.workspace) the step
                           wrote. Empty if the step is read-only.
        commands_run:     Tuple of shell commands the step executed.
                           Recorded so verifiers can re-run or inspect them.
        exit_codes:       Tuple of exit codes, parallel to commands_run.
                           0 = clean. Non-zero = claimed-but-failed.
        tests_run:        Total tests discovered (0 if not a test step).
        tests_passed:     Tests that passed (must be <= tests_run).
        duration_seconds: Wall-clock seconds the step took.
    """

    files_changed: tuple[str, ...]
    commands_run: tuple[str, ...]
    exit_codes: tuple[int, ...]
    tests_run: int
    tests_passed: int
    duration_seconds: float

    def __post_init__(self) -> None:
        # Invariant: tests_passed <= tests_run.
        if self.tests_passed > self.tests_run:
            raise ValueError(
                f"Evidence invalid: tests_passed ({self.tests_passed}) > "
                f"tests_run ({self.tests_run})"
            )
        # Invariant: exit_codes parallel commands_run.
        if len(self.exit_codes) != len(self.commands_run):
            raise ValueError(
                f"Evidence invalid: exit_codes length ({len(self.exit_codes)}) "
                f"!= commands_run length ({len(self.commands_run)})"
            )


# ---------------------------------------------------------------------------
# Verifier — re-checks Evidence before the runtime advances
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class VerifierResult:
    """Verifier's verdict on an Evidence claim.

    Attributes:
        valid:                   True if the Evidence stands. False otherwise.
        reason:                  Human-readable explanation. Always set.
                                 For valid=True, a positive summary ("27/27 pass").
                                 For valid=False, what went wrong.
        recheck_after_seconds:   Optional hint. If set and valid=True, the
                                 verifier is asserting "this is good now, but
                                 may go stale in N seconds." Future feature;
                                 runtime ignores it in v0.1.0.
    """

    valid: bool
    reason: str
    recheck_after_seconds: float | None = None


# A verifier is a coroutine that takes the step's Evidence and the RunContext
# and returns a VerifierResult. We type the runtime ref as `Any` here to avoid
# a circular import; the runtime types itself as `WorkflowRuntime`.
Verifier = Callable[[Evidence, "RunContext"], Awaitable[VerifierResult]]


# ---------------------------------------------------------------------------
# StepSpec — the contract a @step decorator attaches to a coroutine
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StepSpec:
    """The contract attached to a step coroutine by the @step decorator.

    The runtime inspects every StepSpec at workflow load time to build the
    dependency graph and run GraphValidator. See plugins/hermes_workflow/dsl/
    validator.py for the validator.
    """

    name: str
    fn: Callable[..., Awaitable[Evidence]]
    verifier: Verifier | None
    depends_on: tuple[str, ...]
    inputs_from: dict[str, str]
    max_retries: int
    retry_backoff_seconds: float
    timeout_seconds: float | None


# ---------------------------------------------------------------------------
# RunContext — passed to every step fn and verifier
# ---------------------------------------------------------------------------

@dataclass
class RunContext:
    """Mutable per-run context. Passed to every step fn and verifier.

    Attributes:
        run_id:        Stable identifier for the run. Content-addressed
                       (hash of workflow_fn + inputs + start time).
        workspace:     Filesystem path the workflow operates in.
        inputs:        The dict the user passed when they submitted the run.
        step_outputs:  Mutable map from step name to Evidence. Steps append
                       here as they complete. Verifiers read this to check
                       cross-step contracts.
        runtime:       Reference to the WorkflowRuntime singleton. Steps use
                       this to invoke agents (await ctx.runtime.ask_agent(...)).
    """

    run_id: str
    workspace: Path
    inputs: dict[str, Any]
    step_outputs: dict[str, Evidence]
    runtime: "WorkflowRuntime"


# ---------------------------------------------------------------------------
# Errors — typed exceptions the runtime raises
# ---------------------------------------------------------------------------

class WorkflowError(Exception):
    """Base class for all workflow runtime errors."""


class WorkflowValidationError(WorkflowError):
    """Raised at script load time by GraphValidator."""


class CapExceeded(WorkflowError):
    """Base for cap-related errors. Subclasses are MaxConcurrentReached,
    MaxTotalReached."""


class MaxConcurrentReached(CapExceeded):
    """16 concurrent steps already running. New step awaits in queue.

    In practice the runtime waits on the semaphore rather than raising, so
    this exception is rare; it surfaces only if something explicitly checks
    the queue depth.
    """


class MaxTotalReached(CapExceeded):
    """1000 steps already spawned. Workflow halts; partial run journaled."""


class VerifierMismatch(WorkflowError):
    """A step's verifier returned valid=False and retries are exhausted."""

    def __init__(self, step_name: str, verdict: VerifierResult) -> None:
        self.step_name = step_name
        self.verdict = verdict
        super().__init__(
            f"verifier rejected step '{step_name}': {verdict.reason}"
        )
