#!/usr/bin/env python3
"""Update Checker.

Local-first software inventory, update planning, verified GitHub asset updates,
and machine-readable reporting for Ubuntu workstations.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as dt
import difflib
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, BinaryIO, Iterable, TextIO


VERSION = "3.0.0"
DEFAULT_CONFIG = Path("~/.config/update-checker/config.toml").expanduser()
STATUS_ORDER = ("ok", "update", "major", "unknown")
HIGH_PRIORITY_IDS = {
    "google-chrome", "firefox", "obsidian", "claude-desktop",
    "rustdesk", "docker-ce",
}
MEDIUM_PRIORITY_IDS = {
    "vscode", "cursor", "codex-desktop", "codex-cli",
    "claude-code", "uv", "codex-switcher",
}
IGNORE_SECTIONS = {
    "libs", "oldlibs", "debug", "libdevel", "doc", "fonts",
    "localization", "metapackages", "tasks",
}
TRACK_SECTIONS = {
    "web", "net", "editors", "devel", "graphics", "video",
    "mail", "comm", "database", "science",
}
_FIELD_CODES = {
    "%f", "%F", "%u", "%U", "%d", "%D", "%n", "%N",
    "%i", "%c", "%k", "%v", "%m",
}


def _extract_exec_path(exec_value: str) -> Path | None:
    """Parse Exec= value from a .desktop file, handling env/sh wrappers.

    Handles patterns like:
      env VAR=VALUE /opt/app/binary %u
      /usr/bin/env python3 /path/to/script.py
      sh -c "/opt/custom/app --flag"
      /opt/app/binary --ozone-platform=x11 %F

    Returns the absolute Path to the real executable if it exists, else None.
    """
    try:
        tokens = shlex.split(exec_value)
    except ValueError:
        return None
    # Remove freedesktop field codes
    tokens = [t for t in tokens if t not in _FIELD_CODES]
    if not tokens:
        return None
    first = Path(tokens[0]).name
    # Handle env wrapper: skip VAR=VALUE tokens
    if first == "env":
        tokens = tokens[1:]
        while tokens and "=" in tokens[0] and not tokens[0].startswith("/"):
            tokens = tokens[1:]
    # Handle shell -c wrapper: re-parse the inner command string
    elif first in {"sh", "bash"}:
        if len(tokens) >= 3 and tokens[1] == "-c":
            try:
                tokens = shlex.split(tokens[2])
            except ValueError:
                return None
    if not tokens:
        return None
    candidate = Path(tokens[0])
    if candidate.is_absolute() and candidate.exists():
        return candidate
    return None


class ConfigError(RuntimeError):
    pass


class UpdateError(RuntimeError):
    pass


@dataclasses.dataclass(slots=True)
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclasses.dataclass(slots=True)
class Release:
    repo: str
    version: str
    tag: str
    url: str
    assets: list[dict[str, Any]]


@dataclasses.dataclass(slots=True)
class CheckResult:
    app_id: str
    name: str
    category: str
    current: str
    latest: str
    status: str
    update_type: str
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(slots=True)
class Action:
    app: dict[str, Any]
    result: CheckResult
    kind: str
    description: str
    automatic: bool
    needs_root: bool
    argv: list[str] | None = None


class Runner:
    def __init__(self, default_timeout: int = 60) -> None:
        self.default_timeout = default_timeout

    def run(
        self,
        argv: Iterable[str],
        *,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        command = [str(part) for part in argv]
        try:
            completed = subprocess.run(
                command,
                text=True,
                errors="replace",
                capture_output=True,
                timeout=timeout or self.default_timeout,
                env=env,
                check=False,
            )
            return CommandResult(
                argv=command,
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )
        except FileNotFoundError:
            return CommandResult(command, 127, "", f"command not found: {command[0]}")
        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                command,
                124,
                _to_text(exc.stdout),
                _to_text(exc.stderr) or f"timeout after {timeout or self.default_timeout}s",
            )

    def run_stream(self, argv: Iterable[str], log: TextIO) -> int:
        command = [str(part) for part in argv]
        log.write(f"$ {shlex.join(command)}\n")
        log.flush()
        try:
            process = subprocess.Popen(
                command,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
            )
        except FileNotFoundError:
            message = f"command not found: {command[0]}"
            print(message, file=sys.stderr)
            log.write(message + "\n")
            return 127

        assert process.stdout is not None
        for line in process.stdout:
            print(line, end="", file=sys.stderr)
            log.write(line)
            log.flush()
        return process.wait()


class HttpClient:
    def __init__(
        self,
        timeout: int,
        github_token: str = "",
        download_timeout: int = 180,
    ) -> None:
        self.timeout = timeout
        self.github_token = github_token
        self.download_timeout = download_timeout

    def _request(self, url: str) -> urllib.request.Request:
        headers = {
            "Accept": "application/vnd.github+json, application/json",
            "User-Agent": f"update-checker/{VERSION}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.github_token and urllib.parse.urlparse(url).hostname == "api.github.com":
            headers["Authorization"] = f"Bearer {self.github_token}"
        return urllib.request.Request(url, headers=headers)

    def get_json(self, url: str) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(self._request(url), timeout=self.timeout) as response:
                    return json.load(response)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
        raise UpdateError(f"HTTP JSON failed for {url}: {last_error}") from last_error

    def download(self, url: str, destination: Path) -> str:
        last_error: Exception | None = None
        for attempt in range(3):
            digest = hashlib.sha256()
            try:
                with urllib.request.urlopen(
                    self._request(url),
                    timeout=self.download_timeout,
                ) as response:
                    with destination.open("wb") as output:
                        _copy_and_hash(response, output, digest)
                return digest.hexdigest()
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                with contextlib.suppress(FileNotFoundError):
                    destination.unlink()
                if attempt < 2:
                    time.sleep(attempt + 1)
        raise UpdateError(f"download failed for {url}: {last_error}") from last_error

    def get_text(self, url: str) -> str:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml,*/*",
                "User-Agent": f"update-checker/{VERSION}",
            },
        )
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    charset = response.headers.get_content_charset() or "utf-8"
                    return response.read().decode(charset, errors="replace")
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
        raise UpdateError(f"HTTP text failed for {url}: {last_error}") from last_error


def _copy_and_hash(source: BinaryIO, target: BinaryIO, digest: Any) -> None:
    while chunk := source.read(1024 * 1024):
        target.write(chunk)
        digest.update(chunk)


def _to_text(value: bytes | str | None) -> str:
    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else value


def expand_path(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value)))


def load_config(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            config = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot load {path}: {exc}") from exc

    if config.get("schema_version") != 1:
        raise ConfigError("unsupported or missing schema_version")
    apps = config.get("apps")
    if not isinstance(apps, list) or not apps:
        raise ConfigError("config must contain at least one [[apps]] entry")

    seen: set[str] = set()
    for index, app in enumerate(apps, start=1):
        for key in ("id", "name", "installed", "latest", "update"):
            if key not in app:
                raise ConfigError(f"app #{index} is missing {key}")
        app_id = str(app["id"])
        if app_id in seen:
            raise ConfigError(f"duplicate app id: {app_id}")
        seen.add(app_id)
    return config


def read_os_release() -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key] = value.strip().strip('"')
    except OSError:
        pass
    return values


class UpdateChecker:
    def __init__(
        self,
        config: dict[str, Any],
        *,
        runner: Runner | None = None,
        http: HttpClient | None = None,
        quiet: bool = False,
    ) -> None:
        self.config = config
        self.apps: list[dict[str, Any]] = config["apps"]
        self.runner = runner or Runner(int(config.get("command_timeout_seconds", 60)))
        token_name = str(config.get("github_token_env", "GITHUB_TOKEN"))
        self.http = http or HttpClient(
            int(config.get("request_timeout_seconds", 20)),
            os.environ.get(token_name, ""),
            int(config.get("download_timeout_seconds", 180)),
        )
        self.quiet = quiet
        self.report_dir = expand_path(str(config.get("report_dir", "~/.local/share/update-checker")))
        self.release_cache: dict[str, Release] = {}
        self.snap_updates_cache: dict[str, str] | None = None
        self.os_release = read_os_release()
        self.warnings: list[str] = []

    def progress(self, message: str) -> None:
        if not self.quiet:
            print(message, file=sys.stderr)

    def warn(self, message: str) -> None:
        self.warnings.append(message)
        if not self.quiet:
            print(f"warning: {message}", file=sys.stderr)

    def refresh_apt_cache(self) -> None:
        sudo = self.runner.run(["sudo", "-n", "true"], timeout=5)
        if sudo.returncode != 0:
            self.warn("APT cache not refreshed: non-interactive sudo is unavailable")
            return
        self.progress("Refreshing APT metadata…")
        result = self.runner.run(["sudo", "-n", "apt-get", "update", "-qq"], timeout=300)
        if result.returncode != 0:
            self.warn(f"APT refresh failed: {(result.stderr or result.stdout).strip()}")

    def github_release(self, repo: str) -> Release:
        if repo in self.release_cache:
            return self.release_cache[repo]
        encoded_repo = urllib.parse.quote(repo, safe="/")
        payload = self.http.get_json(f"https://api.github.com/repos/{encoded_repo}/releases/latest")
        tag = str(payload.get("tag_name") or "")
        if not tag:
            raise UpdateError(f"GitHub returned no latest tag for {repo}")
        release = Release(
            repo=repo,
            version=tag.removeprefix("v"),
            tag=tag,
            url=str(payload.get("html_url") or f"https://github.com/{repo}/releases/latest"),
            assets=list(payload.get("assets") or []),
        )
        self.release_cache[repo] = release
        return release

    def installed_version(self, app: dict[str, Any]) -> str | None:
        spec = app["installed"]
        source_type = spec["type"]
        if source_type == "dpkg":
            result = self.runner.run(
                ["dpkg-query", "-W", "-f=${Status}\t${Version}", str(spec["package"])]
            )
            if result.returncode != 0 or not result.stdout.startswith("install ok installed\t"):
                return None
            return result.stdout.split("\t", 1)[1].strip()

        if source_type == "snap":
            result = self.runner.run(["snap", "list", str(spec["package"])])
            lines = result.stdout.splitlines()
            if result.returncode != 0 or len(lines) < 2:
                return None
            fields = lines[1].split()
            return fields[1] if len(fields) > 1 else None

        if source_type == "command":
            argv = [str(value) for value in spec["argv"]]
            result = self.runner.run(argv)
            if result.returncode != 0:
                return None
            match = re.search(str(spec["regex"]), result.stdout + result.stderr)
            return match.group(1) if match else None

        if source_type == "json_file":
            path = expand_path(str(spec["path"]))
            try:
                payload: Any = json.loads(path.read_text(encoding="utf-8"))
                for part in str(spec["key"]).split("."):
                    payload = payload[part]
                return str(payload)
            except (OSError, KeyError, TypeError, json.JSONDecodeError):
                app_path = spec.get("app_path")
                if app_path and expand_path(str(app_path)).exists():
                    return "?"
                return None

        if source_type == "appimage_asar":
            path = expand_path(str(spec["path"]))
            if not path.exists():
                return None
            member = str(spec.get("member", "resources/app.asar"))
            result = self.runner.run(["7z", "x", "-so", str(path), member])
            if result.returncode != 0:
                return "?"
            match = re.search(str(spec["version_regex"]), result.stdout)
            return match.group(1) if match else "?"

        raise ConfigError(f"unsupported installed source: {source_type}")

    def latest_version(self, app: dict[str, Any], current: str) -> str:
        spec = app["latest"]
        source_type = spec["type"]
        if source_type == "apt":
            package = str(spec["package"])
            result = self.runner.run(["apt-cache", "policy", package])
            match = re.search(r"^\s*Candidate:\s*(\S+)", result.stdout, re.MULTILINE)
            if result.returncode != 0 or not match or match.group(1) == "(none)":
                raise UpdateError(f"APT has no candidate for {package}")
            return match.group(1)

        if source_type == "snap":
            return self.snap_updates().get(str(spec["package"]), current)

        if source_type == "npm":
            package = urllib.parse.quote(str(spec["package"]), safe="@")
            payload = self.http.get_json(f"https://registry.npmjs.org/{package}/latest")
            version = str(payload.get("version") or "")
            if not version:
                raise UpdateError(f"npm returned no latest version for {spec['package']}")
            return version

        if source_type == "github":
            return self.github_release(str(spec["repo"])).version

        if source_type == "http_scrape":
            text = self.http.get_text(str(spec["url"]))
            match = re.search(str(spec["regex"]), text)
            if not match:
                raise UpdateError(
                    f"scrape regex found no version at {spec['url']}"
                )
            return match.group(1)

        raise ConfigError(f"unsupported latest source: {source_type}")

    def snap_updates(self) -> dict[str, str]:
        if self.snap_updates_cache is not None:
            return self.snap_updates_cache
        result = self.runner.run(["snap", "refresh", "--list"], timeout=120)
        updates: dict[str, str] = {}
        if result.returncode == 0:
            for line in result.stdout.splitlines()[1:]:
                fields = line.split()
                if len(fields) >= 2:
                    updates[fields[0]] = fields[1]
        self.snap_updates_cache = updates
        return updates

    def compare_versions(self, current: str, latest: str) -> int:
        if current == latest:
            return 0
        lower = self.runner.run(["dpkg", "--compare-versions", current, "lt", latest])
        if lower.returncode == 0:
            return -1
        greater = self.runner.run(["dpkg", "--compare-versions", current, "gt", latest])
        if greater.returncode == 0:
            return 1
        return 0

    @staticmethod
    def is_major_update(current: str, latest: str) -> bool:
        current_match = re.search(r"\d+", current)
        latest_match = re.search(r"\d+", latest)
        return bool(
            current_match
            and latest_match
            and current_match.group(0) != latest_match.group(0)
        )

    def check_one(self, app: dict[str, Any]) -> CheckResult | None:
        current = self.installed_version(app)
        if current is None:
            return None
        category = str(app["latest"]["type"]).upper()
        try:
            latest = self.latest_version(app, current)
            if current == "?":
                status = "unknown"
                error = "installed version could not be detected"
            else:
                comparison = self.compare_versions(current, latest)
                if comparison >= 0:
                    status = "ok"
                elif self.is_major_update(current, latest):
                    status = "major"
                else:
                    status = "update"
                error = ""
        except (ConfigError, UpdateError) as exc:
            latest = "?"
            status = "unknown"
            error = str(exc)
        return CheckResult(
            app_id=str(app["id"]),
            name=str(app["name"]),
            category=category,
            current=current,
            latest=latest,
            status=status,
            update_type=str(app["update"]["type"]),
            error=error,
        )

    def check_all(self) -> list[CheckResult]:
        results: list[CheckResult] = []
        for app in self.apps:
            self.progress(f"Checking {app['name']}…")
            result = self.check_one(app)
            if result is not None:
                results.append(result)
        return results

    def build_actions(self, results: list[CheckResult]) -> list[Action]:
        apps_by_id = {str(app["id"]): app for app in self.apps}
        actions: list[Action] = []
        for result in results:
            if result.status not in {"update", "major"}:
                continue
            app = apps_by_id[result.app_id]
            spec = app["update"]
            update_type = str(spec["type"])
            argv: list[str] | None = None
            automatic = True
            needs_root = False

            if update_type == "apt":
                argv = [
                    "sudo",
                    "apt-get",
                    "install",
                    "--only-upgrade",
                    "-y",
                    str(spec["package"]),
                ]
                needs_root = True
                description = shlex.join(argv)
            elif update_type == "snap":
                argv = ["sudo", "snap", "refresh", str(spec["package"])]
                needs_root = True
                description = shlex.join(argv)
            elif update_type == "command":
                argv = [str(value) for value in spec["argv"]]
                description = shlex.join(argv)
            elif update_type in {"github_deb", "github_zip_deb"}:
                needs_root = True
                description = f"verified GitHub asset from {spec['repo']} → apt install"
            elif update_type == "manual":
                automatic = False
                description = str(spec["url"])
            else:
                raise ConfigError(f"unsupported update type: {update_type}")

            actions.append(
                Action(
                    app=app,
                    result=result,
                    kind=update_type,
                    description=description,
                    automatic=automatic,
                    needs_root=needs_root,
                    argv=argv,
                )
            )
        return actions

    def select_github_asset(self, action: Action) -> tuple[Release, dict[str, Any]]:
        spec = action.app["update"]
        release = self.github_release(str(spec["repo"]))
        pattern_template = str(spec["asset_regex"])
        pattern = pattern_template.format(
            version=re.escape(release.version),
            os_version=re.escape(self.os_release.get("VERSION_ID", "")),
            arch=re.escape(self.runner.run(["dpkg", "--print-architecture"]).stdout.strip()),
        )
        matches = [
            asset
            for asset in release.assets
            if re.fullmatch(pattern, str(asset.get("name") or ""))
        ]
        if len(matches) != 1:
            names = ", ".join(str(asset.get("name")) for asset in matches) or "none"
            raise UpdateError(
                f"expected one GitHub asset for {action.result.name}; matched {len(matches)}: {names}"
            )
        return release, matches[0]

    def apply_github_deb(self, action: Action, log: TextIO) -> int:
        spec = action.app["update"]
        release, asset = self.select_github_asset(action)
        asset_name = str(asset["name"])
        asset_url = str(asset["browser_download_url"])
        expected_digest = str(asset.get("digest") or "")
        if spec.get("require_digest", False) and not expected_digest.startswith("sha256:"):
            raise UpdateError(f"GitHub asset has no SHA-256 digest: {asset_name}")

        with tempfile.TemporaryDirectory(prefix="update-checker-") as temp_name:
            temp_dir = Path(temp_name)
            downloaded = temp_dir / asset_name
            log.write(f"Download: {asset_url}\n")
            actual_hash = self.http.download(asset_url, downloaded)
            log.write(f"SHA-256: {actual_hash}\n")
            if expected_digest and actual_hash != expected_digest.removeprefix("sha256:"):
                raise UpdateError(f"SHA-256 mismatch for {asset_name}")

            if action.kind == "github_zip_deb":
                deb_path = self._extract_verified_deb(downloaded, temp_dir, log)
            else:
                deb_path = downloaded

            self._validate_deb(deb_path, str(spec["package"]), release.version, log)
            return self.runner.run_stream(
                ["sudo", "apt-get", "install", "-y", str(deb_path)],
                log,
            )

    def _extract_verified_deb(self, archive: Path, temp_dir: Path, log: TextIO) -> Path:
        try:
            with zipfile.ZipFile(archive) as bundle:
                names = bundle.namelist()
                if any(Path(name).is_absolute() or ".." in Path(name).parts for name in names):
                    raise UpdateError("unsafe path found in GitHub ZIP")
                deb_names = [name for name in names if name.endswith(".deb")]
                if len(deb_names) != 1:
                    raise UpdateError(f"expected one .deb in ZIP, found {len(deb_names)}")
                deb_name = deb_names[0]
                deb_path = temp_dir / Path(deb_name).name
                with bundle.open(deb_name) as source, deb_path.open("wb") as target:
                    shutil.copyfileobj(source, target)

                checksum_name = next(
                    (name for name in names if name.endswith(".deb.sha256sum")),
                    None,
                )
                if checksum_name:
                    checksum_text = bundle.read(checksum_name).decode("utf-8", errors="replace")
                    expected = checksum_text.split()[0]
                    actual = hashlib.sha256(deb_path.read_bytes()).hexdigest()
                    if actual != expected:
                        raise UpdateError(f"inner .deb SHA-256 mismatch for {deb_path.name}")
                    log.write(f"Inner .deb SHA-256: {actual}\n")
                return deb_path
        except (OSError, zipfile.BadZipFile) as exc:
            raise UpdateError(f"invalid GitHub ZIP: {exc}") from exc

    def _validate_deb(
        self,
        deb_path: Path,
        expected_package: str,
        latest: str,
        log: TextIO,
    ) -> None:
        metadata = self.runner.run(["dpkg-deb", "-f", str(deb_path)])
        fields = {
            match.group(1): match.group(2).strip()
            for match in re.finditer(
                r"^(Package|Version|Architecture):\s*(.+)$",
                metadata.stdout,
                re.MULTILINE,
            )
        }
        if metadata.returncode != 0 or len(fields) < 3:
            raise UpdateError(f"cannot read .deb metadata: {metadata.stderr.strip()}")
        package = fields["Package"]
        version = fields["Version"]
        architecture = fields["Architecture"]
        host_arch = self.runner.run(["dpkg", "--print-architecture"]).stdout.strip()
        if package != expected_package:
            raise UpdateError(f"unexpected .deb package: {package}, expected {expected_package}")
        if architecture not in {host_arch, "all"}:
            raise UpdateError(f"unexpected .deb architecture: {architecture}, host is {host_arch}")
        if self.compare_versions(version, latest) < 0:
            raise UpdateError(f".deb version {version} is older than release {latest}")
        log.write(f"Package metadata: {package} {version} {architecture}\n")

    def apply_actions(self, actions: list[Action], *, assume_yes: bool) -> int:
        if not actions:
            print("Không có phần mềm cần cập nhật.")
            return 0

        print_plan(actions)
        automatic = [action for action in actions if action.automatic]
        if not automatic:
            print("Chỉ có cập nhật thủ công; không có lệnh tự động để chạy.")
            return 0

        if not assume_yes:
            if not sys.stdin.isatty():
                raise UpdateError("--apply ngoài terminal phải đi kèm --yes")
            answer = input("Bắt đầu cập nhật? [y/N]: ").strip().lower()
            if answer not in {"y", "yes"}:
                print("Đã hủy.")
                return 0

        if any(action.needs_root for action in automatic):
            if sys.stdin.isatty():
                auth = subprocess.run(["sudo", "-v"], check=False)
            else:
                auth = subprocess.run(["sudo", "-n", "-v"], check=False)
            if auth.returncode != 0:
                raise UpdateError("không thể xác thực sudo")

        self.report_dir.mkdir(parents=True, exist_ok=True)
        timestamp = dt.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        log_path = self.report_dir / f"update-{timestamp}.log"
        failures = 0
        with log_path.open("w", encoding="utf-8") as log:
            log.write(f"# Update Checker — {dt.datetime.now().astimezone().isoformat()}\n\n")
            for index, action in enumerate(actions, start=1):
                log.write(f"## [{index}] {action.result.name}\n\n")
                if not action.automatic:
                    message = f"Skipped manual update: {action.description}"
                    print(message)
                    log.write(message + "\n\n")
                    continue

                print(f"\n[{index}/{len(actions)}] {action.result.name}", file=sys.stderr)
                started = time.monotonic()
                try:
                    if action.kind in {"github_deb", "github_zip_deb"}:
                        returncode = self.apply_github_deb(action, log)
                    else:
                        assert action.argv is not None
                        returncode = self.runner.run_stream(action.argv, log)
                    new_version = self.installed_version(action.app)
                    verified = bool(
                        returncode == 0
                        and new_version
                        and self.compare_versions(new_version, action.result.latest) >= 0
                    )
                    elapsed = round(time.monotonic() - started, 1)
                    if verified:
                        message = (
                            f"OK: {action.result.current} → {new_version} "
                            f"({elapsed}s)"
                        )
                    else:
                        failures += 1
                        message = (
                            f"FAILED: exit={returncode}, installed={new_version or '?'} "
                            f"expected>={action.result.latest} ({elapsed}s)"
                        )
                except (OSError, UpdateError) as exc:
                    failures += 1
                    message = f"FAILED: {exc}"
                print(message, file=sys.stderr)
                log.write(message + "\n\n")

        print(f"Update log: {log_path}")
        return 1 if failures else 0

    def discover(self) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        discovery = self.config.get("discovery", {})
        if not discovery.get("enabled", True):
            return [], []

        known_deb = {
            str(app["installed"]["package"])
            for app in self.apps
            if app["installed"]["type"] == "dpkg"
        }
        known_snap = {
            str(app["installed"]["package"])
            for app in self.apps
            if app["installed"]["type"] == "snap"
        }
        known_commands = {
            str(app["installed"]["argv"][0])
            for app in self.apps
            if app["installed"]["type"] == "command"
        }
        known_commands.update(str(value) for value in discovery.get("ignore_local_bins", []))
        ignore_deb = set(str(value) for value in discovery.get("ignore_deb_packages", []))
        ignore_standalone = set(
            str(value) for value in discovery.get("ignore_standalone", [])
        )
        found: dict[tuple[str, str], dict[str, str]] = {}

        applications_dir = Path("/usr/share/applications")
        for desktop_file in sorted(applications_dir.glob("*.desktop")):
            try:
                text = desktop_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if re.search(r"^NoDisplay=true$", text, re.MULTILINE | re.IGNORECASE):
                continue
            name_match = re.search(r"^Name=(.+)$", text, re.MULTILINE)
            owner = self.runner.run(["dpkg-query", "-S", str(desktop_file)])
            if owner.returncode == 0 and ":" in owner.stdout:
                # APT-owned desktop file
                package = owner.stdout.split(":", 1)[0].split(",", 1)[0]
                if (
                    package in known_deb
                    or package in ignore_deb
                    or package.startswith(
                        (
                            "ibus",
                            "im-",
                            "language-",
                            "lib",
                            "gnome-",
                            "ubuntu-",
                            "yaru-",
                            "xdg-",
                        )
                    )
                ):
                    continue
                name = name_match.group(1).strip() if name_match else package
                version = self.runner.run(
                    ["dpkg-query", "-W", "-f=${Version}", package]
                ).stdout.strip() or "?"
                metadata = self.runner.run(
                    ["dpkg-query", "-W", "-f=${Section}\t${Homepage}", package]
                ).stdout.strip()
                section, _, homepage = metadata.partition("\t")
                found[("APT", package)] = {
                    "category": "APT",
                    "package": package,
                    "name": name,
                    "version": version,
                    "section": section,
                    "homepage": homepage,
                }
            else:
                # Standalone app: .desktop file not owned by any dpkg package
                stem = desktop_file.stem
                if stem in ignore_standalone:
                    continue
                exec_match = re.search(r"^Exec=(.+)$", text, re.MULTILINE)
                if not exec_match:
                    continue
                exec_path = _extract_exec_path(exec_match.group(1))
                if not exec_path:
                    continue
                name = name_match.group(1).strip() if name_match else stem
                found[("Standalone", stem)] = {
                    "category": "Standalone",
                    "package": stem,
                    "name": name,
                    "version": "untracked",
                }

        snap_list = self.runner.run(["snap", "list"])
        if snap_list.returncode == 0:
            for line in snap_list.stdout.splitlines()[1:]:
                fields = line.split()
                if len(fields) < 2:
                    continue
                package, version = fields[:2]
                if package in known_snap or package.startswith(
                    (
                        "bare",
                        "core",
                        "firmware-updater",
                        "gnome-",
                        "gtk-",
                        "mesa-",
                        "snap-store",
                        "snapd-desktop",
                    )
                ):
                    continue
                found[("Snap", package)] = {
                    "category": "Snap",
                    "package": package,
                    "name": package,
                    "version": version,
                }

        local_bin = Path("~/.local/bin").expanduser()
        if local_bin.is_dir():
            for path in sorted(local_bin.iterdir()):
                if path.name in known_commands or not os.access(path, os.X_OK):
                    continue
                found[("Binary", path.name)] = {
                    "category": "Binary",
                    "package": path.name,
                    "name": path.name,
                    "version": "untracked",
                }

        tracked_appimages = {
            expand_path(
                str(
                    app["installed"].get("app_path")
                    or app["installed"].get("path")
                    or ""
                )
            ).name
            for app in self.apps
            if app["installed"]["type"] in {"json_file", "appimage_asar"}
            and (app["installed"].get("app_path") or app["installed"].get("path"))
        }
        for directory_value in discovery.get("appimage_dirs", []):
            directory = expand_path(str(directory_value))
            if not directory.is_dir():
                continue
            for pattern in ("*.AppImage", "*.appimage"):
                for path in directory.rglob(pattern):
                    if path.name in tracked_appimages:
                        continue
                    found[("AppImage", path.name)] = {
                        "category": "AppImage",
                        "package": path.name,
                        "name": path.name,
                        "version": f"{path.stat().st_size} bytes",
                    }

        duplicates: list[dict[str, str]] = []
        ignored_duplicates = set(
            str(value) for value in discovery.get("ignore_duplicate_packages", [])
        )
        installed_snaps = {
            line.split()[0]
            for line in snap_list.stdout.splitlines()[1:]
            if line.split()
        }
        for package in sorted(installed_snaps):
            if package in ignored_duplicates:
                continue
            dpkg_status = self.runner.run(
                ["dpkg-query", "-W", "-f=${Status}\t${Section}", package]
            ).stdout.strip()
            status, _, section = dpkg_status.partition("\t")
            if status == "install ok installed" and section != "oldlibs":
                duplicates.append({"package": package, "sources": "APT + Snap"})

        return list(found.values()), duplicates

    def save_outputs(
        self,
        results: list[CheckResult],
        discovered: list[dict[str, str]],
        duplicates: list[dict[str, str]],
    ) -> Path:
        self.report_dir.mkdir(parents=True, exist_ok=True)
        now = dt.datetime.now().astimezone()
        timestamp = now.strftime("%Y%m%d-%H%M%S")
        report_path = self.report_dir / f"report-{timestamp}.md"
        latest_path = self.report_dir / "latest-report.md"
        summary = summarize(results)

        lines = [
            f"# Update Check Report — {now.strftime('%d/%m/%Y %H:%M:%S')}",
            "",
            "| Nguồn | Phần mềm | Hiện tại | Mới nhất | Trạng thái |",
            "|---|---|---:|---:|---|",
        ]
        labels = {
            "ok": "✅ OK",
            "update": "⚠️ Update",
            "major": "⬆️ Major",
            "unknown": "❓ Unknown",
        }
        for result in results:
            lines.append(
                f"| {result.category} | {result.name} | `{result.current}` | "
                f"`{result.latest}` | {labels[result.status]} |"
            )
        if discovered:
            lines.extend(["", "## Phần mềm phát hiện mới", ""])
            for item in discovered:
                lines.append(
                    f"- {item['category']}: **{item['name']}** "
                    f"(`{item['package']}`, {item['version']})"
                )
        if duplicates:
            lines.extend(["", "## Cài đặt trùng nguồn", ""])
            for item in duplicates:
                lines.append(f"- `{item['package']}`: {item['sources']}")
        if self.warnings:
            lines.extend(["", "## Cảnh báo", ""])
            lines.extend(f"- {warning}" for warning in self.warnings)
        recommendations = build_recommendations(
            results, discovered, duplicates, self.apps
        )
        if recommendations:
            lines.extend(["", "## Khuyến nghị", ""])
            lines.extend(recommendations)
        report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        temporary_link = self.report_dir / ".latest-report.tmp"
        with contextlib.suppress(FileNotFoundError):
            temporary_link.unlink()
        temporary_link.symlink_to(report_path)
        temporary_link.replace(latest_path)

        history_record = {
            "timestamp": now.isoformat(),
            "summary": summary,
            "results": [result.as_dict() for result in results],
            "discovered": discovered,
            "duplicates": duplicates,
            "warnings": self.warnings,
            "report": str(report_path),
        }
        with (self.report_dir / "history.jsonl").open("a", encoding="utf-8") as history:
            history.write(json.dumps(history_record, ensure_ascii=False) + "\n")
        return report_path

    def notify(self, results: list[CheckResult], report_path: Path) -> None:
        if shutil.which("notify-send") is None:
            return
        summary = summarize(results)
        if summary["major"]:
            urgency = "critical"
            message = f"{summary['major']} major, {summary['update']} updates"
        elif summary["update"]:
            urgency = "normal"
            message = f"{summary['update']} updates available"
        elif summary["unknown"]:
            urgency = "normal"
            message = f"{summary['unknown']} checks unknown"
        else:
            urgency = "low"
            message = "All tracked software is up to date"
        env = os.environ.copy()
        env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus")
        self.runner.run(
            ["notify-send", "-u", urgency, "Update Checker v3", f"{message}\n{report_path}"],
            env=env,
        )


def summarize(results: list[CheckResult]) -> dict[str, int]:
    return {
        status: sum(result.status == status for result in results)
        for status in STATUS_ORDER
    }


def print_table(
    results: list[CheckResult],
    discovered: list[dict[str, str]],
    duplicates: list[dict[str, str]],
    warnings: list[str],
) -> None:
    summary = summarize(results)
    print(f"\nUpdate Checker v{VERSION}")
    print(
        f"OK: {summary['ok']}  Update: {summary['update']}  "
        f"Major: {summary['major']}  Unknown: {summary['unknown']}  "
        f"Discovered: {len(discovered)}"
    )
    print()
    print(f"{'Nguồn':<10} {'Phần mềm':<24} {'Hiện tại':<20} {'Mới nhất':<20} TT")
    print("-" * 85)
    icons = {"ok": "✅", "update": "⚠️", "major": "⬆️", "unknown": "❓"}
    for result in results:
        print(
            f"{result.category:<10} {result.name[:23]:<24} "
            f"{result.current[:19]:<20} {result.latest[:19]:<20} {icons[result.status]}"
        )
    if discovered:
        print("\nPhần mềm phát hiện mới:")
        for item in discovered:
            print(f"- {item['category']}: {item['name']} ({item['package']})")
    if duplicates:
        print("\nCài đặt trùng nguồn:")
        for item in duplicates:
            print(f"- {item['package']}: {item['sources']}")
    if warnings:
        print("\nCảnh báo:")
        for warning in warnings:
            print(f"- {warning}")
    recommendations = build_recommendations(results, discovered, duplicates)
    if recommendations:
        print("\nKhuyến nghị:")
        for line in recommendations:
            print(line)


def print_plan(actions: list[Action]) -> None:
    if not actions:
        print("\nKế hoạch: không có cập nhật.")
        return
    print("\nKế hoạch cập nhật:")
    for index, action in enumerate(actions, start=1):
        mode = "Auto" if action.automatic else "Thủ công"
        print(
            f"{index}. {action.result.name}: {action.result.current} → "
            f"{action.result.latest} [{mode}] {action.description}"
        )


def json_payload(
    results: list[CheckResult],
    discovered: list[dict[str, str]],
    duplicates: list[dict[str, str]],
    warnings: list[str],
    report_path: Path,
) -> dict[str, Any]:
    return {
        "version": VERSION,
        "timestamp": dt.datetime.now().astimezone().isoformat(),
        "summary": summarize(results),
        "results": [result.as_dict() for result in results],
        "discovered": discovered,
        "duplicates": duplicates,
        "warnings": warnings,
        "recommendations": build_recommendations(results, discovered, duplicates),
        "report": str(report_path),
    }


def _classify_priority(
    result: CheckResult,
) -> tuple[int, str, str]:
    """Return (sort_key, emoji, label) for a result that needs updating."""
    if result.status == "major" or result.app_id in HIGH_PRIORITY_IDS:
        return (0, "🔴", "Ưu tiên cao")
    if result.app_id in MEDIUM_PRIORITY_IDS:
        return (1, "🟡", "Ưu tiên trung bình")
    return (2, "🟢", "Ưu tiên thấp")


def _update_command_hint(result: CheckResult, apps: list[dict[str, Any]] | None) -> str:
    """Return a short CLI hint for updating *result*."""
    app: dict[str, Any] | None = None
    if apps:
        for candidate in apps:
            if str(candidate["id"]) == result.app_id:
                app = candidate
                break
    if app is None:
        return ""
    spec = app["update"]
    update_type = str(spec["type"])
    if update_type == "apt":
        return f"sudo apt-get install --only-upgrade -y {spec['package']}"
    if update_type == "snap":
        return f"sudo snap refresh {spec['package']}"
    if update_type == "command":
        return shlex.join([str(v) for v in spec["argv"]])
    if update_type in {"github_deb", "github_zip_deb"}:
        return "check-all-updates --apply"
    if update_type == "manual":
        return f"Cập nhật thủ công: {spec['url']}"
    return ""


def _parse_github_repo(url: str) -> str:
    """Extract 'owner/repo' from a GitHub URL, or return empty string."""
    match = re.match(
        r"https?://github\.com/([^/]+/[^/]+?)(?:\.git)?/?$", url.strip()
    )
    return match.group(1) if match else ""


def _classify_discovered(item: dict[str, str]) -> tuple[str, str]:
    """Return (action_emoji, recommendation_text) for a discovered item."""
    category = item.get("category", "")
    section = item.get("section", "").rsplit("/", 1)[-1]  # "universe/utils" → "utils"
    homepage = item.get("homepage", "")

    if category == "Standalone":
        return ("⚙️", "Nên thêm vào config.toml (standalone)")

    if category == "Binary":
        return ("❓", "Nên thêm vào config.toml hoặc ignore_local_bins")

    if category in {"Snap", "AppImage"}:
        return ("❓", f"Nên thêm vào config.toml hoặc ignore")

    # APT packages: classify by section
    if section in IGNORE_SECTIONS:
        return ("🔇", f"Nên ignore (section: {section})")

    # Input method frameworks
    if section in {"utils", "kde", "misc"} and any(
        keyword in item.get("package", "").lower()
        for keyword in ("fcitx", "ibus", "scim", "uim", "input")
    ):
        return ("🔇", "Nên ignore (input method)")

    if section in TRACK_SECTIONS:
        if "github.com" in homepage:
            repo = _parse_github_repo(homepage)
            hint = f" — GitHub: {repo}" if repo else ""
            return ("⚙️", f"Nên thêm config{hint}")
        return ("⚙️", f"Nên thêm vào config.toml (section: {section})")

    return ("❓", "Cần review thủ công")


def _count_discovery_streak(
    history_path: Path, n: int = 5,
) -> dict[str, int]:
    """Count consecutive appearances of each discovered package in last N runs.

    Returns: {"fcitx5": 4, "antigravity-ide": 1, ...}
    """
    if not history_path.exists():
        return {}
    raw_lines = history_path.read_text(encoding="utf-8").splitlines()
    recent = [line for line in raw_lines[-n:] if line.strip()]
    if not recent:
        return {}

    # Build per-run sets of discovered package names
    run_sets: list[set[str]] = []
    for line in recent:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        discovered = record.get("discovered", [])
        pkgs = {item.get("package", "") for item in discovered if item.get("package")}
        run_sets.append(pkgs)

    if not run_sets:
        return {}

    # Count streaks from latest run backward
    latest_pkgs = run_sets[-1]
    streaks: dict[str, int] = {}
    for pkg in latest_pkgs:
        count = 0
        for run in reversed(run_sets):
            if pkg in run:
                count += 1
            else:
                break
        streaks[pkg] = count
    return streaks


def _partition_discovered(
    discovered: list[dict[str, str]],
    streaks: dict[str, int],
    threshold: int = 3,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Split discovered items into (new_items, muted_items).

    new_items: streak < threshold (show in main table)
    muted_items: streak >= threshold (show in collapsed section)
    """
    new_items: list[dict[str, str]] = []
    muted_items: list[dict[str, str]] = []
    for item in discovered:
        pkg = item.get("package", "")
        streak = streaks.get(pkg, 0)
        section = item.get("section", "")
        # Auto-mute IGNORE_SECTIONS items regardless of streak
        base_section = section.rsplit("/", 1)[-1] if section else ""
        if streak >= threshold or base_section in IGNORE_SECTIONS:
            muted_items.append(item)
        else:
            new_items.append(item)
    return new_items, muted_items


