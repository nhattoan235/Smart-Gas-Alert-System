# Gas Sentinel Implementation Plan

**Goal:** Xây dựng ba nút Wokwi/PlatformIO và web MQTT/SQLite cho đồ án LPG và CO.

**Architecture:** Một firmware ESP32-S3 có ba cấu hình nút. MQTT là cầu nối tới một server Python cục bộ; server lưu SQLite và web polling. Logic cảnh báo ở từng nút.

**Tech Stack:** PlatformIO Arduino ESP32-S3, Wokwi VS Code, MQTT 3.1.1, Python standard library, SQLite, HTML/CSS/JavaScript.

**Spec:** `docs/design.md`.

## Global Constraints

- Không biểu diễn Wokwi là cảm biến MQ được kiểm định hoặc đổi tín hiệu giả lập sang ppm/%LEL.
- LPG và CO có đường cảnh báo riêng; không chờ cloud hoặc sự đồng thuận của khí kia.
- Broker Ubuntu người dùng đã cung cấp địa chỉ `192.168.22.128:1883`, tài khoản `iotlab`; mật khẩu được đưa vào lúc build/chạy bằng biến môi trường, không lưu trong repo.
- Không điều khiển van gas thật.

## Review Focus

- Mất MQTT: còi và máy trạng thái tại nút vẫn chạy; hàng đợi không làm treo vòng lặp.
- Mẫu lỗi: không hiển thị `SAFE` và không cập nhật baseline.
- Dữ liệu trùng: SQLite không tạo hai sự kiện cho cùng `(device_id,boot_id,seq)`.
- Mất dữ liệu một nút: web hiển thị offline theo tuổi mẫu, không giữ màu xanh vô hạn.
- Đầu vào MQTT sai: server bỏ qua payload không hợp lệ, không gây lỗi web.

## Tasks

- [x] Tạo cấu trúc PlatformIO, ba cấu hình build, ba sơ đồ Wokwi, bảng đấu dây vật lý và hướng dẫn cấu hình broker.
- [x] Viết driver mô phỏng, lọc tín hiệu và máy trạng thái. Ghi kịch bản kiểm tra thủ công; chưa xác nhận thời gian phản hồi bằng Wokwi.
- [x] Viết MQTT, phát sự kiện QoS 1, hàng đợi NVS tối đa 16 sự kiện và chỉ xóa sau ACK ứng dụng từ dashboard.
- [x] Viết server MQTT/SQLite/API; kiểm tra dữ liệu trùng và payload sai bằng unittest.
- [x] Thiết kế web responsive, biểu đồ và ba ô vị trí nút; xem trực tiếp trong trình duyệt với feed tổng hợp.
- [x] Ghi giới hạn chưa thể kiểm với Wokwi/broker Ubuntu chưa được cung cấp.
- [ ] Driver vật lý ADS1115/SHT31/MQ-7B, hiệu chuẩn khí chuẩn, bản vẽ PCB và đánh giá an toàn điện trước dùng thật.
- [x] Người dùng đã xác nhận ba bo lên dashboard qua broker Ubuntu trong phiên trước.
- [ ] Chạy lại cả ba bản firmware mới nhất qua broker Ubuntu sau khi build với mật khẩu cục bộ; thử đầy đủ bảng `scenarios.md` trong Wokwi và ghi kết quả đo.
