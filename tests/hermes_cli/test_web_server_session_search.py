import asyncio

from hermes_cli import web_server


class _FakeSessionDB:
    """Fake backing the /api/sessions/search endpoint.

    The endpoint surfaces direct session-id matches first, then FTS message
    matches, deduping both by compression lineage root. This fake has no
    compression chains (get_session returns no parent), so each session is its
    own lineage root.
    """

    closed = False

    def search_sessions_by_id(self, query, limit=20, include_archived=True):
        assert query == "20260603"
        assert include_archived is True
        return [
            {
                "id": "20260603_090200_exact",
                "preview": "ID match preview",
                "source": "cli",
                "model": "claude",
                "started_at": 100,
            }
        ]

    def search_messages(self, query, limit=20):
        # Quoted mode wraps the inner phrase as a single FTS5 quoted-phrase
        # token — no prefix wildcards. The test in
        # test_desktop_session_search_merges_id_matches_before_content_matches
        # now queries `'"20260603"'` to exercise the quoted path.
        assert query == '"20260603"'
        return [
            {
                "session_id": "20260603_090200_exact",
                "snippet": "duplicate content hit should not replace ID hit",
                "role": "user",
                "source": "cli",
                "model": "claude",
                "session_started": 100,
            },
            {
                "session_id": "content_session",
                "snippet": "content hit",
                "role": "assistant",
                "source": "desktop",
                "model": "gpt",
                "session_started": 200,
            },
        ]

    def list_sessions_rich(self, **kwargs):
        # The title-mode branch (added for the quote-toggle search) calls this;
        # this fixture exercises the ID + FTS path with no real titles to match.
        return []

    def get_session(self, session_id):
        # No compression chains in this fixture — every session is its own root.
        return {"id": session_id, "parent_session_id": None}

    def get_compression_tip(self, session_id):
        return session_id

    def close(self):
        self.closed = True


class _TitleModeDB:
    """Fake focused on the title-mode branch.

    `search_sessions_by_id` returns nothing, `search_messages` returns one FTS
    hit, and `list_sessions_rich` returns three sessions: one whose title
    matches the unquoted query (case-insensitive), one whose preview
    contains the substring but title does not (must NOT match in title mode),
    and one unrelated title.
    """

    closed = False

    TITLE_HIT = {
        "id": "title_hit_session",
        "title": "Autogenesis mobile UI session",
        "source": "cli",
        "model": "claude",
        "started_at": 100,
    }
    PREVIEW_ONLY = {
        "id": "preview_only_session",
        "title": "Unrelated chat",
        "source": "cli",
        "model": "claude",
        "started_at": 200,
    }
    UNRELATED = {
        "id": "unrelated_session",
        "title": "Cooking recipes",
        "source": "cli",
        "model": "claude",
        "started_at": 300,
    }

    def search_sessions_by_id(self, query, limit=20, include_archived=True):
        return []

    def search_messages(self, query, limit=20):
        # Only the FTS-content path uses this. The quoted-mode prefix_query
        # is `"autogenesis mobile"` — the inner phrase as a single FTS phrase,
        # not the prefix-wildcard split.
        assert query == '"autogenesis mobile"'
        return [
            {
                "session_id": "preview_only_session",
                "snippet": "mentioned autogenesis mobile in passing",
                "role": "user",
                "source": "cli",
                "model": "claude",
                "session_started": 200,
            }
        ]

    def list_sessions_rich(self, source=None, limit=20, order_by_last_active=True, compact_rows=True, **_):
        return [self.TITLE_HIT, self.PREVIEW_ONLY, self.UNRELATED]

    def get_session(self, session_id):
        return {"id": session_id, "parent_session_id": None}

    def get_compression_tip(self, session_id):
        return session_id

    def close(self):
        self.closed = True


def test_desktop_session_search_merges_id_matches_before_content_matches(monkeypatch):
    """With a quoted query, ID matches surface first and FTS content hits
    are deduped by compression lineage root. Title mode (unquoted queries)
    skips the FTS branch entirely and is exercised by the title-mode tests
    below — keeping the FTS assertion here confirms the quoted-mode path
    still works after the toggle."""
    monkeypatch.setattr("hermes_state.SessionDB", _FakeSessionDB)

    response = asyncio.run(web_server.search_sessions(q='"20260603"', limit=2))

    # ID match surfaces first; the content hit on the SAME session is deduped
    # by lineage root (not double-listed); the unrelated content hit follows.
    assert response == {
        "results": [
            {
                "session_id": "20260603_090200_exact",
                "lineage_root": "20260603_090200_exact",
                "snippet": "ID match preview",
                "role": None,
                "source": "cli",
                "model": "claude",
                "session_started": 100,
            },
            {
                "session_id": "content_session",
                "lineage_root": "content_session",
                "snippet": "content hit",
                "role": "assistant",
                "source": "desktop",
                "model": "gpt",
                "session_started": 200,
            },
        ]
    }


