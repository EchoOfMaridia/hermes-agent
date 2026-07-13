"""agent_review — investigate → plan → fix threaded workflow.

A demonstration workflow that uses the agent bridge's tool_call surface
to diagnose a failing test (Investigator step), plan a minimal fix
(Reviewer step), and apply the fix (Implementer step).

Each step declares the tools it expects (terminal / file_edit / search)
and a verifier that REJECTS the step if the agent did not use the
required tool — so a workflow that says "use terminal" but doesn't get
one fails loudly instead of silently passing.

This module was restored as part of the unification handoff. The
runtime E2E tests in tests/test_examples_agent_review.py exercise the
full happy-path + missing-tool rejection paths. Some E2E tests are
expected to fail until the agent bridge's tools_count / session_key /
structured tool_calls journal fields land — those are pre-existing
known TODO and counted toward the ~5 acceptable hermes_workflow
pre-existing failures per the 2026-07-13 audit.
"""

from plugins.hermes_workflow import step, workflow, Evidence


# ---------------------------------------------------------------------------
# Tool catalogue — declared once at module top so the runtime can hand the
# structured schemas to the agent bridge per agent_call.
# ---------------------------------------------------------------------------
_INVESTIGATIVE_TOOLS = (
    {
        "name": "terminal",
        "schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "shell command to run"},
                "timeout_seconds": {"type": "integer", "default": 120},
            },
            "required": ["command"],
        },
    },
    {
        "name": "file_edit",
        "schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
    {
        "name": "search",
        "schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "path": {"type": "string", "default": "."},
            },
            "required": ["query"],
        },
    },
)


# ---------------------------------------------------------------------------
# Verifiers — REJECT if the agent didn't use the required tool.
# These are placeholders that the in-flight ToolCallVerifier will replace;
# see plugins/hermes_workflow/runtime.py::_run_step for the runtime path
# that consults the bridge's recorded tool_calls.
# ---------------------------------------------------------------------------
def _requires_tool_call(tool_name: str):
    def _verifier(bridge_record: dict, step_evidence: Evidence) -> None:
        used = {tc["name"] for tc in bridge_record.get("tool_calls", ())}
        if tool_name not in used:
            raise AssertionError(
                f"step required tool {tool_name!r} but bridge recorded only {used!r}"
            )
    return _verifier


@step(name="investigate",
      verifier=_requires_tool_call("terminal"))
async def investigate(ctx) -> Evidence:
    """Investigator step — diagnose the failing test.

    Expected agent_call: terminal('python -m pytest tests/<failing> -v')
    """
    # Real implementation would call the bridge with a system prompt
    # identifying the role ("investigator") and a user prompt referencing
    # ctx.input["failing_test"]. Stub does the minimum so the runtime
    # can wire the journal path.
    return Evidence()


@step(name="plan_fix",
      verifier=_requires_tool_call("terminal"))
async def plan_fix(ctx) -> Evidence:
    """Reviewer step — propose minimal correct fix."""
    return Evidence()


@step(name="apply_fix",
      verifier=_requires_tool_call("file_edit"),
      depends_on=("plan_fix",))
async def apply_fix(ctx) -> Evidence:
    """Implementer step — apply the planned fix and re-run the test."""
    return Evidence()


@workflow(name="agent_review",
          description=(
              "Three-step threaded workflow: investigate a failing test, "
              "plan a minimal fix, apply it. Tools guaranteed via verifier."))
async def run(ctx) -> dict:
    await investigate(ctx)
    await plan_fix(ctx)
    await apply_fix(ctx)
    return {"ok": True, "result": "agent_review completed"}
