"""GeminiClient — Gemini via OpenRouter's OpenAI-compat endpoint."""
from __future__ import annotations

from typing import Iterator

from openai import OpenAI
from PIL import Image

from .base import (
    AIClient,
    PointParseResult,
    _CLICKY_MAX_TOKENS,
    _CLICKY_SYSTEM_PROMPT,
    _KB_SYSTEM_PREFIX_TEMPLATE,
    image_to_base64_jpeg,
    parse_point_tag,
)


class GeminiClient(AIClient):
    """Gemini 3 Flash Preview (or any OpenRouter google/* model) via the
    OpenAI Python SDK pointed at OpenRouter's OpenAI-compat endpoint.

    Response parsing (parse_point_tag) is identical — both Claude and Gemini
    emit [POINT:x,y:label] per the verbatim Clicky system prompt. The only
    differences vs AnthropicClient are request shape (OpenAI chat.completions
    format instead of Anthropic messages format) and image block format
    (image_url with data URL instead of base64 source block).

    History assumption: we convert Anthropic content-block format to plain
    strings by concatenating text blocks. Non-text blocks in history are
    dropped. Phase 1 history only contains text blocks so this is safe; if
    Phase 2 adds image-bearing history, revisit this.

    See DECISIONS.md 2026-04-19 'Gemini 3 Flash via OpenRouter' for the
    dual-SDK routing rationale.
    """

    def __init__(
        self,
        api_key: str,
        model_id: str,
        base_url: str = "https://openrouter.ai/api/v1",
    ) -> None:
        self.client = OpenAI(api_key=api_key, base_url=base_url, timeout=60.0)
        self.model_id = model_id

    def ask_stream(
        self,
        images: list[tuple[Image.Image, str]],
        transcript: str,
        history: list[dict],
        system_prompt: str = _CLICKY_SYSTEM_PROMPT,
        max_tokens: int = _CLICKY_MAX_TOKENS,
        kb_content: str = "",
        kb_app_name: str = "",
    ):
        """Open a streaming Gemini call. Returns a context manager with the
        same interface as AnthropicClient.ask_stream().

        Builds OpenAI-shaped messages:
            - System prompt goes as messages[0] role=system. If
              ``kb_content`` is non-empty, the KB block is concatenated
              onto the system prompt (Gemini via OpenAI-compat doesn't
              support multiple system blocks or cache_control breakpoints,
              so caching is best-effort via OpenRouter's prompt-caching
              auto-detection).
            - History is converted from Anthropic content-block format to
              OpenAI plain-string content (text blocks are concatenated)
            - Current user turn gets image_url + text blocks

        Usage is identical to AnthropicClient:
            with client.ask_stream(images, transcript, history) as stream:
                for delta in stream.text_deltas():
                    ...
                result = stream.final_result()
        """
        user_content: list[dict] = []
        for img, label in images:
            base64_jpeg = image_to_base64_jpeg(img)
            user_content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{base64_jpeg}",
                },
            })
            user_content.append({"type": "text", "text": label})
        user_content.append({"type": "text", "text": transcript})

        # Concat KB into system prompt for Gemini (no native multi-block
        # support via OpenAI-compat endpoint).
        full_system = system_prompt
        if kb_content:
            display_name = kb_app_name.removesuffix(".exe") or "this software"
            full_system = (
                system_prompt
                + "\n\n"
                + _KB_SYSTEM_PREFIX_TEMPLATE.format(app_name=display_name)
                + kb_content
            )

        openai_messages: list[dict] = [
            {"role": "system", "content": full_system}
        ]
        for turn in history:
            text_parts = []
            for block in turn.get("content", []):
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
            # Skip turns with no text content (e.g., image-only turns from
            # Phase 2, or malformed turns) — OpenRouter rejects empty content.
            if not any(p.strip() for p in text_parts):
                continue
            openai_messages.append({
                "role": turn["role"],
                "content": " ".join(text_parts),
            })
        openai_messages.append({"role": "user", "content": user_content})

        try:
            sdk_iterator = self.client.chat.completions.create(
                model=self.model_id,
                messages=openai_messages,
                max_tokens=max_tokens,
                stream=True,
            )
        except Exception as exc:
            raise RuntimeError(
                f"Gemini request failed (model={self.model_id!r}). "
                "Diagnostic checklist:\n"
                "  1. Is the OpenRouter key (ANTHROPIC_API_KEY in .env) valid + funded?\n"
                "  2. Is the model available on your account? Preview models like "
                "'google/gemini-3-flash-preview' require opt-in at "
                "https://openrouter.ai/settings/privacy. Fallback: set "
                "MODEL_ID=google/gemini-2.5-flash in .env.\n"
                "  3. Is your internet connection up?\n"
                f"Underlying error: {type(exc).__name__}: {exc}"
            ) from exc
        return _GeminiStreamingResponse(sdk_iterator)

    def ask(
        self,
        image: Image.Image,
        transcript: str,
        history: list[dict],
        declared_w: int,
        declared_h: int,
    ) -> dict:
        """Batch wrapper for parity with AnthropicClient.ask()."""
        label = f"primary focus (image dimensions: {declared_w}x{declared_h} pixels)"
        with self.ask_stream([(image, label)], transcript, history) as stream:
            for _ in stream.text_deltas():
                pass
            result = stream.final_result()
        points = []
        if result.coordinate:
            x, y = result.coordinate
            points.append({"x": x, "y": y, "label": result.element_label or ""})
        return {"text": result.spoken_text, "points": points}


class _GeminiStreamingResponse:
    """Wraps OpenAI SDK streaming iterator to match AnthropicClient's
    _StreamingResponse public interface (context manager + text_deltas() +
    final_result()). Consumers of ask_stream() don't need to know which
    client is behind it.
    """

    def __init__(self, sdk_iterator):
        self._sdk_iterator = sdk_iterator
        self._accumulated = ""
        self._deltas_exhausted = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            self._sdk_iterator.close()
        except Exception:
            pass
        return False  # don't swallow exceptions

    def text_deltas(self) -> Iterator[str]:
        for chunk in self._sdk_iterator:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content
            if delta:
                self._accumulated += delta
                yield delta
        self._deltas_exhausted = True

    def final_result(self) -> PointParseResult:
        if not self._deltas_exhausted:
            for _ in self.text_deltas():
                pass
        return parse_point_tag(self._accumulated)
