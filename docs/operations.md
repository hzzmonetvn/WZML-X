# Runbook vận hành WZML-X

Dành cho người cấu hình, triển khai và trực bot. Xem [kiến trúc](architecture.md) để lần theo mã khi triệu chứng đã được khoanh vùng. Các bước khởi động bên dưới dành cho một bản triển khai riêng; **không chạy `start.sh` hoặc `update.py` trong cây mã nguồn đang phát triển**.

## 1. Chuẩn bị

- Docker có Compose và máy có dung lượng trống đủ cho download, giải nén, nén và split. `downloads` là **vùng làm việc tạm**, không phải thư mục lưu file cần giữ. Khi khởi động, bot dọn job của torrent manager và thử xóa rồi tạo lại `DOWNLOAD_DIR`. Đừng dùng mount download chung giữa các bot hoặc trỏ vào thư mục có dữ liệu khác. Nguồn: [`clean_all()`](../bot/helper/ext_utils/files_utils.py#L134-L138).
- Bốn giá trị Telegram: `BOT_TOKEN` từ BotFather, `TELEGRAM_API` và `TELEGRAM_HASH` của ứng dụng Telegram, `OWNER_ID` là ID số của người quản trị. Chuẩn bị chat test riêng, chỉ cấp quyền cho người vận hành cần dùng.
- MongoDB nếu cần lưu cấu hình, dữ liệu người dùng/RSS, phục hồi tác vụ dở hoặc chạy streaming. Bot có thể chạy không MongoDB, nhưng không có những khả năng lưu bền này. Nếu có database, dùng tài khoản giới hạn phạm vi, giữ URI riêng và sao lưu database theo quy trình của môi trường.
- Chỉ thêm credential của backend đang dùng. `DEFAULT_UPLOAD = "rc"` không tự tạo remote rclone; nếu chưa có đích mirror, có thể kiểm thử bằng `/leech` trước.

## 2. Triển khai một instance bằng Compose

### Build từ checkout sạch

[`Dockerfile`](../Dockerfile#L1-L11) cài dependency trên image nền rồi `COPY . .`. `.gitignore` **không** lọc Docker build context. Build từ checkout dành riêng cho triển khai **trước** khi tạo `config.py`, token, cookie, `accounts/`, download hay log trong checkout. Nếu phải rebuild nơi đã có dữ liệu, dùng context sạch hoặc thiết lập `.dockerignore` local để loại toàn bộ secret, dữ liệu, `.git/` và `.code-review-graph/` khỏi context. Xác nhận chưa có secret trong image trước khi chia sẻ image.

```sh
# Từ checkout sạch của revision muốn triển khai.
docker compose config --quiet
# Chỉ build app nếu không cần dùng tunnel.
docker compose build app tunnel

cp -n config_sample.py config.py
chmod 600 config.py
mkdir -p accounts downloads
# Sửa config.py trên máy triển khai; không đưa nội dung file vào Git hoặc chat hỗ trợ.
```

Điền bốn giá trị Telegram, bổ sung `DATABASE_URL` nếu dùng persistence, kiểm tra lại quyền chat/topic và đặt `WEB_ACCESS_PASSWORD` mạnh riêng cho instance. Nếu muốn chạy mã đã build, giữ `UPSTREAM_REPO` trống trong cấu hình **hiệu lực** của updater, bao gồm env và bản lưu MongoDB nếu dùng. Việc để trống không tắt bước nâng dependency trong `update.py`.

### Khởi động và chọn cách truy cập web

```sh
# Khởi động riêng bot trước; chưa mở tunnel ra Internet.
docker compose up -d --no-build app
docker compose ps
docker compose logs --tail=100 app
```

- **Chỉ dùng Telegram:** không khởi động service `tunnel` nếu chưa cần web công khai. `expose: 8080` trong Compose không publish cổng lên host. Khi không có `BASE_URL`, các tính năng cần phát link web sẽ không sẵn sàng.
- **Cần Quick Tunnel:** sau khi xác nhận được phép mở frontend ra Internet, chạy `docker compose up -d --no-build tunnel` và đọc `docker compose logs --tail=100 tunnel` trong kênh riêng. Tunnel dùng chung mạng với `app`, trỏ mặc định tới `localhost:8080`, ghi URL vào `/data/tunnel_url.txt`; bot đọc lúc khởi động và kiểm tra lại mỗi 5 giây. URL có thể đổi khi tunnel khởi động lại, và giá trị khác rỗng trong file ghi đè `Config.BASE_URL`. Nếu bỏ Quick Tunnel để chuyển sang URL cố định, kiểm tra không còn file URL cũ được mount cho bot. Nguồn: [`docker-compose.yml`](../docker-compose.yml#L26-L46), [`tunnel.sh`](../tunnel.sh), [`tunnel_monitor.py`](../bot/helper/ext_utils/tunnel_monitor.py#L11-L44).
- **Cần URL cố định:** dùng reverse proxy HTTPS hoặc named tunnel trỏ tới frontend; đặt `BASE_URL` đúng tên miền công khai. Với một proxy trên host, có thể khai báo ánh xạ loopback `127.0.0.1:8080:8080` cho `app` trong cấu hình triển khai. Nếu đổi `PORT` của web, cập nhật cả đích tunnel/proxy. [`docker-compose.yml`](../docker-compose.yml#L99-L114) có ví dụ named tunnel ở dạng comment, chưa bật mặc định.

| Cổng mặc định | Công dụng |
|---|---|
| `8080` | FastAPI/Gunicorn (`PORT` trong env). Đây là frontend mà tunnel/reverse proxy cần trỏ vào. |
| `8091` | Stream server loopback (`STREAM_PORT` trong env). Không publish ra host. |
| `6800`, `8090`, `8070` | aria2 RPC, qBittorrent Web API, SABnzbd. Không publish các cổng này lên Internet. |

Nguồn: [`startup.py`](../bot/core/startup.py#L410-L430), [`web/wserver.py`](../web/wserver.py#L174-L204), [`stream_server.py`](../bot/core/stream_server.py#L1097-L1117). `cron_boot.py` chỉ gửi request định kỳ nếu có cả `PORT` và `BASE_URL` trong **env của tiến trình**; URL động trong Config không đồng nghĩa hai biến env này được đặt ([nguồn](../cron_boot.py#L6-L23)).

### Smoke test trên môi trường triển khai

1. Đợi log `WZ Client(s) & Services Started !`, xem lỗi lặp lại trước đó và kiểm tra `/ping`, `/help` hoặc `/status` từ tài khoản được cấp quyền. Nếu có `CMD_SUFFIX`, dùng tên thực tế của bot.
2. Dùng file nhỏ, do bạn sở hữu và được phép truyền. Thử `/leech` bằng reply vào media mẫu nếu chỉ kiểm tra Telegram; nếu dùng mirror, thử `/mirror <URL_FILE_TEST>` với đích đã cấu hình. Đợi tin kết quả và xác nhận task biến mất khỏi status. Không lấy log có token/PIN/link làm bằng chứng chia sẻ.
3. Chỉ thử từng tích hợp đã bật: chọn file rồi xác nhận trên Telegram; mở stream bằng media test; kiểm tra status trả kết quả cho job thật. Test bằng user thường nếu cần kiểm tra quota, vì sudo có thể đi đường khác.

Dừng thông thường bằng `docker compose stop` trong thời gian bảo trì. Lần start kế tiếp vẫn chạy startup cleanup. Chạy nhiều instance thì dùng bot token, file cấu hình, mount dữ liệu và file `TUNNEL_URL_FILE` riêng cho từng instance; ví dụ thứ hai trong Compose chỉ là khung comment.

## 3. Đọc cấu hình hiệu lực khi giá trị không như dự kiến

| Giai đoạn | Nguồn và quy tắc |
|---|---|
| `Config.load()` | Mặc định trong `Config` → giá trị có nội dung trong `config.py` → env tên khớp trường `Config`. Nếu có `config.py`, loader kiểm tra bốn giá trị Telegram **trước khi** overlay env. Vì vậy đừng mount nguyên sample còn trống rồi kỳ vọng env bổ sung bốn giá trị đó. |
| `load_settings()` khi có MongoDB | So sánh snapshot triển khai `settings.deployConfig` với file cộng whitelist env. Nếu chưa có snapshot, file/env điền các key thiếu trong cấu hình lưu. Nếu snapshot không đổi, nạp cấu hình lưu. Nếu có thay đổi, trường triển khai mới/đổi và không `None` được trộn vào cấu hình lưu. Không suy ra xóa một dòng khỏi file sẽ xóa giá trị cũ trong database. |
| `update.py` | Loader riêng, tập env giới hạn: `BOT_TOKEN`, `TELEGRAM_API`, `TELEGRAM_HASH`, `OWNER_ID`, `DATABASE_URL`, `BASE_URL`, `UPSTREAM_REPO`, `UPSTREAM_BRANCH`. Có merge MongoDB riêng rồi áp lại whitelist env. Không dùng quy tắc của updater để suy ra kết quả runtime của bot. |
| Tiến trình web | Đọc `BOT_TOKEN` và `WEB_ACCESS_PASSWORD` từ env/file riêng; bot truyền mật khẩu truy cập khi spawn Gunicorn. Nếu bot và web không cùng cấu hình nhận dạng, kiểm tra đồng bộ file/env trước khi điều tra PIN hoặc mật khẩu proxy. |
| Runtime | Tunnel monitor có thể đổi `BASE_URL`. Tuỳ chọn người dùng và cờ lệnh có thể đổi hành vi của một job mà không đổi cấu hình global. |

Nguồn: [`Config.load`](../bot/core/config_manager.py#L206-L265), [`load_settings`](../bot/core/startup.py#L99-L205), [`var_list`](../bot/__init__.py#L67-L76), [`update.py`](../update.py#L50-L128), [web config](../web/wserver.py#L68-L91). `config_sample.py` định hướng cấu hình; mặc định hiệu lực lấy từ `Config` và các loader. Khi file bỏ qua `False`, `0`, `""` hoặc `[]`, một giá trị mặc định khác vẫn có thể giữ nguyên. Ví dụ `DISABLE_NZB`, `DISABLE_JD`, `DISABLE_SEEDR` mặc định `True` trong `Config`; đặt `False` trong sample không đủ để bật nếu đó là nguồn duy nhất. Dùng env của service `app` hoặc cấu hình runtime được lưu và xác nhận lại sau restart; MongoDB merge có thể ảnh hưởng kết quả. Với Compose, khai báo biến trong `environment` hoặc `env_file` của `app`: đặt biến trong shell host không tự chuyển mọi giá trị vào container.

### Cấu hình theo nhu cầu

| Muốn bật hoặc giới hạn | Kiểm tra đầu tiên |
|---|---|
| Cho phép lệnh trong chat/topic | `OWNER_ID`, `AUTHORIZED_CHATS`, `SUDO_USERS`. `AUTHORIZED_CHATS` tách phần tử bằng khoảng trắng, hỗ trợ `chat_id\|topic_id\|topic_id`. Tránh cấp sudo chỉ để sửa lỗi quyền chat. |
| Mirror vào Drive | `DEFAULT_UPLOAD = "gd"`, `GDRIVE_ID`, và token hoặc service account phù hợp. Credential cần quyền trên đúng đích; [gen_scripts](../gen_scripts/README.md) có hướng dẫn chuẩn bị. |
| Mirror qua rclone | `DEFAULT_UPLOAD = "rc"`, `RCLONE_PATH`, file `rclone.conf` hoặc cấu hình riêng của user. Compose mặc định không mount `rclone.conf`; đưa file vào runtime an toàn hoặc dùng DB/thiết lập user phù hợp. |
| Leech và split | `LEECH_LOG_CHAT`, đích chat/topic, `TRANSMISSION_MODE`, `USER_SESSION_STRING` nếu dùng user client, `LEECH_SPLIT_SIZE`. Split hiệu lực còn phụ thuộc quyền và client thực tế. |
| qBittorrent / aria2 | `DISABLE_TORRENTS` cho nhánh torrent, tùy chọn backend và `TORRENT_TIMEOUT`. Bắt đầu với job nhỏ thay vì tăng đồng thời nhiều giới hạn. |
| JDownloader / NZB / Seedr | Cờ `DISABLE_*` hiệu lực, `JD_EMAIL`/`JD_PASS`, `USENET_SERVERS` hoặc credential Seedr tương ứng. Phân biệt backend chưa khởi động với lỗi đăng nhập hay không có nguồn. |
| Web selector và streaming | `BASE_URL`, `WEB_PINCODE`, `DISABLE_STREAM`, MongoDB và `STREAM_TOKENS` hoặc `HELPER_TOKENS` cho stream. Client dùng để stream phải truy cập được media hoặc dump chat. |
| Kiểm soát tải | `BOT_MAX_TASKS` và `USER_MAX_TASKS` là kiểm tra trước task; `QUEUE_ALL`, `QUEUE_DOWNLOAD`, `QUEUE_UPLOAD` là điều phối. `STORAGE_LIMIT` là ngưỡng trống theo GiB, không thay cho giám sát disk; giải nén/nén có thể cần thêm không gian tạm. |
| Crash reports | `ENABLE_TELEMETRY` mặc định bật trong `Config`. [Crash reporter](../bot/helper/ext_utils/crash_reporter.py) có đường gửi dữ liệu ra dịch vụ ngoài; nếu môi trường không cho phép, đặt `ENABLE_TELEMETRY=false` qua env và xác nhận cấu hình lưu không bật lại. |

## 4. Cập nhật, restart và bảo toàn dữ liệu

- [`start.sh`](../start.sh) luôn chạy updater trước bot; script không dừng theo exit code của updater. Khi `UPSTREAM_REPO` có giá trị, [`_run_update()`](../update.py#L131-L164) dựng lại `.git`, fetch và hard reset theo nhánh. Updater còn dọn một số file triển khai trong container và gọi `uv pip install -U -r requirements.txt` kể cả khi bỏ qua Git update. Vì vậy không dùng log updater báo thành công để thay cho việc xác nhận phiên bản, dependency và bot đã sẵn sàng.
- Trước khi đổi phiên bản: ghi lại revision/image, lưu cấu hình và credential cần thiết ngoài container, sao lưu MongoDB nếu dùng, kiểm tra khả năng quay về một image đã biết. `UPSTREAM_REPO` trống tránh Git reset nhưng **không** đảm bảo chạy hoàn toàn từ dependency đã build; giữ điểm rollback và xác nhận sau restart.
- Compose mẫu mount `config.py`, `accounts`, `downloads` và volume tunnel. Các file khác tạo trong filesystem container không mặc nhiên tồn tại sau recreate; kiểm tra DB, mount hoặc bản sao riêng cho token/cấu hình cần giữ. `downloads` được bind mount để dùng trong lúc chạy, không cam kết tồn tại qua cleanup.
- **Soft reload** trong menu `/restart` vẫn chạy `update.py`, sau đó reload module và gắn lại handler trong tiến trình hiện tại. **Hard restart** dừng backend/client, dọn job, chạy updater, rồi exec bot mới. **Restart Sessions** là luồng riêng, reload Telegram client và gắn handler lại. Kiểm tra job đang chạy trước khi sử dụng, kể cả khi không đổi image. Nguồn: [`restart.py`](../bot/modules/restart.py#L195-L302), [`_runtime_reload`](../bot/modules/restart.py#L307-L400), [`__main__.py`](../bot/__main__.py#L189-L205).
- `INC_TASK_NOTIFY` gửi danh sách tác vụ dở; `INC_TASK_RESUME` thử chạy lại lệnh gốc từ dữ liệu MongoDB. Listener lưu dữ liệu này có điều kiện (ví dụ tác vụ ở super chat, bật tuỳ chọn và có DB). **Resume là phát lại lệnh, không phải tiếp tục từ byte tải dở**: reply gốc, dump message hoặc quyền truy cập có thể đã mất. Kiểm tra đích trước khi chạy lại các lệnh có thể tạo bản sao trùng. Nguồn: [ghi job](../bot/helper/listeners/task_listener.py#L119-L134), [restart notification và replay](../bot/modules/restart.py#L86-L190).

## 5. Xử lý sự cố

| Triệu chứng | Kiểm tra và hành động |
|---|---|
| Container restart liên tục hoặc bot không trả lời | `docker compose ps` và `docker compose logs --tail=100 app`; kiểm tra bind mount `config.py`, bốn giá trị Telegram, lỗi xác thực, MongoDB và các log khởi động trước dòng sẵn sàng. Kiểm tra suffix và quyền ở đúng chat/topic. |
| Config file đã sửa nhưng bot vẫn dùng giá trị cũ | So sánh với env, cấu hình lưu trong `/bsetting`, snapshot deploy và việc user ghi đè trong `/usetting`. Với URL web, kiểm tra file tunnel; đừng xóa database để thử. |
| Job chờ lâu hoặc chiếm slot không ngờ | Xem `/status` và xác định chờ metadata, chờ **Done Selecting**, download, hậu xử lý hay queue upload. So sánh cả ba cấu hình queue. Khi có `QUEUE_ALL`, hậu xử lý có thể còn giữ slot download. Chỉ dùng `/forcestart` cho job cụ thể sau khi đánh giá tài nguyên. |
| Không thấy file sau giải nén, hoặc 7z báo lỗi | Tìm log quanh `Extracting` và mã trả về 7z; kiểm tra mật khẩu, các part liên quan và disk. Bước giải nén lỗi có thể giữ archive trong thư mục job lúc đó, nhưng cuối task vẫn có cleanup; giữ bản gốc bên ngoài. |
| Bot báo `Wrong Upload Destination!` hoặc backend upload bị từ chối | Kiểm tra đích hiệu lực, alias/category, token của bot hay của user, và quyền trên Drive/remote/chat. Xác nhận đang xem trạng thái upload, không nhầm với lỗi nguồn download. |
| Web mở được nhưng chọn file không chạy | Mở link mới từ đúng task, kiểm tra PIN và `BASE_URL`, backend còn job hay không; lưu trên web rồi bấm **Done Selecting** trong Telegram. Không mở thẳng cổng RPC để né lỗi proxy. |
| Link stream được tạo nhưng không phát | Kiểm tra token còn tồn tại/chưa hết hạn trong DB, helper/stream bot đọc được message và `STREAM_PORT` của proxy trùng server loopback. Nếu tunnel đổi URL, tạo lại link theo hostname mới. |
| Thiếu disk, RAM hoặc CPU | Kiểm tra mount của `downloads` và `/stats` hoặc `/memory`; giảm đồng thời và tránh chạy nhiều job FFmpeg/7z cùng lúc. Đừng xóa thư mục của job đang chạy. Chờ job kết thúc hoặc hủy từ bot trước khi dọn thủ công. |

### An toàn khi chia sẻ chẩn đoán

`log.txt` và log container có thể chứa URL nguồn, link quản trị web kèm mật khẩu, chat/message ID và đường dẫn credential. Che các giá trị này cùng session string, API key, database URI, PIN và token stream trước khi gửi ra ngoài. Coi link stream là quyền đọc nội dung trong thời gian link còn dùng được. Kiểm soát truy cập `/qbit/`, `/nzb/` và `/app/files` ở proxy nếu môi trường yêu cầu mạng riêng; PIN selector không thay cho ranh giới mạng. Chỉ cài plugin đáng tin vì plugin import vào cùng tiến trình bot và chạy với quyền của tiến trình đó.

## 6. Triển khai đang chạy trên VPS này

Phần này ghi lại cấu hình thực tế của instance đang chạy, khác với phần hướng dẫn chung ở trên. Không chạy `start.sh`/`update.py` trong `/home/opc/wzml`.

| Thành phần | Vị trí / giá trị |
|---|---|
| Cây deploy | `/home/opc/wzml-deploy`, dựng bằng `git archive HEAD` của `/home/opc/wzml` |
| Image | `localhost/wzml:wzv3-dev` |
| File Compose | `/home/opc/wzml-deploy/compose.deploy.yml` (không bật `tunnel`) |
| Cấu hình bot | mount read-only `/home/opc/wzml/config.py` |
| Bí mật Telecloud | `/home/opc/wzml-deploy/telecloud.env` (`600`), nạp qua `env_file` |
| Web UI | container `:8080` → `127.0.0.1:8181` → Caddy `https://mirror.hzzmonet.io.vn` |
| `TELECLOUD_API_URL` | `http://host.containers.internal:8091` (API Telecloud nội bộ) |
| `TELECLOUD_PUBLIC_URL` | `https://telecloud.hzzmonet.io.vn` |
| Hạ tầng AList | AList `:5244` sau Caddy `https://files.hzzmonet.io.vn` |

**Vận hành:**

```sh
systemctl --user status wzml.service      # trạng thái
systemctl --user restart wzml.service     # khởi động lại bot
podman logs -f wzml-deploy_app_1          # log trực tiếp
cd /home/opc/wzml-deploy && podman compose -f compose.deploy.yml ps
```

Sau khi sửa mã trong checkout dev: cập nhật cây deploy (`git archive` revision mới), build lại image, rồi `systemctl --user restart wzml.service`. Không sửa file trong `/home/opc/wzml-deploy` bằng tay vì lần dựng lại sẽ ghi đè.

**Kiểm tra nhanh:** `curl -o /dev/null -w '%{http_code}\n' https://mirror.hzzmonet.io.vn/` trả `200`. Log phải có `WZ Client(s) & Services Started !` và không lặp lại traceback. Xem mục 5 nếu container restart liên tục.

**Giới hạn đang áp dụng:**

- `DATABASE_URL` bị override rỗng vì Mongo Atlas từ chối TLS từ IP VPS. Hệ quả: không lưu `/bsetting`, không phục hồi task, không streaming qua DB.
- `USER_SESSION_STRING` trả `401 AUTH_KEY_UNREGISTERED`; client user không khởi động, chỉ có bot client và helper bot.
- Không bật `tunnel`; `tunnel_monitor` vẫn chạy nhưng volume `/data` rỗng nên không ghi đè `BASE_URL`.

## 7. alist-proxy cho AList

`alist-proxy` phục vụ link tải có chữ ký cho AList, hỗ trợ `Range` để tải đa luồng.

| Thành phần | Giá trị |
|---|---|
| Binary | `/opt/alist-proxy/alist-proxy` (Go static, arm64) |
| Unit | `alist-proxy.service`, user `alist-proxy`, bind `:5243` |
| Biến môi trường | `/opt/alist-proxy/.env` (`600`, chứa `ALIST_ADDRESS`, `ALIST_TOKEN`) |
| Công khai | Caddy `https://api-download.hzzmonet.io.vn` → `127.0.0.1:5243` |
| Storage dùng proxy | `/GDrive` và `/mirror` (GoogleDrive) với `down_proxy_sign` + `proxy_range` |

```sh
systemctl status alist-proxy.service
systemctl restart alist-proxy.service
journalctl -u alist-proxy.service -n 30 --no-pager
```

Kiểm tra: gọi `/api/fs/get` với token AList rồi tải `raw_url` bằng `Range: bytes=0-2047`; phải trả `206` kèm `Content-Range`. Gọi trực tiếp không có `sign` phải trả `{"code":401,"msg":"expire missing"}`.

Không publish `5243`/`5244` ra ngoài; firewall chỉ mở `80/443`. Nếu đổi token AList, cập nhật `/opt/alist-proxy/.env` rồi restart unit — proxy ký và xác thực bằng chính token đó.
