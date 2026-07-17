#!/usr/bin/env python3
"""Tests for delegate_task's per-task field stripping.

Pins the contract recovered from the pre-refactor definition of
`_strip_model_hidden_task_fields` in `tools/delegate_tool.py`. The
function removes model-supplied per-task keys that must never reach
`delegate_task()` itself. The set of hidden keys is exposed as the
module-level constant `_MODEL_HIDDEN_TASK_FIELDS`.

The regression this guards:
- `e4dbb67bf fix(security): remove model-controlled delegate ACP transport`
  added the stripper. It then dropped from `tools/delegate_tool.py`
  in `f3cf79314`'s refactor while the call sites in `run_agent.py`
  and `tests/tools/test_delegate.py` survived, producing
  `ImportError: cannot import name '_strip_model_hidden_task_fields'`
  on the first delegation dispatch.
"""

import collections.abc
import os
import sys
import unittest

REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


class TestStripModelHiddenTaskFieldsImport(unittest.TestCase):
    def test_import_strip_function(self) -> None:
        from tools.delegate_tool import _strip_model_hidden_task_fields
        self.assertTrue(callable(_strip_model_hidden_task_fields))

    def test_import_hidden_fields_constant(self) -> None:
        from tools.delegate_tool import _MODEL_HIDDEN_TASK_FIELDS
        self.assertIsInstance(_MODEL_HIDDEN_TASK_FIELDS, collections.abc.Set)
        self.assertTrue(_MODEL_HIDDEN_TASK_FIELDS)
        for key in ("acp_command", "acp_args"):
            self.assertIn(key, _MODEL_HIDDEN_TASK_FIELDS)


class TestStripModelHiddenTaskFieldsContract(unittest.TestCase):
    def setUp(self) -> None:
        from tools.delegate_tool import _strip_model_hidden_task_fields
        self._strip = _strip_model_hidden_task_fields

    def test_non_list_passthrough(self) -> None:
        self.assertIsNone(self._strip(None))
        self.assertEqual(self._strip("oops"), "oops")
        self.assertEqual(self._strip({"already": "a dict"}), {"already": "a dict"})

    def test_list_with_hidden_fields_stripped(self) -> None:
        tasks = [
            {"goal": "a", "acp_command": "copilot --acp --stdio", "acp_args": ["--x"]},
            {"goal": "b", "toolsets": ["web"]},
        ]
        result = self._strip(tasks)
        self.assertIsInstance(result, list)
        self.assertNotIn("acp_command", result[0])
        self.assertNotIn("acp_args", result[0])
        self.assertEqual(result[0]["goal"], "a")
        self.assertEqual(result[1], {"goal": "b", "toolsets": ["web"]})
        self.assertIsNot(result, tasks)

    def test_list_without_hidden_fields_returns_original_object(self) -> None:
        tasks = [
            {"goal": "a"},
            {"goal": "b", "toolsets": ["web"]},
        ]
        result = self._strip(tasks)
        self.assertIs(result, tasks)

    def test_list_with_non_dict_entries_passed_through(self) -> None:
        tasks = [{"goal": "a"}, "not-a-dict", 42, {"goal": "b", "acp_command": "x"}]
        result = self._strip(tasks)
        self.assertEqual(result[0], {"goal": "a"})
        self.assertEqual(result[1], "not-a-dict")
        self.assertEqual(result[2], 42)
        self.assertNotIn("acp_command", result[3])

    def test_empty_list_returns_empty_list(self) -> None:
        result = self._strip([])
        self.assertEqual(result, [])
        self.assertIsNot(result, [])

    def test_acp_command_and_acp_args_are_the_only_hidden_fields(self) -> None:
        from tools.delegate_tool import _MODEL_HIDDEN_TASK_FIELDS
        self.assertEqual(
            set(_MODEL_HIDDEN_TASK_FIELDS),
            {"acp_command", "acp_args"},
        )


class TestInheritParentBaseUrl(unittest.TestCase):
    """Pins the contract of ``_inherit_parent_base_url`` and
    ``_normalized_runtime_url`` so the regression surface that
    the f3cf79314 refactor accidentally dropped is closed.
    """

    def test_import_inherit_parent_base_url(self) -> None:
        from tools.delegate_tool import _inherit_parent_base_url
        self.assertTrue(callable(_inherit_parent_base_url))

    def test_import_normalized_runtime_url(self) -> None:
        from tools.delegate_tool import _normalized_runtime_url
        self.assertTrue(callable(_normalized_runtime_url))

    def test_normalized_runtime_url_strips_whitespace_and_trailing_slash(self) -> None:
        from tools.delegate_tool import _normalized_runtime_url
        self.assertEqual(_normalized_runtime_url(None), "")
        self.assertEqual(_normalized_runtime_url(""), "")
        self.assertEqual(
            _normalized_runtime_url(" https://openrouter.ai/api/v1/ "),
            "https://openrouter.ai/api/v1",
        )
        self.assertEqual(
            _normalized_runtime_url("http://localhost:11434/v1//"),
            "http://localhost:11434/v1",
        )

    def test_inherit_prefers_client_kwargs_when_different_http_url(self) -> None:
        from tools.delegate_tool import _inherit_parent_base_url

        class _Parent:
            base_url = "https://openrouter.ai/api/v1"
            _client_kwargs = {
                "api_key": "no-key-required",
                "base_url": "http://localhost:11434/v1",
            }

        self.assertEqual(
            _inherit_parent_base_url(_Parent(), "https://openrouter.ai/api/v1"),
            "http://localhost:11434/v1",
        )

    def test_inherit_returns_fallback_when_no_override(self) -> None:
        from tools.delegate_tool import _inherit_parent_base_url

        class _Parent:
            base_url = "https://openrouter.ai/api/v1"
            _client_kwargs = None

        self.assertEqual(
            _inherit_parent_base_url(_Parent(), "https://openrouter.ai/api/v1"),
            "https://openrouter.ai/api/v1",
        )

    def test_inherit_returns_none_when_fallback_is_none(self) -> None:
        from tools.delegate_tool import _inherit_parent_base_url

        class _Parent:
            base_url = None
            _client_kwargs = None

        self.assertIsNone(_inherit_parent_base_url(_Parent(), None))


if __name__ == "__main__":
    unittest.main()
