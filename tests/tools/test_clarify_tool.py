"""Tests for tools/clarify_tool.py - Interactive clarifying questions."""

import json
from typing import List, Optional


from tools.clarify_tool import (
    clarify_tool,
    check_clarify_requirements,
    MAX_CHOICES,
    CLARIFY_SCHEMA,
    _flatten_choice,
)


class TestClarifyToolBasics:
    """Basic functionality tests for clarify_tool."""

    def test_simple_question_with_callback(self):
        """Should return user response for simple question."""
        def mock_callback(question: str, choices: Optional[List[str]]) -> str:
            assert question == "What color?"
            assert choices is None
            return "blue"

        result = json.loads(clarify_tool("What color?", callback=mock_callback))
        assert result["question"] == "What color?"
        assert result["choices_offered"] is None
        assert result["user_response"] == "blue"

    def test_question_with_choices(self):
        """Should pass choices to callback and return response."""
        def mock_callback(question: str, choices: Optional[List[str]]) -> str:
            assert question == "Pick a number"
            assert choices == ["1", "2", "3"]
            return "2"

        result = json.loads(clarify_tool(
            "Pick a number",
            choices=["1", "2", "3"],
            callback=mock_callback
        ))
        assert result["question"] == "Pick a number"
        assert result["choices_offered"] == ["1", "2", "3"]
        assert result["user_response"] == "2"

    def test_empty_question_returns_error(self):
        """Should return error for empty question."""
        result = json.loads(clarify_tool("", callback=lambda q, c: "ignored"))
        assert "error" in result
        assert "required" in result["error"].lower()

    def test_whitespace_only_question_returns_error(self):
        """Should return error for whitespace-only question."""
        result = json.loads(clarify_tool("   \n\t  ", callback=lambda q, c: "ignored"))
        assert "error" in result

    def test_no_callback_returns_error(self):
        """Should return error when no callback is provided."""
        result = json.loads(clarify_tool("What do you want?"))
        assert "error" in result
        assert "not available" in result["error"].lower()


class TestClarifyToolChoicesValidation:
    """Tests for choices parameter validation."""

    def test_choices_trimmed_to_max(self):
        """Should trim choices to MAX_CHOICES."""
        choices_passed = []

        def mock_callback(question: str, choices: Optional[List[str]]) -> str:
            choices_passed.extend(choices or [])
            return "picked"

        many_choices = ["a", "b", "c", "d", "e", "f", "g"]
        clarify_tool("Pick one", choices=many_choices, callback=mock_callback)

        assert len(choices_passed) == MAX_CHOICES

    def test_empty_choices_become_none(self):
        """Empty choices list should become None (open-ended)."""
        choices_received = ["marker"]

        def mock_callback(question: str, choices: Optional[List[str]]) -> str:
            choices_received.clear()
            if choices is not None:
                choices_received.extend(choices)
            return "answer"

        clarify_tool("Open question?", choices=[], callback=mock_callback)
        assert choices_received == []  # Was cleared, nothing added

    def test_choices_with_only_whitespace_stripped(self):
        """Whitespace-only choices should be stripped out."""
        choices_received = []

        def mock_callback(question: str, choices: Optional[List[str]]) -> str:
            choices_received.extend(choices or [])
            return "answer"

        clarify_tool("Pick", choices=["valid", "  ", "", "also valid"], callback=mock_callback)
        assert choices_received == ["valid", "also valid"]

    def test_invalid_choices_type_returns_error(self):
        """Non-list choices should return error."""
        result = json.loads(clarify_tool(
            "Question?",
            choices="not a list",  # type: ignore
            callback=lambda q, c: "ignored"
        ))
        assert "error" in result
        assert "list" in result["error"].lower()

    def test_choices_converted_to_strings(self):
        """Non-string choices should be converted to strings."""
        choices_received = []

        def mock_callback(question: str, choices: Optional[List[str]]) -> str:
            choices_received.extend(choices or [])
            return "answer"

        clarify_tool("Pick", choices=[1, 2, 3], callback=mock_callback)  # type: ignore
        assert choices_received == ["1", "2", "3"]


