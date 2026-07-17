"""Tests for ScriptAuthor's system prompt teaching the new output_schema idiom."""

from __future__ import annotations


class TestScriptAuthorSystemPrompt:
    def test_prompt_teaches_output_schema_kwarg(self):
        prompt = self._extract_prompt()
        assert "output_schema" in prompt, (
            "ScriptAuthor should teach the output_schema= kwarg on @step"
        )

    def test_prompt_teaches_parse_structured(self):
        prompt = self._extract_prompt()
        assert "parse_structured" in prompt, (
            "ScriptAuthor should teach ctx.runtime.parse_structured"
        )

    def test_prompt_warns_against_json_loads_anti_pattern(self):
        prompt = self._extract_prompt()
        # The warning language should discourage the bare json.loads(response.text) pattern.
        assert "Don't parse" in prompt or "do not parse" in prompt.lower(), (
            "ScriptAuthor should warn against the json.loads(response.text) anti-pattern"
        )

    def test_prompt_explains_json_schema_kwarg_on_ask_agent(self):
        prompt = self._extract_prompt()
        assert "json_schema" in prompt, (
            "ScriptAuthor should teach the json_schema= kwarg on ask_agent"
        )

    @staticmethod
    def _extract_prompt() -> str:
        """Return the ScriptAuthor system prompt string.

        The constant is module-level (``_SYSTEM_INSTRUCTIONS``) and is
        the literal sent to the LLM when /workflow create fires.
        """
        from plugins.hermes_workflow.script_author import _SYSTEM_INSTRUCTIONS
        return _SYSTEM_INSTRUCTIONS