def build_recommendations(
    results: list[CheckResult],
    discovered: list[dict[str, str]],
    duplicates: list[dict[str, str]],
    apps: list[dict[str, Any]] | None = None,
    history_path: Path | None = None,
) -> list[str]:
    """Build recommendation lines for the report."""
    updates = [r for r in results if r.status in {"update", "major"}]
    if not updates and not discovered and not duplicates:
        return []

    lines: list[str] = []

    if updates:
        buckets: dict[str, list[CheckResult]] = {}
        for result in updates:
            _key, _emoji, label = _classify_priority(result)
            buckets.setdefault(label, []).append(result)

        priority_order = ["Ưu tiên cao", "Ưu tiên trung bình", "Ưu tiên thấp"]
        emoji_map = {
            "Ưu tiên cao": "🔴",
            "Ưu tiên trung bình": "🟡",
            "Ưu tiên thấp": "🟢",
        }
        for label in priority_order:
            group = buckets.get(label)
            if not group:
                continue
            lines.append(f"### {emoji_map[label]} {label}")
            lines.append("")
            for result in group:
                lines.append(
                    f"- **{result.name}** `{result.current}` → `{result.latest}`"
                )
            commands: list[str] = []
            for result in group:
                hint = _update_command_hint(result, apps)
                if hint and hint not in commands:
                    commands.append(hint)
            if commands:
                lines.append("")
                lines.append("> ```")
                for cmd in commands:
                    lines.append(f"> {cmd}")
                lines.append("> ```")
            lines.append("")

    if discovered:
        # Partition into new vs muted based on history streaks
        if history_path:
            streaks = _count_discovery_streak(history_path)
            new_items, muted_items = _partition_discovered(discovered, streaks)
        else:
            new_items, muted_items = discovered, []

        if new_items:
            lines.append("### 📦 Phần mềm phát hiện mới")
            lines.append("")
            lines.append("| Phần mềm | Package | Loại | Gợi ý |")
            lines.append("|---|---|---|---|")
            for item in new_items:
                emoji, suggestion = _classify_discovered(item)
                category = item.get("category", "")
                section = item.get("section", "")
                if section:
                    type_label = section.rsplit("/", 1)[-1]
                else:
                    type_label = category
                lines.append(
                    f"| {item['name']} | `{item['package']}` | "
                    f"{type_label} | {emoji} {suggestion} |"
                )
            lines.append("")

        if muted_items:
            lines.append("### 📋 Phần mềm đã biết (xuất hiện 3+ lần)")
            lines.append("")
            for item in muted_items:
                pkg = item.get("package", "")
                cat = item.get("category", "")
                lines.append(f"- `{pkg}` ({cat})")
            lines.append("")

    if duplicates:
        lines.append("### ⚠️ Cài đặt trùng nguồn")
        lines.append("")
        for item in duplicates:
            lines.append(
                f"- `{item['package']}` ({item['sources']}): "
                f"gỡ bản không dùng để tránh xung đột"
            )
        lines.append("")

    return lines


