"""MiniMax provider profiles (international + China).

The default API-key routes use anthropic_messages because their base URLs end
with /anthropic. Users can opt MiniMax-M3 into the OpenAI-compatible endpoint
with base_url=https://api.minimax.io/v1; that route needs MiniMax-specific
reasoning controls in extra_body.
"""

from typing import Any
from urllib.parse import urlparse

from providers import register_provider
from providers.base import ProviderProfile


def _is_minimax_global_openai_base_url(base_url: str | None) -> bool:
    parsed = urlparse(str(base_url or "").strip())
    if (parsed.hostname or "").lower() != "api.minimax.io":
        return False
    path = parsed.path.rstrip("/").lower()
    return path == "/v1"


def _is_minimax_m3(model: str | None) -> bool:
    normalized = str(model or "").strip().lower()
    return normalized in {"minimax-m3", "minimax/minimax-m3"}


class MiniMaxProfile(ProviderProfile):
    """MiniMax — M3 OpenAI-compatible reasoning controls."""

    def build_api_kwargs_extras(
        self,
        *,
        reasoning_config: dict | None = None,
        model: str | None = None,
        base_url: str | None = None,
        **context: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Emit M3 thinking-mode controls for api.minimax.io/v1.

        M3's OpenAI-compatible endpoint keeps chain-of-thought inline in
        the streamed content (wrapped in ``<think>…</think>``) unless
        ``reasoning_split`` is sent — and Hermes' frontend reasoning
        extractor (``ui-tui/src/lib/reasoning.ts::splitReasoning``) is
        designed around that inline shape. Sending ``reasoning_split: true``
        routes the reasoning into a separate response field whose name
        does not match the ``delta.reasoning_content`` / ``delta.reasoning``
        accumulator in ``agent/chat_completion_helpers.py:2028``, so the
        reasoning is silently dropped on the wire and the user sees an
        empty ``ToolTrail`` above an otherwise complete answer.

        ``thinking`` controls the M3 mode itself (``adaptive`` enables it,
        ``disabled`` turns it off). Hermes' effort levels are not a MiniMax
        depth knob here — they only select adaptive vs disabled.
        """
        if not _is_minimax_global_openai_base_url(base_url) or not _is_minimax_m3(model):
            return {}, {}

        extra_body: dict[str, Any] = {}

        if isinstance(reasoning_config, dict) and reasoning_config.get("enabled") is False:
            extra_body["thinking"] = {"type": "disabled"}
            return extra_body, {}

        if reasoning_config is not None:
            extra_body["thinking"] = {"type": "adaptive"}

        return extra_body, {}


minimax = MiniMaxProfile(
    name="minimax",
    aliases=("mini-max",),
    api_mode="anthropic_messages",
    env_vars=("MINIMAX_API_KEY",),
    base_url="https://api.minimax.io/anthropic",
    auth_type="api_key",
    default_aux_model="MiniMax-M3",
)

minimax_cn = MiniMaxProfile(
    name="minimax-cn",
    aliases=("minimax-china", "minimax_cn"),
    api_mode="anthropic_messages",
    env_vars=("MINIMAX_CN_API_KEY",),
    base_url="https://api.minimaxi.com/anthropic",
    auth_type="api_key",
    default_aux_model="MiniMax-M3",
)

minimax_oauth = MiniMaxProfile(
    name="minimax-oauth",
    aliases=("minimax_oauth", "minimax-oauth-io"),
    api_mode="anthropic_messages",
    display_name="MiniMax (OAuth)",
    description="MiniMax via OAuth browser flow — no API key required",
    signup_url="https://api.minimax.io/",
    env_vars=(),  # OAuth — tokens in auth.json, not env
    base_url="https://api.minimax.io/anthropic",
    auth_type="oauth_external",
    default_aux_model="MiniMax-M2.7",
)

register_provider(minimax)
register_provider(minimax_cn)
register_provider(minimax_oauth)
