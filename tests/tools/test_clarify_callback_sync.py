"""Tests for the clarify tool's user_response_mode wire contract.

The skill `interactive-plan/references/clarify-tool-callback-discipline.md`
documents the contract every clarify response must follow:

  {
    "question": ...,
    "choices_offered": [...],
    "user_response": "...",
    "user_response_mode": "selected" | "freetext" | "unresolved"
  }

Three modes:

  - "selected"   — the callback returned a value matching one of choices_offered
                   (the user picked). Agent proceeds with that value.
  - "freetext"   — the callback returned the structured {mode:"freetext", value:"..."}
                   Other-channel marker (the user typed a deliberate custom answer).
                   Agent proceeds with the value.
  - "unresolved" — the callback returned text that doesn't match any choice and
                   didn't tag itself as freetext (debug text, timeout sentinel,
                   oneshot fallback, garbage). Agent MUST halt, re-issue the
                   gate, or stand down — NEVER infer the most plausible choice.

Without this tag the agent sees a non-empty `user_response` field and infers
"the user picked something." That inference is wrong in at least three real
scenarios documented in the skill (2026-06-26 bug: user typed
"Report the issue in a way I can copy it" into Other; agent inferred
"Approve and ship" and proceeded past the gate the user was actively halting).

These tests pin the contract at the tool layer so the agent-side discipline
(`user_response_mode == "unresolved"` → halt) is enforceable.
"""

from __future__ import annotations

import json
from typing import List, Optional


from tools.clarify_tool import clarify_tool


def _make_cb(return_value):
    """Build a callback that always returns the given value."""
    def cb(question: str, choices: Optional[List[str]]):
        return return_value
    return cb