def print_history(report_dir: Path, limit: int) -> int:
    path = report_dir / "history.jsonl"
    if not path.exists():
        print("Chưa có lịch sử.")
        return 0
    lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    for line in lines:
        record = json.loads(line)
        summary = record["summary"]
        print(
            f"{record['timestamp']}  OK={summary['ok']} Update={summary['update']} "
            f"Major={summary['major']} Unknown={summary['unknown']}"
        )
    return 0


def acquire_lock(report_dir: Path) -> TextIO:
    report_dir.mkdir(parents=True, exist_ok=True)
    handle = (report_dir / ".update-checker.lock").open("w", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise UpdateError("another update-checker process is already running") from exc
    handle.write(str(os.getpid()))
    handle.flush()
    return handle

_IGNORE_KEY_MAP = {
    "APT": "ignore_deb_packages",
    "Standalone": "ignore_standalone",
    "Binary": "ignore_local_bins",
}

_TRACK_TEMPLATE_APT = """\n# --- Added by --accept-discovery {timestamp} ---
[[apps]]
id = "{app_id}"
name = "{name}"
installed = {{ type = "dpkg", package = "{package}" }}
latest = {{ type = "apt", package = "{package}" }}
update = {{ type = "apt", package = "{package}" }}
"""

_TRACK_TEMPLATE_STANDALONE = """\n# --- Added by --accept-discovery {timestamp} ---
# TODO: Fill in correct latest/update sources for {name}
[[apps]]
id = "{app_id}"
name = "{name}"
installed = {{ type = "command", command = ["{exec_path}", "--version"], regex = "([0-9]+\\\\.[0-9]+\\\\.[0-9]+)" }}
latest = {{ type = "github", repo = "OWNER/REPO" }}
update = {{ type = "manual" }}
"""


def _insert_into_toml_array(text: str, section: str, key: str,
                            value: str) -> str:
    """Insert a value into a TOML array within a section, preserving format.

    Handles: existing array with items, empty array, missing key, missing section.
    """
    quoted = f'  "{value}"'
    # Try to find the key within the section
    # Pattern: key = [... ] possibly multiline
    section_pattern = re.compile(
        rf"^\[{re.escape(section)}\]\s*$", re.MULTILINE
    )
    section_match = section_pattern.search(text)
    if not section_match:
        # Section doesn't exist — append both section and key
        return text.rstrip() + f"\n\n[{section}]\n{key} = [\n{quoted},\n]\n"

    # Find the key within this section (before next section or EOF)
    section_start = section_match.end()
    next_section = re.search(r"^\[", text[section_start:], re.MULTILINE)
    section_end = section_start + next_section.start() if next_section else len(text)
    section_text = text[section_start:section_end]

    # Look for key = [...] pattern
    key_pattern = re.compile(
        rf"^({re.escape(key)}\s*=\s*\[)(.*?)(\])",
        re.MULTILINE | re.DOTALL,
    )
    key_match = key_pattern.search(section_text)
    if not key_match:
        # Key doesn't exist — insert after section header
        insert_pos = section_start
        # Skip to end of section header line
        newline = text.find("\n", section_start)
        if newline != -1:
            insert_pos = newline + 1
        return (
            text[:insert_pos]
            + f"{key} = [\n{quoted},\n]\n"
            + text[insert_pos:]
        )

    # Key exists — insert value before closing bracket
    abs_start = section_start + key_match.start()
    abs_end = section_start + key_match.end()
    existing = key_match.group(2).strip()
    if existing:
        # Non-empty array: insert before closing ]
        close_bracket_pos = section_start + key_match.start(3)
        before = text[:close_bracket_pos].rstrip()
        # Strip trailing comma to avoid double comma
        if before.endswith(","):
            before = before[:-1]
        return (
            before
            + f",\n{quoted},\n"
            + text[close_bracket_pos:]
        )
    else:
        # Empty array: key = []
        return (
            text[:abs_start]
            + f"{key} = [\n{quoted},\n]"
            + text[abs_end:]
        )


def _accept_discovery(config_path: Path, spec: str, report_dir: Path,
                      yes: bool = False) -> int:
    """Process --accept-discovery <id>:<track|ignore>.

    Reads the latest history entry to find the discovered app metadata,
    then modifies config.toml accordingly.
    """
    if ":" not in spec:
        raise ConfigError(
            f"invalid format: '{spec}' — expected id:track or id:ignore"
        )
    app_id, _, action = spec.partition(":")
    if action not in ("track", "ignore"):
        raise ConfigError(
            f"unknown action: '{action}' — expected 'track' or 'ignore'"
        )

    # Find app in latest history
    history_path = report_dir / "history.jsonl"
    if not history_path.exists():
        raise ConfigError(
            "no history found — run check-all-updates first"
        )
    last_line = ""
    with history_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                last_line = line
    if not last_line:
        raise ConfigError("history file is empty")
    record = json.loads(last_line)
    discovered = record.get("discovered", [])
    match = None
    for item in discovered:
        if item.get("package") == app_id:
            match = item
            break
    if match is None:
        raise ConfigError(
            f"'{app_id}' not found in latest discovery "
            f"({len(discovered)} items). Run check-all-updates first."
        )

    # Read config
    if not config_path.exists():
        raise ConfigError(f"config not found: {config_path}")
    original = config_path.read_text(encoding="utf-8")

    category = match.get("category", "APT")
    name = match.get("name", app_id)
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    if action == "ignore":
        ignore_key = _IGNORE_KEY_MAP.get(category, "ignore_deb_packages")
        modified = _insert_into_toml_array(original, "discovery", ignore_key, app_id)
    else:  # track
        package = match.get("package", app_id)
        if category == "APT":
            block = _TRACK_TEMPLATE_APT.format(
                timestamp=now, app_id=app_id, name=name, package=package,
            )
        else:
            exec_path = f"/opt/{app_id}/{app_id}"
            block = _TRACK_TEMPLATE_STANDALONE.format(
                timestamp=now, app_id=app_id, name=name, exec_path=exec_path,
            )
        modified = original.rstrip() + "\n" + block

    # Show diff preview
    if not yes:
        print(f"\n--- {config_path} (before)")
        print(f"+++ {config_path} (after)")
        orig_lines = original.splitlines(keepends=True)
        mod_lines = modified.splitlines(keepends=True)
        diff = difflib.unified_diff(orig_lines, mod_lines, lineterm="")
        for line in diff:
            print(line, end="")
        print()
        try:
            confirm = input("Apply changes? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            return 1
        if confirm != "y":
            print("Cancelled.")
            return 1

    # Backup and write
    backup = config_path.with_suffix(".toml.bak")
    shutil.copy2(config_path, backup)
    config_path.write_text(modified, encoding="utf-8")
    print(f"✅ Config updated: {action} {app_id}")
    print(f"   Backup: {backup}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check-all-updates",
        description="Check and update workstation software",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--quick", action="store_true", help="use cached APT metadata")
    parser.add_argument("--json", action="store_true", help="emit pure JSON on stdout")
    parser.add_argument("--apply", action="store_true", help="apply available updates")
    parser.add_argument("--yes", action="store_true", help="skip apply confirmation")
    parser.add_argument("--dry-run", action="store_true", help="show update plan only")
    parser.add_argument("--notify", action="store_true", help="send desktop notification")
    parser.add_argument("--no-discovery", action="store_true", help="skip discovery")
    parser.add_argument("--history", nargs="?", const=10, type=int, metavar="N")
    parser.add_argument(
        "--accept-discovery", metavar="ID:ACTION",
        help="accept discovered app (id:track or id:ignore)",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.apply and args.json:
        parser.error("--apply cannot be combined with --json")
    if args.apply and args.dry_run:
        parser.error("--apply cannot be combined with --dry-run")
    if args.yes and not args.apply and not args.accept_discovery:
        parser.error("--yes requires --apply")

    try:
        config = load_config(args.config.expanduser())
        checker = UpdateChecker(config, quiet=args.json)
        if args.history is not None:
            return print_history(checker.report_dir, max(1, args.history))
        if args.accept_discovery:
            return _accept_discovery(
                args.config.expanduser(), args.accept_discovery,
                checker.report_dir, yes=args.yes or False,
            )

        with acquire_lock(checker.report_dir):
            if not args.quick:
                checker.refresh_apt_cache()
            results = checker.check_all()
            if args.no_discovery:
                discovered, duplicates = [], []
            else:
                discovered, duplicates = checker.discover()
            report_path = checker.save_outputs(results, discovered, duplicates)
            actions = checker.build_actions(results)

            if args.json:
                print(
                    json.dumps(
                        json_payload(
                            results,
                            discovered,
                            duplicates,
                            checker.warnings,
                            report_path,
                        ),
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            else:
                print_table(results, discovered, duplicates, checker.warnings)

            if args.dry_run:
                print_plan(actions)
            if args.notify:
                checker.notify(results, report_path)
            if args.apply:
                return checker.apply_actions(actions, assume_yes=args.yes)
        return 0
    except (ConfigError, UpdateError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
