from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import Settings
from .content_enricher import CandidateEnricher
from .source_collector import SourceCollector
from .llm import ModelCatalog, PostWriter, RuntimeModelState
from .models import CandidateItem, PublicationResult, RuntimeConfig
from .storage import PublicationStore
from .telegram_client import TelegramPublisher

logger = logging.getLogger(__name__)


class NewsTelegramService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._collector = SourceCollector(
            settings.source_api_token,
            settings.source_feed_urls,
        )
        self._enricher = CandidateEnricher()
        self._writer = PostWriter(
            provider=settings.llm_provider,
            ollama_base_url=settings.ollama_base_url,
            ollama_model=settings.ollama_model,
            opencode_model=settings.opencode_model,
        )
        self._publisher = TelegramPublisher(settings.telegram_bot_token)
        self._store = PublicationStore(settings.database_path)
        self._store.initialize()
        self._apply_runtime_config(self.load_runtime_config())

    def run_once(
        self,
        dry_run: bool = False,
        runtime_config: RuntimeConfig | None = None,
    ) -> list[PublicationResult]:
        active_config = runtime_config or self.load_runtime_config()
        self._apply_runtime_config(active_config)
        ranked_items = self._rank_candidates(
            self._collector.fetch_candidates(active_config.search_window_hours)
        )
        unpublished_items = self._store.filter_unpublished(
            self._publishable_items(ranked_items)
        )
        selected_items = self._enricher.enrich(
            unpublished_items[: self._selection_probe_limit()],
            self._settings.max_posts_per_run,
        )

        results: list[PublicationResult] = []
        for item in selected_items:
            caption = self._writer.compose_post(item)
            if dry_run:
                results.append(
                    PublicationResult(
                        item=item,
                        caption=caption,
                        published=False,
                    )
                )
                continue

            channel_id = self._require_channel_id()

            message_id = self._publisher.publish(
                channel_id,
                item,
                caption,
            )
            self._store.record_publication(item, caption, message_id)
            results.append(
                PublicationResult(
                    item=item,
                    caption=caption,
                    published=True,
                    message_id=message_id,
                )
            )

        return results

    def preview_candidates(
        self,
        runtime_config: RuntimeConfig | None = None,
    ) -> list[CandidateItem]:
        active_config = runtime_config or self.load_runtime_config()
        self._apply_runtime_config(active_config)
        ranked_items = self._rank_candidates(
            self._collector.fetch_candidates(active_config.search_window_hours)
        )
        unpublished_items = self._store.filter_unpublished(
            self._publishable_items(ranked_items)
        )
        return self._enricher.enrich(
            unpublished_items[: self._selection_probe_limit()],
            self._settings.max_posts_per_run,
        )

    def load_runtime_config(self) -> RuntimeConfig:
        return self._store.load_runtime_config(
            default_post_interval_minutes=self._settings.post_interval_minutes,
            default_search_window_hours=self._settings.search_window_hours,
            default_quiet_hours_start_hour=self._settings.quiet_hours_start_hour,
            default_quiet_hours_end_hour=self._settings.quiet_hours_end_hour,
            default_admin_chat_id=self._settings.telegram_admin_chat_id,
            default_llm_provider=self._settings.llm_provider,
            default_opencode_model=self._settings.opencode_model,
            default_ollama_model=self._settings.ollama_model,
        )

    def update_runtime_config(
        self,
        *,
        post_interval_minutes: int | None = None,
        search_window_hours: int | None = None,
        quiet_hours_start_hour: int | None = None,
        quiet_hours_end_hour: int | None = None,
        clear_quiet_hours: bool = False,
        admin_chat_id: str | None = None,
        llm_provider: str | None = None,
        opencode_model: str | None = None,
        ollama_model: str | None = None,
    ) -> RuntimeConfig:
        self._store.update_runtime_config(
            post_interval_minutes=post_interval_minutes,
            search_window_hours=search_window_hours,
            quiet_hours_start_hour=quiet_hours_start_hour,
            quiet_hours_end_hour=quiet_hours_end_hour,
            clear_quiet_hours=clear_quiet_hours,
            admin_chat_id=admin_chat_id,
            llm_provider=llm_provider,
            opencode_model=opencode_model,
            ollama_model=ollama_model,
        )
        runtime_config = self.load_runtime_config()
        self._apply_runtime_config(runtime_config)
        return runtime_config

    def get_llm_runtime_overview(
        self,
        runtime_config: RuntimeConfig | None = None,
    ) -> tuple[ModelCatalog, RuntimeModelState]:
        active_config = runtime_config or self.load_runtime_config()
        self._apply_runtime_config(active_config)
        return self._writer.get_runtime_overview()

    def get_quiet_hours_pause_until(
        self,
        runtime_config: RuntimeConfig | None = None,
        *,
        now: datetime | None = None,
    ) -> datetime | None:
        active_config = runtime_config or self.load_runtime_config()
        start_hour = active_config.quiet_hours_start_hour
        end_hour = active_config.quiet_hours_end_hour

        if start_hour is None or end_hour is None or start_hour == end_hour:
            return None

        current_time = now or datetime.now(timezone.utc)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)
        else:
            current_time = current_time.astimezone(timezone.utc)

        local_now = current_time.astimezone()
        if not self._is_quiet_hour(local_now.hour, start_hour, end_hour):
            return None

        resume_local = local_now.replace(hour=end_hour, minute=0, second=0, microsecond=0)
        if start_hour > end_hour and local_now.hour >= start_hour:
            resume_local += timedelta(days=1)
        elif start_hour < end_hour and resume_local <= local_now:
            resume_local += timedelta(days=1)

        return resume_local.astimezone(timezone.utc)

    def _apply_runtime_config(self, runtime_config: RuntimeConfig) -> None:
        self._writer.set_runtime_preferences(
            provider=runtime_config.llm_provider,
            opencode_model=runtime_config.opencode_model,
            ollama_model=runtime_config.ollama_model,
        )

    def _is_quiet_hour(self, local_hour: int, start_hour: int, end_hour: int) -> bool:
        if start_hour < end_hour:
            return start_hour <= local_hour < end_hour

        return local_hour >= start_hour or local_hour < end_hour

    def _publishable_items(self, items: list[CandidateItem]) -> list[CandidateItem]:
        publishable = [
            item
            for item in items
            if item.title.strip() and item.url.strip()
        ]
        if not publishable:
            logger.info("No publishable candidates after filtering.")
        return publishable

    def _selection_probe_limit(self) -> int:
        return max(self._settings.max_posts_per_run * 6, 12)

    def _require_channel_id(self) -> str:
        channel_id = (self._settings.telegram_channel_id or "").strip()
        if not channel_id:
            raise ValueError("TELEGRAM_CHANNEL_ID is required to publish posts.")

        if channel_id.startswith("http://") or channel_id.startswith("https://") or channel_id.startswith("+"):
            raise ValueError(
                "TELEGRAM_CHANNEL_ID must be a numeric channel chat_id like -1001234567890, not an invite link."
            )

        return channel_id

    def _rank_candidates(self, items: list[CandidateItem]) -> list[CandidateItem]:
        now = datetime.now(timezone.utc)

        for item in items:
            item.score = round(self._score(item, now), 2)

        return sorted(
            items,
            key=lambda item: (item.score, item.published_at),
            reverse=True,
        )

    def _score(self, item: CandidateItem, now: datetime) -> float:
        age_hours = max((now - item.published_at).total_seconds() / 3600, 0.0)
        freshness_bonus = max(0.0, 48.0 - age_hours) * 1.25
        source_weight = {
            "feed-1": 30.0,
            "feed-2": 28.0,
        }.get(item.source, 20.0)

        stars_bonus = math.log10(item.stars) * 12 if item.stars and item.stars > 0 else 0.0
        summary_bonus = 4.0 if item.summary else 0.0
        image_bonus = 2.0 if item.media_url or item.image_url else 0.0
        language_bonus = 1.0 if item.language else 0.0

        return source_weight + freshness_bonus + stars_bonus + summary_bonus + image_bonus + language_bonus
