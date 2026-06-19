"""ai — vision+LLM layer for Clicky Windows.

Public API:
    AIClient              abstract base
    AnthropicClient       cloud (Anthropic / OpenRouter)
    GeminiClient          Gemini via OpenRouter OpenAI-compat endpoint
    OllamaClient          local LLM via Ollama server
    PointParseResult      [POINT:x,y:label] parse result dataclass
    parse_point_tag       strip + extract coordinate tag from response text
    image_to_base64_jpeg  PIL → base64 JPEG for API
    parse_response_text   extract text from non-streaming SDK response
    create_ai_client      factory: routes model_id prefix → correct subclass
"""
from .base import (
    AIClient,
    PointParseResult,
    _CLICKY_MAX_TOKENS,
    _CLICKY_SYSTEM_PROMPT,
    image_to_base64_jpeg,
    parse_point_tag,
    parse_response_text,
)
from .anthropic_client import AnthropicClient
from .gemini_client import GeminiClient
from .ollama_client import OllamaClient

__all__ = [
    "AIClient",
    "AnthropicClient",
    "GeminiClient",
    "OllamaClient",
    "PointParseResult",
    "create_ai_client",
    "image_to_base64_jpeg",
    "parse_point_tag",
    "parse_response_text",
]

# Bare-name prefixes that route to OllamaClient (in addition to ollama/* prefix).
# These match common local-model name conventions Ollama uses.
_OLLAMA_BARE_PREFIXES = ("llama", "qwen", "llava", "mistral", "phi", "gemma")


def create_ai_client(
    model_id: str,
    api_key: str,
    base_url: str | None = None,
    ollama_host: str | None = None,
) -> AIClient:
    """Route to AnthropicClient, GeminiClient, or OllamaClient based on model_id prefix.

    This is THE BYOK abstraction. Users change MODEL_ID in .env, app.py calls
    this factory, and the right SDK routes the request. No app.py logic
    depends on which model family is active.

    Args:
        model_id: OpenRouter-style model ID. Prefix determines client:
            'anthropic/...' or 'claude...'      → AnthropicClient (anthropic SDK)
            'google/...' or 'gemini...'         → GeminiClient (openai SDK)
            'ollama/...' or bare 'llama*' /     → OllamaClient (httpx → local server)
                'qwen*' / 'llava*' / 'mistral*' /
                'phi*' / 'gemma*'
            Other prefixes raise ValueError with an actionable message.
        api_key: API key. Ignored for Ollama (local server, unauthenticated).
            For Anthropic/Gemini: same value for both — OpenRouter key when
            OpenRouter is configured via ANTHROPIC_BASE_URL, or the direct
            provider key otherwise.
        base_url: Optional override for cloud providers' API endpoints. Testing
            hook; production leaves it None so each client uses its SDK's
            default. Ignored by OllamaClient (use ``ollama_host`` instead).
        ollama_host: Optional override for Ollama server URL. Defaults to
            ``http://localhost:11434`` (Ollama's out-of-the-box binding).
            Only used when dispatching to OllamaClient.

    Returns:
        A concrete AIClient subclass ready for .ask_stream() calls.

    Raises:
        ValueError: if model_id prefix is not recognized. Error message lists
            the supported prefixes and hints how to add a new provider.
    """
    mid = model_id.lower()

    # Ollama dispatch FIRST — `llama*` and `qwen*` prefixes are unambiguous local
    # (no cloud provider ships them under those bare names).
    if mid.startswith("ollama/") or any(
        mid.startswith(p) for p in _OLLAMA_BARE_PREFIXES
    ):
        return OllamaClient(
            host=ollama_host or "http://localhost:11434",
            model_id=model_id,
        )

    if mid.startswith("anthropic/") or mid.startswith("claude"):
        # Auto-route OpenRouter keys (sk-or-v1-*) to OpenRouter's
        # Anthropic-compat endpoint when no explicit base_url given.
        # Bundled Clicky.exe has cwd outside the repo, so .env doesn't
        # load and ANTHROPIC_BASE_URL env var is unset — without this
        # fallback the SDK defaults to api.anthropic.com and Anthropic
        # rejects the OpenRouter-namespaced key with 401 invalid x-api-key.
        # Direct Anthropic keys (sk-ant-*) leave base_url=None so the
        # SDK uses its default api.anthropic.com endpoint, where those
        # keys are valid.
        if base_url is None and api_key and api_key.startswith("sk-or-"):
            base_url = "https://openrouter.ai/api"
        return AnthropicClient(
            api_key=api_key, model_id=model_id, base_url=base_url,
        )
    if mid.startswith("google/") or mid.startswith("gemini"):
        from config import OPENROUTER_BASE_URL
        return GeminiClient(
            api_key=api_key,
            model_id=model_id,
            base_url=base_url or OPENROUTER_BASE_URL,
        )
    raise ValueError(
        f"Unsupported MODEL_ID prefix: {model_id!r}. "
        f"Supported prefixes: 'anthropic/...' (or 'claude...'), "
        f"'google/...' (or 'gemini...'), 'ollama/...' (or bare "
        f"'llama*'/'qwen*'/'llava*'/'mistral*'/'phi*'/'gemma*'). "
        f"To add a new provider, subclass AIClient in ai/base.py and extend "
        f"create_ai_client() in ai/__init__.py with a new branch."
    )
