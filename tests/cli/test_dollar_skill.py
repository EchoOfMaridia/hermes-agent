"""Tests for `$`-prefixed skill/bundle rewrite at the CLI dispatch layer.

The CLI accepts `$skill-a $skill-b do XYZ` at position 0 and rewrites it to
`/skill-a /skill-b do XYZ` before the existing stacked-skill handler picks
it up. This rewrite is a single pure function in ``agent/skill_commands``
(`rewrite_dollar_chain_to_slash`), so the test surface is unit-level —
no full CLI instance is required.

The dispatch wiring itself (cli.py position-0 branch) is exercised by
existing integration tests for the ``/`` form. Once
``rewrite_dollar_chain_to_slash`` returns the expected slash form, the
existing stacked-skill handler carries the rest.
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.skill_commands import scan_skill_commands


def _make_skill(skills_dir, name, body="Do the thing."):
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    content = f"""\
---
name: {name}
description: Description for {name}.
---

# {name}

{body}
"""
    (skill_dir / "SKILL.md").write_text(content)
    return skill_dir


class TestRewriteDollarChainToSlash:
    """Pure-function tests on `rewrite_dollar_chain_to_slash(text) -> (rewritten, refs)`."""

    def _setup_three_skills(self, tmp_path):
        _make_skill(tmp_path, "skill-a", body="Body A.")
        _make_skill(tmp_path, "skill-b", body="Body B.")
        _make_skill(tmp_path, "skill-c", body="Body C.")

    def test_returns_input_unchanged_when_no_dollar_refs(self, tmp_path):
        """Plain text passes through; nothing rewritten, no refs returned."""
        from agent.skill_commands import rewrite_dollar_chain_to_slash

        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            self._setup_three_skills(tmp_path)
            scan_skill_commands()
            rewritten, refs = rewrite_dollar_chain_to_slash("just plain text")
        assert rewritten == "just plain text"
        assert refs == []

    def test_position_zero_single_skill_rewrites_to_slash(self, tmp_path):
        """`$skill-a do XYZ` at position 0 → `/skill-a do XYZ`."""
        from agent.skill_commands import rewrite_dollar_chain_to_slash

        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            self._setup_three_skills(tmp_path)
            scan_skill_commands()
            rewritten, refs = rewrite_dollar_chain_to_slash("$skill-a do the thing")
        assert rewritten == "/skill-a do the thing"
        assert refs == [("skill", "/skill-a")]

    def test_position_zero_multi_skill_rewrites_to_stacked(self, tmp_path):
        """`$skill-a $skill-b do XYZ` at position 0 → `/skill-a /skill-b do XYZ`.

        This is the forced multi-skill case the user requested — every
        `$token` becomes a leading `/token` so the existing stacked-skill
        handler (`split_stacked_skill_commands`) picks up the rest.
        """
        from agent.skill_commands import rewrite_dollar_chain_to_slash

        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            self._setup_three_skills(tmp_path)
            scan_skill_commands()
            rewritten, refs = rewrite_dollar_chain_to_slash(
                "$skill-a $skill-b do the thing"
            )
        assert rewritten == "/skill-a /skill-b do the thing"
        assert refs == [
            ("skill", "/skill-a"),
            ("skill", "/skill-b"),
        ]

    def test_position_zero_bundle_rewrites_to_slash(self, tmp_path):
        """`$/bundle-name do XYZ` at position 0 → `/bundle-name do XYZ`."""
        from agent.skill_bundles import _bundles_cache
        from agent.skill_commands import rewrite_dollar_chain_to_slash

        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            self._setup_three_skills(tmp_path)
            scan_skill_commands()
            (tmp_path / "my-bundle.yaml").write_text(
                "name: my-bundle\nskills:\n  - skill-a\n  - skill-b\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"HERMES_BUNDLES_DIR": str(tmp_path)},
                clear=False,
            ):
                _bundles_cache.clear()
                rewritten, refs = rewrite_dollar_chain_to_slash(
                    "$my-bundle do the thing"
                )
        assert rewritten == "/my-bundle do the thing"
        assert refs == [("bundle", "/my-bundle")]

    def test_unknown_dollar_refs_are_dropped_from_rewrite(self, tmp_path):
        """`$bogus $skill-a run` at position 0 → `/skill-a run` (bogus dropped)."""
        from agent.skill_commands import rewrite_dollar_chain_to_slash

        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            self._setup_three_skills(tmp_path)
            scan_skill_commands()
            rewritten, refs = rewrite_dollar_chain_to_slash(
                "$bogus $skill-a run"
            )
        assert rewritten == "/skill-a run"
        assert refs == [("skill", "/skill-a")]

    def test_does_not_rewrite_mid_prose_dollar(self, tmp_path):
        """A `$token` mid-prose does NOT trigger the CLI rewrite — only position-0.

        Mid-prose `$skill` references are handled by the TUI/Desktop
        completion adapter separately. The CLI rewrite is the
        command-style trigger only.
        """
        from agent.skill_commands import rewrite_dollar_chain_to_slash

        with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
            self._setup_three_skills(tmp_path)
            scan_skill_commands()
            rewritten, refs = rewrite_dollar_chain_to_slash(
                "please run $skill-a for me"
            )
        # Mid-prose `$token` does not get the CLI rewrite.
        assert rewritten == "please run $skill-a for me"
        assert refs == []
