# Quyết định tài liệu

## Tách tài liệu theo người đọc

**Chọn:** Giữ README là điểm vào. Đặt bản đồ mã nguồn và vòng đời tác vụ trong [Kiến trúc](docs/architecture.md); đặt triển khai, cấu hình, restart và chẩn đoán trong [Runbook](docs/operations.md). Cả hai viết bằng tiếng Việt; README giữ ngôn ngữ hiện có.

**Không chọn:** Dồn toàn bộ nội dung vào README hoặc tạo tài liệu riêng cho từng adapter. Cách đầu làm khó tìm chỉ dẫn khi có sự cố; cách sau lặp lifecycle chung và dễ lệch nguồn. Các hướng dẫn plugin và script thiết lập hiện có được dẫn link thay vì chép lại.

## Dùng graph để định tuyến việc đọc, không coi là trace runtime

**Chọn:** Build graph bằng CLI local, đọc luồng `mirror`, tìm caller/callee của điểm chuyển pha rồi đối chiếu với source tại snapshot `41cd54b`. Dẫn link vào nguồn ở các nhánh quan trọng. Database graph chỉ dùng cục bộ.

**Không chọn:** Xuất wiki tự động làm tài liệu gốc. Quan hệ tĩnh không xác định thứ tự `if/elif`, trạng thái queue, callback động hay cấu hình lúc chạy; đây lại là những điều người vận hành cần nhất.

## Tách build, cấu hình và công khai web

**Chọn:** Hướng dẫn build từ checkout sạch, chỉ tạo file chứa credential sau khi build, rồi khởi động `app` với `--no-build`. Quick Tunnel là bước riêng khi thật sự cần truy cập công khai. Ghi rõ `downloads` là vùng tạm và updater/DB có tác động riêng khi restart.

**Không chọn:** Một lệnh build và chạy toàn bộ Compose ngay sau khi thêm secret. `COPY . .` không lọc theo `.gitignore`, và chạy tất cả service sẽ bật Quick Tunnel. Cũng không đổi Compose, updater hay cơ chế bảo mật trong một thay đổi chỉ về tài liệu.

## Tính toàn vẹn của Hyper download trước khi tăng concurrency

**Chọn:** Căn dải theo chunk để mỗi offset có đúng một dải sở hữu, xác minh chiều dài phản hồi gồm cả chunk cuối, và ghi vào file tạm cùng thư mục. Chỉ thay tên file đích khi không còn chunk lỗi và mọi ghi đĩa đã xong. Lỗi đọc hết retry có thể đi theo fallback hiện có; lỗi ghi không được tính là tải thành công. Giữ nguyên cấu hình concurrency và luồng upload.

**Không chọn:** Chỉ vá byte cuối hoặc kiểm tra kích thước file đầu ra. File đã được đặt trước kích thước nên các lỗ do chunk thiếu vẫn có thể qua kiểm tra đó. Dùng SHA-256 trong test đối chiếu nội dung nguồn; không thêm lượt đọc toàn bộ file vào đường truyền production khi chưa có checksum chuẩn từ nguồn.

**Kiểm tra:** `python3 -B -m unittest discover -s tests -v` dùng nguồn chunk giả lập với ghi file thật, không gọi Telegram. Trong benchmark `96 MiB + 1 byte`, chunk `512 KiB`, cửa sổ ban đầu 4/worker, 3 worker và độ trễ giả lập `2 ms`, median trước/sau là `1371.2 / 1367.3 MiB/s`; SHA-256 từ `0/7` lên `7/7`. Đây là số đo pipeline local, không phải tốc độ mạng Telegram.

## Đích API trong `/uphoster`: FileCloud24 và Telecloud

**Chọn:** Thêm adapter theo hợp đồng thực tế, đăng ký trong bảng đích hiện có và giữ Gofile làm mặc định. FileCloud24 dùng PUT hoặc phiên chunk tuần tự. Telecloud dùng API Bearer multipart trên mạng nội bộ, poll task và dựng share link bằng URL công khai. API URL và key chỉ lấy từ môi trường tiến trình, không lưu qua cấu hình Telegram. Xem [hướng dẫn cấu hình](docs/uphoster-api.md).

