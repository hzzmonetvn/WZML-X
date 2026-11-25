# Kiến trúc WZML-X

Tài liệu cho người phát triển và bảo trì. Đọc cùng [runbook vận hành](operations.md) nếu cần triển khai hoặc điều tra lỗi. Mô tả dưới đây được đối chiếu với mã nguồn ở `41cd54b`, không phải kết quả chạy tích hợp với Telegram hay các dịch vụ cloud.

## 1. Mô hình hệ thống

WZML-X là bot Telegram điều phối truyền file. **Mirror** tải từ nguồn rồi đẩy tới lưu trữ từ xa. **Leech** dùng chung nhiều bước xử lý nhưng trả file về Telegram. **Clone** là luồng riêng: ưu tiên thao tác sao chép qua API/backend thay vì tải về đĩa rồi tải lên lại. **Streaming** cũng là luồng riêng: web đọc dữ liệu từ media Telegram thông qua các client phục vụ stream.

```text
Telegram update → bộ lọc và handler → đối tượng tác vụ → adapter tải xuống
                                                          ↓
         tin kết quả ← adapter tải lên ← queue upload ← hậu xử lý

Trình duyệt → FastAPI → API downloader (chọn file, giao diện quản trị)
                     → stream server trong bot → Telegram (đọc media)

MongoDB ←→ cấu hình lưu, tuỳ chọn người dùng, RSS, tác vụ dở, token stream
```

