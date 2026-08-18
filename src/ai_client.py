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


def _fetch_json(url: str, timeout: int = 5) -> Any:
    """GET a JSON endpoint and return parsed data. Returns None on error."""
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "meta-update/1.0", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        log.warning("Failed to fetch %s: %s", url, exc)
        return None


def generate_app_config(
    client: AIClient,
    app_metadata: dict[str, str],
) -> str | None:
    """Generate [[apps]] TOML block for discovered app using LLM.

    Returns validated TOML string, or None if LLM output is invalid.
    """
    import tomllib

    package = app_metadata.get("package", "")
    name = app_metadata.get("name", package)
    category = app_metadata.get("category", "Standalone")
    exec_path = app_metadata.get("exec_path", f"/opt/{package}/{package}")
    version = app_metadata.get("version", "untracked")

    prompt = (
        f"Given this discovered app:\n"
        f"  Name: {name}\n"
        f"  Package: {package}\n"
        f"  Exec: {exec_path}\n"
        f"  Category: {category}\n"
        f"  Current version: {version}\n\n"
        f"Generate a TOML [[apps]] block with id, name, installed, latest, update fields.\n"
        f"Use only these adapter types: dpkg, snap, command, json_file, github, apt, "
        f"github_deb, manual.\n"
        f"Return ONLY valid TOML, no explanation, no markdown fences.\n"
    )

    raw = client.complete(prompt, cache_key=f"config:{package}")
    if not raw:
        return None

    # Clean up markdown fences if present
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])

    # Validate TOML
    try:
        parsed = tomllib.loads(raw)
        if "apps" not in parsed:
            return None
    except Exception:
        return None

    return f"# Generated by AI — please review\n{raw}\n"


def _filter_recent_cves(
    data: list[dict[str, Any]], days: int = 90
) -> list[dict[str, Any]]:
    """Filter CVEs published within the last N days, sorted by CVSS desc."""
    import datetime as dt

    cutoff = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(days=days)
    recent: list[dict[str, Any]] = []
    for cve in data:
        pub = cve.get("Published", "")
        try:
            pub_dt = dt.datetime.fromisoformat(pub.replace("Z", "+00:00"))
            if pub_dt >= cutoff:
                recent.append(cve)
        except (ValueError, AttributeError):
            continue
    recent.sort(key=lambda c: float(c.get("cvss", 0) or 0), reverse=True)
    return recent


def lookup_cve_summary(
    client: AIClient,
    product: str,
    version: str,
) -> str:
    """Query cve.circl.lu for recent CVEs and summarize with AI.

    Returns 1-line severity text for report, or empty string if no CVEs found.
    """
    encoded = urllib.parse.quote(product)
    url = f"https://cve.circl.lu/api/search/{encoded}"
    data = _fetch_json(url, timeout=10)
    if not data or not isinstance(data, list):
        return ""

    recent = _filter_recent_cves(data)
    if not recent:
        return ""

    cve_text = "\n".join(
        f"- {cve.get('id', 'N/A')}: CVSS {cve.get('cvss', 'N/A')} — "
        f"{cve.get('summary', 'No summary')[:200]}"
        for cve in recent[:5]
    )

    prompt = (
        f"Đánh giá mức độ nghiêm trọng của các CVE sau cho {product} {version}.\n"
        f"Trả về đúng 1 dòng:\n"
        f"- 🔴 Nghiêm trọng: [nếu có CVE critical/high]\n"
        f"- 🟡 Lưu ý: [nếu có CVE medium]\n"
        f"- 🟢 An toàn: [nếu không có CVE đáng lo ngại]\n\n"
        f"CVEs:\n{cve_text}"
    )

    return client.complete(prompt, cache_key=f"cve:{product}:{version}")
