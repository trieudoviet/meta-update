# Update Checker v3 — Implementation Summary

- **Ngày triển khai:** 27/07/2026
- **Phiên bản:** `3.0.0`
- **Runtime:** Python `3.12`, không có dependency Python bên thứ ba

## Kết quả

- Thay Bash monolith bằng Python core và TOML inventory.
- Giữ launcher tương thích tại `~/.local/bin/check-all-updates`.
- Giữ bản rollback v2 tại `~/.local/bin/check-all-updates-v2.20260727`.
- Hợp nhất mỗi app thành một identity; không còn đếm RustDesk/Flameshot hai lần.
- Thay `eval` bằng argv có cấu trúc.
- JSON stdout đã được kiểm bằng `jq`.
- `--apply` ngoài TTY fail-closed nếu thiếu `--yes`.
- Verification yêu cầu version sau update đạt version mục tiêu.
- Thêm lock chống chạy chồng, `--dry-run`, history JSONL và duplicate detector.
- RustDesk/Flameshot dùng verified GitHub asset updater với SHA-256 và `.deb` metadata validation.
- Obsidian version được đọc trực tiếp từ `resources/app.asar` trong AppImage.
- Chuyển cron sang systemd user timer lúc `16:00` local với `Persistent=true`.

## Kiểm thử

- `python3 -m unittest discover`: 11/11 test pass.
- Python bytecode compilation: pass.
- TOML config: 23 app IDs, không trùng.
- Full live check: `OK=23`, `Update=0`, `Major=0`, `Unknown=0`.
- Auto-discovery: chỉ còn `winbox`, khớp kỳ vọng từ v2.
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
| `/home/pavel/.local/bin/check-all-updates` | Launcher v3 |
| `/home/pavel/.local/lib/update-checker/update_checker.py` | Core |
| `/home/pavel/.config/update-checker/config.toml` | Inventory/config |
| `/home/pavel/.config/systemd/user/update-checker.service` | Check service |
| `/home/pavel/.config/systemd/user/update-checker.timer` | Lịch 16:00 |
| `/home/pavel/.local/lib/update-checker/tests/test_update_checker.py` | Unit tests |

#history #update-checker #python #systemd #ubuntu
