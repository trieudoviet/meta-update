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
    def __init__(self, payload):
        self.payload = payload

    def get_json(self, _url):
        return self.payload


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


if __name__ == "__main__":
    unittest.main()
