from __future__ import annotations

import re
from html import unescape

IMAGE_RE = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.IGNORECASE)
VIDEO_RE = re.compile(r"<(?:video|source)[^>]+src=[\"']([^\"']+)[\"']", re.IGNORECASE)
META_TAG_RE = re.compile(
    r"<meta[^>]+(?:property|name)=[\"']([^\"']+)[\"'][^>]+content=[\"']([^\"']+)[\"'][^>]*>"
    r"|<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+(?:property|name)=[\"']([^\"']+)[\"'][^>]*>",
    re.IGNORECASE,
)
TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")


def extract_first_image_url(value: str | None) -> str | None:
    if not value:
        return None

    match = IMAGE_RE.search(value)
    if not match:
        return None

    return unescape(match.group(1).strip())


def extract_first_video_url(value: str | None) -> str | None:
    if not value:
        return None

    match = VIDEO_RE.search(value)
    if not match:
        return None

    return unescape(match.group(1).strip())


def extract_meta_content(value: str | None, keys: tuple[str, ...]) -> str | None:
    if not value:
        return None

    lowered_keys = {key.lower() for key in keys}
    for match in META_TAG_RE.finditer(value):
        name = (match.group(1) or match.group(4) or "").strip().lower()
        if name not in lowered_keys:
            continue

        content = (match.group(2) or match.group(3) or "").strip()
        if content:
            return unescape(content)

    return None


def strip_html(value: str | None) -> str:
    if not value:
        return ""

    text = unescape(TAG_RE.sub(" ", value))
    return SPACE_RE.sub(" ", text).strip()


def shorten(value: str, max_length: int) -> str:
    if len(value) <= max_length:
        return value

    trimmed = value[: max_length - 1].rsplit(" ", 1)[0].strip()
    if not trimmed:
        trimmed = value[: max_length - 1].strip()
    return f"{trimmed}…"
