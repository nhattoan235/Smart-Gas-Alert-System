# Gas Sentinel — ba bo IoT giống nhau giám sát LPG và CO

Mỗi bo ESP32-S3 đo **cả LPG và CO**, nhiệt độ và độ ẩm; tự cảnh báo bằng LED/còi. Ba bản sao của cùng một mạch đặt ở ba vị trí minh họa: gần bình/van (`node1`), dọc tuyến ống (`node2`), khu bếp (`node3`). Mọi bo gửi về Mosquitto Ubuntu và cùng một dashboard. [Thiết kế](docs/design.md) · [Phần cứng](docs/hardware.md) · [Kịch bản thử](docs/scenarios.md).

## Chạy phiên bản mới

**Dừng các phiên Wokwi cũ** có tên `cylinder`, `line`, `co`. Dashboard đang mở cần **dừng bằng Ctrl+C rồi khởi động lại** để nạp cấu hình ba bo mới. SQLite cũ không bị xóa; trang mới chỉ lấy dữ liệu `node1`–`node3`.

1. Trên Windows, cài Python 3 và PlatformIO Core, rồi mở PowerShell tại thư mục repository đã clone. Cấu hình broker theo mục bên dưới. Đặt mật khẩu MQTT của tài khoản bạn dùng trong chính phiên PowerShell đó rồi build firmware:

   ```powershell
   python -m pip install platformio
   $secure = Read-Host 'Mat khau MQTT' -AsSecureString
   $env:GAS_MQTT_PASSWORD = [System.Net.NetworkCredential]::new('', $secure).Password
   Push-Location firmware
   python -m platformio run
   Pop-Location
   python dashboard/server.py
   ```

   `pio run` sẽ dừng và báo rõ nếu `GAS_MQTT_PASSWORD` đang rỗng. Mật khẩu phải được đặt **trước khi build**, trong chính cửa sổ PowerShell chạy lệnh build; chỉ đặt mật khẩu cho dashboard sau khi build không sửa được file firmware `.bin`.

2. Khi dashboard báo đã kết nối broker, mở <http://127.0.0.1:8080>. Mở thẳng `dashboard/index.html` bằng `file://` chỉ thấy **bản xem trước** với dữ liệu minh họa.
3. Trong VS Code/Wokwi, chọn `firmware/sim/node1/wokwi.toml` qua **Wokwi: Select Config File**, rồi **Wokwi: Start Simulator**. Một sơ đồ gồm **một bo đầy đủ hai kênh**. `node2` và `node3` dùng bản sao của sơ đồ đó, chỉ khác mã bo trong firmware. Để cả ba trực tuyến cùng lúc, mở ba file `wokwi-node1.code-workspace`, `wokwi-node2.code-workspace`, `wokwi-node3.code-workspace` trong ba cửa sổ VS Code riêng bằng `code --new-window <đường-dẫn-file.code-workspace>`. Trong mỗi cửa sổ, chọn `sim/nodeX/wokwi.toml` tương ứng và giữ tab **Wokwi Simulator** hiển thị. Không mở lặp lại cùng thư mục `firmware` vì VS Code có thể đưa về cửa sổ đang mở.
4. Vặn riêng biến trở **LPG signal %** và **CO signal %** trên từng bo. Núm **LPG interference +/-15%** để ở giữa khi thử bình thường; xoay lệch để giả lập nhiễu làm tín hiệu đo tăng hoặc che bớt tín hiệu nguồn. Khi làm một ca thử có sự cố, bật công tắc nhãn **LPG LEAK TRUE** hoặc **CO HAZARD TRUE** trên đúng bo; tắt nhãn khi kết thúc. Nhãn chỉ phục vụ bảng precision/recall và độ trễ theo ca, không điều khiển cảnh báo. Mốc 12/28/65% chỉ thử logic WATCH/WARNING/DANGER; đây **không phải ppm, %LEL hay giới hạn an toàn**. Nút `LPG FAULT`/`CO FAULT` mô phỏng lỗi từng đầu đo. `ACK / MUTE` tạm tắt còi khi WARNING/FAULT, không tắt DANGER; sau sự cố, đợi WATCH với tín hiệu thấp ổn định ít nhất 10 giây, kiểm tra rồi nhấn `INSPECTED` để có thể về SAFE. Dashboard hiển thị hai kênh riêng, tín hiệu nguồn LPG kịch bản, trạng thái chung, biểu đồ, sự cố chờ xác nhận và nhật ký thao tác thực tế tại bo.

