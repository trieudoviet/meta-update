# 🔄 Meta Update (Update Checker)

[![Python Version](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-Ubuntu%20Linux-orange.svg)](https://ubuntu.com/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Systemd](https://img.shields.io/badge/automation-systemd%20timer-brightgreen.svg)](systemd/)

**Meta Update** is a lightweight, local-first software inventory manager and verified update automation tool for Ubuntu Linux workstations. It unifies update tracking across multiple package managers and distribution formats into a single, clean dashboard and JSON API.

---

## ✨ Features

- **Multi-Source Inventory Tracking**: Monitors software from **APT**, **Snap**, **GitHub Releases**, **npm**, **Standalone CLI binaries**, **AppImage**, and **HTTP scrape endpoints**.
- **Verified GitHub Updates**: Automatically downloads GitHub release assets (e.g. RustDesk, Flameshot), verifies SHA-256 hashes, extracts inner archive checksums, validates `.deb` metadata with `dpkg-deb`, and installs cleanly via `apt`.
- **Zero Third-Party Python Dependencies**: Built entirely using Python 3.12 standard library and native OS tools (`dpkg`, `apt`, `snap`, `7z`, `systemd`, `notify-send`).
- **Safe Execution & Fail-Closed Design**:
  - No `eval` or shell injection vectors — all commands execute via structured `argv` arrays.
  - Verification checks require the post-update installed version to match or exceed the target version.
  - Interactive confirmation prompts for `--apply` mode, failing closed in non-interactive sessions unless `--yes` is specified.
  - File locking prevents concurrent update checker instances.
- **Smart Auto-Discovery & Management**:
  - Scans system `.desktop` entries and local binaries with wrapper support (`env VAR=...`, `sh -c`, `/usr/bin/env`).
  - CLI management via `--accept-discovery <id>:<track|ignore>`: automatically edits `config.toml` (safe array insertion & skeleton generation), creates `.bak` backups, and shows unified diff preview.
  - Noise reduction / soft muting: separates frequently recurring items (3+ appearances) and system sections (`libs`, `debug`, `fonts`) into a collapsed list.
- **Optional AI-Assisted Intelligence (`--ai`)**:
  - Zero external dependencies: uses stdlib `urllib.request` + `json` to integrate with **DeepSeek**, **Google Gemini**, **Groq**, or **OpenAI**.
  - Automated Vietnamese changelog summaries for GitHub releases.
  - CVE vulnerability lookup & severity rating via `cve.circl.lu`.
  - Local JSON caching (7-day TTL) for high speed and minimal token usage.
  - Fail-open design: gracefully falls back if API keys are missing or offline.
- **Automation-Friendly**: Clean JSON output (`--json`) for scripting/piping into `jq` or external dashboards.
- **Systemd User Timer & Desktop Notifications**: Native background checks at scheduled times with desktop alert popups.

---

## 📁 Project Structure

```text
meta-update/
├── bin/                 # Executable entry point (check-all-updates launcher)
├── src/                 # Python 3.12 core engine
│   ├── update_checker.py # Core update checking & discovery engine
│   └── ai_client.py     # Lightweight stdlib AI adapter (DeepSeek/Gemini/OpenAI)
├── tests/               # Unit test suite
│   ├── test_update_checker.py
│   └── test_ai_client.py
└── docs/                # Detailed operational guides and architecture notes
```

---

## 🚀 Quick Start

### 1. Requirements

- Ubuntu 22.04 LTS / 24.04 LTS (or compatible Debian-based Linux)
- Python 3.12+
- Tools: `dpkg`, `apt-get`, `snap`, `7z` (optional, for zip/deb inspection)

### 2. Local Run

Clone the repository and run directly without installation:

```bash
# Quick check using existing package metadata cache
./bin/check-all-updates --quick

# Perform a full check (refreshes APT cache if non-interactive sudo is active)
./bin/check-all-updates

# Run with optional AI analysis (changelog summary & CVE check)
./bin/check-all-updates --quick --ai

# Accept or ignore a newly discovered app directly from CLI
./bin/check-all-updates --accept-discovery fcitx5:ignore
./bin/check-all-updates --accept-discovery myapp:track

# Output machine-readable JSON
./bin/check-all-updates --quick --json

# Preview update execution plan without making changes
./bin/check-all-updates --quick --dry-run
```

---

## 🛠️ Installation & System Setup

To install the utility for your user account:

```bash
# Create local directory structure
mkdir -p ~/.local/bin ~/.local/lib/update-checker ~/.config/update-checker

# Copy core files
cp bin/check-all-updates ~/.local/bin/
cp src/update_checker.py src/ai_client.py ~/.local/lib/update-checker/
chmod +x ~/.local/bin/check-all-updates
```

### Enable Daily Background Timer (Systemd)

Automatically check for updates daily at 16:00 (local time) and send desktop notifications:

```bash
# Install systemd user service & timer
mkdir -p ~/.config/systemd/user
cp systemd/update-checker.service ~/.config/systemd/user/
cp systemd/update-checker.timer ~/.config/systemd/user/

# Reload systemd user daemon & enable timer
systemctl --user daemon-reload
systemctl --user enable --now update-checker.timer

# Verify timer status
systemctl --user list-timers update-checker.timer
```

---

## ⚙️ Configuration (`config.toml`)

Applications and optional AI settings are configured in `~/.config/update-checker/config.toml`:

```toml
schema_version = 1
report_dir = "~/.local/share/update-checker"
github_token_env = "GITHUB_TOKEN"

[discovery]
enabled = true
appimage_dirs = ["~/Apps", "~/Applications", "~/Desktop"]
ignore_deb_packages = ["fcitx5", "totem", "yelp"]
ignore_local_bins = ["python3", "7zz"]

# --- AI-Assisted Analysis (Optional) ---
[ai]
enabled = false                    # Set to true to activate
provider = "deepseek"              # deepseek, openai, groq, gemini
api_key_env = "DEEPSEEK_API_KEY"   # Name of environment variable holding API key
model = "deepseek-chat"            # e.g., deepseek-chat, gemini-2.5-flash
timeout_seconds = 10
features = ["changelog", "cve"]    # changelog: release notes, cve: CVE lookup

[[apps]]
id = "google-chrome"
name = "Google Chrome"
installed = { type = "dpkg", package = "google-chrome-stable" }
latest = { type = "apt", package = "google-chrome-stable" }
update = { type = "apt", package = "google-chrome-stable" }

[[apps]]
id = "rustdesk"
name = "RustDesk"
installed = { type = "dpkg", package = "rustdesk" }
latest = { type = "github", repo = "rustdesk/rustdesk" }
update = {
  type = "github_deb",
  repo = "rustdesk/rustdesk",
  package = "rustdesk",
  asset_regex = '^rustdesk-{version}-x86_64\.deb$',
  require_digest = true
}
```

### Supported Source Adapters

| Role | Supported Adapters |
|---|---|
| **Installed Detection** | `dpkg`, `snap`, `command`, `json_file`, `appimage_asar` |
| **Latest Lookup** | `apt`, `snap`, `npm`, `github`, `http_scrape` |
| **Update Mechanism** | `apt`, `snap`, `command`, `github_deb`, `github_zip_deb`, `manual` |

---

## 📖 Command Line Reference

```text
usage: check-all-updates [-h] [--config PATH] [--quick] [--json] [--apply] [--yes]
                         [--dry-run] [--notify] [--no-discovery] [--history [N]]
                         [--accept-discovery ID:ACTION] [--ai] [--version]

Options:
  --config PATH                 Path to TOML config file (default: ~/.config/update-checker/config.toml)
  --quick                       Skip APT metadata refresh (fast execution)
  --json                        Output clean JSON payload to stdout
  --dry-run                     Display proposed update commands without executing
  --apply                       Sequentially execute automatic updates
  --yes                         Skip confirmation prompt (required for non-interactive --apply)
  --notify                      Send desktop notification via notify-send
  --no-discovery                Skip scanning for untracked desktop apps/binaries
  --history [N]                 View recent check execution history log
  --accept-discovery ID:ACTION  Accept discovered app into config (id:track or id:ignore)
  --ai                          Enable AI-assisted analysis (release summaries & CVE lookup)
  --version                     Show program's version number and exit
```

---

## 🧪 Testing

Run the built-in unit test suite (80+ test cases covering parsing, discovery, CLI actions, and AI adapters):

```bash
python3 -m unittest discover -s tests -v
```

---

## 📘 Documentation

- [Hướng dẫn sử dụng chi tiết (Vietnamese Usage Guide)](docs/Hướng-dẫn-check-all-updates.md)
- [Brainstorm Discovery & AI](docs/brainstorm-discovery-va-ai.md)

---

## 📄 License

Distributed under the MIT License. See `LICENSE` for details.
