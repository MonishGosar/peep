"""Shared constants, helpers, and AIClient abstract base for the ai package."""
from __future__ import annotations

import base64
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from io import BytesIO
from typing import Iterator

from PIL import Image


# --- Constants ----------------------------------------------------------------

_CLICKY_SYSTEM_PROMPT = """\
you're clicky, a friendly always-on companion that lives in the user's menu bar. the user just spoke to you via push-to-talk and you can see their screen(s). your reply will be spoken aloud via text-to-speech, so write the way you'd actually talk. this is an ongoing conversation — you remember everything they've said before.

rules:
- default to one or two sentences. be direct and dense. BUT if the user asks you to explain more, go deeper, or elaborate, then go all out — give a thorough, detailed explanation with no length limit.
- all lowercase, casual, warm. no emojis.
- write for the ear, not the eye. short sentences. no lists, bullet points, markdown, or formatting — just natural speech.
- don't use abbreviations or symbols that sound weird read aloud. write "for example" not "e.g.", spell out small numbers.
- if the user's question relates to what's on their screen, reference specific things you see.
- if the screenshot doesn't seem relevant to their question, just answer the question directly.
- you can help with anything — coding, writing, general knowledge, brainstorming.
- never say "simply" or "just".
- don't read out code verbatim. describe what the code does or what needs to change conversationally.
- focus on giving a thorough, useful explanation. don't end with simple yes/no questions like "want me to explain more?" or "should i show you?" — those are dead ends that force the user to just say yes.
- instead, when it fits naturally, end by planting a seed — mention something bigger or more ambitious they could try, a related concept that goes deeper, or a next-level technique that builds on what you just explained. make it something worth coming back for, not a question they'd just nod to. it's okay to not end with anything extra if the answer is complete on its own.
- if you receive multiple screen images, the one labeled "primary focus" is where the cursor is — prioritize that one but reference others if relevant.

element pointing:
you have a small blue cursor that can fly to and point at things on screen. use it whenever pointing would genuinely help the user — if they're asking how to do something, looking for a menu, trying to find a button, or need help navigating an app, point at the relevant element. err on the side of pointing rather than not pointing, because it makes your help way more useful and concrete.

don't point at things when it would be pointless — like if the user asks a general knowledge question, or the conversation has nothing to do with what's on screen, or you'd just be pointing at something obvious they're already looking at. but if there's a specific UI element, menu, button, or area on screen that's relevant to what you're helping with, point at it.

when you point, append a coordinate tag at the very end of your response, AFTER your spoken text. the screenshot images are labeled with their pixel dimensions. use those dimensions as the coordinate space. the origin (0,0) is the top-left corner of the image. x increases rightward, y increases downward.

format: [POINT:x,y:label] where x,y are integer pixel coordinates in the screenshot's coordinate space, and label is a short 1-3 word description of the element (like "search bar" or "save button"). if the element is on the cursor's screen you can omit the screen number. if the element is on a DIFFERENT screen, append :screenN where N is the screen number from the image label (e.g. :screen2). this is important — without the screen number, the cursor will point at the wrong place.

if pointing wouldn't help, append [POINT:none].

examples:
- user asks how to color grade in final cut: "you'll want to open the color inspector — it's right up in the top right area of the toolbar. click that and you'll get all the color wheels and curves. [POINT:1100,42:color inspector]"
- user asks what html is: "html stands for hypertext markup language, it's basically the skeleton of every web page. curious how it connects to the css you're looking at? [POINT:none]"
- user asks how to commit in xcode: "see that source control menu up top? click that and hit commit, or you can use command option c as a shortcut. [POINT:285,11:source control]"
- element is on screen 2 (not where cursor is): "that's over on your other monitor — see the terminal window? [POINT:400,300:terminal:screen2]"\
"""

_POINT_TAG_RE = re.compile(
    r"\[POINT:(?:none|(\d+)\s*,\s*(\d+)(?::(?!screen\d)([^\]:\s][^\]:]*?))?(?::screen(\d+))?)\]\s*$"
)
"""Regex for Clicky's [POINT:x,y:label(:screenN)?] coordinate tag.

Python port of CompanionManager.parsePointingCoordinates
(leanring-buddy/CompanionManager.swift:784-828).
"""

