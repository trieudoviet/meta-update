# 🔄 Hướng Dẫn Sử Dụng: Update Checker v3

> **Ngày nâng cấp:** 27/07/2026  
> **Lệnh:** `~/.local/bin/check-all-updates`  
> **Phiên bản:** v3.0.0 — Python core · TOML config · Verified updates  
> **Lịch tự động:** 16:00 giờ Việt Nam mỗi ngày

---

## Tổng quan

Update Checker kiểm tra phần mềm được cài từ APT, Snap, GitHub Releases,
npm, standalone CLI và AppImage. Phiên bản v3 ưu tiên ba mục tiêu:

1. Không chạy lệnh update thông qua `eval`.
2. Chỉ báo thành công khi version sau update đạt version mục tiêu.
3. Output JSON, lịch chạy và log có thể dùng ổn định cho automation.

Hiện cấu hình theo dõi 23 ứng dụng. RustDesk và Flameshot có thể tự tải
GitHub asset phù hợp với Ubuntu 24.04/amd64, kiểm SHA-256, kiểm metadata
gói rồi cài bằng APT.

---

## Cách sử dụng

### Kiểm tra

```bash
# Refresh APT metadata nếu đã có sudo session, sau đó kiểm tra tất cả
check-all-updates

# Dùng APT cache, phù hợp cho kiểm tra nhanh
check-all-updates --quick

# JSON thuần; có thể pipe trực tiếp vào jq
check-all-updates --quick --json

# Bỏ bước auto-discovery
check-all-updates --quick --no-discovery
```

Check-only trả exit code `0` nếu quy trình kiểm tra hoàn thành. Việc có bản
update được thể hiện trong `summary`, không bị coi là lỗi systemd.

### Xem kế hoạch và cập nhật

```bash
# Chỉ xem action sẽ chạy
check-all-updates --quick --dry-run

# Cập nhật tuần tự, hỏi xác nhận trước khi chạy
check-all-updates --quick --apply

# Không hỏi xác nhận; bắt buộc khi chạy ngoài terminal
check-all-updates --quick --apply --yes
```

Quy tắc an toàn:

- `--apply` ngoài terminal sẽ từ chối chạy nếu thiếu `--yes`.
- APT/Snap/GitHub `.deb` yêu cầu sudo.
- Một app lỗi không chặn app tiếp theo.
- `Same version` và `Unverified` không còn được tính là thành công.
- Lock riêng ngăn hai phiên Update Checker chạy chồng nhau.
- Download GitHub có timeout riêng, retry/backoff và tự xóa partial file.
- `--json` không được kết hợp với `--apply`.

### Xem lịch sử

```bash
check-all-updates --history
check-all-updates --history 30
```

---

## Đọc trạng thái

| Trạng thái | Ý nghĩa |
|---|---|
| ✅ OK | Version hiện tại bằng hoặc mới hơn nguồn latest |
| ⚠️ Update | Có bản mới trong cùng major version |
| ⬆️ Major | Có major version mới; không đồng nghĩa với lỗi bảo mật |
| ❓ Unknown | Không lấy được version hiện tại/latest |

V3 không dùng nhãn “Critical” cho mọi major update vì major version không
phản ánh mức độ nghiêm trọng bảo mật.

---

## Cấu hình

File cấu hình:

```text
~/.config/update-checker/config.toml
```

Mỗi app chỉ có một identity, gồm ba adapter:

```toml
[[apps]]
id = "example"
name = "Example"
installed = { type = "dpkg", package = "example" }
latest = { type = "github", repo = "owner/example" }
update = { type = "github_deb", repo = "owner/example", package = "example", asset_regex = '^example-{version}-amd64\.deb$', require_digest = true }
```

- `installed`: cách lấy version hiện tại.
- `latest`: nguồn version mới nhất.
- `update`: action cập nhật.

Các loại adapter hiện có:

| Vai trò | Adapter |
|---|---|
| Installed | `dpkg`, `snap`, `command`, `json_file`, `appimage_asar` |
| Latest | `apt`, `snap`, `npm`, `github` |
| Update | `apt`, `snap`, `command`, `github_deb`, `github_zip_deb`, `manual` |

Nếu đặt biến môi trường `GITHUB_TOKEN`, v3 tự gửi token cho GitHub API.

---

## Verified GitHub update

Quy trình áp dụng cho RustDesk và Flameshot:

1. Đọc release stable mới nhất từ GitHub API.
2. Chọn đúng một asset theo regex, OS và kiến trúc máy.
3. Tải vào thư mục tạm riêng.
4. So SHA-256 với trường `digest` của GitHub.
5. Với ZIP của Flameshot, kiểm thêm checksum `.deb` bên trong.
6. Xác minh package name, version và architecture bằng `dpkg-deb`.
7. Cài bằng `sudo apt-get install`.
8. Đọc lại version và chỉ ghi thành công khi đạt version mục tiêu.

---

## Systemd user timer

V3 không còn dùng cron. Timer đang chạy theo timezone local của hệ thống:

```ini
OnCalendar=*-*-* 16:00:00
Persistent=true
```

`Persistent=true` giúp chạy bù khi máy tắt đúng thời điểm 16:00.

Các lệnh quản lý:

```bash
systemctl --user list-timers update-checker.timer --all
systemctl --user status update-checker.timer
systemctl --user status update-checker.service
journalctl --user -u update-checker.service -n 100 --no-pager

# Chạy thử ngay
systemctl --user start update-checker.service
```

File unit:

```text
~/.config/systemd/user/update-checker.service
~/.config/systemd/user/update-checker.timer
```

Timer chỉ thực hiện check và gửi desktop notification; không tự động apply.

---

## Dữ liệu và log

```text
~/.local/share/update-checker/
├── history.jsonl
├── latest-report.md
├── report-v3-YYYYMMDD-HHMMSS.md
├── update-v3-YYYYMMDD-HHMMSS.log
└── .update-checker.lock
```

- `history.jsonl`: một JSON record cho mỗi lần check.
- `latest-report.md`: symlink tới report mới nhất.
- `update-v3-*`: output và kết quả verify của mỗi lần apply.
- Journal của timer nằm trong systemd user journal.

---

## Cấu trúc chương trình

```text
~/.local/bin/check-all-updates
    └── launcher

~/.local/lib/update-checker/update_checker.py
    └── Python 3.12 core

~/.config/update-checker/config.toml
    └── inventory và adapter config
```

V3 chỉ dùng Python standard library và các công cụ hệ thống sẵn có:
`dpkg`, `apt`, `snap`, `7z`, `sudo` và `notify-send`.

---

## Rollback

Bản Bash v2 được giữ tại:

```text
~/.local/bin/check-all-updates-v2.20260727
```

Khôi phục tạm thời:

```bash
cp ~/.local/bin/check-all-updates-v2.20260727 ~/.local/bin/check-all-updates
chmod +x ~/.local/bin/check-all-updates
```

Backup crontab trước migration:

```text
~/.local/share/update-checker/crontab-before-v3-20260727.txt
```

---

## Các cải tiến để sau

- Cleanup APT/Snap chỉ triển khai dưới dạng opt-in.
- Bổ sung test fixture cho lỗi mạng, GitHub rate limit và package install failure.
- Có thể thêm ETag/cache cho GitHub nếu số repo tăng đáng kể.

#tools #linux #automation #maintenance #update-checker
