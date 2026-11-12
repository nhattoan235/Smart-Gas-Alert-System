# Bố trí và phần cứng nguyên mẫu

## Ba bo giống nhau

```text
Bo 1: bình/van       Bo 2: tuyến ống       Bo 3: khu bếp
ESP32-S3             ESP32-S3               ESP32-S3
LPG + CO + T/RH      LPG + CO + T/RH        LPG + CO + T/RH
LED + còi + ACK      LED + còi + ACK        LED + còi + ACK
       \                    |                    /
            Wi-Fi → MQTT Ubuntu → Dashboard
```

Mỗi bo có cùng sơ đồ và firmware, khác định danh MQTT. Vị trí đặt chỉ là ví dụ cho không gian bếp; đầu lấy mẫu LPG/CO phải tuân theo yêu cầu lắp của từng loại đầu dò và khảo sát luồng khí. Việc hai cảm biến dùng cùng ESP không có nghĩa hai đầu phải gắn sát nhau. Nếu tách đầu dò khỏi bo điều khiển, cần kiểm tra nhiễu, suy hao, nguồn và bảo vệ dây; không mặc định kéo dây analog dài.

## Kết nối mục tiêu cho **mỗi** bo

| Khối | Giao tiếp dự kiến | Ghi chú |
|---|---|---|
| MQ-6 LPG | AO qua mạch bảo vệ → ADS1115 A0 | Nguồn heater riêng đủ dòng; không đưa 5 V trực tiếp vào ADC/ESP32 3,3 V. |
| MQ-7B CO | AO qua mạch bảo vệ → ADS1115 A1 | Heater hai mức 5 V / 1,5 V có liên động, pha làm sạch/đo theo module thực; chỉ xét mẫu hợp lệ trong pha đo. |
| ADS1115 | I²C SDA GPIO8, SCL GPIO9 | Cấp 3,3 V, pull-up về 3,3 V; kiểm tra điện áp và nhiễu thực tế. |
| SHT31 | Cùng bus I²C | Đặt tránh nhiệt heater, dầu mỡ và hơi nước trực tiếp. |
| LED xanh/vàng/đỏ | GPIO5/6/7 | Mỗi LED có điện trở hạn dòng phù hợp. |
| Còi 5 V | GPIO4 qua MOSFET | Không kéo tải 5 V trực tiếp từ GPIO; thêm linh kiện bảo vệ phù hợp loại tải. |
| ACK, INSPECTED, kiểm tra lỗi LPG/CO | GPIO14, GPIO12, GPIO13/21 trong mô phỏng | INSPECTED chỉ mô phỏng việc đã kiểm tra hiện trường; nút lỗi chỉ phục vụ Wokwi. Bản thật cần phát hiện lỗi cảm biến/nguồn/bus. |
| Công tắc ngữ cảnh | GPIO15/16 trong mô phỏng | Chỉ là dữ liệu giả lập van/bếp, không đo trạng thái van thật. |
| Công tắc nhãn sự cố | GPIO11/17 trong mô phỏng | Chỉ tạo ground truth cho bảng đánh giá Wokwi; firmware phát hiện không được dùng hai công tắc này để ra cảnh báo. |
| Nguồn | 5 V và 3,3 V | Tính dòng tổng heater, Wi-Fi, còi; cầu chì, chống đảo cực, tụ lọc; UPS là bước sau. |

GPIO1/2/8/9/10 (đều thuộc ADC1) trong Wokwi đọc **năm biến trở**: nguồn tín hiệu LPG giả định, CO, nhiệt độ, độ ẩm và sai lệch nhiễu LPG. Núm nhiễu ở giữa là 0; hai phía tạo sai lệch tối đa ±15 điểm phần trăm. Nhiệt/ẩm tạo thêm sai lệch tổng hợp giả định tối đa ±8 điểm phần trăm; không phải đường bù cảm biến đã hiệu chuẩn. Đây là pin map mô phỏng, không phải kết nối điện trực tiếp cho MQ/SHT31 ngoài đời. Trong bản vật lý, GPIO8/9 chuyển sang I²C cho ADS1115/SHT31 thay vì hai biến trở môi trường. Mạch vật lý cần driver ADS1115/SHT31, điều khiển heater và hiệu chuẩn trước khi thử khí. Tỷ số chia áp/buffer phải chốt theo module thực và đo sai số, không suy ra ppm từ điện áp giả lập.

## Vị trí, khoảng cách và giới hạn

Ghi tọa độ/chiều cao từng **đầu dò**, vị trí bình, ống, bếp, cửa thông gió và nguồn cháy trên bản khảo sát. `dashboard/site.json` chỉ lưu khoảng cách từ vị trí lắp **đầu LPG** đến nguồn cháy gần nhất dưới dạng tham khảo; không cho biết lỗ rò ở đâu và không dùng để quyết định một tình huống là an toàn. Muốn định vị rò chính xác hay ước lượng nồng độ tại nguồn cháy cần cảm biến, mô hình và thử nghiệm bổ sung.

Danh sách vật tư sơ bộ **mỗi bo**: ESP32-S3, MQ-6, MQ-7B, ADS1115, SHT31, bộ nguồn/heater phù hợp, còi và MOSFET, ba LED và điện trở, nút ACK, nguồn 5 V/3,3 V, vỏ và đầu lấy mẫu. Ba bo là ba bản sao danh sách này. Các mạch này chưa phải thiết kế được chứng nhận để bảo vệ nhà ở.
