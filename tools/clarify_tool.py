#!/usr/bin/env python3
"""
Clarify Tool Module - Interactive Clarifying Questions

Allows the agent to present structured multiple-choice questions or open-ended
prompts to the user. In CLI mode, choices are navigable with arrow keys. On
messaging platforms, choices are rendered as a numbered list.

The actual user-interaction logic lives in the platform layer (cli.py for CLI,
gateway/run.py for messaging). This module defines the schema, validation, and
a thin dispatcher that delegates to a platform-provided callback.
"""

import json
from typing import List, Optional, Callable


# Maximum number of predefined choices the agent can offer.
# A 5th "Other (type your answer)" option is always appended by the UI.
MAX_CHOICES = 4


def _flatten_outer_lists(choices):
    """Flatten one level of list nesting before per-choice normalisation.

    LLMs sometimes wrap choices in an extra list (``choices=[["a", "b", "c"]]``
    instead of ``choices=["a", "b", "c"]``) — a single-element outer list
    containing all the options. Without this pass, the per-element loop
    below would treat the inner list as ONE choice and `_flatten_choice`
    used to join its items with spaces, producing a single concatenated
    string for the UI to render as one row instead of three. Bug report
    (bigwang agent, 2026-06-26): the user saw one button labelled
    "Approve and ship Approve but skip the build verify (I trust the
    schema) Change something first" instead of three separate rows.

    Strings inside the inner list are preserved; nested-list elements are
    re-flattened up to one additional level. Non-list elements pass through
    untouched so the per-element `_flatten_choice` can still handle dicts
    and other odd shapes.
    """
    out = []
    for c in choices:
        if isinstance(c, (list, tuple)):
            out.extend(_flatten_outer_lists(list(c)))
        else:
            out.append(c)
    return out


def _flatten_choice(c) -> str:
    """Coerce a single choice into its user-facing display string.

    The schema declares choices as bare strings, but LLMs sometimes emit
    dict-shaped choices like ``[{"description": "..."}]``. A naive ``str(c)``
    turns the whole dict into its Python repr — ``{'description': '...'}`` —
    which then leaks onto every surface that renders the choice (CLI panel,
    Discord buttons, Telegram numbered list) AND is returned verbatim as the
    user's answer. Normalising here, at the one platform-agnostic entry point,
    fixes the whole class in one place instead of per-adapter.

    Dict unwrap order is the canonical LLM tool-call user-facing keys:
    ``label`` → ``description`` → ``text`` → ``title``. ``name`` and ``value``
    are deliberately excluded — they're component-shaped fields that could
    carry raw enum values or short identifiers, not human-readable labels. A
    dict with none of the canonical keys is dropped (returns ""), since a
    garbage label is worse than no choice at all.

    Note: nested lists are unwrapped by `_flatten_outer_lists` BEFORE this
    function is called per element — the historical ``(list, tuple)``
    branch was removed because it would otherwise join the options with
    spaces into a single display string (the bug above).
    """
    if c is None:
        return ""
    if isinstance(c, str):
        return c.strip()
    if isinstance(c, dict):
        for key in ("label", "description", "text", "title"):
            v = c.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""
    return str(c).strip()


# Recognised callback response shapes. Anything else falls through to
# "unresolved" so the agent sees a halt signal rather than inferring a
# pick from arbitrary text.
_RESPONSE_MODE_SELECTED = "selected"
_RESPONSE_MODE_FREETEXT = "freetext"
_RESPONSE_MODE_UNRESOLVED = "unresolved"

# Sentinel string returned by the gateway when a clarify prompt was
# unanswered (timeout / cancel / session-boundary cleanup).  Matches
# structurally so any timeout duration ("1m", "60m") is caught without
# hard-coding.  These are NEVER a real user answer.
_GATEWAY_TIMEOUT_SENTINEL_PREFIXES = ("[user did not respond within ",)
_KNOWN_RESPONSE_MODES = {_RESPONSE_MODE_SELECTED, _RESPONSE_MODE_FREETEXT}