class TestClarifyGateUnresolved:
    """The seven regression guards the skill demands.

    These tests are the red-then-green contract — they FAIL on the current
    code because clarify_tool.py does not emit `user_response_mode` at all
    (the field is missing from the JSON), then pass once the field is added
    with the documented semantics.
    """

    def test_response_always_carries_user_response_mode(self):
        """Every clarify response MUST include user_response_mode.

        The agent-side discipline ('if mode == "unresolved" halt') is
        impossible without the field present. No field = no halt signal.
        """
        result = json.loads(clarify_tool(
            "Q?",
            choices=["a", "b", "c"],
            callback=_make_cb("a"),
        ))
        assert "user_response_mode" in result, (
            "clarify_tool must emit user_response_mode; the agent needs the "
            "tag to distinguish a real pick from debug text in Other."
        )
        assert result["user_response_mode"] in {"selected", "freetext", "unresolved"}

    def test_exact_match_to_offered_choice_is_selected(self):
        """Callback returning a choice string verbatim → mode=selected."""
        def cb(question, choices):
            assert choices == ["Approve and ship", "Deny", "Request changes"]
            return "Approve and ship"

        result = json.loads(clarify_tool(
            "Approve?",
            choices=["Approve and ship", "Deny", "Request changes"],
            callback=cb,
        ))
        assert result["user_response_mode"] == "selected"
        assert result["user_response"] == "Approve and ship"

    def test_unmatched_plain_string_is_marked_unresolved(self):
        """REGRESSION GUARD for bug #2 (2026-06-26 bigwang session).

        When the callback returns a plain string that doesn't match any
        of choices_offered, the tool MUST mark mode='unresolved' so the
        agent sees a halt signal instead of inferring the most plausible
        pick.

        In the reported bug, the user typed 'Report the issue in a way I
        can copy it' into the Other field. That string doesn't match any
        of the offered choices, so the agent must NOT proceed as if the
        user had approved the plan.
        """
        result = json.loads(clarify_tool(
            "Approve the plan?",
            choices=["Approve and ship", "Approve but skip", "Change something first"],
            callback=_make_cb("Report the issue in a way I can copy it"),
        ))
        assert result["user_response_mode"] == "unresolved"

    def test_matched_substring_is_not_auto_selected(self):
        """Strict equality only. The UI must send the full choice text via
        the structured callback when the user picks.

        Without strict equality, "Approve" gets marked "selected" and the
        agent sees a 1-character answer that loses the choice's full context.
        The skill says: 'Strict equality only' (callback-discipline.md line 122).
        """
        result = json.loads(clarify_tool(
            "Pick action",
            choices=["Approve and ship", "Approve but skip", "Change something first"],
            callback=_make_cb("Approve"),  # substring of choices[0], not equal
        ))
        assert result["user_response_mode"] == "unresolved"

    def test_structured_freetext_marker_is_freetext_mode(self):
        """Other-channel answers must be tagged as 'freetext' so the agent
        knows it was a deliberate custom answer (not a debug message)."""
        def cb(question, choices):
            return {"mode": "freetext", "value": "None of those"}

        result = json.loads(clarify_tool(
            "Pick",
            choices=["X", "Y"],
            callback=cb,
        ))
        assert result["user_response_mode"] == "freetext"
        assert result["user_response"] == "None of those"

    def test_structured_selected_with_matching_value_is_selected(self):
        """Structured callback {mode:'selected', value:<exact choice>} → selected."""
        def cb(question, choices):
            return {"mode": "selected", "value": "Deny"}

        result = json.loads(clarify_tool(
            "Approve?",
            choices=["Approve and ship", "Deny", "Request changes"],
            callback=cb,
        ))
        assert result["user_response_mode"] == "selected"
        assert result["user_response"] == "Deny"

    def test_structured_selected_with_mismatched_value_is_unresolved(self):
        """Structured {mode:'selected', value:<non-matching>} → unresolved.

        A structured callback that claims 'selected' but the value doesn't
        match any offered choice is a bug. The tag survives because it's
        set by the resolve-response-mode helper, not by string-matching.
        """
        def cb(question, choices):
            return {"mode": "selected", "value": "approve"}

        result = json.loads(clarify_tool(
            "Approve?",
            choices=["Approve and ship", "Deny", "Request changes"],
            callback=cb,
        ))
        assert result["user_response_mode"] == "unresolved"

    def test_empty_string_response_is_unresolved(self):
        """Empty user_response means the user did NOT answer.

        Skill pitfall: 'Empty user_response is also a non-answer' (clarify-tool-
        discipline.md:361). Verified in 2026-06-26 plus-writer-token-budgeting
        session. Agent must NOT infer 'approved by silence'.
        """
        result = json.loads(clarify_tool(
            "Pick",
            choices=["A", "B"],
            callback=_make_cb(""),
        ))
        assert result["user_response_mode"] == "unresolved"

    def test_oneshot_synthetic_prompt_is_unresolved(self):
        """The oneshot-mode fallback returns a synthetic instruction:

            '[oneshot mode: no user available. Pick the best option from
            [\"a\",\"b\",\"c\"] using your own judgment and continue.]'

        Plain text, non-empty, doesn't match a choice. Without the tag, a
        downstream agent that doesn't know about oneshot mode reads this as
        a real answer. The tag MUST mark it 'unresolved' so the agent
        stops and acknowledges the synthetic fallback instead of proceeding.
        """
        result = json.loads(clarify_tool(
            "Pick",
            choices=["a", "b", "c"],
            callback=_make_cb(
                "[oneshot mode: no user available. Pick the best option "
                "from [\"a\",\"b\",\"c\"] using your own judgment and continue.]"
            ),
        ))
        assert result["user_response_mode"] == "unresolved"

    def test_legacy_oneshot_sentinel_in_gateway_callback_is_unresolved(self):
        """The gateway _clarify_callback_sync returns sentinels on timeout
        and on send-failure:

          - '[user did not respond within Xm]'
          - '[clarify prompt could not be delivered]'

        These flow back into clarify_tool's callback as plain strings. The
        sentinel pattern is exactly what the skill calls 'meta/debugging'
        (line 167 of clarify-tool-discipline.md). Agent must NOT proceed
        as if the user picked anything; it must halt or fall back.

        This is the actual surface that bricks Discord sessions today: the
        gateway returns a sentinel, clarify_tool currently emits no mode
        tag, the agent reads 'user_response: [user did not respond within 1m]'
        as if the user typed that, and proceeds to call into the kill-
        recovery path that the user described ('Stopping agent is no good
        either, resulting in the entire session needing to be destroyed').
        """
        for sentinel in (
            "[user did not respond within 1m]",
            "[user did not respond within 60m]",
            "[clarify prompt could not be delivered]",
        ):
            result = json.loads(clarify_tool(
                "Approve the plan?",
                choices=["Approve and ship", "Deny", "Request changes"],
                callback=_make_cb(sentinel),
            ))
            assert result["user_response_mode"] == "unresolved", (
                f"sentinel {sentinel!r} must be unresolved, not selected/freetext"
            )

    def test_open_ended_question_unmatched_text_is_freetext(self):
        """Open-ended (choices=None) — any non-sentinel text the user types
        IS the answer, by definition. Tag it as freetext, not unresolved,
        so the agent can proceed with the value.

        This is the OTHER branch: in multi-choice mode, non-matching text
        is unresolved (the user picked something that wasn't offered); in
        open-ended mode, any text is the answer. The mode field MUST
        distinguish these two cases.
        """
        result = json.loads(clarify_tool(
            "What color?",
            choices=None,
            callback=_make_cb("mauve"),
        ))
        assert result["user_response_mode"] == "freetext"
        assert result["user_response"] == "mauve"

    def test_open_ended_question_sentinel_is_still_unresolved(self):
        """But the timeout/cancel sentinel is still unresolved in open-ended
        mode — it's never a real answer even when no choices were offered.
        """
        result = json.loads(clarify_tool(
            "What color?",
            choices=None,
            callback=_make_cb("[user did not respond within 1m]"),
        ))
        assert result["user_response_mode"] == "unresolved"


class TestClarifyToolRegressionNoModeFieldBug:
    """Backward-compat regression — existing call sites that read `user_response`
    must keep working. The new field is additive.
    """

    def test_existing_user_response_field_unchanged(self):
        result = json.loads(clarify_tool(
            "Pick",
            choices=["a", "b"],
            callback=_make_cb("a"),
        ))
        # Old contract: user_response is the user's text (stripped)
        assert result["user_response"] == "a"
        # New contract: same field, just augmented with mode
        assert "user_response_mode" in result

    def test_choices_offered_unchanged(self):
        result = json.loads(clarify_tool(
            "Pick",
            choices=["a", "b"],
            callback=_make_cb("a"),
        ))
        assert result["choices_offered"] == ["a", "b"]
        assert result["question"] == "Pick"