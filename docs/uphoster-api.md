# FileCloud24 và Telecloud trong `/uphoster`

Đây là hai đích upload tùy chọn. Các host cũ và đích mặc định Gofile không thay đổi. Sau khi quản trị viên cấu hình, vào **`/usetting` → Uphoster Settings → Change Destination**, chọn FileCloud24 hoặc Telecloud. Bỏ chọn Gofile nếu chỉ muốn gửi tới đích mới. Nếu chọn nhiều đích, bot gửi một bản tới mỗi đích được chọn.

```text
/uphoster <URL_FILE_THỬ>
/uphoster <URL_THƯ_MỤC_THỬ> -z
```

Hai adapter nhận một file mỗi tác vụ. Dùng `-z` để đóng gói thư mục thành một file ZIP trước khi gửi. Chỉ dùng với nội dung được phép chia sẻ: link trả về là link công khai, ai có link đều có thể truy cập theo chính sách của host.

## FileCloud24

Bật `FILECLOUD24_ENABLED = True` trong `config.py` hoặc đặt `FILECLOUD24_ENABLED=true` trong môi trường của bot. Không cần API key; đích cố định là `https://filecloud24.cc`.

- Tối đa 100 MB cho đường quick upload: bot gửi PUT với file làm request body và khai báo `Content-Length`.
- Trên 100 MB: bot tạo phiên, gửi tuần tự theo kích thước chunk của server rồi hoàn tất để lấy share URL. Nếu một chunk gặp lỗi tạm thời, bot thử lại tối đa 3 lần tính cả lần đầu. `Retry-After` dài hơn 60 giây được trả về thành lỗi thay vì giữ job chờ quota quá lâu.
- Bot chỉ chạy một upload FileCloud24 tại một thời điểm trong mỗi tiến trình. Quota và giới hạn đồng thời của nhà cung cấp vẫn áp dụng nếu nhiều instance dùng chung IP. Xem [API FileCloud24](https://filecloud24.cc/developers).

## Telecloud

Bật **Upload API** trong Telecloud và tạo key riêng cho tài khoản mà bot sẽ lưu file vào. Dùng tài khoản con được giới hạn quyền nếu phiên bản Telecloud hỗ trợ. Không dùng key quản trị nếu không cần.

| Thiết lập | Nơi đặt | Ý nghĩa |
|---|---|---|
| `TELECLOUD_API_URL` | Môi trường của tiến trình bot | URL gốc Telecloud mà bot truy cập được, không thêm `/api/upload-api`. |
| `TELECLOUD_API_KEY` | Môi trường của tiến trình bot | Bearer key Telecloud. Không đặt trong tin Telegram, source hoặc file commit. |
| `TELECLOUD_PUBLIC_URL` | `config.py`, env hoặc Bot Settings | URL gốc HTTPS để dựng link chia sẻ. Có thể khác URL nội bộ. |
| `TELECLOUD_UPLOAD_PATH` | `config.py`, env hoặc Bot Settings | Thư mục bên trong tài khoản Telecloud, ví dụ `/WZML`; mặc định `/`. |

Nếu bot chạy trực tiếp trên host cùng Telecloud và Telecloud lắng nghe ở cổng 8091, API URL có thể là `http://127.0.0.1:8091`. **Nếu bot ở container, `127.0.0.1` là chính container**, không phải host. Dùng địa chỉ host mà container tới được; ví dụ `host.docker.internal` nếu môi trường Docker đã cấu hình host gateway. Khai báo hai biến API qua `environment` hoặc `env_file` của service `app`. Chỉ dùng HTTP trên loopback hoặc mạng riêng đáng tin; các trường hợp khác dùng HTTPS. Không cần expose cổng nội bộ ra Internet.

Bot stream file tới `POST /api/upload-api/upload`, yêu cầu Telecloud xử lý nền, rồi poll task đến khi có token chia sẻ. `TELECLOUD_PUBLIC_URL` dùng để dựng link `/s/<token>`; Bearer key chỉ gửi tới API URL, và adapter không tự theo redirect. Đây là đường upload multipart của Telecloud, **không phải** API session resumable `/api/uploads`: nếu gọi qua một proxy có giới hạn request body, file lớn có thể bị từ chối. Dùng đường nội bộ cho hai service cùng máy. Hủy sau khi nhận được task ID gửi `DELETE` tới Telecloud; nếu chưa có ID hoặc server đã lưu xong, không bảo đảm dọn được file phía server.

## Kiểm tra sau khi cấu hình

1. Từ một chat test, chọn đúng đích và chạy `/uphoster` với một file nhỏ được phép công khai.
2. Mở link trả về trên thiết bị khác. Với Telecloud, link phải dùng hostname công khai, không phải IP nội bộ. Kiểm tra file nằm đúng thư mục.
3. Với FileCloud24, dùng file test lớn hơn 100 MB nếu cần xác nhận đường chunk. Nếu bị `429` hoặc `503`, kiểm tra quota và tiến trình khác đang upload trước khi thử lại.

Test local không cần khoá thật: `python -m unittest discover -s tests -p test_api_uphosters.py -v` với `aiohttp` và `tenacity` đã cài.