def _resolve_response_mode(user_response, choices) -> tuple:
    """Classify the callback's return value into a (mode, value) pair.

    The wire contract:

      - ``{"mode": "selected", "value": "<choice text>"}`` — the UI confirms
        the user picked a specific offered choice. Returns
        ``("selected", value)`` ONLY when ``value`` matches one of
        ``choices`` exactly (substring matches are NOT accepted — the user
        might be typing the start of a custom answer).

      - ``{"mode": "freetext", "value": "<typed text>"}`` — the user
        explicitly used the Other channel. Intent is acknowledged;
        returns ``("freetext", value)``.

      - Plain string — legacy callers (tests, oneshot fallback, anything
        that doesn't have a structured UI). If the string matches an
        offered choice exactly, treat as "selected" so the legacy path
        still works. Otherwise "unresolved" — the agent MUST halt rather
        than infer the most plausible pick.

      - Any other shape (unknown mode dict, list, scalar, None) →
        ``("unresolved", str(user_response).strip())`` so the agent can
        see what came back without mistaking it for a pick.

    Returns ``(mode_str, value_str)``. ``value_str`` is the canonical
    text to surface to the agent (whitespace-stripped).
    """
    # Dict: structured UI contract.
    if isinstance(user_response, dict):
        mode = user_response.get("mode")
        value = user_response.get("value", "")
        if not isinstance(value, str):
            value = str(value)
        value = value.strip()
        if mode == _RESPONSE_MODE_SELECTED and isinstance(choices, list) and value in choices:
            return _RESPONSE_MODE_SELECTED, value
        if mode == _RESPONSE_MODE_FREETEXT:
            return _RESPONSE_MODE_FREETEXT, value
        # Unknown mode or "selected" with a non-matching value — treat as
        # unresolved. The caller tagged it incorrectly or it's noise.
        return _RESPONSE_MODE_UNRESOLVED, value

    # Plain string: legacy contract. Exact-match against offered choices
    # counts as "selected" so older callers still get a coherent result.
    # In open-ended mode (choices is None) any text the user typed is, by
    # definition, a deliberate answer — tag as "freetext" so the agent
    # can proceed with the value. (Multi-choice non-match stays "unresolved":
    # the user typed something that wasn't on the menu.)
    # The gateway timeout / cancel sentinel (e.g. "[user did not respond
    # within 1m]") is never a real user answer — tag as "unresolved"
    # regardless of open-ended mode so the agent halts rather than treats
    # absence of a real answer as a pick.
    if isinstance(user_response, str):
        value = user_response.strip()
        if value.startswith(_GATEWAY_TIMEOUT_SENTINEL_PREFIXES):
            return _RESPONSE_MODE_UNRESOLVED, value
        if isinstance(choices, list) and value in choices:
            return _RESPONSE_MODE_SELECTED, value
        if not isinstance(choices, list):
            return _RESPONSE_MODE_FREETEXT, value
        return _RESPONSE_MODE_UNRESOLVED, value

    # Anything else (None, int, list, ...): coerce to string, mark
    # unresolved. The agent MUST halt rather than treat this as a pick.
    value = str(user_response).strip() if user_response is not None else ""
    return _RESPONSE_MODE_UNRESOLVED, value