def test_unquoted_query_skips_fts_branch(monkeypatch):
    """An unquoted query must not invoke the FTS branch at all. The
    `_FakeSessionDB.search_messages` mock would raise if called."""
    monkeypatch.setattr("hermes_state.SessionDB", _FakeSessionDB)

    response = asyncio.run(web_server.search_sessions(q="20260603", limit=2))

    # Only the ID match surfaces; the FTS-content hit on "content_session"
    # is correctly excluded because we're in title mode.
    assert response == {
        "results": [
            {
                "session_id": "20260603_090200_exact",
                "lineage_root": "20260603_090200_exact",
                "snippet": "ID match preview",
                "role": None,
                "source": "cli",
                "model": "claude",
                "session_started": 100,
            },
        ]
    }


def test_unquoted_query_returns_title_matches_only(monkeypatch):
    """An unquoted query must match session titles (case-insensitive) and
    exclude sessions whose only matching substring lives in their preview."""
    monkeypatch.setattr("hermes_state.SessionDB", _TitleModeDB)

    response = asyncio.run(web_server.search_sessions(q="autogenesis mobile", limit=10))

    session_ids = [r["session_id"] for r in response["results"]]
    # Title hit → present.
    assert "title_hit_session" in session_ids
    # Preview-only hit → absent (that's the whole point of title mode).
    assert "preview_only_session" not in session_ids
    # Unrelated title → absent.
    assert "unrelated_session" not in session_ids


def test_unquoted_query_is_case_insensitive(monkeypatch):
    monkeypatch.setattr("hermes_state.SessionDB", _TitleModeDB)

    upper = asyncio.run(web_server.search_sessions(q="AUTOGENESIS MOBILE", limit=10))
    mixed = asyncio.run(web_server.search_sessions(q="AuToGeNeSiS MoBiLe", limit=10))

    upper_ids = [r["session_id"] for r in upper["results"]]
    mixed_ids = [r["session_id"] for r in mixed["results"]]

    assert "title_hit_session" in upper_ids
    assert "title_hit_session" in mixed_ids


def test_unquoted_query_uses_title_as_snippet(monkeypatch):
    """Title-mode hits carry the title as the snippet so the renderer can
    display something before the row is loaded into $sessions."""
    monkeypatch.setattr("hermes_state.SessionDB", _TitleModeDB)

    response = asyncio.run(web_server.search_sessions(q="autogenesis mobile", limit=10))

    title_result = next(r for r in response["results"] if r["session_id"] == "title_hit_session")
    assert title_result["snippet"] == "Autogenesis mobile UI session"
    assert title_result["role"] is None


def test_quoted_query_routes_to_fts_path(monkeypatch):
    """A quoted query must hit the existing FTS message-content path."""
    monkeypatch.setattr("hermes_state.SessionDB", _TitleModeDB)

    response = asyncio.run(web_server.search_sessions(q='"autogenesis mobile"', limit=10))

    # The FTS path surfaces the preview_only_session hit (where the substring
    # only exists in the message body) — proving the title branch was skipped.
    session_ids = [r["session_id"] for r in response["results"]]
    assert "preview_only_session" in session_ids
    # The pure title hit should still appear (the FTS path was told to
    # search for it too — the title branch is what filters it out, and we
    # skipped that branch for the quoted query).
    assert "title_hit_session" not in session_ids


def test_empty_query_returns_no_results(monkeypatch):
    monkeypatch.setattr("hermes_state.SessionDB", _TitleModeDB)

    response = asyncio.run(web_server.search_sessions(q="", limit=10))

    assert response == {"results": []}


def test_is_quoted_query_helper():
    assert web_server._is_quoted_query('"foo"') is True
    assert web_server._is_quoted_query('"foo bar"') is True
    assert web_server._is_quoted_query('foo') is False
    assert web_server._is_quoted_query('foo"') is False
    assert web_server._is_quoted_query('"foo') is False
    assert web_server._is_quoted_query('""') is True  # matched pair wins on shape; caller filters empty needle
    assert web_server._is_quoted_query('') is False
    assert web_server._is_quoted_query(None) is False  # type: ignore[arg-type]