| Khu vực | Trách nhiệm | Đọc từ đâu |
|---|---|---|
| Điểm vào | Khởi tạo event loop, logging, bộ lập lịch, registry trong bộ nhớ; sau đó bootstrap | [`bot/__init__.py`](../bot/__init__.py#L9-L91), [`bot/__main__.py`](../bot/__main__.py#L40-L123) |
| Telegram | Vòng đời bot, user, helper bot/user, stream bot; đăng ký lệnh và callback | [`TgClient`](../bot/core/tg_client.py#L22), [`add_handlers`](../bot/core/handlers.py#L15) |
| Điều phối tác vụ | Cấu hình chung của job, chọn nguồn/đích, trạng thái, điều phối hậu xử lý | [`TaskConfig`](../bot/helper/common.py#L77), [`TaskListener`](../bot/helper/listeners/task_listener.py#L70) |
| Adapter | RPC, client SDK hoặc subprocess cho từng backend; status wrapper của mỗi giai đoạn | [`download_utils/`](../bot/helper/mirror_leech_utils/download_utils/), [`status_utils/`](../bot/helper/mirror_leech_utils/status_utils/) |
| Dịch vụ và lưu trữ | Bootstrap backend, MongoDB, vị trí file tạm | [`startup.py`](../bot/core/startup.py), [`torrent_manager.py`](../bot/core/torrent_manager.py), [`db_handler.py`](../bot/helper/ext_utils/db_handler.py) |
| Web | File selector, proxy quản trị, trình phát và API stream | [`web/wserver.py`](../web/wserver.py), [`stream_server.py`](../bot/core/stream_server.py) |
| Tính năng mở rộng | Các lệnh RSS, tìm kiếm, cài đặt và plugin | [`bot/modules/`](../bot/modules/), [hướng dẫn plugin](../plugins/README.md) |

## 2. Trình tự khởi động

1. [`start.sh`](../start.sh) gọi updater rồi chuyển tiến trình sang `python -m bot`. Việc import package `bot` tạo loop, logger, các lock, bộ lập lịch và các registry. Entry point nạp `Config` từ file/env trước khi chạy `main()`.
2. [`main()`](../bot/__main__.py#L40-L97) khôi phục dữ liệu MongoDB nếu cấu hình, thiết lập SABnzbd nếu bật, làm mới tên lệnh, rồi khởi động các Telegram client. Sau đó `load_configurations()` và `update_variables()` chạy song song: một bên khởi động backend/web/stream, bên kia chuẩn hoá split size, quyền chat, danh mục và các bảng tra cứu. Options downloader được đồng bộ sau hai bước đó.
3. Bot lưu cấu hình, khởi chạy các việc nền và lên lịch `clean_all()`. Sau `main()` mới gắn callback aria2 và handler Telegram, chờ bước dọn thư mục tải, xử lý thông báo tác vụ dở, bật monitor tunnel và boot plugin, rồi chuyển sang loop chạy lâu dài. Xem [`__main__.py`](../bot/__main__.py#L109-L174).

Web FastAPI chạy trong Gunicorn riêng; stream server aiohttp chạy trong tiến trình bot. Chúng không chia sẻ trực tiếp `task_dict`: web truy vấn API của backend hoặc chuyển yêu cầu stream đến loopback của bot. Xem [`load_configurations`](../bot/core/startup.py#L399-L434) và [`lifespan`](../web/wserver.py#L194-L211).

## 3. Từ lệnh Telegram đến backend tải xuống

### Đăng ký lệnh và kiểm tra quyền

[`BotCommands`](../bot/helper/telegram_helper/bot_commands.py#L9-L61) liệt kê lệnh/alias; phần lớn lệnh có `CMD_SUFFIX` nếu cấu hình. [`add_handlers()`](../bot/core/handlers.py#L182-L259) ghép handler với filter. `authorized` xét owner, quyền người dùng và quyền chat/topic; `sudo` bảo vệ các lệnh quản trị như cài đặt bot và restart. Một số callback còn đối chiếu người bấm với người tạo tác vụ. Không suy ra quyền của một callback chỉ từ quyền gọi lệnh ban đầu: đọc [filter](../bot/helper/telegram_helper/filters.py#L28-L102), rồi đọc chính handler.

### Chuẩn hoá tác vụ

`/mirror` tạo `Mirror(...).new_event()` trên event loop; các biến thể đặt cờ backend hoặc cờ leech. `Mirror.new_event()` kiểm tra chế độ bị tắt, gọi `pre_task_check()`, parse cờ rồi xử lý nguồn từ đối số hoặc tin nhắn được reply. Link Telegram có thể phải được resolve bằng session truy cập được tin gốc; tài liệu `.torrent`, `.dlc`, `.nzb` trong reply được tải về để trao cho backend phù hợp. `-b` là bulk, `-i` là số tác vụ multi, `-m` là thư mục nhóm; `-e` giải nén, `-z` nén và `-j` nối phần. Xem [`mirror_leech.py`](../bot/modules/mirror_leech.py#L115-L234) và [phần nhận nguồn](../bot/modules/mirror_leech.py#L323-L429).

[`TaskConfig.before_start()`](../bot/helper/common.py#L323-L376) chuẩn hoá cài đặt theo từng trường: đích chỉ định trên lệnh, alias, tuỳ chọn cá nhân và mặc định bot. Không có một quy tắc ghi đè duy nhất cho mọi trường. Nhánh mirror giải quyết danh mục, Drive/rclone/token; nhánh leech giải quyết đích chat hoặc topic, kiểm tra quyền client, chọn chế độ truyền và chặn split size ở mức client hỗ trợ. Xem [chọn đích mirror](../bot/helper/common.py#L378-L561) và [đích leech](../bot/helper/common.py#L562-L686).

### Chọn adapter

Bảng sau theo **thứ tự các nhánh sau khi chuẩn hoá**, không phải danh sách backend chạy song song. Trước dispatch, nhánh AllDebrid có thể đổi nguồn thành link trực tiếp hoặc cấu trúc nhiều file; nguồn web thông thường cũng có thể qua `direct_link_generator`. Xem [resolve nguồn](../bot/modules/mirror_leech.py#L431-L528) và [dispatch thực tế](../bot/modules/mirror_leech.py#L530-L558).

| Điều kiện | Adapter |
|---|---|
| Media Telegram đã resolve | `TelegramDownloadHelper` |
| Nguồn dạng `dict` (nhiều file trực tiếp) | `add_direct_download` |
| Cờ JDownloader, qBittorrent, NZB hoặc Seedr | `add_jd_download`, `add_qb_torrent`, `add_nzb` hoặc `add_seedr_download`, theo thứ tự |
| Path rclone | `add_rclone_download` |
| Link/ID Drive | `add_gd_download` |
| Link MEGA | `add_mega_download` |
| Còn lại: ví dụ HTTP trực tiếp hay magnet không chọn qBittorrent | `add_aria2_download` |

`/ytdl` và `/ytdlleech` dùng [`YtDlp`](../bot/modules/ytdlp.py#L459-L534): lấy metadata, dùng format đã chỉ định hoặc hỏi chất lượng, rồi gọi `YoutubeDLHelper.add_download()`. Sau tải, nó quay về `TaskListener` như các luồng mirror/leech khác. `/clone` tự dispatch Drive, rclone, MEGA hoặc Telegram trong [`clone.py`](../bot/modules/clone.py#L287-L505); khi đọc nhánh này, không áp thứ tự hậu xử lý mirror một cách máy móc.

### Backend báo tiến độ và hoàn tất

Ví dụ, qBittorrent add torrent kèm tag của job, đặt `QbittorrentStatus` vào registry và có thể tạo torrent ở trạng thái dừng để chờ queue. Listener qBittorrent polling API; aria2 đăng ký callback RPC; SABnzbd polling job/history. Adapter Telegram và yt-dlp gọi trực tiếp lifecycle của listener khi tải xong. Xem [qBittorrent adapter](../bot/helper/mirror_leech_utils/download_utils/qbit_download.py#L45-L145), [qBittorrent listener](../bot/helper/listeners/qbit_listener.py#L87-L132), [aria2 callback](../bot/helper/listeners/aria2_listener.py#L202-L207) và [NZB listener](../bot/helper/listeners/nzb_listener.py#L68-L120).

## 4. Registry, queue, giới hạn và hủy

- **`mid`** là ID message dùng làm khóa job trong `task_dict`; **`gid`** là định danh mà status/backend đưa ra (ví dụ ID aria2 hoặc hash torrent). Khi đổi giai đoạn, code thay status wrapper của cùng `mid` để Telegram hiển thị thông tin tương ứng. Các registry ở [`bot/__init__.py`](../bot/__init__.py#L49-L91) là trạng thái trong tiến trình, không phải hàng đợi lưu bền.
- **Ba lớp kiểm soát:** [`pre_task_check`](../bot/helper/ext_utils/task_manager.py#L249-L314) kiểm tra điều kiện sử dụng lệnh và số task; [`limit_checker`](../bot/helper/ext_utils/task_manager.py#L171-L229) kiểm tra kích thước/playlist/dung lượng theo nhánh; [`check_running_tasks`](../bot/helper/ext_utils/task_manager.py#L64-L102) quyết định job được chạy hay chờ. Chạy smoke test bằng tài khoản sudo không kiểm chứng được mọi giới hạn của người dùng thường.
- **Queue:** `queued_dl` và `queued_up` lưu `asyncio.Event`; `non_queued_dl` và `non_queued_up` đếm job đã được chạy. Với `QUEUE_ALL`, hàm nhả queue ưu tiên upload khi có chỗ; nếu không có `QUEUE_ALL`, hai hàng được nhả theo giới hạn riêng. Khi đồng thời đặt `QUEUE_ALL` và giới hạn theo chiều, phép kiểm tra nhận job mới không coi `QUEUE_ALL` là trần tuyệt đối: còn dưới giới hạn theo chiều thì tổng có thể vượt ngưỡng. Xem [điều kiện nhận và nhả queue](../bot/helper/ext_utils/task_manager.py#L64-L168).
- **Chuyển pha:** Khi không dùng `QUEUE_ALL`, listener nhả slot download trước hậu xử lý; nếu dùng, job thường giữ slot download tới khi vào kiểm tra queue upload. Nếu upload phải đợi, status chuyển thành `QueueStatus` và listener chờ event, kiểm tra hủy sau khi thức dậy. Xem [`task_listener.py`](../bot/helper/listeners/task_listener.py#L229-L248) và [vào queue upload](../bot/helper/listeners/task_listener.py#L344-L355).
- **Điều khiển:** [`cancel_task.py`](../bot/modules/cancel_task.py) tìm job và chuyển hủy cho adapter/status hiện tại; [`force_start.py`](../bot/modules/force_start.py#L18-L83) có thể mở event của job cụ thể. Đừng xóa mục registry thủ công để giả lập hủy: adapter vẫn có thể giữ tiến trình hoặc file đang dùng.

## 5. Hậu xử lý, upload và dọn dẹp

[`TaskListener.on_download_complete()`](../bot/helper/listeners/task_listener.py#L136-L235) là điểm hội tụ. Nó bỏ qua task đã hủy, xử lý nhóm cùng thư mục, lấy `name`/`gid` từ status và tìm đường dẫn thật. Nếu torrent tiếp tục seed, nó tạo cây symlink riêng để xử lý/upload mà giữ nguyên dữ liệu seed. Các nhánh không seed dùng thư mục job trực tiếp.

| Thứ tự | Bước, nếu có yêu cầu |
|---|---|
| 1 | Loại extension bị loại trừ; nối các phần thô nếu đầu vào là thư mục. |
| 2 | Giải nén, trừ nhánh `is_nzb`; cập nhật lại tên, kích thước, loại file và lọc lại extension. |
| 3 | Áp dụng FFmpeg preset, metadata và quy tắc đổi tên. |
| 4 | Tạo screenshot, convert audio/video, tạo sample video. |
| 5 | Nén nếu yêu cầu. Nếu là leech và không nén, chạy bước split theo giới hạn Telegram. |
| 6 | Kiểm tra queue upload, sau đó dispatch đích và cập nhật trạng thái. |

Nguồn: [`_on_download_complete`](../bot/helper/listeners/task_listener.py#L229-L423). Thứ tự upload thực tế là YouTube nếu `is_yt`, rồi Telegram leech, file host, Google Drive, MEGA hoặc rclone. Một thay đổi đường dẫn ở bước trước phải được dùng lại ở bước sau; đừng giả định tên từ downloader luôn là tên đầu ra.

**Nối không phải giải nén.** [`join_files`](../bot/helper/ext_utils/files_utils.py#L248-L275) dò phần số thứ hai ở thư mục đầu vào và loại trừ các MIME archive được kiểm tra. Tên được quote khi ghép; lỗi ghép xóa đầu ra dở, còn các phần chỉ được dọn ở nhóm ghép thành công. [`proceed_extract`](../bot/helper/common.py#L873-L926) phân loại ứng viên archive, đặt `SevenZStatus`, rồi gọi [`SevenZ.extract`](../bot/helper/ext_utils/files_utils.py#L362-L400). Trong một thư mục, nếu không có mục được xử lý hoặc 7z trả lỗi, bước đó không dọn archive gốc của thư mục. Đây không phải cam kết giữ bản sao lâu dài: tác vụ tiếp tục và cleanup cuối vòng đời vẫn có thể xoá thư mục job. Không lưu dữ liệu duy nhất vào đó.

**Kết thúc:** Khi thành công, listener gửi kết quả, xóa dữ liệu tạm và nhả slot; nhánh còn seed chỉ dọn staging upload và để downloader theo dõi seed. Lỗi hậu xử lý được chuyển vào `on_upload_error`; cả hai nhánh lỗi upload/download đều dọn trạng thái và queue, đánh thức job khác và dọn đường dẫn tạm. Nhánh lỗi download còn cập nhật nhóm cùng thư mục. Xem [thành công](../bot/helper/listeners/task_listener.py#L643-L670) và [đường lỗi](../bot/helper/listeners/task_listener.py#L672-L774).

## 6. Web, streaming và plugin

- **Chọn file:** Bot tạm dừng job, phát nút trỏ đến `/app/files`; UI gọi `/app/files/torrent` để đọc hoặc ghi lựa chọn qua API qBittorrent, aria2, SABnzbd. API yêu cầu PIN; `WEB_PINCODE` đổi cách PIN được đưa cho người dùng, không tắt kiểm tra phía web. Lưu lựa chọn trong web và bấm **Done Selecting** trên Telegram là hai bước riêng. Callback trên bot đối chiếu chủ task, kiểm tra lại kích thước khi có hỗ trợ và chỉ resume nếu không còn chờ queue. Xem [nút chọn](../bot/helper/ext_utils/bot_utils.py#L171-L200), [API](../web/wserver.py#L288-L410) và [callback](../bot/modules/file_selector.py#L120-L196).
- **Quản trị web:** `/qbit/` và `/nzb/` là proxy có kiểm tra mật khẩu, không phải API Telegram. Giữ các tuyến quản trị ở mạng tin cậy nếu có thể. Xem [`protected_proxy`](../web/wserver.py#L520-L570).
- **Streaming:** [`/stream`](../bot/modules/stream.py#L237-L255) kiểm tra tính năng, client và `BASE_URL`; đăng ký token ánh xạ tới chat/message trong [MongoDB](../bot/helper/ext_utils/db_handler.py#L243-L322). Web phục vụ trang `/xstrm/` hoặc chuyển `/stream/`, `/dl/` tới stream server loopback để đọc dữ liệu Telegram, gồm hỗ trợ HTTP Range. Quyền đọc media của client và token còn hạn là hai điều kiện khác nhau. Xem [web proxy](../web/wserver.py#L585-L662) và [đọc media](../bot/core/stream_server.py#L448-L553).
- **Plugin:** Plugin khai báo metadata và lệnh trong `wzml_plugin.yml`; manager khám phá, kiểm tra, import module và gắn handler theo access của manifest. Đây là mã Python chạy trong tiến trình bot, không phải sandbox. Điểm vào là [`PluginManager.boot`](../bot/core/plugin_manager.py#L871) và [hướng dẫn plugin](../plugins/README.md).

## 7. Tự lần theo bằng Code Review Graph

Chạy từ gốc repo trên máy đã cài CLI:

```sh
code-review-graph status
code-review-graph build                     # lần đầu; lần sau dùng update
code-review-graph flow --name mirror
code-review-graph query callers_of check_running_tasks
code-review-graph query callees_of "$(pwd)/bot/helper/listeners/task_listener.py::TaskListener._on_download_complete"
```

Graph ở snapshot này tìm được luồng `mirror` và các điểm gọi `check_running_tasks` từ nhiều adapter, listener upload và nhánh relay clone. Dùng graph để tìm nơi đọc, rồi kiểm tra `if/elif`, lock, callback, `await` và cleanup trong nguồn. Cạnh tĩnh là quan hệ có thể đi qua, không phải thứ tự chạy hay bằng chứng về runtime. Các tên trùng như `on_download_complete` cần đường dẫn và tên lớp để chọn đúng nút. Không commit `.code-review-graph/`.