def clarify_tool(
    question: str,
    choices: Optional[List[str]] = None,
    callback: Optional[Callable] = None,
) -> str:
    """
    Ask the user a question, optionally with multiple-choice options.

    Args:
        question: The question text to present.
        choices:  Up to 4 predefined answer choices. When omitted the
                  question is purely open-ended.
        callback: Platform-provided function that handles the actual UI
                  interaction. Signature: callback(question, choices) -> str.
                  Injected by the agent runner (cli.py / gateway).

    Returns:
        JSON string with the user's response.
    """
    if not question or not question.strip():
        return tool_error("Question text is required.")

    question = question.strip()

    # Validate and trim choices
    if choices is not None:
        if not isinstance(choices, list):
            return tool_error("choices must be a list of strings.")
        # LLMs sometimes emit dict-shaped choices (e.g. [{"description": "..."}])
        # instead of bare strings. _flatten_choice unwraps them to their
        # user-facing text here — the single platform-agnostic entry point —
        # so the CLI panel, Discord buttons, and Telegram list all render clean
        # text and the resolved answer is never a raw Python dict repr.
        # Unwrap one level of list nesting (LLMs sometimes emit
        # choices=[["a", "b", "c"]] instead of ["a", "b", "c"] — see
        # _flatten_outer_lists for the bug this guards against).
        choices = _flatten_outer_lists(choices)
        choices = [s for s in (_flatten_choice(c) for c in choices) if s]
        if len(choices) > MAX_CHOICES:
            choices = choices[:MAX_CHOICES]
        if not choices:
            choices = None  # empty list → open-ended

    if callback is None:
        return json.dumps(
            {"error": "Clarify tool is not available in this execution context."},
            ensure_ascii=False,
        )

    try:
        raw_response = callback(question, choices)
    except Exception as exc:
        return json.dumps(
            {"error": f"Failed to get user input: {exc}"},
            ensure_ascii=False,
        )

    # Tag the response mode so the agent can distinguish a real pick from
    # a freetext custom answer from a noisy non-answer (bug #2: agent
    # treating absence of a real answer as implicit approval).
    response_mode, user_response_value = _resolve_response_mode(raw_response, choices)

    return json.dumps({
        "question": question,
        "choices_offered": choices,
        "user_response": user_response_value,
        "user_response_mode": response_mode,
    }, ensure_ascii=False)


def check_clarify_requirements() -> bool:
    """Clarify tool has no external requirements -- always available."""
    return True


# =============================================================================
# OpenAI Function-Calling Schema
# =============================================================================

CLARIFY_SCHEMA = {
    "name": "clarify",
    "description": (
        "Ask the user a question when you need clarification, feedback, or a "
        "decision before proceeding. Supports two modes:\n\n"
        "1. **Multiple choice** — provide up to 4 choices. The user picks one "
        "or types their own answer via a 5th 'Other' option.\n"
        "2. **Open-ended** — omit choices entirely. The user types a free-form "
        "response.\n\n"
        "CRITICAL: when you are offering options, put each option ONLY in the "
        "`choices` array — NEVER enumerate the options inside the `question` "
        "text. The UI renders `choices` as selectable rows; options written "
        "into the question string render as dead prose the user can't pick. "
        "Right: question='Which deployment target?', choices=['staging', "
        "'prod']. Wrong: question='Which target? 1) staging 2) prod', choices=[].\n\n"
        "Use this tool when:\n"
        "- The task is ambiguous and you need the user to choose an approach\n"
        "- You want post-task feedback ('How did that work out?')\n"
        "- You want to offer to save a skill or update memory\n"
        "- A decision has meaningful trade-offs the user should weigh in on\n\n"
        "Do NOT use this tool for simple yes/no confirmation of dangerous "
        "commands (the terminal tool handles that). Prefer making a reasonable "
        "default choice yourself when the decision is low-stakes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": (
                    "The question itself, and ONLY the question (e.g. 'Which "
                    "deployment target?'). Do NOT embed the answer options here "
                    "— pass them as separate elements in `choices`."
                ),
            },
            "choices": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": MAX_CHOICES,
                "description": (
                    "REQUIRED whenever you are presenting selectable options: "
                    "each distinct option is its own array element (up to 4). "
                    "The UI renders these as pickable rows and auto-appends an "
                    "'Other (type your answer)' option. Omit this parameter "
                    "entirely ONLY for a genuinely open-ended free-text question."
                ),
            },
        },
        "required": ["question"],
    },
}


# --- Registry ---
from tools.registry import registry, tool_error

registry.register(
    name="clarify",
    toolset="clarify",
    schema=CLARIFY_SCHEMA,
    handler=lambda args, **kw: clarify_tool(
        question=args.get("question", ""),
        choices=args.get("choices"),
        callback=kw.get("callback")),
    check_fn=check_clarify_requirements,
    emoji="❓",
)
