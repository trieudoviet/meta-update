from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LIB_DIR = PROJECT_ROOT / "src"
CONFIG_PATH = PROJECT_ROOT / "config" / "config.toml"
sys.path.insert(0, str(LIB_DIR))

import update_checker as uc  # noqa: E402


class FakeRunner:
    def __init__(self) -> None:
        self.default_timeout = 10

    def run(self, argv, **_kwargs):
        command = [str(value) for value in argv]
        if command == ["dpkg", "--print-architecture"]:
            return uc.CommandResult(command, 0, "amd64\n", "")
        if command[:2] == ["dpkg", "--compare-versions"]:
            current, operator, latest = command[2:]
            current_key = tuple(int(part) for part in current.split("-")[0].split("."))
            latest_key = tuple(int(part) for part in latest.split("-")[0].split("."))
            matches = {
                "lt": current_key < latest_key,
                "gt": current_key > latest_key,
            }
            return uc.CommandResult(command, 0 if matches[operator] else 1, "", "")
        return uc.CommandResult(command, 0, "", "")


class FakeHttp:
    def __init__(self, payload, text=""):
        self.payload = payload
        self.text = text

    def get_json(self, _url):
        return self.payload

    def get_text(self, _url):
        return self.text


def minimal_config(apps):
    return {
        "schema_version": 1,
        "report_dir": tempfile.gettempdir(),
        "apps": apps,
        "discovery": {"enabled": False},
    }


