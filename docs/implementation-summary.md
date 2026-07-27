# Update Checker — Implementation Summary

- **Runtime:** Python `3.12`, không có dependency Python bên thứ ba

## Tính năng chính

- Python core và TOML inventory thay vì Bash monolith.
- Launcher tương thích tại `~/.local/bin/check-all-updates`.
- Mỗi app là một identity duy nhất; không đếm trùng.
- Không sử dụng `eval`; mọi lệnh chạy qua argv có cấu trúc.
- JSON stdout (`--json`) tương thích `jq`.
- `--apply` ngoài TTY fail-closed nếu thiếu `--yes`.
- Verification yêu cầu version sau update đạt version mục tiêu.
- Lock chống chạy chồng, `--dry-run`, history JSONL và duplicate detector.
- RustDesk/Flameshot dùng verified GitHub asset updater với SHA-256 và `.deb` metadata validation.
- Obsidian version được đọc trực tiếp từ `resources/app.asar` trong AppImage.
- Systemd user timer lúc `16:00` local với `Persistent=true`.

## Kiểm thử

- `python3 -m unittest discover`: 11/11 test pass.
- Python bytecode compilation: pass.
- TOML config: 23 app IDs, không trùng.
- Full live check: `OK=23`, `Update=0`, `Major=0`, `Unknown=0`.
- Auto-discovery: chỉ còn `winbox`, khớp kỳ vọng.
- Live GitHub asset resolution:
  - RustDesk `1.4.9`: tải thật, hash và package metadata đều hợp lệ.
  - Flameshot `14.0.0`: tải thật, outer/inner hash và package metadata đều hợp lệ.
- API timeout và download timeout được tách riêng; download có ba lần retry,
  backoff và tự xóa partial file.
- Systemd service chạy thử: `status=0/SUCCESS`.
- Timer: `enabled`, `active`, lần chạy kế tiếp `16:00 +07`.

## File chính

| File | Vai trò |
|---|---|
| `~/.local/bin/check-all-updates` | Launcher |
| `~/.local/lib/update-checker/update_checker.py` | Core |
| `~/.config/update-checker/config.toml` | Inventory/config |
| `~/.config/systemd/user/update-checker.service` | Check service |
| `~/.config/systemd/user/update-checker.timer` | Lịch 16:00 |
| `~/.local/lib/update-checker/tests/test_update_checker.py` | Unit tests |

#history #update-checker #python #systemd #ubuntu
