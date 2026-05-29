from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import feedparser
import httpx

from .models import CandidateItem
from .text_utils import extract_first_image_url, shorten, strip_html

logger = logging.getLogger(__name__)


class SourceCollector:
    def __init__(self, api_token: str | None, feed_urls: tuple[str, ...]) -> None:
        self._api_token = api_token
        self._feed_urls = feed_urls

    def fetch_candidates(self, search_window_hours: int = 72) -> list[CandidateItem]:
        if not self._feed_urls:
            logger.info("No SOURCE_FEED_URLS configured. Source collector returned no candidates.")
            return []

        cutoff = datetime.now(timezone.utc) - timedelta(hours=max(search_window_hours, 1))
        with httpx.Client(
            follow_redirects=True,
            timeout=20.0,
            headers={"User-Agent": "telegram-news-bot"},
        ) as client:
            candidates: list[CandidateItem] = []
            for index, feed_url in enumerate(self._feed_urls, start=1):
                candidates.extend(self._fetch_feed(client, feed_url, f"feed-{index}", cutoff))

        deduplicated: dict[str, CandidateItem] = {}
        for item in candidates:
            deduplicated[item.source_key] = item

        return list(deduplicated.values())

    def _fetch_feed(
        self,
        client: httpx.Client,
        feed_url: str,
        source: str,
        cutoff: datetime,
    ) -> list[CandidateItem]:
        try:
            response = client.get(feed_url, headers=self._request_headers())
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("Failed to fetch feed %s: %s", feed_url, exc)
            return []

        feed = feedparser.parse(response.text)
        items: list[CandidateItem] = []

        for entry in feed.entries[:20]:
            link = (entry.get("link") or "").strip()
            if not link:
                continue

            published_at = _feed_datetime(entry)
            if published_at < cutoff:
                continue

            html_summary = ""
            content_blocks = entry.get("content") or []
            if content_blocks:
                html_summary = content_blocks[0].get("value") or ""
            if not html_summary:
                html_summary = entry.get("summary") or ""

            title = (entry.get("title") or "News update").strip()
            items.append(
                CandidateItem(
                    source=source,
                    external_id=(entry.get("id") or link).strip(),
                    title=title,
                    url=link,
                    summary=shorten(strip_html(html_summary), 400),
                    published_at=published_at,
                    image_url=extract_first_image_url(html_summary),
                    author_context=shorten(strip_html(html_summary), 280),
                )
            )

        return items

    def _request_headers(self) -> dict[str, str]:
        headers = {"Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8"}
        if self._api_token:
            headers["Authorization"] = f"Bearer {self._api_token}"
        return headers


def _feed_datetime(entry: feedparser.FeedParserDict) -> datetime:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed is None:
        return datetime.now(timezone.utc)

    return datetime(
        parsed.tm_year,
        parsed.tm_mon,
        parsed.tm_mday,
        parsed.tm_hour,
        parsed.tm_min,
        parsed.tm_sec,
        tzinfo=timezone.utc,
    )