Nếu trang vẫn hiện `0/3` dù dashboard ghi `MQTT connected`, trình duyệt đã tới server nhưng chưa có telemetry hợp lệ từ bo. Kiểm tra bạn đã chọn `sim/node1/wokwi.toml`, đã build **sau khi đặt** `GAS_MQTT_PASSWORD`, và xem Serial của Wokwi: `MQTT connected` nghĩa là bo đã vào broker; `MQTT connect failed` cần kiểm tra gateway/địa chỉ/tài khoản. Không gửi mật khẩu hoặc nội dung file `MqttCredentials.h` khi nhờ hỗ trợ.

## Cấu hình MQTT

Firmware Wokwi mặc định dùng `host.wokwi.internal:1883`; dashboard mặc định dùng broker local `127.0.0.1:1883`. Khi clone dự án, hãy sửa `MQTT_HOST` và `MQTT_USER` trong `firmware/platformio.ini` theo broker của bạn, rồi đặt cùng `MQTT_HOST`, `MQTT_PORT`, `MQTT_USER` trong PowerShell chạy dashboard. Bật **Private IoT Gateway** trong Wokwi VS Code nếu cần kết nối broker qua mạng cục bộ. Repository không kèm broker hay thông tin đăng nhập dùng chung.

Mật khẩu chỉ đọc từ `GAS_MQTT_PASSWORD` lúc build và khi chạy dashboard. Nó được nhúng trong file firmware `.bin`, nên không chia sẻ file build; header sinh ra nằm trong `firmware/.pio` đã bị bỏ qua bởi `.gitignore`. Chưa bật TLS: chỉ dùng trên mạng lab tin cậy, không mở cổng MQTT ra Internet. Nếu dùng ACL, ba bo cần publish `home/demo-home/device/<device_id>/{telemetry,event,availability}` và subscribe `.../ack`; dashboard cần subscribe `home/demo-home/device/+/+` và publish `.../ack` sau khi ghi SQLite.

## Giới hạn của nguyên mẫu

Wokwi dùng biến trở làm tín hiệu tương đối cho hai khí và môi trường. Núm nhiễu là thao tác giả lập, không đồng nghĩa bo đã nhận diện được loại hóa chất gây nhiễu ngoài đời. Firmware Wokwi chưa đọc MQ-6/MQ-7B, ADS1115 hay SHT31 thật; vị trí đầu dò LPG và CO ngoài đời phải khảo sát riêng dù chung bo xử lý. Bếp gas mini không được dùng làm mục tiêu dự báo sớm. Các chức năng nghe tiếng xì, kiểm tra gioăng, đo gas tiêu thụ, xác định chính xác mối nối hỏng thuộc hướng phát triển. `lpg_distance_to_ignition_m` trong `dashboard/site.json` là khoảng cách khảo sát từ **đầu LPG** đến nguồn cháy. Phần khoảng cách kịch bản dùng vị trí lỗ rò do người dùng **giả định**, không suy ra vị trí rò thật hay cho phép hạ cảnh báo. Chưa có pin/UPS, chu kỳ heater MQ-7B, hiệu chuẩn ppm, thông báo người thân hoặc đồng bộ cloud; không coi các thử nghiệm Wokwi là bằng chứng đạt an toàn thực địa.

Bạn có thể thử dashboard bằng dữ liệu tổng hợp với `python dashboard/demo_feed.py` trong terminal khác. Dừng chương trình đó trước khi nhận dữ liệu Wokwi thật để không trộn dữ liệu; dữ liệu tổng hợp luôn có `simulated: true`. Dự án chưa phải hệ thống an toàn được hiệu chuẩn hoặc chứng nhận.