_CLICKY_MAX_TOKENS = 1024
"""Token budget matching Clicky's analyzeImageStreaming call."""

_MEMORY_PREFIX_MARKER = "[context from past sessions"
"""Sentinel that app.py prepends to the user transcript when memory is
injected. Used by AnthropicClient.ask_stream to split the transcript into a
cached memory-prefix block + an uncached current-turn block. Must match
app.py ClickyApp._pipeline_worker's f-string exactly."""

_KB_SYSTEM_PREFIX_TEMPLATE = (
    "app knowledge base:\n"
    "you are helping the user with {app_name}. here is reference "
    "documentation that you should treat as authoritative:\n\n"
)
"""Marker prefix prepended to user-uploaded KB content before injection
into the system prompt as a SECOND cache_control system block. Caller (app.py
_pipeline_worker → ask_stream's kb_content kwarg) supplies the raw
markdown body; ask_stream formats this prefix in front and adds the
ephemeral cache breakpoint. Per-app cache hit on subsequent turns within
the same app session; cache miss on app switch (acceptable since each
KB read is the dominant cost anyway). Empty kb_content means no second
block — Claude proceeds with vision + memory only (the 'Claude already
knows that software' path)."""


# --- PointParseResult ---------------------------------------------------------

@dataclass
class PointParseResult:
    """Result of parsing the [POINT:...] tag from Claude's response text."""
    spoken_text: str
    coordinate: tuple[int, int] | None
    element_label: str | None
    screen_number: int | None


# --- Pure functions -----------------------------------------------------------

def parse_point_tag(text: str) -> PointParseResult:
    """Extract coordinate from a trailing [POINT:x,y:label] tag and strip it.

    Returns PointParseResult with coordinate=None on [POINT:none] or no match.
    The spoken_text field has the tag removed so TTS never reads it aloud.
    """
    match = _POINT_TAG_RE.search(text)
    if not match:
        return PointParseResult(
            spoken_text=text.strip(),
            coordinate=None,
            element_label=None,
            screen_number=None,
        )

    spoken = _POINT_TAG_RE.sub("", text).strip()

    if match.group(1) is None:
        return PointParseResult(
            spoken_text=spoken,
            coordinate=None,
            element_label=None,
            screen_number=None,
        )

    x, y = int(match.group(1)), int(match.group(2))
    label = match.group(3)
    screen = int(match.group(4)) if match.group(4) else None

    return PointParseResult(
        spoken_text=spoken,
        coordinate=(x, y),
        element_label=label,
        screen_number=screen,
    )


def image_to_base64_jpeg(img: Image.Image, quality: int = 85) -> str:
    """Encode a PIL image to a base64-ASCII JPEG string for the Claude API."""
    buf = BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _get(obj, key, default=None):
    """Dual-access helper: works on both dict-shaped test mocks and
    anthropic SDK objects (via attribute access)."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def parse_response_text(response) -> str:
    """Concatenate all text-type content blocks into a single string.

    Dual-access compatible (dict mocks or SDK objects). Used by the batch
    ask() wrapper to extract the full text from a non-streaming response.
    """
    content = _get(response, "content", []) or []
    texts: list[str] = []
    for block in content:
        if _get(block, "type") != "text":
            continue
        text = _get(block, "text", "") or ""
        if text:
            texts.append(text)
    return " ".join(texts).strip()


# --- AIClient abstract base ---------------------------------------------------

class AIClient(ABC):
    """Abstract base for vision+LLM providers.

    Phase 1: AnthropicClient (vision-tag streaming).
    Phase 2: OpenRouterClient, GeminiClient, etc. as subclass drops.
    """

    @abstractmethod
    def ask(
        self,
        image: Image.Image,
        transcript: str,
        history: list[dict],
        declared_w: int,
        declared_h: int,
    ) -> dict:
        """Return {"text": str, "points": [{"x":int,"y":int,"label":str}]}.

        Coordinates are in Claude's declared-resolution space (Space C),
        unclamped. Caller uses capture.unscale_claude_coords() to map to
        physical pixels (Space A).
        """
        ...
