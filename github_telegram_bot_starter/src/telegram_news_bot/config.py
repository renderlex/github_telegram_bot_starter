from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


VALID_LLM_PROVIDERS = {"auto", "opencode", "ollama", "fallback"}


@dataclass(slots=True)
class Settings:
    telegram_bot_token: str
    telegram_channel_id: str | None
    telegram_admin_chat_id: str | None
    telegram_admin_pair_code: str | None
    source_api_token: str | None
    source_feed_urls: tuple[str, ...]
    llm_provider: str
    opencode_model: str | None
    ollama_base_url: str
    ollama_model: str
    database_path: Path
    logs_directory: Path
    post_interval_minutes: int
    search_window_hours: int
    quiet_hours_start_hour: int | None
    quiet_hours_end_hour: int | None
    max_posts_per_run: int


def _read_int(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default)).strip()
    try:
        return int(raw_value)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be an integer.") from exc


def _read_llm_provider() -> str:
    provider = os.getenv("LLM_PROVIDER", "auto").strip().lower()
    if provider not in VALID_LLM_PROVIDERS:
        allowed_values = ", ".join(sorted(VALID_LLM_PROVIDERS))
        raise ValueError(
            f"Environment variable LLM_PROVIDER must be one of: {allowed_values}."
        )

    return provider


def _read_optional_hour(name: str) -> int | None:
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return None

    try:
        hour = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be an integer hour between 0 and 23.") from exc

    if hour < 0 or hour > 23:
        raise ValueError(f"Environment variable {name} must be between 0 and 23.")

    return hour


def _read_csv_env(name: str) -> tuple[str, ...]:
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return ()

    normalized = raw_value.replace("\r", "\n").replace(",", "\n")
    return tuple(
        dict.fromkeys(
            part.strip()
            for part in normalized.split("\n")
            if part.strip()
        )
    )


def _read_quiet_hours() -> tuple[int | None, int | None]:
    quiet_hours_start_hour = _read_optional_hour("QUIET_HOURS_START_HOUR")
    quiet_hours_end_hour = _read_optional_hour("QUIET_HOURS_END_HOUR")

    if (quiet_hours_start_hour is None) != (quiet_hours_end_hour is None):
        raise ValueError(
            "QUIET_HOURS_START_HOUR and QUIET_HOURS_END_HOUR must both be set or both be empty."
        )

    if (
        quiet_hours_start_hour is not None
        and quiet_hours_end_hour is not None
        and quiet_hours_start_hour == quiet_hours_end_hour
    ):
        raise ValueError(
            "QUIET_HOURS_START_HOUR and QUIET_HOURS_END_HOUR must differ. Use empty values to disable quiet hours."
        )

    return quiet_hours_start_hour, quiet_hours_end_hour


def _load_local_dotenv() -> None:
    configured_path = os.getenv("TELEGRAM_NEWS_BOT_DOTENV", "").strip()
    candidate_paths: list[Path] = []

    if configured_path:
        candidate_paths.append(Path(configured_path).expanduser())

    candidate_paths.extend(
        [
            Path.cwd() / ".env",
            Path(__file__).resolve().parents[2] / ".env",
        ]
    )

    seen_paths: set[Path] = set()
    for candidate in candidate_paths:
        normalized = candidate.resolve(strict=False)
        if normalized in seen_paths:
            continue
        seen_paths.add(normalized)

        if normalized.is_file():
            load_dotenv(dotenv_path=normalized)
            return

    load_dotenv()


def load_settings() -> Settings:
    _load_local_dotenv()

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not bot_token:
        raise ValueError("TELEGRAM_BOT_TOKEN is required.")

    database_path = Path(os.getenv("DATABASE_PATH", "data/bot.sqlite3")).expanduser()
    logs_directory = Path(os.getenv("LOGS_DIRECTORY", "logs")).expanduser()
    quiet_hours_start_hour, quiet_hours_end_hour = _read_quiet_hours()

    return Settings(
        telegram_bot_token=bot_token,
        telegram_channel_id=os.getenv("TELEGRAM_CHANNEL_ID", "").strip() or None,
        telegram_admin_chat_id=os.getenv("TELEGRAM_ADMIN_CHAT_ID", "").strip() or None,
        telegram_admin_pair_code=os.getenv("TELEGRAM_ADMIN_PAIR_CODE", "").strip() or None,
        source_api_token=os.getenv("SOURCE_API_TOKEN", "").strip() or None,
        source_feed_urls=_read_csv_env("SOURCE_FEED_URLS"),
        llm_provider=_read_llm_provider(),
        opencode_model=os.getenv("OPENCODE_MODEL", "").strip() or None,
        ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").strip(),
        ollama_model=os.getenv("OLLAMA_MODEL", "qwen2.5:7b-instruct").strip(),
        database_path=database_path,
        logs_directory=logs_directory,
        post_interval_minutes=_read_int("POST_INTERVAL_MINUTES", 60),
        search_window_hours=_read_int("SEARCH_WINDOW_HOURS", 72),
        quiet_hours_start_hour=quiet_hours_start_hour,
        quiet_hours_end_hour=quiet_hours_end_hour,
        max_posts_per_run=_read_int("MAX_POSTS_PER_RUN", 2),
    )