class TestClarifyToolCallbackHandling:
    """Tests for callback error handling."""

    def test_callback_exception_returns_error(self):
        """Should return error if callback raises exception."""
        def failing_callback(question: str, choices: Optional[List[str]]) -> str:
            raise RuntimeError("User cancelled")

        result = json.loads(clarify_tool("Question?", callback=failing_callback))
        assert "error" in result
        assert "Failed to get user input" in result["error"]
        assert "User cancelled" in result["error"]

    def test_callback_receives_stripped_question(self):
        """Callback should receive trimmed question."""
        received_question = []

        def mock_callback(question: str, choices: Optional[List[str]]) -> str:
            received_question.append(question)
            return "answer"

        clarify_tool("  Question with spaces  \n", callback=mock_callback)
        assert received_question[0] == "Question with spaces"

    def test_user_response_stripped(self):
        """User response should be stripped of whitespace."""
        def mock_callback(question: str, choices: Optional[List[str]]) -> str:
            return "  response with spaces  \n"

        result = json.loads(clarify_tool("Q?", callback=mock_callback))
        assert result["user_response"] == "response with spaces"


class TestCheckClarifyRequirements:
    """Tests for the requirements check function."""

    def test_always_returns_true(self):
        """clarify tool has no external requirements."""
        assert check_clarify_requirements() is True


class TestClarifyDictChoices:
    """Dict-shaped choices must be unwrapped to user-facing text at the source.

    LLMs sometimes emit [{"description": "..."}] instead of bare strings. The
    naive str(c) coercion leaked the Python dict repr onto every surface (CLI
    panel, Discord buttons, Telegram list) AND returned it verbatim as the
    user's answer. _flatten_choice normalises at the one platform-agnostic
    entry point so the whole class is fixed in one place.
    """

    def test_flatten_unwraps_label_first(self):
        assert _flatten_choice({"label": "Short", "description": "Long"}) == "Short"

    def test_flatten_unwraps_description_when_no_label(self):
        assert _flatten_choice({"description": "A loose layout"}) == "A loose layout"

    def test_flatten_unwrap_order_label_over_description(self):
        assert _flatten_choice({"description": "verbose", "label": "tight"}) == "tight"

    def test_flatten_drops_name_value_only_dict(self):
        # name/value are component-shaped fields, not user-facing labels —
        # picking them would leak raw enum values / short model ids.
        assert _flatten_choice({"name": "tight", "value": "x"}) == ""

    def test_flatten_prefers_canonical_key_over_name(self):
        assert _flatten_choice({"name": "tight", "description": "Tight desc"}) == "Tight desc"

    def test_flatten_drops_keyless_dict(self):
        assert _flatten_choice({"foo": "bar", "n": 1}) == ""

    def test_flatten_passthrough_string_and_scalar(self):
        assert _flatten_choice("plain") == "plain"
        assert _flatten_choice(7) == "7"
        assert _flatten_choice(None) == ""

    def test_dict_choices_reach_callback_as_clean_text(self):
        """The whole point: the UI callback never sees a dict repr."""
        seen = []

        def cb(question, choices):
            seen.extend(choices or [])
            return choices[0]

        result = json.loads(clarify_tool(
            "Pick a layout",
            choices=[
                {"choice": "Tight", "description": "Tight, covers all 3 points"},
                {"description": "Loose layout"},
                {"name": "modelid", "value": "abc"},  # dropped, not leaked
                "A plain string choice",
            ],
            callback=cb,
        ))  # type: ignore
        assert seen == [
            "Tight, covers all 3 points",
            "Loose layout",
            "A plain string choice",
        ]
        # and the resolved answer is clean text, not a dict repr
        assert result["user_response"] == "Tight, covers all 3 points"
        assert "{" not in result["user_response"]
        assert all("{" not in c for c in result["choices_offered"])