**Không chọn:** Endpoint tùy ý theo người dùng; hoặc dùng API remote URL thay cho file đã xử lý. Hai giao thức có cơ chế retry, hủy và xác thực khác nhau. File chỉ được gửi đến đích đã cấu hình, không tự theo redirect. Không triển khai API resumable riêng của Telecloud trong lượt này vì đường nội bộ không đi qua giới hạn body của tunnel và API resumable dùng cơ chế xác thực khác. Hai adapter nhận một file; dùng `-z` cho thư mục để tránh tự tạo thêm link hoặc một trang chỉ mục ngoài ý muốn.

**Kiểm tra:** Local HTTP fixture xác nhận byte gửi, thứ tự/retry chunk, cleanup khi phiên lỗi, multipart/Bearer, polling, share link và hủy. Không dùng credential thật hay upload lên dịch vụ đang chạy.

## Kết nối WZML → Telecloud không qua tunnel

**Chọn:** Compose chỉ truyền API URL và key từ môi trường; hướng dẫn dùng host gateway theo container runtime. Podman trên VPS đã chạm tới API Telecloud nội bộ qua `host.containers.internal:8091` với request không có key (`401`). URL HTTPS công khai vẫn chỉ phục vụ link trả về.

**Không chọn:** Đổi Compose sang host networking, đổi bind/firewall của Telecloud hoặc tự bật một bot mới. Không có instance WZML chạy trong checkout này. Chặng nội bộ không xóa băng thông Telecloud → Telegram; không xác minh chính sách tính phí của nhà cung cấp VPS.

## Triển khai bot trên VPS: cây deploy riêng thay vì chạy trong checkout

**Chọn:** Dựng `/home/opc/wzml-deploy` bằng `git archive HEAD`, thêm `.dockerignore` loại `config.py`, `.git`, log rồi build image `localhost/wzml:wzv3-dev`. `config.py` được mount read-only từ checkout dev; `telecloud.env` chứa `TELECLOUD_API_URL`/`TELECLOUD_API_KEY` với quyền `600`. Chạy bằng file `compose.deploy.yml` riêng (không bật `tunnel`, không publish cổng ra ngoài; web UI chỉ bind `127.0.0.1`).

**Không chọn:** `podman compose up` ngay trong `/home/opc/wzml`. `start.sh` chạy `update.py`, và khi `UPSTREAM_REPO` có giá trị thì updater `rm -rf .git` rồi `git reset --hard` — trên cây dev sẽ xóa lịch sử local và mọi thay đổi chưa commit. Cũng không copy `config.py` vào image: `COPY . .` sẽ đóng token, URI Mongo và session string vào layer.

**Kiểm tra:** Image không chứa `config.py` và `grep` prefix bot id trong `/usr/src/app` không khớp. Container chạy commit `ae37f71` khớp `origin/wzv3-dev`; log đạt `WZ Client(s) & Services Started !`.

## Tự khởi động lại sau reboot

**Chọn:** Unit systemd user `wzml.service` (`podman-compose up -d`) với `Linger=yes`, `WantedBy=default.target`. `podman-restart.service` của root không quản container rootless của `opc`.

**Không chọn:** Bật `podman-restart.service` toàn hệ thống (không thấy container rootless) hoặc chạy `nohup` thủ công (mất khi reboot, không có log tập trung).

## Mongo Atlas chặn TLS từ IP VPS

**Chọn:** Chạy tạm `DATABASE_URL: ""` qua env của Compose cho tới khi IP `149.118.54.254` được allowlist. Không sửa `config.py` để tránh mất cấu hình gốc.

