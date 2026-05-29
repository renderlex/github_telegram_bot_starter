from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(slots=True)
class CandidateItem:
    source: str
    external_id: str
    title: str
    url: str
    summary: str
    published_at: datetime
    image_url: str | None = None
    developer_url: str | None = None
    media_url: str | None = None
    media_kind: str | None = None
    stars: int | None = None
    rating_label: str | None = None
    language: str | None = None
    topics: tuple[str, ...] = ()
    author_context: str = ""
    validated_url: bool = False
    score: float = 0.0

    def __post_init__(self) -> None:
        if self.published_at.tzinfo is None:
            self.published_at = self.published_at.replace(tzinfo=timezone.utc)
        else:
            self.published_at = self.published_at.astimezone(timezone.utc)

    @property
    def source_key(self) -> str:
        return f"{self.source}:{self.external_id}"


@dataclass(slots=True)
class RuntimeConfig:
    post_interval_minutes: int
    search_window_hours: int
    quiet_hours_start_hour: int | None = None
    quiet_hours_end_hour: int | None = None
    admin_chat_id: str | None = None
    llm_provider: str = "auto"
    opencode_model: str | None = None
    ollama_model: str | None = None


@dataclass(slots=True)
class PublicationResult:
    item: CandidateItem
    caption: str
    published: bool
    message_id: int | None = None
