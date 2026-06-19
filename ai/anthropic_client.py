"""AnthropicClient — vision streaming via Anthropic SDK with [POINT:x,y:label] tag."""
from __future__ import annotations

from typing import Iterator

from anthropic import Anthropic
from PIL import Image

from .base import (
    AIClient,
    PointParseResult,
    _CLICKY_MAX_TOKENS,
    _CLICKY_SYSTEM_PROMPT,
    _KB_SYSTEM_PREFIX_TEMPLATE,
    _MEMORY_PREFIX_MARKER,
    image_to_base64_jpeg,
    parse_point_tag,
)


class AnthropicClient(AIClient):
    """Phase 1 implementation using plain vision streaming + [POINT:x,y:label].

    Matches Clicky's actual shipping path: ClaudeAPI.analyzeImageStreaming +
    CompanionManager.parsePointingCoordinates. NOT Computer Use API beta
    (that was dead code in Clicky — ElementLocationDetector.swift, 0 refs).
    See DECISIONS.md 2026-04-12 (evening 3).
    """

    def __init__(
        self,
        api_key: str,
        model_id: str,
        base_url: str | None = None,
    ) -> None:
        kwargs: dict = {"api_key": api_key, "timeout": 60.0}
        if base_url is not None:
            kwargs["base_url"] = base_url
        self.client = Anthropic(**kwargs)
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
        """Open a streaming Claude call, return a context manager.

        Args:
            images: list of (PIL Image, label string) tuples — one per screen.
                Sorted cursor-screen-first by capture_all_screens(). Each
                becomes an image content block + a text label block in the
                user message. Matches Clicky's analyzeImageStreaming(images:
                [(Data, String)], ...) shape.
            transcript: user's voice question (raw STT output).
            history: prior turns in Anthropic SDK message format.
            system_prompt: persona + pointing instructions.
            max_tokens: token budget (1024 default, matches Clicky).
            kb_content: optional curated KB markdown body (from
                kb.recall). If non-empty, injected as a SECOND
                cache_control system block alongside the persona block.
                Empty (default) → only the persona block is sent.
            kb_app_name: sanitized .exe basename used to format the KB
                injection marker (e.g. "edupack.exe" → display "edupack").
                Ignored when kb_content is empty.

        Usage:
            with client.ask_stream(images, transcript, history) as stream:
                for delta in stream.text_deltas():
                    # progressive text for sentence-level TTS chunking
                    pass
                result = stream.final_result()
                # result.spoken_text, result.coordinate, etc.
        """
        content_blocks: list[dict] = []
        for img, label in images:
            base64_jpeg = image_to_base64_jpeg(img)
            content_blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": base64_jpeg,
                },
            })
            content_blocks.append({"type": "text", "text": label})

        # Split the user transcript into a cached memory-prefix block + an
        # uncached current-transcript block when the memory marker is present.
        # app.py (Step 7 pipeline worker) prepends memory context as:
        #     "[context from past sessions ...]\n<memory>\n\n<actual transcript>"
        # Caching the prefix saves ~50-100ms TTFT after the first hit (5-min
        # TTL). NEVER cache the current transcript — per-turn content is what
        # makes the full-context-caching latency paradox bite (arxiv 2601.06007
        # "Don't Break the Cache" — only stable prefixes help).
        if _MEMORY_PREFIX_MARKER in transcript:
            parts = transcript.split("\n\n", 1)
            if len(parts) == 2:
                memory_text, actual_transcript = parts
                content_blocks.append({
                    "type": "text",
                    "text": memory_text + "\n\n",
                    "cache_control": {"type": "ephemeral"},
                })
                content_blocks.append({"type": "text", "text": actual_transcript})
            else:
                content_blocks.append({"type": "text", "text": transcript})
        else:
            content_blocks.append({"type": "text", "text": transcript})

        new_user_turn = {"role": "user", "content": content_blocks}

        # Cache the system prompt (largest stable text block, ~1500 chars).
        # OpenRouter passes Anthropic-native cache_control through for
        # anthropic/* routes per openrouter.ai/docs/guides/best-practices/prompt-caching.
        system_blocks = [
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ]
        # Optional second cache_control block for user-uploaded curated KB
        # (kb.recall result). Per-app cache: hit within same app session,
        # miss on app switch. Anthropic's 4-block limit accommodates this
        # plus the user-message memory prefix block.
        if kb_content:
            display_name = kb_app_name.removesuffix(".exe") or "this software"
            kb_text = (
                _KB_SYSTEM_PREFIX_TEMPLATE.format(app_name=display_name)
                + kb_content
            )
            system_blocks.append({
                "type": "text",
                "text": kb_text,
                "cache_control": {"type": "ephemeral"},
            })

        sdk_stream_mgr = self.client.messages.stream(
            model=self.model_id,
            max_tokens=max_tokens,
            system=system_blocks,
            messages=[*history, new_user_turn],
        )

        return _StreamingResponse(sdk_stream_mgr)

    def ask(
        self,
        image: Image.Image,
        transcript: str,
        history: list[dict],
        declared_w: int,
        declared_h: int,
    ) -> dict:
        """Batch wrapper: consumes the full stream, returns parsed dict.

        Wraps a single image into the list format ask_stream() expects.
        Backwards-compatible with the __main__ gate and test shapes.
        """
        label = f"primary focus (image dimensions: {declared_w}x{declared_h} pixels)"
        with self.ask_stream(
            [(image, label)], transcript, history
        ) as stream:
            for _ in stream.text_deltas():
                pass
            result = stream.final_result()

        points = []
        if result.coordinate:
            x, y = result.coordinate
            points.append({"x": x, "y": y, "label": result.element_label or ""})

        return {"text": result.spoken_text, "points": points}


class _StreamingResponse:
    """Wraps the SDK's MessageStreamManager for Clicky's streaming pattern."""

    def __init__(self, sdk_stream_mgr):
        self._sdk_mgr = sdk_stream_mgr
        self._sdk_stream = None
        self._accumulated = ""
        self._deltas_exhausted = False

    def __enter__(self):
        self._sdk_stream = self._sdk_mgr.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return self._sdk_mgr.__exit__(exc_type, exc_val, exc_tb)

    def text_deltas(self) -> Iterator[str]:
        """Yield progressive text deltas for sentence-level TTS chunking."""
        for delta in self._sdk_stream.text_stream:
            self._accumulated += delta
            yield delta
        self._deltas_exhausted = True

    def final_result(self) -> PointParseResult:
        """Parse the accumulated text for a [POINT:x,y:label] tag.

        If text_deltas() was fully exhausted, uses the accumulated text.
        Otherwise falls back to get_final_text() which blocks until the
        stream completes.
        """
        if not self._deltas_exhausted:
            self._accumulated = self._sdk_stream.get_final_text()
        return parse_point_tag(self._accumulated)
