"""Optional AI-assisted analysis for meta-update.

Requires [ai] section in config.toml with enabled = true.
Falls back silently when disabled, misconfigured, or API errors occur.
Uses only stdlib: urllib.request + json. No third-party dependencies.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

log = logging.getLogger("update-checker.ai")

PROVIDERS: dict[str, dict[str, str]] = {
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "format": "openai",
    },
    "openai": {
        "base_url": "https://api.openai.com",
        "format": "openai",
    },
    "groq": {
        "base_url": "https://api.groq.com/openai",
        "format": "openai",
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com",
        "format": "gemini",
    },
}

_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days


class AIClient:
    """Lightweight LLM client using urllib.request (stdlib only).

    Supports two API formats:
    - OpenAI-compatible: DeepSeek, OpenAI, Groq
    - Gemini-native: Google Gemini
    """

    def __init__(
        self,
        provider: str,
        api_key: str,
        model: str,
        cache_path: Path,
        timeout: int = 10,
    ):
        self.provider = provider
        self.api_key = api_key
        self.model = model
        self.cache_path = cache_path
        self.timeout = timeout
        provider_info = PROVIDERS.get(provider, {})
        self.base_url = provider_info.get("base_url", "")
        self.api_format = provider_info.get("format", "openai")

    def complete(self, prompt: str, cache_key: str | None = None) -> str:
        """Send prompt to LLM, return response text. Uses cache if available."""
        if cache_key:
            cached = self._get_cached(cache_key)
            if cached is not None:
                return cached
        try:
            if self.api_format == "gemini":
                result = self._gemini_request(prompt)
            else:
                result = self._openai_request(prompt)
        except Exception as exc:
            log.warning("AI request failed: %s", exc)
            return ""
        if cache_key and result:
            self._set_cached(cache_key, result)
        return result

    def _openai_request(self, prompt: str) -> str:
        """POST /v1/chat/completions — DeepSeek, OpenAI, Groq."""
        url = f"{self.base_url}/v1/chat/completions"
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 500,
            "temperature": 0.3,
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return body["choices"][0]["message"]["content"].strip()

    def _gemini_request(self, prompt: str) -> str:
        """POST /v1beta/models/{model}:generateContent — Gemini."""
        url = (
            f"{self.base_url}/v1beta/models/{self.model}:generateContent"
            f"?key={self.api_key}"
        )
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": 500, "temperature": 0.3},
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return body["candidates"][0]["content"]["parts"][0]["text"].strip()

    # ---- Cache layer ----

    def _load_cache(self) -> dict[str, Any]:
        if self.cache_path.exists():
            try:
                return json.loads(self.cache_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _save_cache(self, cache: dict[str, Any]) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _get_cached(self, key: str) -> str | None:
        cache = self._load_cache()
        entry = cache.get(key)
        if entry is None:
            return None
        if time.time() - entry.get("timestamp", 0) > _CACHE_TTL_SECONDS:
            return None
        return entry.get("value")

    def _set_cached(self, key: str, value: str) -> None:
        cache = self._load_cache()
        cache[key] = {"value": value, "timestamp": time.time()}
        self._save_cache(cache)


def create_client(ai_config: dict[str, Any]) -> AIClient | None:
    """Factory: create AIClient from [ai] config section.

    Returns None if disabled, misconfigured, or API key not set.
    """
    if not ai_config.get("enabled", False):
        return None
    provider = ai_config.get("provider", "deepseek")
    if provider not in PROVIDERS:
        log.warning("Unknown AI provider: %s", provider)
        return None
    key_env = ai_config.get("api_key_env", "")
    api_key = os.environ.get(key_env, "")
    if not api_key:
        log.warning("AI API key not found in env var: %s", key_env)
        return None
    model = ai_config.get("model", "deepseek-chat")
    timeout = ai_config.get("timeout_seconds", 10)
    cache_dir = Path(
        os.environ.get(
            "XDG_DATA_HOME",
            os.path.expanduser("~/.local/share"),
        )
    ) / "update-checker"
    cache_path = cache_dir / "ai_cache.json"
    return AIClient(provider, api_key, model, cache_path, timeout)


def summarize_release_notes(
    client: AIClient,
    app_name: str,
    from_ver: str,
    to_ver: str,
    release_body: str,
) -> str:
    """Summarize release notes into 2 bullet points (Vietnamese).

    Returns empty string on failure or empty input.
    """
    if not release_body or not release_body.strip():
        return ""
    prompt = (
        f"Tóm tắt release notes sau đây cho {app_name} "
        f"(upgrade {from_ver} → {to_ver}).\n"
        f"Trả về đúng 2 dòng:\n"
        f"- ✨ Mới: [tóm tắt tính năng mới chính]\n"
        f"- ⚠️ Lưu ý: [breaking changes hoặc 'Không có breaking changes']\n\n"
        f"Release notes:\n{release_body[:3000]}"
    )
    cache_key = f"changelog:{app_name}:{from_ver}:{to_ver}"
    return client.complete(prompt, cache_key=cache_key)
