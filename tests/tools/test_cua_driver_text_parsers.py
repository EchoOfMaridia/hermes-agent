"""Regression tests for cua-driver >= 0.19.0 text-format parsers.

cua-driver >= 0.19.0 dropped structuredContent for list_windows /
list_apps in favour of plain text content parts. The wrapper has to
parse that text into the same dict shape the rest of the code expects
or capture() falls through to "no windows" / "no apps" with no
operator-visible signal.

These tests pin the parser behaviour so a future driver format change
surfaces as a failing test rather than silent breakage in the
desktop agent.
"""

from __future__ import annotations

import pytest

from tools.computer_use.cua_backend import (
    _parse_list_apps_text,
    _parse_list_windows_text,
)


class TestParseListWindowsText:
    def test_empty_when_header_only(self) -> None:
        """The new driver returns 'Found 0 windows:' with no body when
        no display is attached. The parser must yield an empty list, not
        a parse error."""
        assert _parse_list_windows_text("Found 0 windows:") == []

    def test_parses_well_formed_entry(self) -> None:
        text = (
            "Found 2 windows:\n"
            "- Warp (window_id 12582917, pid 2701530, role=AXWindow, title=\"hermes\")\n"
            "- Discord (window_id 27262982, pid 2822908, role=AXWindow, title=\"@x - Discord\")\n"
        )
        result = _parse_list_windows_text(text)
        assert result == [
            {
                "app_name": "Warp",
                "pid": 2701530,
                "window_id": 12582917,
                "is_on_screen": True,
                "title": "",
                "z_index": 0,
            },
            {
                "app_name": "Discord",
                "pid": 2822908,
                "window_id": 27262982,
                "is_on_screen": True,
                "title": "",
                "z_index": 0,
            },
        ]

    def test_skips_entries_without_pid(self) -> None:
        """X11 desktop/panel windows may omit pid. We skip them per
        _ingest_windows's existing rule (uncapturable, no PID)."""
        text = (
            "Found 2 windows:\n"
            "- Desktop (window_id 12345)\n"
            "- Calculator (window_id 99999, pid 997669)\n"
        )
        result = _parse_list_windows_text(text)
        assert len(result) == 1
        assert result[0]["app_name"] == "Calculator"
        assert result[0]["pid"] == 997669

    def test_handles_bullet_styles(self) -> None:
        """Some builds use '* ' bullets, others '- '. Both must parse."""
        text = (
            "Found 2 windows:\n"
            "* Foo (window_id 1, pid 10)\n"
            "- Bar (window_id 2, pid 20)\n"
        )
        result = _parse_list_windows_text(text)
        assert [w["app_name"] for w in result] == ["Foo", "Bar"]

    def test_app_name_with_spaces(self) -> None:
        text = "- Visual Studio Code (window_id 42, pid 100)\n"
        result = _parse_list_windows_text(text)
        assert result[0]["app_name"] == "Visual Studio Code"

    def test_garbage_lines_skipped(self) -> None:
        text = (
            "Found 1 windows:\n"
            "this is not a window line\n"
            "- Real (window_id 1, pid 1)\n"
            "another bad line\n"
        )
        result = _parse_list_windows_text(text)
        assert len(result) == 1
        assert result[0]["app_name"] == "Real"


class TestParseListAppsText:
    def test_empty(self) -> None:
        assert _parse_list_apps_text("") == []

    def test_skips_summary_header(self) -> None:
        """'Found N app(s): N running, M installed-not-running.' is a
        summary line, not an app entry. The parser must skip it."""
        text = (
            "Found 3 app(s): 2 running, 1 installed-not-running.\n"
            "- systemd (pid 1)\n"
            "- bash (pid 4368)\n"
            "- kthreadd (pid 2)\n"
        )
        result = _parse_list_apps_text(text)
        assert result == [
            {"name": "systemd", "pid": 1},
            {"name": "bash", "pid": 4368},
            {"name": "kthreadd", "pid": 2},
        ]

    def test_handles_truncated_names(self) -> None:
        """The driver truncates long names with '-'. Preserve as-is."""
        text = "- power-profiles- (pid 4656)\n"
        result = _parse_list_apps_text(text)
        assert result == [{"name": "power-profiles-", "pid": 4656}]

    def test_handles_bullet_styles(self) -> None:
        text = "* systemd (pid 1)\n- bash (pid 2)\n"
        result = _parse_list_apps_text(text)
        assert result == [{"name": "systemd", "pid": 1}, {"name": "bash", "pid": 2}]


class TestLoadWindowsFallsBackToText:
    """End-to-end check: when structuredContent is empty (the 0.19.0
    shape) and `data` is the text payload, _load_windows should still
    return parsed windows. This is the regression that originally made
    capture() fail silently after the driver upgrade."""

    def test_structured_content_empty_falls_back_to_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tools.computer_use import cua_backend

        captured_text = (
            "Found 1 windows:\n"
            "- Calculator (window_id 1234, pid 5678)\n"
        )
        # Stub out the inner session so we never hit cua-driver.
        class _FakeOut:
            def get(self, key, default=None):
                return {  # always return same dict for get
                    "structuredContent": None,
                    "data": captured_text,
                    "images": [],
                    "isError": False,
                }[key] if key in (
                    "structuredContent", "data", "images", "isError"
                ) else default

        class _FakeSession:
            def call_tool(self, name, args):
                return _FakeOut()

        monkeypatch.setattr(cua_backend, "_ingest_windows", lambda raw: [
            {
                "app_name": w["app_name"],
                "pid": w["pid"],
                "window_id": w["window_id"],
                "off_screen": False,
                "title": w.get("title", ""),
                "z_index": w.get("z_index", 0),
            }
            for w in raw
        ])

        backend = cua_backend.CuaDriverBackend.__new__(cua_backend.CuaDriverBackend)
        backend._session = _FakeSession()
        backend._session_id = "test"

        windows = backend._load_windows()
        assert len(windows) == 1
        assert windows[0]["app_name"] == "Calculator"
        assert windows[0]["pid"] == 5678
        assert windows[0]["window_id"] == 1234
