"""OllamaClient — local LLM via Ollama server's /api/chat streaming endpoint."""
from __future__ import annotations

from typing import Iterator

import httpx
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


class OllamaClient(AIClient):
    """Local LLM via Ollama server (https://ollama.com).

    Speaks Ollama's /api/chat streaming protocol over HTTP. Accepts multi-image
    screenshots passed as base64-encoded JPEGs inline in the user message's
    ``images`` field (Ollama-specific extension to the OpenAI-style messages
    array).

    **Pixel-pointing caveat:** local vision models (llama3.2-vision, qwen2.5-vl,
    llava) generally cannot return precise pixel coordinates via free-text
    [POINT:x,y:label] tags. app.py wires a grid-locator fallback (ai/locator.py)
    that runs AFTER the streamed response completes: if no [POINT:x,y] tag was
    emitted AND the query was directional, the locator pass derives coordinates
    via two-stage grid annotation. See DECISIONS.md 2026-06-05 (Sprint v0.2.0)
    for the rationale + Bitshank-2338/clicky-windows reference implementation.

    Public interface matches AnthropicClient + GeminiClient: ``ask_stream(...)``
    returns a context manager with .text_deltas() generator + .final_result()
    returning PointParseResult. app.py's _pipeline_worker doesn't need to know
    which client is behind it.
    """

    def __init__(self, host: str, model_id: str) -> None:
        # Strip optional 'ollama/' prefix — Ollama API wants the bare model name.
        if model_id.lower().startswith("ollama/"):
            self.model_id = model_id[len("ollama/"):]
        else:
            self.model_id = model_id
        self.host = host.rstrip("/")

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
        """Open a streaming Ollama /api/chat call. Returns a context manager
        with the same interface as AnthropicClient.ask_stream().

        Builds Ollama-shaped messages:
            - System prompt as messages[0] role=system. KB block concat'd onto
              system prompt (Ollama doesn't support multi-block cache_control).
            - History flattened from Anthropic content-block format to plain
              strings (Ollama uses OpenAI-style string content).
            - Current user turn: {role: user, content: transcript, images: [b64...]}
              with base64-encoded JPEG screenshots as a list of strings (Ollama's
              extension to the standard message shape).

        Usage:
            with client.ask_stream(images, transcript, history) as stream:
                for delta in stream.text_deltas():
                    ...
                result = stream.final_result()
        """
        # Concat KB into system prompt (no multi-block support on Ollama).
        full_system = system_prompt
        if kb_content:
            display_name = kb_app_name.removesuffix(".exe") or "this software"
            full_system = (
                system_prompt
                + "\n\n"
                + _KB_SYSTEM_PREFIX_TEMPLATE.format(app_name=display_name)
                + kb_content
            )

        # Build OpenAI-style messages array.
        ollama_messages: list[dict] = [
            {"role": "system", "content": full_system}
        ]

        # Flatten history (Anthropic content-blocks → plain strings).
        for turn in history:
            text_parts: list[str] = []
            content = turn.get("content", "")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
            elif isinstance(content, str):
                text_parts.append(content)
            # Skip empty turns (image-only or malformed) — Ollama rejects empty content.
            if not any(p.strip() for p in text_parts):
                continue
            ollama_messages.append({
                "role": turn["role"],
                "content": " ".join(text_parts),
            })

        # Current user turn — transcript as content, screenshots as images list
        user_turn: dict = {"role": "user", "content": transcript}
        if images:
            user_turn["images"] = [
                image_to_base64_jpeg(img) for img, _label in images
            ]
        ollama_messages.append(user_turn)

        payload = {
            "model": self.model_id,
            "messages": ollama_messages,
            "stream": True,
            "options": {
                "num_predict": max_tokens,
                "temperature": 0.7,
            },
        }

        return _OllamaStreamingResponse(
            host=self.host,
            payload=payload,
            model_for_errors=self.model_id,
        )

    def ask(
        self,
        image: Image.Image,
        transcript: str,
        history: list[dict],
        declared_w: int,
        declared_h: int,
    ) -> dict:
        """Batch wrapper for parity with AnthropicClient.ask() / GeminiClient.ask()."""
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