class TestClarifyNestedChoices:
    """Nested-list choices must not collapse to a single space-joined string.

    LLMs sometimes emit `choices=[["a", "b", "c"]]` (a list with one nested
    list) instead of `choices=["a", "b", "c"]`. _flatten_choice used to
    flatten the inner list via `" ".join(...)`, producing a single combined
    string per outer element. The UI then rendered one row containing
    "a b c" instead of three separate selectable rows. This test pins the
    correct behavior: each leaf string must remain its own array element
    so the rendering layer can show N pickable rows.

    Bug report (bigwang agent, 2026-06-26): "When calling the `clarify` tool
    with a `choices` array of multiple strings, the UI renders only one
    option row containing all three strings concatenated together. The user
    sees a single button labeled e.g. 'Approve and ship Approve but skip
    the build verify (I trust the schema) Change something first' instead
    of three separate selectable rows."
    """

    def test_flatten_outer_lists_unwraps_one_level(self):
        from tools.clarify_tool import _flatten_outer_lists

        # The reported symptom: a single-element list of strings
        # (choices=[["Approve and ship", ...]]) — the outer wrap must be
        # peeled off so each leaf becomes its own choice.
        assert _flatten_outer_lists(
            [["Approve and ship", "Approve but skip", "Change something first"]]
        ) == ["Approve and ship", "Approve but skip", "Change something first"]

    def test_flatten_outer_lists_unwraps_double_nested_to_flat(self):
        from tools.clarify_tool import _flatten_outer_lists

        # choices=[["a", "b"], ["c", "d"]] is two outer items each with two
        # inner strings — flatten to four flat strings.
        assert _flatten_outer_lists([["a", "b"], ["c", "d"]]) == ["a", "b", "c", "d"]

    def test_flatten_outer_lists_mixes_lists_and_strings(self):
        from tools.clarify_tool import _flatten_outer_lists

        # Mixed input: a dict-shaped choice (handled later by
        # _flatten_choice) next to a list of strings next to a bare string.
        # _flatten_outer_lists only unwraps list layers; it leaves non-list
        # elements alone so the per-element normalisation can do its job.
        result = _flatten_outer_lists([
            "plain string",
            ["a", "b"],
            {"label": "dict choice"},
        ])
        assert result == ["plain string", "a", "b", {"label": "dict choice"}]

    def test_nested_choices_reach_callback_as_separate_rows(self):
        """Reproduce the bug report's exact symptom.

        The LLM emitted choices as a single-element list containing the
        options: `choices=[["Approve and ship", "Approve but skip ...", "Change something first"]]`.
        The current code flattens this to one space-joined string per
        outer element, producing a single-element choices_offered. The
        fix must flatten to N separate strings.
        """
        seen = []

        def cb(question, choices):
            seen.extend(choices or [])
            return choices[0] if choices else ""

        result = json.loads(clarify_tool(
            "Pick action",
            choices=[
                [
                    "Approve and ship",
                    "Approve but skip the build verify (I trust the schema)",
                    "Change something first",
                ]
            ],
            callback=cb,
        ))
        # Exactly three rows visible to the UI, not one concatenated blob.
        assert seen == [
            "Approve and ship",
            "Approve but skip the build verify (I trust the schema)",
            "Change something first",
        ]
        assert result["choices_offered"] == [
            "Approve and ship",
            "Approve but skip the build verify (I trust the schema)",
            "Change something first",
        ]
        # The reported bug's signature: a single-element array with the
        # three options joined into one string. After the fix this MUST
        # not happen for nested-list input either.
        assert len(result["choices_offered"]) == 3
        assert "{" not in result["user_response"]

    def test_truly_nested_list_of_lists_still_flattens_to_strings(self):
        # Defensive: if the LLM does pass choices=[["a", "b"], ["c"]],
        # the callback should still see three distinct strings, not one
        # combined blob per outer element.
        seen = []

        def cb(question, choices):
            seen.extend(choices or [])
            return "x"

        clarify_tool(
            "Pick",
            choices=[["a", "b"], ["c"]],
            callback=cb,
        )
        assert seen == ["a", "b", "c"]


class TestClarifySchema:
    """Tests for the OpenAI function-calling schema."""

    def test_schema_name(self):
        """Schema should have correct name."""
        assert CLARIFY_SCHEMA["name"] == "clarify"

    def test_schema_has_description(self):
        """Schema should have a description."""
        assert "description" in CLARIFY_SCHEMA
        assert len(CLARIFY_SCHEMA["description"]) > 50

    def test_schema_question_required(self):
        """Question parameter should be required."""
        assert "question" in CLARIFY_SCHEMA["parameters"]["required"]

    def test_schema_choices_optional(self):
        """Choices parameter should be optional."""
        assert "choices" not in CLARIFY_SCHEMA["parameters"]["required"]

    def test_schema_choices_max_items(self):
        """Schema should specify max items for choices."""
        choices_spec = CLARIFY_SCHEMA["parameters"]["properties"]["choices"]
        assert choices_spec.get("maxItems") == MAX_CHOICES

    def test_max_choices_is_four(self):
        """MAX_CHOICES constant should be 4."""
        assert MAX_CHOICES == 4