**Không chọn:** Tắt xác thực TLS của driver hoặc đổi sang `tlsInsecure`. Handshake bị chặn ở phía Atlas (alert 80), hạ chuẩn TLS không giải quyết và làm yếu đường truyền. Hệ quả của việc bỏ DB: không lưu `/bsetting`, không phục hồi task, không streaming qua Mongo, `/usetting` không bền.

## BASE_URL qua reverse proxy Caddy thay vì tunnel

**Chọn:** `BASE_URL=https://mirror.hzzmonet.io.vn`, Caddy reverse proxy `127.0.0.1:8181` → cổng 8080 trong container, kèm `trusted_proxies` dải Cloudflare và `header_up Host/X-Forwarded-Proto`. DNS A record proxied về `149.118.54.254`. Chứng chỉ do Caddy tự xin (Let's Encrypt, HTTP-01 qua Cloudflare).

**Không chọn:** Dùng service `tunnel` trong Compose. Quick Tunnel đổi URL mỗi lần khởi động và ghi đè `Config.BASE_URL` qua `/data/tunnel_url.txt`, làm link selector trong Telegram hết hiệu lực sau restart. Cũng không publish cổng 8080 ra `0.0.0.0`: web UI có proxy qBittorrent/SABnzbd và trang chọn file, chỉ nên đi qua HTTPS của Caddy.

**Kiểm tra:** `https://mirror.hzzmonet.io.vn/` trả `200` qua Cloudflare; `/app/files?gid=...` trả `200`; `BASE_URL` đọc trong tiến trình container đúng giá trị.

## alist-proxy: bản Go trên host, không dùng Worker

**Chọn:** Build `alist-proxy` từ `AlistGo/alist-proxy` (arm64, static), chạy bằng systemd `alist-proxy.service` dưới user riêng, bind `:5243`, tham số `-address http://127.0.0.1:5244 -token <AList token>`. Caddy phục vụ `https://api-download.hzzmonet.io.vn` → `127.0.0.1:5243`. Hai storage GoogleDrive (`/GDrive`, `/mirror`) đổi `down_proxy_url` sang host mới và bật `down_proxy_sign` + `proxy_range`.

**Không chọn:** Bản Cloudflare Worker: worker phải gọi ngược về AList, thêm một chặng egress và không có lợi ích vì AList ở cùng máy. Cũng không giữ `cdn1.hzzmonet.io.vn`: storage đang trỏ host cũ và `proxy_range` tắt nên tải đa luồng bị chặn. Không sửa trực tiếp SQLite: dùng `/api/admin/storage/update` với payload đầy đủ để AList tự refresh cache.

**Kiểm tra:** `Range: bytes=0-2047` qua `api-download` trả `206` với `Content-Range` đúng ở cả ba chặng (proxy 5243, Caddy local, Cloudflare). Không có `sign` trả `{"code":401,"msg":"expire missing"}`; sign sai trả `sign invalid`. `raw_url` do `/api/fs/get` trả về đã trỏ `api-download.hzzmonet.io.vn`.

## TELECLOUD_PUBLIC_URL trỏ sai host

**Chọn:** Đổi `TELECLOUD_PUBLIC_URL` từ `https://files.hzzmonet.io.vn/telecloud` sang `https://telecloud.hzzmonet.io.vn`. `files.hzzmonet.io.vn` là AList (5244) mount Telecloud qua WebDAV; `/telecloud/s/<token>` trả trang AList chứ không phải trang share của Telecloud.

**Không chọn:** Thêm route `/telecloud/s/*` trong Caddy trỏ sang 8091. Host `telecloud.` đã tồn tại và đúng vai trò; thêm đường thứ hai chỉ tăng bề mặt cấu hình.

**Kiểm tra:** Upload thử qua API nội bộ trả `share_link` dạng `http://127.0.0.1:8091/s/<token>`; thay host bằng `telecloud.hzzmonet.io.vn` trả `200` và trang có tên file. Link qua `files.hzzmonet.io.vn/telecloud/s/...` trả trang AList. Đã xóa file test (soft delete + clear `share_token`), hai link test trả `404`.
