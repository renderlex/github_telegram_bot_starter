from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from html import escape
from time import monotonic

import httpx

from .models import CandidateItem
from .text_utils import shorten

MAX_CAPTION_LENGTH = 1024
MAX_BODY_LENGTH = 420
OPENCODE_TIMEOUT_SECONDS = 120
OPENCODE_MODEL_DISCOVERY_TIMEOUT_SECONDS = 20
OLLAMA_MODEL_DISCOVERY_TIMEOUT_SECONDS = 5
MODEL_CATALOG_CACHE_TTL_SECONDS = 60.0
VALID_RUNTIME_PROVIDERS = {"auto", "opencode", "ollama", "fallback"}


@dataclass(slots=True, frozen=True)
class ModelCatalog:
    providers: tuple[str, ...]
    opencode_models: tuple[str, ...]
    ollama_models: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class RuntimeModelState:
    requested_provider: str
    effective_provider: str
    requested_opencode_model: str | None
    effective_opencode_model: str | None
    requested_ollama_model: str | None
    effective_ollama_model: str | None


class PostWriter:
    def __init__(
        self,
        provider: str,
        ollama_base_url: str,
        ollama_model: str,
        opencode_model: str | None = None,
    ) -> None:
        self._default_provider = provider if provider in VALID_RUNTIME_PROVIDERS else "auto"
        self._ollama_base_url = ollama_base_url.rstrip("/")
        self._default_ollama_model = ollama_model.strip()
        self._default_opencode_model = opencode_model.strip() if opencode_model else None
        self._requested_provider = self._default_provider
        self._requested_ollama_model = self._default_ollama_model
        self._requested_opencode_model = self._default_opencode_model
        self._active_ollama_model = self._requested_ollama_model
        self._active_opencode_model = self._requested_opencode_model
        self._opencode_binary = shutil.which("opencode")
        self._cached_model_catalog: ModelCatalog | None = None
        self._cached_model_catalog_until = 0.0

    def set_runtime_preferences(
        self,
        *,
        provider: str | None = None,
        ollama_model: str | None = None,
        opencode_model: str | None = None,
    ) -> None:
        preferences_changed = False

        if provider is not None and provider in VALID_RUNTIME_PROVIDERS:
            preferences_changed = preferences_changed or provider != self._requested_provider
            self._requested_provider = provider

        if ollama_model is not None:
            normalized_ollama = ollama_model.strip()
            preferences_changed = (
                preferences_changed
                or normalized_ollama != self._requested_ollama_model
            )
            self._requested_ollama_model = normalized_ollama or self._default_ollama_model

        if opencode_model is not None:
            normalized_opencode = opencode_model.strip()
            preferences_changed = (
                preferences_changed
                or normalized_opencode != (self._requested_opencode_model or "")
            )
            self._requested_opencode_model = normalized_opencode or None

        if preferences_changed:
            self._invalidate_model_catalog_cache()

        self._active_ollama_model = self._requested_ollama_model
        self._active_opencode_model = self._requested_opencode_model

    def get_runtime_overview(self) -> tuple[ModelCatalog, RuntimeModelState]:
        catalog = self.get_model_catalog()
        return catalog, RuntimeModelState(
            requested_provider=self._requested_provider,
            effective_provider=self._effective_provider(catalog),
            requested_opencode_model=self._requested_opencode_model,
            effective_opencode_model=self._resolve_opencode_model(catalog.opencode_models),
            requested_ollama_model=self._requested_ollama_model,
            effective_ollama_model=self._resolve_ollama_model(catalog.ollama_models),
        )

    def get_model_catalog(self) -> ModelCatalog:
        if (
            self._cached_model_catalog is not None
            and monotonic() < self._cached_model_catalog_until
        ):
            return self._cached_model_catalog

        opencode_models = self._list_opencode_models()
        ollama_models = self._list_ollama_models()

        providers = ["auto"]
        if opencode_models:
            providers.append("opencode")
        if ollama_models:
            providers.append("ollama")
        providers.append("fallback")

        catalog = ModelCatalog(
            providers=tuple(providers),
            opencode_models=opencode_models,
            ollama_models=ollama_models,
        )
        self._cached_model_catalog = catalog
        self._cached_model_catalog_until = monotonic() + MODEL_CATALOG_CACHE_TTL_SECONDS
        return catalog

    def _invalidate_model_catalog_cache(self) -> None:
        self._cached_model_catalog = None
        self._cached_model_catalog_until = 0.0

    def compose_post(self, item: CandidateItem) -> str:
        prompt = self._build_prompt(item)

        for backend in self._provider_order():
            if backend == "opencode":
                generated = self._generate_with_opencode(prompt)
            else:
                generated = self._generate_with_ollama(prompt)

            if generated:
                return self._build_caption(item, generated)

        return self._build_caption(item, self._fallback_body(item))

    def _provider_order(self) -> tuple[str, ...]:
        if self._requested_provider == "fallback":
            return ()

        if self._requested_provider == "opencode":
            return ("opencode",)

        if self._requested_provider == "ollama":
            return ("ollama",)

        return ("opencode", "ollama")

    def _effective_provider(self, catalog: ModelCatalog) -> str:
        if self._requested_provider == "fallback":
            return "fallback"

        if self._requested_provider == "opencode":
            return "opencode" if catalog.opencode_models else "fallback"

        if self._requested_provider == "ollama":
            return "ollama" if catalog.ollama_models else "fallback"

        if catalog.opencode_models:
            return "opencode"
        if catalog.ollama_models:
            return "ollama"
        return "fallback"

    def _generate_with_opencode(self, prompt: str) -> str | None:
        if not self._opencode_binary:
            return None

        command = [self._opencode_binary, "run", "--format", "json"]
        model = self._resolve_opencode_model()
        if model:
            command.extend(["--model", model])
        command.append(prompt)

        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=OPENCODE_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None

        if completed.returncode != 0:
            return None

        return self._extract_opencode_text(completed.stdout)

    def _resolve_opencode_model(self, model_names: tuple[str, ...] | None = None) -> str | None:
        if model_names is None:
            model_names = self._list_opencode_models()
        if not model_names:
            return None

        for candidate in (
            self._active_opencode_model,
            self._requested_opencode_model,
            self._default_opencode_model,
        ):
            if candidate and candidate in model_names:
                self._active_opencode_model = candidate
                return candidate

        for preferred_name in (
            "opencode/deepseek-v4-flash-free",
            "opencode/minimax-m2.5-free",
            "opencode/nemotron-3-super-free",
            "opencode/big-pickle",
        ):
            if preferred_name in model_names:
                self._active_opencode_model = preferred_name
                return preferred_name

        self._active_opencode_model = model_names[0]
        return self._active_opencode_model

    def _known_opencode_models(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                candidate.strip()
                for candidate in (
                    self._active_opencode_model,
                    self._requested_opencode_model,
                    self._default_opencode_model,
                )
                if candidate and candidate.strip()
            )
        )

    def _prioritize_opencode_models(self, model_names: tuple[str, ...]) -> tuple[str, ...]:
        prioritized = sorted(
            model_names,
            key=lambda name: (
                0 if name.startswith("opencode/") else 1,
                0 if name in self._known_opencode_models() else 1,
                name.lower(),
            ),
        )
        return tuple(dict.fromkeys(prioritized))

    def _list_opencode_models(self) -> tuple[str, ...]:
        if not self._opencode_binary:
            return self._known_opencode_models()

        try:
            completed = subprocess.run(
                [self._opencode_binary, "models"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=OPENCODE_MODEL_DISCOVERY_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return self._known_opencode_models()

        if completed.returncode != 0:
            return self._known_opencode_models()

        model_names = tuple(
            dict.fromkeys(line.strip() for line in completed.stdout.splitlines() if line.strip())
        )
        if not model_names:
            return self._known_opencode_models()

        return self._prioritize_opencode_models(
            tuple(dict.fromkeys((*self._known_opencode_models(), *model_names)))
        )

    def _extract_opencode_text(self, output: str) -> str | None:
        fragments: list[str] = []
        saw_json_event = False
        for line in output.splitlines():
            raw_line = line.strip()
            if not raw_line:
                continue

            if raw_line.startswith("{"):
                saw_json_event = True

            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue

            if event.get("type") != "text":
                continue

            part = event.get("part")
            if not isinstance(part, dict):
                continue

            text = part.get("text")
            if isinstance(text, str) and text.strip():
                fragments.append(text)

        if fragments:
            return "".join(fragments).strip()

        normalized_output = output.strip()
        if normalized_output and not saw_json_event:
            return normalized_output

        return None

    def _generate_with_ollama(self, prompt: str) -> str | None:
        selected_model = self._resolve_ollama_model()
        if not selected_model:
            return None

        try:
            return self._generate_ollama(prompt, selected_model)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                return None

            fallback_model = self._discover_ollama_model()
            if not fallback_model or fallback_model == selected_model:
                return None

            self._active_ollama_model = fallback_model
            try:
                return self._generate_ollama(prompt, self._active_ollama_model)
            except httpx.HTTPError:
                return None
        except httpx.HTTPError:
            return None

    def _generate_ollama(self, prompt: str, model: str) -> str:
        with httpx.Client(timeout=90.0) as client:
            response = client.post(
                f"{self._ollama_base_url}/api/generate",
                json={
                    "model": model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {
                        "temperature": 0.35,
                    },
                },
            )
            response.raise_for_status()

        payload = response.json()
        return (payload.get("response") or "").strip()

    def _discover_ollama_model(self) -> str | None:
        return self._resolve_ollama_model()

    def _resolve_ollama_model(self, model_names: tuple[str, ...] | None = None) -> str | None:
        if model_names is None:
            model_names = self._list_ollama_models()
        if not model_names:
            return None

        for candidate in (
            self._active_ollama_model,
            self._requested_ollama_model,
            self._default_ollama_model,
        ):
            if candidate and candidate in model_names:
                self._active_ollama_model = candidate
                return candidate

        for preferred_name in (
            self._default_ollama_model,
            "kwangsuklee/Qwen3.5-9B-Claude-4.6-Opus-Reasoning-Distilled-GGUF:latest",
            "hir0rameel/qwen-claude:latest",
            "qwen2.5:7b-instruct",
            "llama3.2:latest",
        ):
            if preferred_name in model_names:
                self._active_ollama_model = preferred_name
                return preferred_name

        qwen_models = [name for name in model_names if "qwen" in name.lower()]
        if qwen_models:
            self._active_ollama_model = qwen_models[0]
            return self._active_ollama_model

        llama_models = [name for name in model_names if "llama" in name.lower()]
        if llama_models:
            self._active_ollama_model = llama_models[0]
            return self._active_ollama_model

        self._active_ollama_model = model_names[0]
        return self._active_ollama_model

    def _list_ollama_models(self) -> tuple[str, ...]:
        try:
            with httpx.Client(timeout=OLLAMA_MODEL_DISCOVERY_TIMEOUT_SECONDS) as client:
                response = client.get(f"{self._ollama_base_url}/api/tags")
                response.raise_for_status()
        except httpx.HTTPError:
            return ()

        payload = response.json()
        models = payload.get("models") or []
        return tuple(
            dict.fromkeys(
                str(model.get("name")).strip()
                for model in models
                if model.get("name")
            )
        )

    def _build_prompt(self, item: CandidateItem) -> str:
        facts = [
            f"Title: {item.title}",
            f"Source type: {item.source}",
            f"Summary: {item.summary or 'No summary provided.'}",
            f"Verified source URL: {item.url}",
            f"Published at (UTC): {item.published_at.isoformat()}",
        ]

        if item.developer_url:
            facts.append(f"Developer website: {item.developer_url}")
        if item.author_context:
            facts.append(f"Author context: {item.author_context}")
        if item.language:
            facts.append(f"Language: {item.language}")
        if item.rating_label:
            facts.append(f"Popularity: {item.rating_label}")
        elif item.stars:
            facts.append(f"Stars: {item.stars}")
        if item.topics:
            facts.append(f"Topics: {', '.join(item.topics[:6])}")

        return "\n".join(
            [
                "Ти пишеш стислий опис українською для Telegram-каналу про нові технічні проєкти та оновлення.",
                "Використовуй тільки наведені факти, нічого не вигадуй.",
                "Поверни тільки 2-3 речення без заголовка, без URL, без Markdown і без списків.",
                "Опиши що це за програма, що саме її виділяє та кому вона корисна.",
                "Якщо є Author context, тримайся формулювань автора або офіційного сайту максимально близько.",
                "Без емодзі, без рекламного тону, у межах 420 символів.",
                "",
                *facts,
            ]
        )

    def _fallback_body(self, item: CandidateItem) -> str:
        summary = item.author_context or item.summary or "Свіжий технічний проєкт без додаткового опису."
        summary = shorten(summary, 220)

        extras: list[str] = []
        if item.language:
            extras.append(f"працює на {item.language}")
        if item.rating_label:
            extras.append(f"має позначку популярності {item.rating_label}")

        if extras:
            return f"{summary} Проєкт {' та '.join(extras)}."

        return summary

    def _build_caption(self, item: CandidateItem, body: str) -> str:
        normalized_body = self._normalize_generated_body(body)
        body_text = shorten(normalized_body or self._fallback_body(item), MAX_BODY_LENGTH)

        parts = [
            f'<b><a href="{escape(item.url, quote=True)}">{escape(item.title)}</a></b>',
            "",
            escape(body_text),
        ]

        details: list[str] = []
        if item.rating_label:
            details.append(f"<b>Популярність:</b> {escape(item.rating_label)}")
        if item.language:
            details.append(f"<b>Мова:</b> {escape(item.language)}")
        if item.developer_url:
            details.append("<b>Офіційний сайт:</b> кнопка нижче")

        if details:
            parts.extend(["", *details])

        caption = "\n".join(parts)
        return self._finalize_caption(caption)

    def _normalize_generated_body(self, body: str) -> str:
        normalized = re.sub(r"https?://\S+", "", body)
        normalized = re.sub(r"^[\-*•]+\s*", "", normalized, flags=re.MULTILINE)
        normalized = re.sub(r"\n{3,}", "\n\n", normalized)
        return normalized.strip()

    def _finalize_caption(self, caption: str) -> str:
        normalized = re.sub(r"\n{3,}", "\n\n", caption).strip()
        if len(normalized) <= MAX_CAPTION_LENGTH:
            return normalized

        return shorten(normalized, MAX_CAPTION_LENGTH)
