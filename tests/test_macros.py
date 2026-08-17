"""TDD contract for the Macros feature.

These tests pin the behavior of ``agent.macros`` BEFORE the module exists.
Per the project TDD discipline, this file is the executable contract that
``agent/macros.py`` must satisfy.

Coverage:
    - load_macros: missing file -> empty dict, valid file -> string mapping,
      malformed YAML -> WARNING + empty dict, non-string value -> ValueError
      with key in message, invalid key name -> ValueError.
    - expand_macros: defined key expands, undefined key stays literal, multiple
      in one prompt, single-pass no-recursion, empty dict leaves input unchanged,
      non-string input returned unchanged, whitespace inside brackets does
      not match.
    - reload_macros: cache invalidation across reloads; identity of get_macros()
      preserved between reloads.
    - chokepoint call shape: the exact (text, macros) -> (expanded, used_keys)
      shape that ``agent/conversation_loop.run_conversation`` relies on.

Run: ``pytest tests/test_macros.py -v``
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


class TestLoadMacros:
    """``load_macros(hermes_home: Path) -> Dict[str, str]``."""

    def test_missing_file_returns_empty_dict(self, tmp_path):
        from agent.macros import load_macros

        result = load_macros(hermes_home=tmp_path)
        assert result == {}

    def test_valid_yaml_three_keys(self, tmp_path):
        from agent.macros import load_macros

        (tmp_path / "macros.yaml").write_text(
            "alpha: one\n"
            "beta: two\n"
            "gamma: three\n",
            encoding="utf-8",
        )
        result = load_macros(hermes_home=tmp_path)
        assert result == {"alpha": "one", "beta": "two", "gamma": "three"}
        # All values must be coerced to str.
        assert all(isinstance(value, str) for value in result.values())

    def test_malformed_yaml_logs_warning_and_returns_empty(self, tmp_path, caplog):
        from agent.macros import load_macros

        bad = tmp_path / "macros.yaml"
        bad.write_text(":\n:bad\n", encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="agent.macros"):
            result = load_macros(hermes_home=tmp_path)
        assert result == {}
        assert any(
            record.levelno == logging.WARNING for record in caplog.records
        ), "expected at least one WARNING-level record from agent.macros"

    def test_non_string_value_raises_with_key_in_message(self, tmp_path):
        from agent.macros import load_macros

        (tmp_path / "macros.yaml").write_text(
            "good: ok\n"
            "bad: 42\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError) as excinfo:
            load_macros(hermes_home=tmp_path)
        message = str(excinfo.value)
        assert "bad" in message
        assert "int" in message or "42" in message

    def test_invalid_key_name_raises(self, tmp_path):
        from agent.macros import load_macros

        (tmp_path / "macros.yaml").write_text(
            "good: ok\n"
            "foo-bar: nope\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError) as excinfo:
            load_macros(hermes_home=tmp_path)
        assert "foo-bar" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Expansion
# ---------------------------------------------------------------------------


class TestExpandMacros:
    """``expand_macros(text: str, macros: Dict[str, str]) -> Tuple[str, List[str]]``."""

    def test_defined_key_expands(self):
        from agent.macros import expand_macros

        expanded, used = expand_macros("visit [signup]", {"signup": "https://x"})
        assert expanded == "visit https://x"
        assert used == ["signup"]

    def test_undefined_key_left_literal(self):
        from agent.macros import expand_macros

        expanded, used = expand_macros("hello [missing]", {})
        assert expanded == "hello [missing]"
        assert used == []

    def test_multiple_macros_in_one_prompt(self):
        from agent.macros import expand_macros

        expanded, used = expand_macros("[a] and [b]", {"a": "1", "b": "2"})
        assert expanded == "1 and 2"
        assert used == ["a", "b"]

    def test_single_pass_no_recursion(self):
        # [outer] value contains [inner] but the expansion is single-pass:
        # the literal text "inner [inner]" is the result, [inner] is NOT
        # re-expanded.
        from agent.macros import expand_macros

        expanded, used = expand_macros(
            "[outer]",
            {"outer": "inner [inner]", "inner": "X"},
        )
        assert expanded == "inner [inner]"
        assert used == ["outer"]

    def test_empty_macros_dict_returns_input_unchanged(self):
        from agent.macros import expand_macros

        expanded, used = expand_macros("plain text", {})
        assert expanded == "plain text"
        assert used == []

    def test_non_string_input_returned_unchanged(self):
        from agent.macros import expand_macros

        expanded, used = expand_macros(None, {"a": "1"})
        assert expanded is None
        assert used == []

    def test_whitespace_inside_brackets_does_not_match(self):
        from agent.macros import expand_macros

        expanded, used = expand_macros("[ key ]", {"key": "v"})
        assert expanded == "[ key ]"
        assert used == []

    def test_duplicate_token_in_text_counted_once(self):
        # Same [key] appearing twice in input -> expansion fires twice, but
        # used_keys records the key once (it's a "what fired", not "how often").
        from agent.macros import expand_macros

        expanded, used = expand_macros("[x] and [x] again", {"x": "Y"})
        assert expanded == "Y and Y again"
        assert used == ["x"]


# ---------------------------------------------------------------------------
# Cache / reload
# ---------------------------------------------------------------------------


class TestReloadMacros:
    """``reload_macros()`` + ``get_macros()`` cache semantics."""

    def test_reload_returns_new_mapping(self, tmp_path, monkeypatch):
        import agent.macros as macros_module

        # Point the module at tmp_path by overriding the default Hermes home.
        monkeypatch.setattr(macros_module, "_HERMES_HOME", lambda: tmp_path)

        (tmp_path / "macros.yaml").write_text('a: "1"\n', encoding="utf-8")
        macros_module.reload_macros()
        assert macros_module.get_macros().get("a") == "1"

        (tmp_path / "macros.yaml").write_text('b: "2"\n', encoding="utf-8")
        macros_module.reload_macros()
        mapping = macros_module.get_macros()
        assert "a" not in mapping
        assert mapping.get("b") == "2"

    def test_get_macros_returns_cached_value_until_reload(self, tmp_path, monkeypatch):
        import agent.macros as macros_module

        monkeypatch.setattr(macros_module, "_HERMES_HOME", lambda: tmp_path)
        (tmp_path / "macros.yaml").write_text('k: "v"\n', encoding="utf-8")

        macros_module.reload_macros()
        first = macros_module.get_macros()
        second = macros_module.get_macros()
        assert first is second  # identity preserved until reload


# ---------------------------------------------------------------------------
# Chokepoint integration call shape
# ---------------------------------------------------------------------------


class TestChokepointIntegration:
    """The exact call shape ``agent/conversation_loop.run_conversation`` relies on."""

    def test_chokepoint_call_shape(self):
        # This is the contract the chokepoint patch in
        # ``conversation_loop.run_conversation`` relies on:
        #   expanded, used = expand_macros(user_message, get_macros())
        # If the signature ever changes (named-only args, different return
        # shape, etc.), this test fails first.
        from agent.macros import expand_macros, get_macros

        user_message = "Use [signup] today"
        expanded, used = expand_macros(user_message, get_macros())

        # With an empty macros mapping (test env may have no macros.yaml),
        # the result must still be the literal form, not an error.
        assert isinstance(expanded, str)
        assert isinstance(used, list)
        assert "[signup]" in expanded or "signup" in used
