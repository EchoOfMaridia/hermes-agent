"""Tests for .env sanitization during load to prevent token duplication (#8908)."""

import tempfile
from pathlib import Path
from unittest.mock import patch


def test_load_env_sanitizes_concatenated_lines():
    """Verify load_env() splits concatenated KEY=VALUE pairs.

    Reproduces the scenario from #8908 where a corrupted .env file
    contained multiple tokens on a single line, causing the bot token
    to be duplicated 8 times.
    """
    from hermes_cli.config import load_env

    token = "0123456789:test"
    # Simulate concatenated line: TOKEN=xxx followed immediately by another key
    corrupted = f"TELEGRAM_BOT_TOKEN={token}ANTHROPIC_API_KEY=sk-ant-test123\n"

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".env", delete=False, encoding="utf-8"
    ) as f:
        f.write(corrupted)
        env_path = Path(f.name)

    try:
        with patch("hermes_cli.config.get_env_path", return_value=env_path):
            result = load_env()
        assert result.get("TELEGRAM_BOT_TOKEN") == token, (
            f"Token should be exactly '{token}', got '{result.get('TELEGRAM_BOT_TOKEN')}'"
        )
        assert result.get("ANTHROPIC_API_KEY") == "sk-ant-test123"
    finally:
        env_path.unlink(missing_ok=True)


def test_load_env_normal_file_unchanged():
    """A well-formed .env file should be parsed identically."""
    from hermes_cli.config import load_env

    content = (
        "TELEGRAM_BOT_TOKEN=mytoken123\n"
        "ANTHROPIC_API_KEY=sk-ant-key\n"
        "# comment\n"
        "\n"
        "OPENAI_API_KEY=sk-openai\n"
    )

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".env", delete=False, encoding="utf-8"
    ) as f:
        f.write(content)
        env_path = Path(f.name)

    try:
        with patch("hermes_cli.config.get_env_path", return_value=env_path):
            result = load_env()
        assert result["TELEGRAM_BOT_TOKEN"] == "mytoken123"
        assert result["ANTHROPIC_API_KEY"] == "sk-ant-key"
        assert result["OPENAI_API_KEY"] == "sk-openai"
    finally:
        env_path.unlink(missing_ok=True)


def test_env_loader_sanitizes_before_dotenv():
    """Verify env_loader._sanitize_env_file_if_needed fixes corrupted files."""
    from hermes_cli.env_loader import _sanitize_env_file_if_needed

    token = "0123456789:test"
    corrupted = f"TELEGRAM_BOT_TOKEN={token}ANTHROPIC_API_KEY=sk-ant-test\n"

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".env", delete=False, encoding="utf-8"
    ) as f:
        f.write(corrupted)
        env_path = Path(f.name)

    try:
        _sanitize_env_file_if_needed(env_path)
        with open(env_path, encoding="utf-8") as f:
            lines = f.readlines()
        # Should be split into two separate lines
        assert len(lines) == 2, f"Expected 2 lines, got {len(lines)}: {lines}"
        assert lines[0].startswith("TELEGRAM_BOT_TOKEN=")
        assert lines[1].startswith("ANTHROPIC_API_KEY=")
        # Token should not contain the second key
        parsed_token = lines[0].strip().split("=", 1)[1]
        assert parsed_token == token
    finally:
        env_path.unlink(missing_ok=True)


def test_load_env_strips_inline_comments():
    """Verify load_env() strips trailing ``# comment`` from values.

    Regression: previously, a line like
    ``MINIMAX_BASE_URL=https://api.x/v1  # override default base URL``
    would be parsed with the trailing comment baked into the value,
    which then produced 404s when the URL was sent to the upstream API.
    python-dotenv strips inline comments by default; ``load_env()`` was
    missing that behavior until this fix.
    """
    from hermes_cli.config import load_env

    content = (
        "MINIMAX_BASE_URL=https://api.minimax.io/v1  # Override default base URL\n"
        "GLM_BASE_URL=https://api.z.ai/api/paas/v4  # trailing comment\n"
        "ALIBABA_BASE_URL=https://example.com/v1  \n"  # trailing whitespace only
        "NO_COMMENT=https://example.com/v1\n"
    )
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".env", delete=False, encoding="utf-8"
    ) as f:
        f.write(content)
        env_path = Path(f.name)

    try:
        with patch("hermes_cli.config.get_env_path", return_value=env_path):
            result = load_env()
        # Inline comments stripped:
        assert result.get("MINIMAX_BASE_URL") == "https://api.minimax.io/v1", (
            f"got {result.get('MINIMAX_BASE_URL')!r}"
        )
        assert result.get("GLM_BASE_URL") == "https://api.z.ai/api/paas/v4", (
            f"got {result.get('GLM_BASE_URL')!r}"
        )
        # Trailing whitespace alone is also fine (already handled by .strip()):
        assert result.get("ALIBABA_BASE_URL") == "https://example.com/v1"
        # No comment to strip:
        assert result.get("NO_COMMENT") == "https://example.com/v1"
    finally:
        env_path.unlink(missing_ok=True)


def test_load_env_preserves_url_fragments():
    """Verify ``#`` chars NOT preceded by whitespace are preserved.

    URL fragments (e.g. ``https://example.com/page#section``) must survive
    parsing. The inline-comment stripper only fires on ``\\s+#``, so a ``#``
    glued to other chars is left alone.
    """
    from hermes_cli.config import load_env

    content = "DOCS_URL=https://example.com/docs#section\n"
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".env", delete=False, encoding="utf-8"
    ) as f:
        f.write(content)
        env_path = Path(f.name)

    try:
        with patch("hermes_cli.config.get_env_path", return_value=env_path):
            result = load_env()
        assert result.get("DOCS_URL") == "https://example.com/docs#section", (
            f"URL fragment was stripped: got {result.get('DOCS_URL')!r}"
        )
    finally:
        env_path.unlink(missing_ok=True)