class ConfigTests(unittest.TestCase):
    def test_real_config_loads_with_unique_ids(self):
        config = uc.load_config(CONFIG_PATH)
        ids = [app["id"] for app in config["apps"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertGreaterEqual(len(ids), 20)

    def test_duplicate_id_is_rejected(self):
        text = """
schema_version = 1
[[apps]]
id = "same"
name = "One"
installed = { type = "dpkg", package = "one" }
latest = { type = "apt", package = "one" }
update = { type = "apt", package = "one" }
[[apps]]
id = "same"
name = "Two"
installed = { type = "dpkg", package = "two" }
latest = { type = "apt", package = "two" }
update = { type = "apt", package = "two" }
"""
        with tempfile.NamedTemporaryFile("w", suffix=".toml") as handle:
            handle.write(text)
            handle.flush()
            with self.assertRaises(uc.ConfigError):
                uc.load_config(Path(handle.name))

    def test_download_timeout_is_longer_than_api_timeout(self):
        config = uc.load_config(CONFIG_PATH)
        self.assertGreater(
            config["download_timeout_seconds"],
            config["request_timeout_seconds"],
        )


class VersionTests(unittest.TestCase):
    def setUp(self):
        self.checker = uc.UpdateChecker(
            minimal_config(
                [
                    {
                        "id": "sample",
                        "name": "Sample",
                        "installed": {"type": "dpkg", "package": "sample"},
                        "latest": {"type": "apt", "package": "sample"},
                        "update": {"type": "apt", "package": "sample"},
                    }
                ]
            ),
            runner=FakeRunner(),
            http=FakeHttp({}),
            quiet=True,
        )

    def test_version_comparison(self):
        self.assertEqual(self.checker.compare_versions("1.2.0", "1.3.0"), -1)
        self.assertEqual(self.checker.compare_versions("2.0.0", "1.9.0"), 1)
        self.assertEqual(self.checker.compare_versions("1.2.0", "1.2.0"), 0)

    def test_major_is_not_called_critical(self):
        self.assertTrue(self.checker.is_major_update("1.9.0", "2.0.0"))
        self.assertFalse(self.checker.is_major_update("1.9.0", "1.10.0"))

    def test_unknown_installed_version_is_not_compared(self):
        with mock.patch.object(self.checker, "installed_version", return_value="?"):
            with mock.patch.object(self.checker, "latest_version", return_value="2.0.0"):
                result = self.checker.check_one(self.checker.apps[0])
        self.assertIsNotNone(result)
        self.assertEqual(result.status, "unknown")
        self.assertIn("could not be detected", result.error)


class AssetSelectionTests(unittest.TestCase):
    def make_checker(self, app, assets, tag):
        payload = {
            "tag_name": tag,
            "html_url": "https://example.test/release",
            "assets": assets,
        }
        return uc.UpdateChecker(
            minimal_config([app]),
            runner=FakeRunner(),
            http=FakeHttp(payload),
            quiet=True,
        )

    def test_selects_exact_rustdesk_deb(self):
        app = {
            "id": "rustdesk",
            "name": "RustDesk",
            "installed": {"type": "dpkg", "package": "rustdesk"},
            "latest": {"type": "github", "repo": "rustdesk/rustdesk"},
            "update": {
                "type": "github_deb",
                "repo": "rustdesk/rustdesk",
                "package": "rustdesk",
                "asset_regex": r"^rustdesk-{version}-x86_64\.deb$",
            },
        }
        checker = self.make_checker(
            app,
            [
                {"name": "rustdesk-1.4.9-aarch64.deb"},
                {"name": "rustdesk-1.4.9-x86_64.deb"},
            ],
            "1.4.9",
        )
        result = uc.CheckResult(
            "rustdesk", "RustDesk", "GITHUB", "1.4.7", "1.4.9", "update", "github_deb"
        )
        action = checker.build_actions([result])[0]
        _release, asset = checker.select_github_asset(action)
        self.assertEqual(asset["name"], "rustdesk-1.4.9-x86_64.deb")

    def test_selects_ubuntu_2404_flameshot_zip(self):
        app = {
            "id": "flameshot",
            "name": "Flameshot",
            "installed": {"type": "dpkg", "package": "flameshot"},
            "latest": {"type": "github", "repo": "flameshot-org/flameshot"},
            "update": {
                "type": "github_zip_deb",
                "repo": "flameshot-org/flameshot",
                "package": "flameshot",
                "asset_regex": (
                    r"^flameshot-v[0-9.]+\+git.*-artifact-ubuntu-"
                    r"{os_version}-amd64\.zip$"
                ),
            },
        }
        checker = self.make_checker(
            app,
            [
                {"name": "flameshot-v14.0+git0.abc-artifact-ubuntu-22.04-amd64.zip"},
                {"name": "flameshot-v14.0+git0.abc-artifact-ubuntu-24.04-amd64.zip"},
            ],
            "v14.0.0",
        )
        checker.os_release["VERSION_ID"] = "24.04"
        result = uc.CheckResult(
            "flameshot",
            "Flameshot",
            "GITHUB",
            "12.1.0",
            "14.0.0",
            "major",
            "github_zip_deb",
        )
        action = checker.build_actions([result])[0]
        _release, asset = checker.select_github_asset(action)
        self.assertIn("ubuntu-24.04-amd64", asset["name"])


class SafetyAndOutputTests(unittest.TestCase):
    def setUp(self):
        self.app = {
            "id": "sample",
            "name": "Sample",
            "installed": {"type": "command", "argv": ["sample", "--version"], "regex": r"(\d+)"},
            "latest": {"type": "npm", "package": "sample"},
            "update": {"type": "command", "argv": ["sample", "update"]},
        }
        self.checker = uc.UpdateChecker(
            minimal_config([self.app]),
            runner=FakeRunner(),
            http=FakeHttp({}),
            quiet=True,
        )

    def test_actions_are_argv_not_shell_eval_strings(self):
        result = uc.CheckResult(
            "sample", "Sample", "NPM", "1", "2", "major", "command"
        )
        action = self.checker.build_actions([result])[0]
        self.assertEqual(action.argv, ["sample", "update"])

    def test_noninteractive_apply_fails_closed(self):
        result = uc.CheckResult(
            "sample", "Sample", "NPM", "1", "2", "major", "command"
        )
        action = self.checker.build_actions([result])[0]
        fake_stdin = io.StringIO()
        with redirect_stdout(io.StringIO()):
            with mock.patch("sys.stdin", fake_stdin):
                with self.assertRaisesRegex(uc.UpdateError, "--yes"):
                    self.checker.apply_actions([action], assume_yes=False)

    def test_json_payload_round_trips(self):
        result = uc.CheckResult(
            "sample", 'Sample "quoted"', "NPM", "1", "2", "major", "command"
        )
        payload = uc.json_payload([result], [], [], [], Path("/tmp/report.md"))
        encoded = json.dumps(payload, ensure_ascii=False)
        decoded = json.loads(encoded)
        self.assertEqual(decoded["results"][0]["name"], 'Sample "quoted"')
        self.assertEqual(decoded["summary"]["major"], 1)


class HttpScrapeLatestTests(unittest.TestCase):
    WINBOX_HTML = (
        '<a href="https://download.mikrotik.com/routeros/winbox/4.3/'
        'WinBox_Linux.zip">Linux</a>'
    )

    def make_checker(self, html):
        app = {
            "id": "winbox",
            "name": "WinBox",
            "installed": {
                "type": "command",
                "argv": ["winbox", "--version"],
                "regex": r"WinBox\s+([0-9][0-9.]*)",
            },
            "latest": {
                "type": "http_scrape",
                "url": "https://mikrotik.com/download/winbox",
                "regex": r"routeros/winbox/([0-9][0-9.]*)/WinBox_Linux\.zip",
            },
            "update": {"type": "manual", "url": "https://mikrotik.com/download/winbox"},
        }
        return uc.UpdateChecker(
            minimal_config([app]),
            runner=FakeRunner(),
            http=FakeHttp({}, text=html),
            quiet=True,
        )

    def test_scrapes_version_from_download_url(self):
        checker = self.make_checker(self.WINBOX_HTML)
        self.assertEqual(
            checker.latest_version(checker.apps[0], "4.3"),
            "4.3",
        )

    def test_missing_version_raises_update_error(self):
        checker = self.make_checker("<html>no winbox link here</html>")
        with self.assertRaises(uc.UpdateError):
            checker.latest_version(checker.apps[0], "4.3")


class RecommendationTests(unittest.TestCase):
    def _result(self, app_id, name, status, category="APT", update_type="apt"):
        return uc.CheckResult(
            app_id=app_id,
            name=name,
            category=category,
            current="1.0",
            latest="2.0",
            status=status,
            update_type=update_type,
        )

    def test_high_priority_for_browser(self):
        result = self._result("google-chrome", "Google Chrome", "update")
        lines = uc.build_recommendations([result], [], [])
        text = "\n".join(lines)
        self.assertIn("Ưu tiên cao", text)
        self.assertIn("Google Chrome", text)

    def test_medium_priority_for_dev_tool(self):
        result = self._result("cursor", "Cursor", "update")
        lines = uc.build_recommendations([result], [], [])
        text = "\n".join(lines)
        self.assertIn("Ưu tiên trung bình", text)

    def test_low_priority_for_unknown_app(self):
        result = self._result("gimp", "GIMP", "update")
        lines = uc.build_recommendations([result], [], [])
        text = "\n".join(lines)
        self.assertIn("Ưu tiên thấp", text)

    def test_major_update_promoted_to_high(self):
        result = self._result("gimp", "GIMP", "major")
        lines = uc.build_recommendations([result], [], [])
        text = "\n".join(lines)
        self.assertIn("Ưu tiên cao", text)
        self.assertNotIn("Ưu tiên thấp", text)

    def test_discovery_suggestions(self):
        discovered = [
            {
                "category": "APT", "package": "fcitx5", "name": "Fcitx 5",
                "version": "5.1", "section": "universe/utils",
                "homepage": "https://github.com/fcitx/fcitx5",
            },
        ]
        lines = uc.build_recommendations([], discovered, [])
        text = "\n".join(lines)
        self.assertIn("Phần mềm phát hiện mới", text)
        self.assertIn("fcitx5", text)
        self.assertIn("Nên ignore", text)
        self.assertIn("input method", text)

    def test_binary_discovery_suggests_ignore_local_bins(self):
        discovered = [
            {"category": "Binary", "package": "mytool", "name": "mytool", "version": "untracked"},
        ]
        lines = uc.build_recommendations([], discovered, [])
        text = "\n".join(lines)
        self.assertIn("ignore_local_bins", text)

    def test_duplicate_suggestions(self):
        duplicates = [{"package": "discord", "sources": "APT + Snap"}]
        lines = uc.build_recommendations([], [], duplicates)
        text = "\n".join(lines)
        self.assertIn("trùng nguồn", text)
        self.assertIn("discord", text)

    def test_no_updates_returns_empty(self):
        ok_result = self._result("gimp", "GIMP", "ok")
        lines = uc.build_recommendations([ok_result], [], [])
        self.assertEqual(lines, [])

    def test_command_hints_with_apps(self):
        apps = [
            {
                "id": "cursor",
                "name": "Cursor",
                "installed": {"type": "dpkg", "package": "cursor"},
                "latest": {"type": "apt", "package": "cursor"},
                "update": {"type": "apt", "package": "cursor"},
            }
        ]
        result = self._result("cursor", "Cursor", "update")
        lines = uc.build_recommendations([result], [], [], apps=apps)
        text = "\n".join(lines)
        self.assertIn("sudo apt-get install --only-upgrade -y cursor", text)

    def test_snap_command_hint(self):
        apps = [
            {
                "id": "firefox",
                "name": "Firefox",
                "installed": {"type": "snap", "package": "firefox"},
                "latest": {"type": "snap", "package": "firefox"},
                "update": {"type": "snap", "package": "firefox"},
            }
        ]
        result = self._result("firefox", "Firefox", "update", category="SNAP", update_type="snap")
        lines = uc.build_recommendations([result], [], [], apps=apps)
        text = "\n".join(lines)
        self.assertIn("sudo snap refresh firefox", text)

    def test_github_deb_hint(self):
        apps = [
            {
                "id": "obsidian",
                "name": "Obsidian",
                "installed": {"type": "dpkg", "package": "obsidian"},
                "latest": {"type": "github", "repo": "obsidianmd/obsidian-releases"},
                "update": {
                    "type": "github_deb",
                    "repo": "obsidianmd/obsidian-releases",
                    "package": "obsidian",
                    "asset_regex": "^obsidian_{version}_amd64\\.deb$",
                },
            }
        ]
        result = self._result(
            "obsidian", "Obsidian", "update", category="GITHUB", update_type="github_deb"
        )
        lines = uc.build_recommendations([result], [], [], apps=apps)
        text = "\n".join(lines)
        self.assertIn("check-all-updates --apply", text)

class DiscoveryClassificationTests(unittest.TestCase):
    def test_classify_standalone_recommends_track(self):
        item = {"category": "Standalone", "package": "antigravity-ide", "name": "Antigravity IDE"}
        emoji, text = uc._classify_discovered(item)
        self.assertEqual(emoji, "⚙️")
        self.assertIn("config.toml", text)
        self.assertIn("standalone", text)

    def test_classify_ignore_section_libs(self):
        item = {"category": "APT", "package": "libfoo", "section": "libs"}
        emoji, text = uc._classify_discovered(item)
        self.assertEqual(emoji, "🔇")
        self.assertIn("ignore", text)

    def test_classify_track_section_devel(self):
        item = {
            "category": "APT", "package": "myeditor",
            "section": "devel", "homepage": "https://example.com",
        }
        emoji, text = uc._classify_discovered(item)
        self.assertEqual(emoji, "⚙️")
        self.assertIn("config.toml", text)

    def test_classify_track_github_homepage(self):
        item = {
            "category": "APT", "package": "coolapp",
            "section": "web", "homepage": "https://github.com/owner/coolapp",
        }
        emoji, text = uc._classify_discovered(item)
        self.assertEqual(emoji, "⚙️")
        self.assertIn("owner/coolapp", text)

    def test_classify_input_method_ignored(self):
        item = {
            "category": "APT", "package": "fcitx5",
            "section": "universe/utils", "homepage": "",
        }
        emoji, text = uc._classify_discovered(item)
        self.assertEqual(emoji, "🔇")
        self.assertIn("input method", text)

    def test_classify_binary_suggests_review(self):
        item = {"category": "Binary", "package": "mytool"}
        emoji, text = uc._classify_discovered(item)
        self.assertEqual(emoji, "❓")
        self.assertIn("ignore_local_bins", text)

    def test_classify_unknown_section_needs_review(self):
        item = {"category": "APT", "package": "weirdpkg", "section": "alien"}
        emoji, text = uc._classify_discovered(item)
        self.assertEqual(emoji, "❓")
        self.assertIn("review", text)

    def test_parse_github_repo_valid(self):
        self.assertEqual(uc._parse_github_repo("https://github.com/fcitx/fcitx5"), "fcitx/fcitx5")
        self.assertEqual(uc._parse_github_repo("https://github.com/owner/repo.git"), "owner/repo")
        self.assertEqual(uc._parse_github_repo("https://github.com/a/b/"), "a/b")

    def test_parse_github_repo_invalid(self):
        self.assertEqual(uc._parse_github_repo("https://example.com/foo"), "")
        self.assertEqual(uc._parse_github_repo(""), "")
        self.assertEqual(uc._parse_github_repo("https://github.com/only-owner"), "")

    def test_discovery_table_has_type_column(self):
        discovered = [
            {
                "category": "APT", "package": "gimp-extra",
                "name": "GIMP Extra", "version": "1.0",
                "section": "graphics", "homepage": "",
            },
            {
                "category": "Standalone", "package": "antigravity-ide",
                "name": "Antigravity IDE", "version": "untracked",
            },
        ]
        lines = uc.build_recommendations([], discovered, [])
        text = "\n".join(lines)
        # Table header includes Loại column
        self.assertIn("| Loại |", text)
        # APT item shows section as type
        self.assertIn("graphics", text)
        # Standalone item shows category as type
        self.assertIn("Standalone", text)
        self.assertIn("⚙️", text)


if __name__ == "__main__":
    unittest.main()
