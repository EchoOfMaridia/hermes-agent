"""Model tool surface: call_workflow.

The LLM agent invokes this tool to run a workflow. The schema is fixed;
the runtime dispatches based on the ``mode`` field (ad-hoc or library).

Schema:
    {
        "name": str,             # workflow name (library mode) or
                                  # natural-language intent (ad-hoc mode)
        "inputs": object,        # dict of inputs passed to the workflow
        "mode": str              # "library" | "ad-hoc"
    }

Returns:
    {
        "run_id": str,
        "status": "submitted",
        "workflow": str,
        "mode": str
    }

The tool is async: the handler is ``async def``. Hermes runs it in the
agent's event loop. The handler submits the workflow to the runtime
and returns the run_id immediately; the workflow continues running in
the background.

v0.2.0 ad-hoc mode: requires the script-author LLM integration. Until
that ships, ad-hoc mode raises NotImplementedError; library mode works.
"""

from __future__ import annotations

from typing import Any

from plugins.hermes_workflow.runtime_factory import default_journal_root
from plugins.hermes_workflow.library import Library


# Standard JSON Schema the agent uses to validate tool inputs.
_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": (
                "Workflow name (library mode) or natural-language "
                "intent (ad-hoc mode). For library mode, the name "
                "must match a saved workflow in ~/.hermes/workflows/."
            ),
        },
        "inputs": {
            "type": "object",
            "description": (
                "Dict of inputs to pass to the workflow. The "
                "workflow's @step functions read these from "
                "ctx.inputs."
            ),
            "additionalProperties": True,
            "default": {},
        },
        "mode": {
            "type": "string",
            "enum": ["library", "ad-hoc"],
            "default": "library",
            "description": (
                "'library' to call a saved workflow by name. "
                "'ad-hoc' to generate a workflow from a natural-"
                "language intent (v0.2.0 feature; raises "
                "NotImplementedError until the script-author "
                "integration ships)."
            ),
        },
    },
    "required": ["name"],
}


def build_tool_schema() -> dict:
    """Return the JSON Schema for the call_workflow tool."""
    return _SCHEMA


def build_tool_handler(runtime: Any, script_author: Any | None = None) -> Any:
    """Build the async handler that hermes invokes when the LLM
    calls the tool.

    The handler signature is whatever hermes expects; based on the
    ``is_async=True`` registration in __init__.py, hermes calls it as
    ``await handler(**kwargs)``.

    The handler returns a dict; hermes surfaces the dict to the LLM
    as the tool's response.

    Args:
        runtime:       WorkflowRuntime instance.
        script_author: ScriptAuthor instance for ad-hoc mode. When None,
                       ad-hoc mode returns a v0.2.0 stub error.
    """
    async def handler(*, name: str, inputs: dict | None = None,
                        mode: str = "library", **_: Any) -> dict:
        inputs = inputs or {}
        if mode == "ad-hoc":
            if script_author is None:
                return _ad_hoc_stub(name, inputs)
            # Treat `name` as the natural-language intent.
            result = await script_author.generate(
                intent=name, runtime=runtime, inputs=inputs,
            )
            if result.ok:
                return {
                    "run_id": result.run_id,
                    "status": "submitted",
                    "workflow": result.workflow,
                    "script_path": result.script_path,
                    "mode": "ad-hoc",
                }
            return {
                "error": result.error,
                "error_stage": result.error_stage,
                "workflow_name": result.name,
                "script_preview": (result.raw_script[:500]
                                    if result.raw_script else ""),
                "mode": "ad-hoc",
            }
        # Library mode: look up the workflow, submit it.
        # The library lives alongside the journals under the same root.
        library = Library(default_journal_root())
        try:
            workflow_fn = library.load(name)
        except KeyError:
            return {"error": f"unknown workflow: {name!r}",
                    "available": library.list_names()}
        run_id = await runtime.submit(workflow_fn, inputs)
        return {"run_id": run_id, "status": "submitted",
                "workflow": name, "mode": "library"}
    return handler


def _ad_hoc_stub(intent: str, inputs: dict) -> dict:
    """v0.2.0 stub. Generate-a-workflow-from-intent is a follow-on."""
    return {
        "error": "ad-hoc mode is a v0.2.0 feature",
        "detail": (
            "Generating a workflow script from natural-language "
            "intent requires the script-author integration (an LLM "
            "call that produces a Python script validated by the "
            "runtime's graph validator). Until that ships, save "
            "your script to ~/.hermes/workflows/<name>.py and call "
            "it via mode='library'."
        ),
        "intent": intent,
        "inputs": inputs,
    }
