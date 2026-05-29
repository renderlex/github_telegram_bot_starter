from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any

from .config import Settings
from .llm import ModelCatalog, RuntimeModelState
from .logging_utils import tail_log
from .models import RuntimeConfig
from .pipeline import NewsTelegramService
from .telegram_client import TelegramPublisher

logger = logging.getLogger(__name__)
_INTERVAL_OPTIONS = (30, 60, 180)
_PERIOD_OPTIONS = (24, 72, 168)
_QUIET_HOUR_PRESETS = ((22, 8), (23, 8), (0, 8))
_MODEL_PAGE_SIZE = 6
_PROVIDER_LABELS = {
    "auto": "Авто",
    "opencode": "OpenCode",
    "ollama": "Ollama",
    "fallback": "Шаблон",
}


class AutomationDaemon:
    def __init__(
        self,
        settings: Settings,
        service: NewsTelegramService,
        log_file: Path,
    ) -> None:
        self._settings = settings
        self._service = service
        self._publisher = TelegramPublisher(settings.telegram_bot_token)
        self._log_file = log_file
        self._next_run_at: datetime | None = None
        self._update_offset: int | None = None
        self._control_chat_id = service.load_runtime_config().admin_chat_id

    def run(self) -> None:
        runtime_config = self._service.load_runtime_config()
        self._next_run_at = datetime.now(timezone.utc)
        logger.info(
            "Automation loop started with interval=%s minutes and search window=%s hours.",
            runtime_config.post_interval_minutes,
            runtime_config.search_window_hours,
        )

        while True:
            now = datetime.now(timezone.utc)
            runtime_config = self._service.load_runtime_config()
            timeout = self._poll_timeout_seconds(now, self._next_run_at or now)

            try:
                updates = self._publisher.get_updates(
                    offset=self._update_offset,
                    timeout=timeout,
                    allowed_updates=["message", "callback_query"],
                )
            except Exception as exc:
                logger.exception("Failed to poll Telegram updates: %s", exc)
                updates = []

            run_requested = False
            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    self._update_offset = update_id + 1

                if self._handle_update(update):
                    run_requested = True

            now = datetime.now(timezone.utc)
            if run_requested:
                self._run_publish_cycle(runtime_config, requested_by_admin=run_requested)
                runtime_config = self._service.load_runtime_config()
                self._next_run_at = self._schedule_next_run(runtime_config)
                continue

            if now >= (self._next_run_at or now):
                pause_until = self._service.get_quiet_hours_pause_until(runtime_config, now=now)
                if pause_until is not None:
                    if self._next_run_at != pause_until:
                        logger.info(
                            "Automatic publication is paused by quiet hours until %s.",
                            self._format_local_datetime(pause_until),
                        )
                    self._next_run_at = pause_until
                    continue

                self._run_publish_cycle(runtime_config, requested_by_admin=False)
                runtime_config = self._service.load_runtime_config()
                self._next_run_at = self._schedule_next_run(runtime_config)

    def _poll_timeout_seconds(self, now: datetime, next_run_at: datetime) -> int:
        seconds_until_run = max(int((next_run_at - now).total_seconds()), 0)
        return min(seconds_until_run, 20)

    def _handle_update(self, update: dict[str, Any]) -> bool:
        message = update.get("message")
        if isinstance(message, dict):
            return self._handle_message(message)

        callback_query = update.get("callback_query")
        if isinstance(callback_query, dict):
            return self._handle_callback_query(callback_query)

        return False

    def _handle_message(self, message: dict[str, Any]) -> bool:
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        chat_id = str(chat.get("id") or "")
        text = (message.get("text") or "").strip()
        if not text:
            return False

        if self._try_pair(chat, sender, text):
            self._send_status(chat_id)
            return False

        if not self._is_authorized(chat, str(sender.get("id") or "")):
            logger.warning("Rejected control message from chat_id=%s", chat_id)
            return False

        self._remember_control_chat(chat_id)

        if text in {"/start", "/menu"}:
            self._send_status(chat_id)
            return False

        if text == "/status":
            self._send_status(chat_id)
            return False

        if text == "/logs":
            self._send_logs(chat_id)
            return False

        if text == "/llm":
            self._send_llm_control(chat_id)
            return False

        if text == "/quiet":
            self._send_quiet_hours_control(chat_id)
            return False

        if text.lower().startswith("/quiet "):
            self._apply_quiet_hours_command(chat_id, text)
            return False

        if text == "/run":
            self._safe_send_text(chat_id, "Запускаю позаплановий пошук і публікацію.")
            return True

        self._safe_send_text(
            chat_id,
            "Доступні команди: /menu, /status, /logs, /llm, /quiet, /run",
            reply_markup=self._control_keyboard(),
            parse_mode="HTML",
        )
        return False

    def _handle_callback_query(self, callback_query: dict[str, Any]) -> bool:
        callback_id = str(callback_query.get("id") or "")
        message = callback_query.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id") or "")
        data = (callback_query.get("data") or "").strip()
        sender = callback_query.get("from") or {}

        if not self._is_authorized(chat, str(sender.get("id") or "")):
            logger.warning("Rejected callback query from chat_id=%s", chat_id)
            self._publisher.answer_callback_query(callback_id, "Доступ закритий.")
            return False

        self._remember_control_chat(chat_id)

        if data.startswith("interval:"):
            minutes = self._parse_int(data.partition(":")[2])
            if minutes is None:
                self._publisher.answer_callback_query(callback_id, "Некоректний інтервал.")
                return False
            runtime_config = self._service.update_runtime_config(post_interval_minutes=minutes)
            self._next_run_at = datetime.now(timezone.utc) + timedelta(
                minutes=runtime_config.post_interval_minutes
            )
            self._publisher.answer_callback_query(callback_id, "Інтервал оновлено.")
            self._send_status(chat_id)
            return False

        if data.startswith("period:"):
            hours = self._parse_int(data.partition(":")[2])
            if hours is None:
                self._publisher.answer_callback_query(callback_id, "Некоректний період.")
                return False
            self._service.update_runtime_config(search_window_hours=hours)
            self._publisher.answer_callback_query(callback_id, "Період пошуку оновлено.")
            self._send_status(chat_id)
            return False

        if data == "action:status":
            self._publisher.answer_callback_query(callback_id, "Оновлюю статус.")
            self._send_status(chat_id)
            return False

        if data == "action:logs":
            self._publisher.answer_callback_query(callback_id, "Надсилаю хвіст логів.")
            self._send_logs(chat_id)
            return False

        if data == "action:llm":
            self._publisher.answer_callback_query(callback_id, "Відкриваю LLM-меню.")
            self._send_llm_control(chat_id)
            return False

        if data == "action:quiet":
            self._publisher.answer_callback_query(callback_id, "Відкриваю тихі години.")
            self._send_quiet_hours_control(chat_id)
            return False

        if data.startswith("quiet:set:"):
            payload = data.removeprefix("quiet:set:")
            if payload == "off":
                runtime_config = self._service.update_runtime_config(clear_quiet_hours=True)
                self._next_run_at = datetime.now(timezone.utc)
                self._publisher.answer_callback_query(callback_id, "Тихі години вимкнено.")
                self._send_quiet_hours_control(chat_id, runtime_config=runtime_config)
                return False

            start_raw, _, end_raw = payload.partition(":")
            start_hour = self._parse_hour(start_raw)
            end_hour = self._parse_hour(end_raw)
            if start_hour is None or end_hour is None or start_hour == end_hour:
                self._publisher.answer_callback_query(callback_id, "Некоректний діапазон тихих годин.")
                return False

            runtime_config = self._service.update_runtime_config(
                quiet_hours_start_hour=start_hour,
                quiet_hours_end_hour=end_hour,
            )
            self._sync_next_run_after_quiet_hours_change(runtime_config)
            self._publisher.answer_callback_query(callback_id, "Тихі години оновлено.")
            self._send_quiet_hours_control(chat_id, runtime_config=runtime_config)
            return False

        if data.startswith("llm:provider:"):
            provider = data.removeprefix("llm:provider:")
            if provider not in _PROVIDER_LABELS:
                self._publisher.answer_callback_query(callback_id, "Некоректний провайдер.")
                return False

            self._publisher.answer_callback_query(callback_id, "Оновлюю LLM-режим.")
            runtime_config = self._service.load_runtime_config()
            catalog, state = self._service.get_llm_runtime_overview(runtime_config)
            if provider in {"opencode", "ollama"} and provider not in catalog.providers:
                self._safe_send_text(
                    chat_id,
                    "Запитаний провайдер зараз недоступний на цьому ПК.",
                )
                self._send_llm_control(chat_id, runtime_config=runtime_config)
                return False

            update_kwargs: dict[str, str] = {"llm_provider": provider}
            if provider == "opencode" and state.effective_opencode_model:
                update_kwargs["opencode_model"] = state.effective_opencode_model
            if provider == "ollama" and state.effective_ollama_model:
                update_kwargs["ollama_model"] = state.effective_ollama_model

            runtime_config = self._service.update_runtime_config(**update_kwargs)
            self._send_llm_control(chat_id, runtime_config=runtime_config)
            return False

        if data.startswith("llm:list:"):
            payload = data.removeprefix("llm:list:")
            backend, _, raw_page = payload.partition(":")
            page = self._parse_int(raw_page)
            if backend not in {"opencode", "ollama"} or page is None:
                self._publisher.answer_callback_query(callback_id, "Некоректний список моделей.")
                return False

            self._publisher.answer_callback_query(callback_id, "Показую моделі.")
            self._send_llm_model_list(chat_id, backend, page)
            return False

        if data.startswith("llm:model:"):
            payload = data.removeprefix("llm:model:")
            backend, _, token = payload.partition(":")
            if backend not in {"opencode", "ollama"} or not token:
                self._publisher.answer_callback_query(callback_id, "Некоректна модель.")
                return False

            self._publisher.answer_callback_query(callback_id, "Перемикаю модель.")
            runtime_config = self._service.load_runtime_config()
            catalog, _ = self._service.get_llm_runtime_overview(runtime_config)
            model_name = self._resolve_llm_model_token(backend, token, catalog)
            if not model_name:
                self._safe_send_text(
                    chat_id,
                    "Список моделей змінився. Відкрий меню ще раз.",
                )
                self._send_llm_control(chat_id, runtime_config=runtime_config)
                return False

            if backend == "opencode":
                runtime_config = self._service.update_runtime_config(
                    llm_provider="opencode",
                    opencode_model=model_name,
                )
            else:
                runtime_config = self._service.update_runtime_config(
                    llm_provider="ollama",
                    ollama_model=model_name,
                )

            self._send_llm_control(chat_id, runtime_config=runtime_config)
            return False

        if data == "action:run":
            self._publisher.answer_callback_query(callback_id, "Запускаю позаплановий прогін.")
            self._safe_send_text(chat_id, "Запускаю позаплановий пошук і публікацію.")
            return True

        self._publisher.answer_callback_query(callback_id, "Команду не впізнано.")
        return False

    def _run_publish_cycle(
        self,
        runtime_config: RuntimeConfig,
        *,
        requested_by_admin: bool,
    ) -> None:
        logger.info(
            "Starting publish cycle with interval=%s minutes and search window=%s hours.",
            runtime_config.post_interval_minutes,
            runtime_config.search_window_hours,
        )
        try:
            results = self._service.run_once(runtime_config=runtime_config)
        except Exception as exc:
            logger.exception("Publish cycle failed: %s", exc)
            if requested_by_admin:
                self._notify_control_chat("Прогін завершився з помилкою. Подивись /logs.")
            return

        if not results:
            logger.info("No new validated repository candidates were selected.")
            if requested_by_admin:
                self._notify_control_chat("Нових валідних репозиторіїв для публікації не знайдено.")
            return

        summary = "\n".join(
            f"- {result.item.title} (message_id={result.message_id})"
            for result in results
        )
        logger.info("Published %s item(s):\n%s", len(results), summary)
        if requested_by_admin:
            self._notify_control_chat(
                f"Опубліковано {len(results)} пост(и):\n{summary}"
            )

    def _send_status(self, chat_id: str) -> None:
        runtime_config = self._service.load_runtime_config()
        _, llm_state = self._service.get_llm_runtime_overview(runtime_config)
        pause_until = self._service.get_quiet_hours_pause_until(runtime_config)
        next_run = (
            self._next_run_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            if self._next_run_at is not None
            else "негайно"
        )
        lines = [
            "<b>Керування Telegram News Bot</b>",
            "",
            f"<b>Інтервал пошуку:</b> {runtime_config.post_interval_minutes} хв",
            f"<b>Період пошуку:</b> {runtime_config.search_window_hours} год",
            f"<b>Тихі години:</b> {escape(self._quiet_hours_label(runtime_config))}",
            f"<b>LLM режим:</b> {escape(self._provider_title(llm_state.requested_provider))}",
            f"<b>Активний рушій:</b> {escape(self._provider_title(llm_state.effective_provider))}",
            f"<b>Активна модель:</b> {escape(self._current_llm_model(llm_state) or 'автовибір')}",
            f"<b>Наступний запуск:</b> {escape(next_run)}",
            f"<b>Лог-файл:</b> {escape(self._log_file.name)}",
        ]
        if pause_until is not None:
            lines.insert(
                5,
                f"<b>Автопублікація:</b> пауза до {escape(self._format_local_datetime(pause_until))}",
            )
        text = "\n".join(lines)
        self._safe_send_text(
            chat_id,
            text,
            reply_markup=self._control_keyboard(),
            parse_mode="HTML",
        )

    def _send_logs(self, chat_id: str) -> None:
        payload = tail_log(self._log_file)
        self._safe_send_text(chat_id, payload, parse_mode=None)

    def _control_keyboard(self) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {"text": "Статус", "callback_data": "action:status"},
                    {"text": "Запустити зараз", "callback_data": "action:run"},
                ],
                [
                    {"text": "Модель ШІ", "callback_data": "action:llm"},
                    {"text": "Тихі години", "callback_data": "action:quiet"},
                ],
                [
                    *[
                        {"text": f"Інтервал {minutes} хв", "callback_data": f"interval:{minutes}"}
                        for minutes in _INTERVAL_OPTIONS
                    ]
                ],
                [
                    *[
                        {"text": f"Період {hours} год", "callback_data": f"period:{hours}"}
                        for hours in _PERIOD_OPTIONS
                    ]
                ],
                [{"text": "Логи", "callback_data": "action:logs"}],
            ]
        }

    def _send_quiet_hours_control(
        self,
        chat_id: str,
        *,
        runtime_config: RuntimeConfig | None = None,
    ) -> None:
        active_config = runtime_config or self._service.load_runtime_config()
        pause_until = self._service.get_quiet_hours_pause_until(active_config)

        text_lines = [
            "<b>Тихі години</b>",
            "",
            f"<b>Поточний режим:</b> {escape(self._quiet_hours_label(active_config))}",
            "",
            "Тихі години застосовуються до автоматичних відправок і працюють за локальним часом цього ПК.",
            "Щоб задати довільний проміжок, надішли команду у форматі /quiet 22 8.",
            "Щоб вимкнути обмеження, надішли /quiet off.",
        ]
        if pause_until is not None:
            text_lines.extend(
                [
                    "",
                    f"<b>Зараз автопублікація на паузі до:</b> {escape(self._format_local_datetime(pause_until))}",
                ]
            )

        self._safe_send_text(
            chat_id,
            "\n".join(text_lines),
            reply_markup=self._quiet_hours_keyboard(),
            parse_mode="HTML",
        )

    def _send_llm_control(
        self,
        chat_id: str,
        *,
        runtime_config: RuntimeConfig | None = None,
    ) -> None:
        active_config = runtime_config or self._service.load_runtime_config()
        catalog, state = self._service.get_llm_runtime_overview(active_config)

        text_lines = [
            "<b>Керування LLM</b>",
            "",
            f"<b>Вибраний режим:</b> {escape(self._provider_title(state.requested_provider))}",
            f"<b>Фактично працює:</b> {escape(self._provider_title(state.effective_provider))}",
            f"<b>Поточна модель:</b> {escape(self._current_llm_model(state) or 'автовибір')}",
            f"<b>OpenCode:</b> {len(catalog.opencode_models)} моделей на цьому ПК",
            f"<b>Ollama:</b> {len(catalog.ollama_models)} моделей на цьому ПК",
            "",
            "Вибір конкретної моделі автоматично перемикає бот на відповідний рушій.",
        ]

        if state.requested_provider != state.effective_provider:
            text_lines.extend(
                [
                    "",
                    "<b>Примітка:</b> запитаний режим зараз недоступний на цьому ПК, тому бот безпечно переключається на доступний варіант або шаблон.",
                ]
            )

        self._safe_send_text(
            chat_id,
            "\n".join(text_lines),
            reply_markup=self._llm_keyboard(catalog, state),
            parse_mode="HTML",
        )

    def _send_llm_model_list(self, chat_id: str, backend: str, page: int) -> None:
        runtime_config = self._service.load_runtime_config()
        catalog, state = self._service.get_llm_runtime_overview(runtime_config)
        models = catalog.opencode_models if backend == "opencode" else catalog.ollama_models

        if not models:
            self._safe_send_text(
                chat_id,
                f"Для {self._provider_title(backend)} зараз немає доступних моделей на цьому ПК.",
                reply_markup={"inline_keyboard": [[{"text": "Назад до LLM", "callback_data": "action:llm"}]]},
            )
            return

        max_page = max((len(models) - 1) // _MODEL_PAGE_SIZE, 0)
        safe_page = min(max(page, 0), max_page)
        start = safe_page * _MODEL_PAGE_SIZE
        visible_models = models[start : start + _MODEL_PAGE_SIZE]
        selected_model = (
            state.effective_opencode_model if backend == "opencode" else state.effective_ollama_model
        )

        rows: list[list[dict[str, str]]] = []
        for index in range(0, len(visible_models), 2):
            row: list[dict[str, str]] = []
            for model_name in visible_models[index : index + 2]:
                row.append(
                    {
                        "text": self._model_button_text(model_name, selected=model_name == selected_model),
                        "callback_data": f"llm:model:{backend}:{self._llm_model_token(backend, model_name)}",
                    }
                )
            rows.append(row)

        navigation: list[dict[str, str]] = []
        if safe_page > 0:
            navigation.append(
                {"text": "< Назад", "callback_data": f"llm:list:{backend}:{safe_page - 1}"}
            )
        if safe_page < max_page:
            navigation.append(
                {"text": "Далі >", "callback_data": f"llm:list:{backend}:{safe_page + 1}"}
            )
        if navigation:
            rows.append(navigation)

        rows.append([{"text": "Назад до LLM", "callback_data": "action:llm"}])

        self._safe_send_text(
            chat_id,
            "\n".join(
                [
                    f"<b>{escape(self._provider_title(backend))}: доступні моделі</b>",
                    "",
                    f"Сторінка {safe_page + 1} з {max_page + 1}",
                    f"Поточний вибір: {escape(selected_model or 'автовибір')}",
                ]
            ),
            reply_markup={"inline_keyboard": rows},
            parse_mode="HTML",
        )

    def _llm_keyboard(
        self,
        catalog: ModelCatalog,
        state: RuntimeModelState,
    ) -> dict[str, Any]:
        provider_buttons = [
            {
                "text": self._provider_button_text(provider, active=provider == state.requested_provider),
                "callback_data": f"llm:provider:{provider}",
            }
            for provider in catalog.providers
        ]

        rows: list[list[dict[str, str]]] = [provider_buttons]
        model_rows: list[dict[str, str]] = []
        if catalog.opencode_models:
            model_rows.append({"text": "Моделі OpenCode", "callback_data": "llm:list:opencode:0"})
        if catalog.ollama_models:
            model_rows.append({"text": "Моделі Ollama", "callback_data": "llm:list:ollama:0"})
        if model_rows:
            rows.append(model_rows)
        rows.append([{"text": "Оновити статус", "callback_data": "action:status"}])

        return {"inline_keyboard": rows}

    def _provider_title(self, provider: str) -> str:
        return _PROVIDER_LABELS.get(provider, provider)

    def _quiet_hours_keyboard(self) -> dict[str, Any]:
        rows: list[list[dict[str, str]]] = [
            [
                {
                    "text": self._quiet_hours_preset_label(start_hour, end_hour),
                    "callback_data": f"quiet:set:{start_hour}:{end_hour}",
                }
                for start_hour, end_hour in _QUIET_HOUR_PRESETS[:2]
            ],
            [
                {
                    "text": self._quiet_hours_preset_label(*_QUIET_HOUR_PRESETS[2]),
                    "callback_data": f"quiet:set:{_QUIET_HOUR_PRESETS[2][0]}:{_QUIET_HOUR_PRESETS[2][1]}",
                },
                {"text": "Вимкнути", "callback_data": "quiet:set:off"},
            ],
            [{"text": "Оновити статус", "callback_data": "action:status"}],
        ]
        return {"inline_keyboard": rows}

    def _quiet_hours_label(self, runtime_config: RuntimeConfig) -> str:
        start_hour = runtime_config.quiet_hours_start_hour
        end_hour = runtime_config.quiet_hours_end_hour
        if start_hour is None or end_hour is None or start_hour == end_hour:
            return "вимкнено"
        return f"{self._format_hour(start_hour)}-{self._format_hour(end_hour)} (локальний час ПК)"

    def _quiet_hours_preset_label(self, start_hour: int, end_hour: int) -> str:
        return f"{self._format_hour(start_hour)}-{self._format_hour(end_hour)}"

    def _format_hour(self, hour: int) -> str:
        return f"{hour:02d}:00"

    def _format_local_datetime(self, timestamp: datetime) -> str:
        return timestamp.astimezone().strftime("%Y-%m-%d %H:%M локального часу")

    def _schedule_next_run(
        self,
        runtime_config: RuntimeConfig,
        *,
        now: datetime | None = None,
    ) -> datetime:
        base_time = now or datetime.now(timezone.utc)
        return base_time + timedelta(minutes=runtime_config.post_interval_minutes)

    def _sync_next_run_after_quiet_hours_change(self, runtime_config: RuntimeConfig) -> None:
        pause_until = self._service.get_quiet_hours_pause_until(runtime_config)
        if pause_until is not None:
            self._next_run_at = pause_until

    def _apply_quiet_hours_command(self, chat_id: str, text: str) -> None:
        parts = text.split()
        if len(parts) == 2 and parts[1].lower() in {"off", "disable", "none", "вимк", "вимкнути"}:
            self._service.update_runtime_config(clear_quiet_hours=True)
            self._next_run_at = datetime.now(timezone.utc)
            self._safe_send_text(chat_id, "Тихі години вимкнено.")
            self._send_status(chat_id)
            return

        if len(parts) == 3:
            start_hour = self._parse_hour(parts[1])
            end_hour = self._parse_hour(parts[2])
            if start_hour is not None and end_hour is not None and start_hour != end_hour:
                runtime_config = self._service.update_runtime_config(
                    quiet_hours_start_hour=start_hour,
                    quiet_hours_end_hour=end_hour,
                )
                self._sync_next_run_after_quiet_hours_change(runtime_config)
                self._safe_send_text(
                    chat_id,
                    f"Тихі години оновлено: {self._quiet_hours_label(runtime_config)}.",
                )
                self._send_status(chat_id)
                return

        self._safe_send_text(
            chat_id,
            "Формат команди: /quiet 22 8 або /quiet off",
        )

    def _provider_button_text(self, provider: str, *, active: bool) -> str:
        title = self._provider_title(provider)
        return f"[{title}]" if active else title

    def _current_llm_model(self, state: RuntimeModelState) -> str | None:
        if state.effective_provider == "opencode":
            return state.effective_opencode_model
        if state.effective_provider == "ollama":
            return state.effective_ollama_model
        if state.requested_provider == "opencode":
            return state.requested_opencode_model
        if state.requested_provider == "ollama":
            return state.requested_ollama_model
        return None

    def _model_button_text(self, model_name: str, *, selected: bool) -> str:
        short_name = self._shorten_label(model_name, 26)
        return f"> {short_name}" if selected else short_name

    def _shorten_label(self, value: str, limit: int) -> str:
        normalized = value.strip()
        if len(normalized) <= limit:
            return normalized
        return f"{normalized[: limit - 3]}..."

    def _llm_model_token(self, backend: str, model_name: str) -> str:
        return hashlib.sha1(f"{backend}:{model_name}".encode("utf-8")).hexdigest()[:12]

    def _resolve_llm_model_token(
        self,
        backend: str,
        token: str,
        catalog: ModelCatalog,
    ) -> str | None:
        models = catalog.opencode_models if backend == "opencode" else catalog.ollama_models
        for model_name in models:
            if self._llm_model_token(backend, model_name) == token:
                return model_name
        return None

    def _notify_control_chat(self, text: str) -> None:
        if not self._control_chat_id:
            return
        self._safe_send_text(self._control_chat_id, text, parse_mode=None)

    def _remember_control_chat(self, chat_id: str) -> None:
        if chat_id and self._control_chat_id:
            self._control_chat_id = chat_id

    def _is_authorized(self, chat: dict[str, Any], sender_id: str) -> bool:
        if chat.get("type") != "private":
            return False

        admin_chat_id = self._control_chat_id
        if not admin_chat_id:
            return False

        chat_id = str(chat.get("id") or "")
        return chat_id == admin_chat_id and sender_id == admin_chat_id

    def _try_pair(self, chat: dict[str, Any], sender: dict[str, Any], text: str) -> bool:
        if self._control_chat_id:
            return False
        if chat.get("type") != "private":
            return False

        pair_code = self._settings.telegram_admin_pair_code
        if not pair_code:
            return False

        normalized = text.strip()
        if normalized != f"/pair {pair_code}":
            return False

        chat_id = str(chat.get("id") or "")
        sender_id = str(sender.get("id") or "")
        if not chat_id or chat_id != sender_id:
            logger.warning("Rejected pairing attempt because chat_id and sender_id did not match.")
            return False

        self._control_chat_id = chat_id
        self._service.update_runtime_config(admin_chat_id=chat_id)
        logger.info("Bound Telegram admin access to chat_id=%s", chat_id)
        self._safe_send_text(
            chat_id,
            "Доступ до керування прив'язано до цього приватного чату.",
        )
        return True

    def _safe_send_text(
        self,
        chat_id: str,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str | None = None,
    ) -> None:
        if not chat_id:
            return

        try:
            self._publisher.send_message(
                chat_id,
                text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
        except Exception as exc:
            logger.exception("Failed to send control message: %s", exc)

    def _parse_int(self, value: str) -> int | None:
        try:
            return int(value)
        except ValueError:
            return None

    def _parse_hour(self, value: str) -> int | None:
        hour = self._parse_int(value)
        if hour is None or hour < 0 or hour > 23:
            return None
        return hour
