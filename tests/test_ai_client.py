"""Tests for ai_client module."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

# Ensure src/ is importable
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import ai_client as ai


class AIClientCacheTests(unittest.TestCase):
    def test_cache_hit_avoids_request(self):
        """Second call with same cache_key returns cached value."""
        cache_dir = tempfile.mkdtemp()
        cache_path = Path(cache_dir) / "cache.json"
        client = ai.AIClient("deepseek", "key", "model", cache_path)
        # Pre-populate cache
        cache = {"test:key": {"value": "cached result", "timestamp": time.time()}}
        cache_path.write_text(json.dumps(cache), encoding="utf-8")
        # Should return cached without calling API
        result = client.complete("ignored prompt", cache_key="test:key")
        self.assertEqual(result, "cached result")

    def test_cache_ttl_expired_triggers_request(self):
        """Expired cache entry should be ignored."""
        cache_dir = tempfile.mkdtemp()
        cache_path = Path(cache_dir) / "cache.json"
        client = ai.AIClient("deepseek", "key", "model", cache_path)
        # Write expired cache
        old_ts = time.time() - (8 * 24 * 60 * 60)  # 8 days ago
        cache = {"test:key": {"value": "old", "timestamp": old_ts}}
        cache_path.write_text(json.dumps(cache), encoding="utf-8")
        # API call will fail (no real server) → returns empty string
        result = client.complete("prompt", cache_key="test:key")
        self.assertEqual(result, "")  # graceful degradation

    def test_cache_set_creates_file(self):
        cache_dir = tempfile.mkdtemp()
        cache_path = Path(cache_dir) / "sub" / "cache.json"
        client = ai.AIClient("deepseek", "key", "model", cache_path)
        client._set_cached("key1", "value1")
        self.assertTrue(cache_path.exists())
        data = json.loads(cache_path.read_text())
        self.assertEqual(data["key1"]["value"], "value1")


class CreateClientTests(unittest.TestCase):
    def test_disabled_returns_none(self):
        result = ai.create_client({"enabled": False})
        self.assertIsNone(result)

    def test_enabled_no_key_returns_none(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            result = ai.create_client({
                "enabled": True,
                "provider": "deepseek",
                "api_key_env": "NONEXISTENT_KEY",
            })
        self.assertIsNone(result)

    def test_unknown_provider_returns_none(self):
        result = ai.create_client({
            "enabled": True,
            "provider": "unknown_llm",
        })
        self.assertIsNone(result)

    def test_valid_config_returns_client(self):
        with mock.patch.dict("os.environ", {"TEST_KEY": "sk-test123"}):
            result = ai.create_client({
                "enabled": True,
                "provider": "deepseek",
                "api_key_env": "TEST_KEY",
                "model": "deepseek-chat",
            })
        self.assertIsNotNone(result)
        self.assertEqual(result.provider, "deepseek")
        self.assertEqual(result.api_key, "sk-test123")


class OpenAIRequestFormatTests(unittest.TestCase):
    def test_payload_structure(self):
        """Verify OpenAI-compatible request builds correct JSON payload."""
        cache_path = Path(tempfile.mkdtemp()) / "cache.json"
        client = ai.AIClient("deepseek", "test-key", "deepseek-chat", cache_path)

        captured = {}

        def mock_urlopen(req, timeout=10):
            captured["url"] = req.full_url
            captured["headers"] = dict(req.headers)
            captured["data"] = json.loads(req.data)
            resp = mock.MagicMock()
            resp.read.return_value = json.dumps({
                "choices": [{"message": {"content": "AI response"}}]
            }).encode()
            resp.__enter__ = mock.MagicMock(return_value=resp)
            resp.__exit__ = mock.MagicMock(return_value=False)
            return resp

        with mock.patch("urllib.request.urlopen", side_effect=mock_urlopen):
            result = client._openai_request("Test prompt")

        self.assertEqual(result, "AI response")
        self.assertIn("/v1/chat/completions", captured["url"])
        self.assertEqual(captured["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual(captured["data"]["model"], "deepseek-chat")
        self.assertEqual(captured["data"]["messages"][0]["content"], "Test prompt")


class GeminiRequestFormatTests(unittest.TestCase):
    def test_payload_structure(self):
        """Verify Gemini request builds correct JSON payload with API key in URL."""
        cache_path = Path(tempfile.mkdtemp()) / "cache.json"
        client = ai.AIClient("gemini", "gemini-key", "gemini-2.5-flash", cache_path)

        captured = {}

        def mock_urlopen(req, timeout=10):
            captured["url"] = req.full_url
            captured["data"] = json.loads(req.data)
            resp = mock.MagicMock()
            resp.read.return_value = json.dumps({
                "candidates": [{"content": {"parts": [{"text": "Gemini response"}]}}]
            }).encode()
            resp.__enter__ = mock.MagicMock(return_value=resp)
            resp.__exit__ = mock.MagicMock(return_value=False)
            return resp

        with mock.patch("urllib.request.urlopen", side_effect=mock_urlopen):
            result = client._gemini_request("Test prompt")

        self.assertEqual(result, "Gemini response")
        self.assertIn("key=gemini-key", captured["url"])
        self.assertIn("generateContent", captured["url"])
        self.assertIn("gemini-2.5-flash", captured["url"])
        parts = captured["data"]["contents"][0]["parts"]
        self.assertEqual(parts[0]["text"], "Test prompt")


class SummarizeReleaseNotesTests(unittest.TestCase):
    def test_empty_body_returns_empty(self):
        cache_path = Path(tempfile.mkdtemp()) / "cache.json"
        client = ai.AIClient("deepseek", "key", "model", cache_path)
        result = ai.summarize_release_notes(client, "App", "1.0", "2.0", "")
        self.assertEqual(result, "")

    def test_prompt_contains_app_info(self):
        """Verify prompt template includes app name and versions."""
        cache_path = Path(tempfile.mkdtemp()) / "cache.json"
        client = ai.AIClient("deepseek", "key", "model", cache_path)
        captured_prompt = None

        def mock_complete(prompt, cache_key=None):
            nonlocal captured_prompt
            captured_prompt = prompt
            return "✨ Mới: Feature X\n⚠️ Lưu ý: Không có breaking changes"

        client.complete = mock_complete
        result = ai.summarize_release_notes(
            client, "Obsidian", "1.13.4", "1.13.7", "Bug fixes and improvements"
        )
        self.assertIn("Obsidian", captured_prompt)
        self.assertIn("1.13.4", captured_prompt)
        self.assertIn("1.13.7", captured_prompt)
        self.assertIn("✨", result)


class GracefulDegradationTests(unittest.TestCase):
    def test_api_error_returns_empty(self):
        """API errors should be caught and return empty string."""
        cache_path = Path(tempfile.mkdtemp()) / "cache.json"
        client = ai.AIClient("deepseek", "bad-key", "model", cache_path)

        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.HTTPError(
                            "url", 401, "Unauthorized", {}, None)):
            result = client.complete("prompt")

        self.assertEqual(result, "")


class GenerateAppConfigTests(unittest.TestCase):
    def test_valid_toml_returned(self):
        """Valid TOML from LLM is returned with AI marker."""
        cache_path = Path(tempfile.mkdtemp()) / "cache.json"
        client = ai.AIClient("deepseek", "key", "model", cache_path)
        valid_toml = '[[apps]]\nid = "myapp"\nname = "My App"\ninstalled = { type = "command", command = ["myapp", "--version"], regex = "([0-9.]+)" }\nlatest = { type = "github", repo = "owner/myapp" }\nupdate = { type = "manual" }\n'
        client.complete = mock.MagicMock(return_value=valid_toml)
        result = ai.generate_app_config(client, {"package": "myapp", "name": "My App"})
        self.assertIsNotNone(result)
        self.assertIn("Generated by AI", result)
        self.assertIn("myapp", result)

    def test_invalid_toml_returns_none(self):
        """Invalid TOML from LLM returns None."""
        cache_path = Path(tempfile.mkdtemp()) / "cache.json"
        client = ai.AIClient("deepseek", "key", "model", cache_path)
        client.complete = mock.MagicMock(return_value="This is not TOML at all")
        result = ai.generate_app_config(client, {"package": "myapp"})
        self.assertIsNone(result)

    def test_markdown_fences_stripped(self):
        """TOML wrapped in markdown code fences should still work."""
        cache_path = Path(tempfile.mkdtemp()) / "cache.json"
        client = ai.AIClient("deepseek", "key", "model", cache_path)
        fenced = '```toml\n[[apps]]\nid = "app"\nname = "App"\ninstalled = { type = "manual" }\nlatest = { type = "manual" }\nupdate = { type = "manual" }\n```'
        client.complete = mock.MagicMock(return_value=fenced)
        result = ai.generate_app_config(client, {"package": "app"})
        self.assertIsNotNone(result)
        self.assertNotIn("```", result)

    def test_empty_llm_response_returns_none(self):
        cache_path = Path(tempfile.mkdtemp()) / "cache.json"
        client = ai.AIClient("deepseek", "key", "model", cache_path)
        client.complete = mock.MagicMock(return_value="")
        result = ai.generate_app_config(client, {"package": "app"})
        self.assertIsNone(result)


class FilterRecentCvesTests(unittest.TestCase):
    def test_filters_by_date(self):
        import datetime as dt
        recent_date = (dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(days=10)).isoformat()
        old_date = (dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(days=200)).isoformat()
        data = [
            {"id": "CVE-2026-001", "Published": recent_date, "cvss": 7.5},
            {"id": "CVE-2025-999", "Published": old_date, "cvss": 9.0},
        ]
        result = ai._filter_recent_cves(data, days=90)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "CVE-2026-001")

    def test_sorts_by_cvss_descending(self):
        import datetime as dt
        recent = (dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(days=5)).isoformat()
        data = [
            {"id": "low", "Published": recent, "cvss": 2.0},
            {"id": "high", "Published": recent, "cvss": 9.8},
            {"id": "med", "Published": recent, "cvss": 5.5},
        ]
        result = ai._filter_recent_cves(data, days=90)
        self.assertEqual([c["id"] for c in result], ["high", "med", "low"])


class LookupCveSummaryTests(unittest.TestCase):
    def test_no_data_returns_empty(self):
        """API returning empty/None gives empty string."""
        cache_path = Path(tempfile.mkdtemp()) / "cache.json"
        client = ai.AIClient("deepseek", "key", "model", cache_path)
        with mock.patch.object(ai, "_fetch_json", return_value=None):
            result = ai.lookup_cve_summary(client, "myapp", "1.0")
        self.assertEqual(result, "")

    def test_no_recent_cves_returns_empty(self):
        """All CVEs older than 90 days gives empty string."""
        import datetime as dt
        old = (dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(days=200)).isoformat()
        cache_path = Path(tempfile.mkdtemp()) / "cache.json"
        client = ai.AIClient("deepseek", "key", "model", cache_path)
        with mock.patch.object(ai, "_fetch_json", return_value=[
            {"id": "CVE-old", "Published": old, "cvss": 5.0}
        ]):
            result = ai.lookup_cve_summary(client, "myapp", "1.0")
        self.assertEqual(result, "")

    def test_with_cves_calls_llm(self):
        """Recent CVEs should trigger LLM summarization."""
        import datetime as dt
        recent = (dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(days=5)).isoformat()
        cache_path = Path(tempfile.mkdtemp()) / "cache.json"
        client = ai.AIClient("deepseek", "key", "model", cache_path)
        client.complete = mock.MagicMock(return_value="🟡 Lưu ý: CVE medium severity")
        with mock.patch.object(ai, "_fetch_json", return_value=[
            {"id": "CVE-2026-100", "Published": recent, "cvss": 6.5,
             "summary": "A vulnerability in..."}
        ]):
            result = ai.lookup_cve_summary(client, "firefox", "130.0")
        self.assertIn("Lưu ý", result)
        client.complete.assert_called_once()


if __name__ == "__main__":
    unittest.main()
