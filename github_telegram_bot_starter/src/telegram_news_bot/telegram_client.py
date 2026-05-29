from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from .models import CandidateItem


logger = logging.getLogger(__name__)
_IGNORABLE_CALLBACK_ERRORS = (
    "query is too old and response timeout expired",
    "query id is invalid",
)


class TelegramApiError(RuntimeError):
    pass


class TelegramPublisher:
    def __init__(self, bot_token: str) -> None:
        self._base_url = f"https://api.telegram.org/bot{bot_token}"

    def get_me(self) -> dict[str, Any]:
        return self._call("getMe")

    def get_updates(
        self,
        *,
        offset: int | None = None,
        timeout: int = 0,
        allowed_updates: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": timeout,
        }
        if offset is not None:
            payload["offset"] = offset
        if allowed_updates is not None:
            payload["allowed_updates"] = allowed_updates

        return self._call("getUpdates", payload=payload)

    def discover_channels(self) -> list[dict[str, Any]]:
        updates = self._call(
            "getUpdates",
            payload={
                "allowed_updates": [
                    "channel_post",
                    "edited_channel_post",
                    "my_chat_member",
                    "chat_member",
                ]
            },
        )

        channels: dict[str, dict[str, Any]] = {}
        for update in updates:
            for key in (
                "channel_post",
                "edited_channel_post",
                "my_chat_member",
                "chat_member",
            ):
                event = update.get(key)
                if not isinstance(event, dict):
                    continue

                chat = event.get("chat")
                if not isinstance(chat, dict):
                    continue
                if chat.get("type") != "channel":
                    continue

                channel_id = str(chat.get("id"))
                channels[channel_id] = {
                    "chat_id": channel_id,
                    "title": chat.get("title"),
                    "username": chat.get("username"),
                }

        return list(channels.values())

    def discover_private_chats(self) -> list[dict[str, Any]]:
        updates = self._call(
            "getUpdates",
            payload={
                "allowed_updates": [
                    "message",
                    "callback_query",
                ]
            },
        )

        private_chats: dict[str, dict[str, Any]] = {}
        for update in updates:
            message = update.get("message")
            if isinstance(message, dict):
                self._collect_private_chat(private_chats, message)

            callback_query = update.get("callback_query")
            if not isinstance(callback_query, dict):
                continue

            sender = callback_query.get("from")
            if not isinstance(sender, dict):
                continue

            sender_id = str(sender.get("id") or "")
            if not sender_id:
                continue

            private_chats[sender_id] = {
                "chat_id": sender_id,
                "user_id": sender_id,
                "username": sender.get("username"),
                "first_name": sender.get("first_name"),
                "last_name": sender.get("last_name"),
            }

        return list(private_chats.values())

    def _collect_private_chat(
        self,
        private_chats: dict[str, dict[str, Any]],
        message: dict[str, Any],
    ) -> None:
        chat = message.get("chat")
        if not isinstance(chat, dict):
            return
        if chat.get("type") != "private":
            return

        sender = message.get("from") if isinstance(message.get("from"), dict) else {}
        chat_id = str(chat.get("id") or "")
        if not chat_id:
            return

        private_chats[chat_id] = {
            "chat_id": chat_id,
            "user_id": str(sender.get("id") or chat_id),
            "username": chat.get("username") or sender.get("username"),
            "first_name": chat.get("first_name") or sender.get("first_name"),
            "last_name": chat.get("last_name") or sender.get("last_name"),
        }

    def publish(self, chat_id: str, item: CandidateItem, caption: str) -> int:
        result: dict[str, Any]
        media_url = item.media_url or item.image_url
        reply_markup = self._resource_keyboard(item)

        if media_url and item.media_kind == "video":
            try:
                result = self._call(
                    "sendVideo",
                    payload={
                        "chat_id": chat_id,
                        "video": media_url,
                        "caption": caption,
                        "parse_mode": "HTML",
                        "reply_markup": reply_markup,
                    },
                )
            except TelegramApiError as exc:
                logger.warning(
                    "Telegram rejected video media for %s: %s. Falling back to text post.",
                    item.source_key,
                    exc,
                )
                result = self.send_message(
                    chat_id,
                    caption,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                    disable_web_page_preview=False,
                )
        elif media_url:
            try:
                result = self._call(
                    "sendPhoto",
                    payload={
                        "chat_id": chat_id,
                        "photo": media_url,
                        "caption": caption,
                        "parse_mode": "HTML",
                        "reply_markup": reply_markup,
                    },
                )
            except TelegramApiError as exc:
                logger.warning(
                    "Telegram rejected photo media for %s: %s. Falling back to text post.",
                    item.source_key,
                    exc,
                )
                result = self.send_message(
                    chat_id,
                    caption,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                    disable_web_page_preview=False,
                )
        else:
            result = self.send_message(
                chat_id,
                caption,
                parse_mode="HTML",
                reply_markup=reply_markup,
                disable_web_page_preview=False,
            )

        return int(result["message_id"])

    def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: dict[str, Any] | None = None,
        disable_web_page_preview: bool = True,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": disable_web_page_preview,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup:
            payload["reply_markup"] = reply_markup

        return self._call("sendMessage", payload=payload)

    def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> None:
        payload: dict[str, Any] = {
            "callback_query_id": callback_query_id,
        }
        if text:
            payload["text"] = text

        try:
            self._call("answerCallbackQuery", payload=payload)
        except TelegramApiError as exc:
            normalized_error = str(exc).lower()
            if any(fragment in normalized_error for fragment in _IGNORABLE_CALLBACK_ERRORS):
                logger.info(
                    "Ignoring expired Telegram callback query %s: %s",
                    callback_query_id,
                    exc,
                )
                return
            raise

    def _resource_keyboard(self, item: CandidateItem) -> dict[str, Any] | None:
        row = [{"text": "Джерело", "url": item.url}]
        if item.developer_url:
            row.append({"text": "Сайт розробника", "url": item.developer_url})

        return {"inline_keyboard": [row]}

    def _call(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(f"{self._base_url}/{method}", json=payload or {})

        try:
            body = response.json()
        except json.JSONDecodeError:
            body = None

        if response.is_error:
            if isinstance(body, dict):
                description = body.get("description") or f"Telegram API HTTP {response.status_code}."
                raise TelegramApiError(str(description))
            response.raise_for_status()

        if not isinstance(body, dict):
            raise TelegramApiError("Telegram API returned a non-JSON response.")

        if not body.get("ok"):
            description = body.get("description", "Telegram API request failed.")
            raise TelegramApiError(description)

        return body.get("result")
