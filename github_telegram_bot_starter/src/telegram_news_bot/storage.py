from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import VALID_LLM_PROVIDERS
from .models import CandidateItem, RuntimeConfig


def _normalize_hour(value: int) -> int:
    if value < 0 or value > 23:
        raise ValueError("Quiet hour must be between 0 and 23.")
    return value


class PublicationStore:
    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path

    def initialize(self) -> None:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)

        with sqlite3.connect(self._database_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS published_items (
                    source_key TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL,
                    caption TEXT NOT NULL,
                    image_url TEXT,
                    telegram_message_id INTEGER,
                    source_published_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_settings (
                    name TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.commit()

    def filter_unpublished(self, items: list[CandidateItem]) -> list[CandidateItem]:
        if not items:
            return []

        source_keys = [item.source_key for item in items]
        placeholders = ",".join("?" for _ in source_keys)

        with sqlite3.connect(self._database_path) as connection:
            rows = connection.execute(
                f"SELECT source_key FROM published_items WHERE source_key IN ({placeholders})",
                source_keys,
            ).fetchall()

        published_keys = {row[0] for row in rows}
        return [item for item in items if item.source_key not in published_keys]

    def record_publication(
        self,
        item: CandidateItem,
        caption: str,
        telegram_message_id: int | None,
    ) -> None:
        created_at = datetime.now(timezone.utc).isoformat()

        with sqlite3.connect(self._database_path) as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO published_items (
                    source_key,
                    source,
                    title,
                    url,
                    caption,
                    image_url,
                    telegram_message_id,
                    source_published_at,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.source_key,
                    item.source,
                    item.title,
                    item.url,
                    caption,
                    item.media_url or item.image_url,
                    telegram_message_id,
                    item.published_at.isoformat(),
                    created_at,
                ),
            )
            connection.commit()

    def load_runtime_config(
        self,
        default_post_interval_minutes: int,
        default_search_window_hours: int,
        default_quiet_hours_start_hour: int | None = None,
        default_quiet_hours_end_hour: int | None = None,
        default_admin_chat_id: str | None = None,
        default_llm_provider: str = "auto",
        default_opencode_model: str | None = None,
        default_ollama_model: str | None = None,
    ) -> RuntimeConfig:
        values = {
            "post_interval_minutes": default_post_interval_minutes,
            "search_window_hours": default_search_window_hours,
            "quiet_hours_start_hour": default_quiet_hours_start_hour,
            "quiet_hours_end_hour": default_quiet_hours_end_hour,
            "admin_chat_id": default_admin_chat_id,
            "llm_provider": default_llm_provider,
            "opencode_model": default_opencode_model,
            "ollama_model": default_ollama_model,
        }

        with sqlite3.connect(self._database_path) as connection:
            rows = connection.execute(
                "SELECT name, value FROM runtime_settings WHERE name IN (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "post_interval_minutes",
                    "search_window_hours",
                    "quiet_hours_start_hour",
                    "quiet_hours_end_hour",
                    "admin_chat_id",
                    "llm_provider",
                    "opencode_model",
                    "ollama_model",
                ),
            ).fetchall()

        for name, value in rows:
            if name == "admin_chat_id":
                values[name] = value or None
                continue
            if name in {"quiet_hours_start_hour", "quiet_hours_end_hour"}:
                if value == "":
                    values[name] = None
                    continue
                try:
                    values[name] = _normalize_hour(int(value))
                except (TypeError, ValueError):
                    continue
                continue
            if name in {"opencode_model", "ollama_model"}:
                values[name] = value or None
                continue
            if name == "llm_provider":
                normalized = (value or "").strip().lower()
                if normalized in VALID_LLM_PROVIDERS:
                    values[name] = normalized
                continue
            try:
                values[name] = int(value)
            except (TypeError, ValueError):
                continue

        quiet_hours_start_hour = values["quiet_hours_start_hour"]
        quiet_hours_end_hour = values["quiet_hours_end_hour"]
        if quiet_hours_start_hour == quiet_hours_end_hour:
            quiet_hours_start_hour = None
            quiet_hours_end_hour = None
        elif (quiet_hours_start_hour is None) != (quiet_hours_end_hour is None):
            quiet_hours_start_hour = default_quiet_hours_start_hour
            quiet_hours_end_hour = default_quiet_hours_end_hour

        return RuntimeConfig(
            post_interval_minutes=max(5, int(values["post_interval_minutes"])),
            search_window_hours=max(24, int(values["search_window_hours"])),
            quiet_hours_start_hour=quiet_hours_start_hour,
            quiet_hours_end_hour=quiet_hours_end_hour,
            admin_chat_id=values["admin_chat_id"],
            llm_provider=str(values["llm_provider"]),
            opencode_model=values["opencode_model"],
            ollama_model=values["ollama_model"],
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
    ) -> None:
        updates: list[tuple[str, str, str]] = []
        updated_at = datetime.now(timezone.utc).isoformat()

        if post_interval_minutes is not None:
            updates.append(("post_interval_minutes", str(max(5, post_interval_minutes)), updated_at))
        if search_window_hours is not None:
            updates.append(("search_window_hours", str(max(24, search_window_hours)), updated_at))
        if clear_quiet_hours:
            updates.append(("quiet_hours_start_hour", "", updated_at))
            updates.append(("quiet_hours_end_hour", "", updated_at))
        elif quiet_hours_start_hour is not None or quiet_hours_end_hour is not None:
            if quiet_hours_start_hour is None or quiet_hours_end_hour is None:
                raise ValueError("Both quiet hour values must be provided together.")

            start_hour = _normalize_hour(quiet_hours_start_hour)
            end_hour = _normalize_hour(quiet_hours_end_hour)
            if start_hour == end_hour:
                raise ValueError("Quiet hour start and end must differ.")

            updates.append(("quiet_hours_start_hour", str(start_hour), updated_at))
            updates.append(("quiet_hours_end_hour", str(end_hour), updated_at))
        if admin_chat_id is not None:
            updates.append(("admin_chat_id", admin_chat_id.strip(), updated_at))
        if llm_provider is not None:
            normalized_provider = llm_provider.strip().lower()
            if normalized_provider in VALID_LLM_PROVIDERS:
                updates.append(("llm_provider", normalized_provider, updated_at))
        if opencode_model is not None:
            updates.append(("opencode_model", opencode_model.strip(), updated_at))
        if ollama_model is not None:
            updates.append(("ollama_model", ollama_model.strip(), updated_at))

        if not updates:
            return

        with sqlite3.connect(self._database_path) as connection:
            connection.executemany(
                """
                INSERT INTO runtime_settings (name, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                updates,
            )
            connection.commit()
