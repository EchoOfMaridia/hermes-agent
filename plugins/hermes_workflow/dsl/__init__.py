"""hermes_workflow DSL.

The DSL is the surface where "total control over agent workflow" is expressed.
Three primitives do the work: step(), parallel()/gather(), workflow().
Everything else is composition.

Import contract:
    from hermes_workflow import step, parallel, gather, workflow, Evidence
"""

from .types import (
    Evidence,
    RunContext,
    RunState,
    StepSpec,
    StepState,
    Verifier,
    VerifierResult,
    WorkflowError,
    WorkflowValidationError,
    CapExceeded,
    MaxConcurrentReached,
    MaxTotalReached,
    VerifierMismatch,
)
from .primitives import step, parallel, gather, workflow

__all__ = [
    "Evidence",
    "RunContext",
    "RunState",
    "StepSpec",
    "StepState",
    "Verifier",
    "VerifierResult",
    "WorkflowError",
    "WorkflowValidationError",
    "CapExceeded",
    "MaxConcurrentReached",
    "MaxTotalReached",
    "VerifierMismatch",
    "step",
    "parallel",
    "gather",
    "workflow",
]
