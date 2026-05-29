from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

REDACTED = "[REDACTED]"

LOG_FILE_NAME = "telegram_news_bot.log"


class RedactingFormatter(logging.Formatter):
    def __init__(self, fmt: str, secrets: list[str] | None = None) -> None:
        super().__init__(fmt)
        self._secrets = [secret for secret in (secrets or []) if secret]

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        return _redact_text(rendered, self._secrets)


def configure_logging(logs_directory: Path, secrets: list[str] | None = None) -> Path:
    logs_directory.mkdir(parents=True, exist_ok=True)
    log_file = logs_directory / LOG_FILE_NAME
    _sanitize_existing_log(log_file, secrets or [])

    formatter = RedactingFormatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        secrets=secrets,
    )
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=1_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    return log_file


def _sanitize_existing_log(log_file: Path, secrets: list[str]) -> None:
    if not secrets or not log_file.exists():
        return

    content = log_file.read_text(encoding="utf-8", errors="replace")
    sanitized = _redact_text(content, secrets)
    if sanitized != content:
        log_file.write_text(sanitized, encoding="utf-8")


def _redact_text(value: str, secrets: list[str]) -> str:
    sanitized = value
    for secret in secrets:
        if secret:
            sanitized = sanitized.replace(secret, REDACTED)
    return sanitized


def tail_log(log_file: Path, max_lines: int = 25, max_chars: int = 3500) -> str:
    if not log_file.exists():
        return "Лог-файл ще не створений."

    content = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
    tail = "\n".join(content[-max_lines:]).strip()
    if not tail:
        return "Лог-файл поки порожній."

    if len(tail) <= max_chars:
        return tail

    return tail[-max_chars:]
