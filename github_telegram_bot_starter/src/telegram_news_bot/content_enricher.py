from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx

from .models import CandidateItem
from .text_utils import (
    extract_first_image_url,
    extract_first_video_url,
    extract_meta_content,
    shorten,
    strip_html,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PageContext:
    final_url: str
    description: str | None = None
    media_url: str | None = None
    media_kind: str | None = None


class CandidateEnricher:
    def enrich(self, items: list[CandidateItem], max_count: int) -> list[CandidateItem]:
        if max_count <= 0:
            return []

        prepared: list[CandidateItem] = []
        with httpx.Client(
            follow_redirects=True,
            timeout=20.0,
            headers={"User-Agent": "telegram-news-bot"},
        ) as client:
            for item in items:
                enriched = self._enrich_item(client, item)
                if enriched is None:
                    continue

                prepared.append(enriched)
                if len(prepared) >= max_count:
                    break

        return prepared

    def _enrich_item(
        self,
        client: httpx.Client,
        item: CandidateItem,
    ) -> CandidateItem | None:
        if not item.url:
            return None

        validated_source_url = self._validate_public_url(client, item.url)
        if not validated_source_url:
            logger.info("Skipping %s because the source URL could not be verified.", item.title)
            return None

        item.url = validated_source_url
        item.validated_url = True
        if not item.rating_label and item.stars:
            item.rating_label = f"{item.stars:,} stars"

        if item.developer_url:
            page_context = self._fetch_page_context(client, item.developer_url)
            if page_context is not None:
                item.developer_url = page_context.final_url
                if page_context.description:
                    item.author_context = shorten(page_context.description, 280)
                if page_context.media_url:
                    item.media_url = page_context.media_url
                    item.media_kind = page_context.media_kind
            else:
                item.developer_url = None

        if not item.author_context:
            item.author_context = shorten(item.summary or item.title, 280)

        if not item.media_url and item.image_url:
            item.media_url = item.image_url
            item.media_kind = "photo"

        return item

    def _validate_public_url(
        self,
        client: httpx.Client,
        url: str,
    ) -> str | None:
        normalized_url = self._normalize_url(url)
        if not normalized_url:
            return None

        try:
            with client.stream("GET", normalized_url, headers={"Range": "bytes=0-0"}) as response:
                response.raise_for_status()
                final_url = str(response.url)
        except httpx.HTTPError:
            return None

        return final_url

    def _fetch_page_context(
        self,
        client: httpx.Client,
        url: str,
    ) -> PageContext | None:
        normalized_url = self._normalize_url(url)
        if not normalized_url:
            return None

        try:
            response = client.get(normalized_url)
            response.raise_for_status()
        except httpx.HTTPError:
            return None

        final_url = str(response.url)
        content_type = (response.headers.get("content-type") or "").lower()
        if "text/html" not in content_type:
            return PageContext(final_url=final_url)

        html = response.text
        description = self._extract_description(html)
        media_url, media_kind = self._discover_media(client, html, final_url)

        return PageContext(
            final_url=final_url,
            description=description,
            media_url=media_url,
            media_kind=media_kind,
        )

    def _discover_media(
        self,
        client: httpx.Client,
        html: str,
        page_url: str,
    ) -> tuple[str | None, str | None]:
        video_candidates = [
            extract_meta_content(html, ("og:video:url", "og:video", "twitter:player:stream")),
            extract_first_video_url(html),
        ]
        for candidate in video_candidates:
            resolved = self._resolve_url(page_url, candidate)
            if not resolved:
                continue
            if self._validate_media_url(client, resolved, expected_kind="video"):
                return resolved, "video"

        image_candidates = [
            extract_meta_content(html, ("og:image", "twitter:image", "twitter:image:src")),
            extract_first_image_url(html),
        ]
        for candidate in image_candidates:
            resolved = self._resolve_url(page_url, candidate)
            if not resolved:
                continue
            if self._validate_media_url(client, resolved, expected_kind="photo"):
                return resolved, "photo"

        return None, None

    def _validate_media_url(
        self,
        client: httpx.Client,
        url: str,
        expected_kind: str,
    ) -> bool:
        try:
            with client.stream("GET", url, headers={"Range": "bytes=0-0"}) as response:
                response.raise_for_status()
                content_type = (response.headers.get("content-type") or "").lower()
        except httpx.HTTPError:
            return False

        if expected_kind == "video":
            return content_type.startswith("video/")

        return content_type.startswith("image/")

    def _extract_description(self, html: str) -> str | None:
        description = extract_meta_content(
            html,
            (
                "description",
                "og:description",
                "twitter:description",
            ),
        )
        if not description:
            return None

        cleaned = shorten(strip_html(description), 280)
        return cleaned or None

    def _resolve_url(self, base_url: str, raw_url: str | None) -> str | None:
        if not raw_url:
            return None

        return self._normalize_url(urljoin(base_url, raw_url.strip()))

    def _normalize_url(self, url: str | None) -> str | None:
        if not url:
            return None

        normalized = url.strip()
        if not normalized:
            return None

        if normalized.startswith("//"):
            normalized = f"https:{normalized}"
        elif not normalized.startswith(("http://", "https://")):
            normalized = f"https://{normalized}"

        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"}:
            return None
        if not parsed.netloc:
            return None

        return normalized