class _OllamaStreamingResponse:
    """Wraps httpx streaming response from Ollama /api/chat to match
    AnthropicClient + GeminiClient's _StreamingResponse public interface
    (context manager + text_deltas() + final_result()). Consumers of
    ask_stream() don't need to know which client is behind it.

    Each instance opens its own httpx.Client + stream context on __enter__
    (so the network call doesn't fire until the caller actually wants to
    stream). __exit__ closes both.
    """

    def __init__(self, host: str, payload: dict, model_for_errors: str) -> None:
        self._host = host
        self._payload = payload
        self._model_for_errors = model_for_errors
        self._httpx_client = None
        self._stream_cm = None
        self._response = None
        self._accumulated = ""
        self._deltas_exhausted = False

    def __enter__(self):
        # Open the httpx client first. If any subsequent open-step raises,
        # we MUST close this client — the caller's `with` block never gets
        # entered, so __exit__ won't fire. Without the try/except below the
        # client would leak its connection pool every time Ollama is
        # unreachable (DNS failure, ECONNREFUSED, server crash, etc).
        # Caught by superpowers:code-reviewer 2026-06-05.
        self._httpx_client = httpx.Client(timeout=120.0)
        self._httpx_client.__enter__()
        try:
            self._stream_cm = self._httpx_client.stream(
                "POST",
                f"{self._host}/api/chat",
                json=self._payload,
            )
            self._response = self._stream_cm.__enter__()

            # Friendly error for the most common Ollama mistake: model not pulled
            if self._response.status_code == 404:
                # Close before raising to release the connection
                try:
                    self._stream_cm.__exit__(None, None, None)
                finally:
                    self._httpx_client.__exit__(None, None, None)
                    self._httpx_client = None
                    self._stream_cm = None
                raise RuntimeError(
                    f"Ollama doesn't have '{self._model_for_errors}' installed. "
                    f"Run: ollama pull {self._model_for_errors}"
                )
            self._response.raise_for_status()
        except BaseException:
            # Any failure between httpx_client open and successful
            # raise_for_status (network error, 5xx, etc.): close the
            # client we just opened, null the slot so __exit__ is a no-op,
            # re-raise so the caller sees the original exception.
            if self._stream_cm is not None:
                try:
                    self._stream_cm.__exit__(None, None, None)
                except Exception:
                    pass
                self._stream_cm = None
            try:
                self._httpx_client.__exit__(None, None, None)
            except Exception:
                pass
            self._httpx_client = None
            raise
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if self._stream_cm is not None:
                self._stream_cm.__exit__(exc_type, exc_val, exc_tb)
        finally:
            if self._httpx_client is not None:
                self._httpx_client.__exit__(exc_type, exc_val, exc_tb)
        return False  # don't swallow exceptions

    def text_deltas(self) -> Iterator[str]:
        """Yield progressive text chunks from Ollama's JSON-per-line stream.

        Each line is a JSON object like {"message": {"content": "..."}, "done": false}.
        We accumulate the .message.content fields and yield non-empty chunks.

        On socket failure mid-stream (Ollama crash, network drop), httpx raises
        ReadError / RemoteProtocolError. We MUST mark deltas_exhausted=True in
        a finally block so that a subsequent final_result() call doesn't re-
        enter this iterator and re-raise the same exception, instead returning
        whatever was accumulated before the failure (graceful degradation —
        the user gets a partial response with parse_point_tag falling back to
        no coordinate). Caught by superpowers:code-reviewer 2026-06-05.
        """
        import json as _json
        try:
            for line in self._response.iter_lines():
                if not line:
                    continue
                # iter_lines() in httpx returns str by default in newer
                # versions, bytes in older. Handle both.
                if isinstance(line, bytes):
                    line = line.decode("utf-8", errors="replace")
                try:
                    data = _json.loads(line)
                except _json.JSONDecodeError:
                    continue
                chunk = data.get("message", {}).get("content", "")
                if chunk:
                    self._accumulated += chunk
                    yield chunk
                if data.get("done"):
                    break
        finally:
            # ALWAYS set this — even on exception. Prevents final_result()
            # from re-entering this generator and re-raising the same error.
            self._deltas_exhausted = True

    def final_result(self) -> PointParseResult:
        """Parse the accumulated text for a [POINT:x,y:label] tag.

        Safe to call even if text_deltas() raised mid-stream: we just parse
        whatever was accumulated before the failure (may be empty string,
        in which case parse_point_tag returns PointParseResult with no
        coordinate and empty spoken_text). The pipeline worker handles
        empty results gracefully.
        """
        if not self._deltas_exhausted:
            # text_deltas() guarantees _deltas_exhausted=True even on failure,
            # so this re-entry can only happen if the caller never consumed
            # any deltas. Consume them now (best-effort — if iter_lines
            # raises, our own try/finally above flips the flag so we don't
            # loop forever).
            try:
                for _ in self.text_deltas():
                    pass
            except Exception:
                # Already accumulated whatever streamed in; parse_point_tag
                # below will return a PointParseResult with no coordinate.
                pass
        return parse_point_tag(self._accumulated)
