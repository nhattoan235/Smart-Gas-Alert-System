# Thiết kế ba bo giám sát giống nhau

## Mục tiêu và bố trí

Một bo ESP32-S3 giám sát đồng thời hai kênh LPG và CO, nhiệt độ, độ ẩm, trạng thái giả lập van/bếp; có LED, còi, ACK và nút tạo lỗi riêng cho từng kênh khí. Ba bản sao cùng phần cứng và cùng mã nguồn, chỉ khác `device_id` (`node1`, `node2`, `node3`) và vị trí lắp. Cả ba gửi về Mosquitto trên Ubuntu, dashboard lưu SQLite và hiển thị theo vị trí bo.

| Bo | Vị trí minh họa | Chức năng |
|---|---|---|
| `node1` | Gần bình và van | Đo LPG, CO và môi trường |
| `node2` | Dọc tuyến ống | Đo LPG, CO và môi trường |
| `node3` | Khu bếp | Đo LPG, CO và môi trường |

Một bo có hai **đầu dò** độc lập. Bố trí đầu LPG và đầu CO theo hướng dẫn của module/thiết bị tương ứng; không giả định hai loại khí cần lấy mẫu cùng một cao độ. Nếu một hộp không đáp ứng được vị trí lấy mẫu, dùng đầu dò tách khỏi bo xử lý với giao tiếp và chiều dài dây được kiểm chứng. Các vị trí trên là minh họa, cần khảo sát bếp thật trước khi lắp.

## Luồng quyết định

Mỗi kênh có bộ lọc, baseline, theo dõi độ tăng, thời gian duy trì và trạng thái `WARMUP`, `SAFE`, `WATCH`, `WARNING`, `DANGER`, `FAULT` riêng. Trạng thái chung của bo là mức cần xử lý cao hơn theo thứ tự `DANGER > FAULT > WARNING > WATCH > SAFE > WARMUP`; còi/LED tại bo theo trạng thái chung. Mọi chuyển trạng thái của một trong hai kênh đều tạo sự kiện, kể cả khi trạng thái chung không đổi. Lỗi CO không biến LPG thành SAFE và ngược lại. Khi khởi động tín hiệu thấp đi từ WARMUP đến SAFE, không tạo WATCH giả. Sau WARNING/DANGER/FAULT, bo cần thời gian dữ liệu ổn định thấp ít nhất 10 giây trong WATCH rồi mới nhận nút INSPECTED để về SAFE; nút ACK chỉ tạm tắt còi mức cho phép. Nhật ký phân biệt thao tác ACK/INSPECTED được ghi tại bo với trạng thái LED/còi suy ra từ logic.

Ngưỡng 12/28/65% là **tham số tín hiệu biến trở để thử thuật toán**, không phải nồng độ an toàn hay ppm/%LEL. Tăng từ nền >=4% và giữ >=30 giây cũng đưa vào WATCH để thử tình huống tăng nhỏ. Một nhiễu ngắn có lọc/trễ. Núm LPG INTERFERENCE trên Wokwi tạo sai lệch ±15 điểm phần trăm; thay đổi nhiệt/ẩm tạo thêm sai lệch **tổng hợp giả định** trong khoảng ±8 điểm phần trăm vào tín hiệu bo nhìn thấy. Các hệ số này chỉ phục vụ thử thuật toán, không phải đặc tính MQ đo được. Biến `lpg_truth_sim_pct` chỉ là nhãn kịch bản để so sánh, không được dùng bởi thuật toán cảnh báo. DANGER duy trì được báo tại bo mà không chờ MQTT, dashboard hay xác nhận chéo. Chưa có đường hiệu chỉnh định lượng theo nhiệt/ẩm hay cơ sở hạ mức cảnh báo. Khoảng cách từ đầu dò lắp đặt đến nguồn cháy chỉ là thông tin khảo sát, không đại diện khoảng cách từ lỗ rò thật và không dùng để tắt cảnh báo. Dashboard có ba vị trí rò **giả định** và tọa độ **minh họa** để tập phân tích khoảng cách; kết quả đó không phải ước lượng hiện trường.

Bếp gas mini không nằm trong mục tiêu dự báo sớm của nguyên mẫu vì sự cố có thể diễn tiến nhanh mà không có tín hiệu báo trước. Các tình huống LPG bình/van/ống và CO vẫn là các kênh cần nhận biết của ba bo. Hệ thống chưa nghe tiếng xì, kiểm tra gioăng, đo lượng gas tiêu thụ hay xác định chính xác mối nối hỏng.

## Giao thức và ứng dụng

Mỗi bo dùng `home/<home_id>/device/<device_id>/{telemetry,event,availability,ack}`. Schema 2 chứa `state` chung, hai đối tượng `lpg`/`co` (trạng thái, tín hiệu tương đối, baseline, độ tăng, hợp lệ), môi trường, công tắc ngữ cảnh, `changed_gas` và cờ mô phỏng. Telemetry QoS 0 mỗi 2 giây; sự kiện đổi trạng thái được xếp hàng tối đa 16 bản trong NVS mặc định 20 KiB, gửi QoS 1 và chỉ gỡ khỏi hàng sau khi dashboard COMMIT SQLite rồi gửi ACK ứng dụng trên topic `ack`. Nếu đầy, hàng đợi thay sự kiện ưu tiên thấp bằng sự kiện ưu tiên cao và tăng `lost_events`; lỗi ghi NVS cũng tăng bộ đếm này. Bộ nhớ hữu hạn nên không thể bảo đảm giữ vô hạn khi mất mạng dài. Broker có retained Last Will cho `availability`.

Dashboard lưu sự cố WARNING/DANGER/FAULT riêng, cho người dùng xác nhận đã xem và đánh dấu quá hạn sau 60 giây nếu chưa xác nhận. Xác nhận web không tắt còi bo. Khi công tắc giả lập báo van mở, bếp tắt liên tục hơn hai phút, dashboard hiển thị lời nhắc. Chưa gửi tin đến người thân vì chưa cấu hình kênh và danh sách người nhận.

SQLite đặt `synchronous=FULL`. Trên Python dùng SQLite từ 3.51.3 trở lên, dashboard chọn WAL; bản cũ hơn dùng rollback journal (`DELETE`) theo lưu ý tương thích trong báo cáo giai đoạn 2. ACK ứng dụng chỉ gửi sau khi lệnh ghi SQLite đã kết thúc thành công. Chưa có tầng đồng bộ cloud hay `server_commit_ack`.

Dashboard nhận cả hai kênh từ mỗi bo, vẽ hai đường tín hiệu, hiển thị trạng thái riêng và nhật ký sự kiện theo khí. Dữ liệu schema 1 của kiến trúc cũ vẫn có thể còn trong SQLite nhưng bị loại khỏi màn hình ba bo mới; không xóa dữ liệu cũ. Muốn dùng phiên bản mới, cần dừng phiên Wokwi cũ, build lại cả ba firmware với mật khẩu MQTT cục bộ rồi chạy các config `sim/node1`, `sim/node2`, `sim/node3`.

## Giới hạn

Wokwi dùng biến trở thay cho đáp ứng thật của MQ-6/MQ-7B và điều kiện khí. Phần cứng mục tiêu cần ADS1115, SHT31, mạch nguồn/heater MQ-7B, bảo vệ đầu vào và hiệu chuẩn. Nguyên mẫu không thay thiết bị báo gas/CO được chứng nhận. Ba bo cùng thiết kế giúp thử khả năng nhân rộng; Wokwi vẫn cần một phiên mô phỏng cho mỗi ESP32 vì một config firmware chỉ định một bản nhị phân.
