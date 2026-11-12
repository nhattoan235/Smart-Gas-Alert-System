# Kịch bản thử ba bo Wokwi giống nhau

Mỗi bo có biến trở **LPG signal %** (nguồn LPG của kịch bản), **CO signal %**, nhiệt độ, độ ẩm và **LPG interference +/-15%**; hai nút tạo lỗi riêng, ACK, INSPECTED, LED/còi và hai công tắc nhãn kiểm thử **LPG LEAK TRUE**/**CO HAZARD TRUE**. Để núm nhiễu ở giữa khi thử các ca không có nhiễu. Bật nhãn đúng lúc bắt đầu sự cố kịch bản; tắt khi sự cố kết thúc. Nhãn chỉ để tính báo giả/bỏ sót, thuật toán tại bo không được đọc nhãn. Giá trị LPG bo đo là nguồn LPG cộng sai lệch từ núm nhiễu và sai lệch nhiệt/ẩm tổng hợp giả định (tối đa ±8 điểm phần trăm), giới hạn 0–100%; đường nét đứt trên web chỉ là nguồn giả định để đánh giá thử nghiệm, thuật toán tại bo không được đọc đường này. Các hệ số gây sai lệch chưa được hiệu chuẩn theo cảm biến thật. Các giá trị không phải nồng độ hoặc mức an toàn ngoài đời. Không xả LPG/tạo CO trong phòng để thử.

| Tình huống | Thao tác | Kỳ vọng |
|---|---|---|
| Khởi động | Giữ cả LPG/CO quanh 3–5% | Mỗi kênh qua WARMUP rồi về SAFE; không phát WATCH giả khi tín hiệu ổn định thấp. |
| Rò LPG nhỏ kéo dài | Ở bo 2, tăng LPG tới khoảng 15%, giữ; CO thấp | LPG vào WATCH sau lọc/trễ; CO vẫn SAFE; bo 2 và dashboard nêu đúng kênh LPG. |
| Tăng rất nhỏ kéo dài | Ở bo 2, giữ LPG quanh 9–10% sau nền 3–5% hơn 35 giây | LPG vào WATCH do độ tăng từ nền kéo dài dù dưới mốc 12%. Đây là mốc tín hiệu thử, không phải lưu lượng rò. |
| Nhiễu làm báo giả | Giữ nguồn LPG 3–5%, xoay LPG interference sang phía dương | Biểu đồ hiển thị nguồn thấp nhưng tín hiệu bo nhìn thấy tăng; lọc/trễ chặn xung ngắn, còn nhiễu kéo dài có thể gây WATCH/WARNING. Không được tuyên bố đã nhận dạng hóa chất gây nhiễu. |
| Nhiễu che rò | Giữ nguồn LPG khoảng 15%, xoay LPG interference sang phía âm để tín hiệu đo thấp | Biểu đồ cho thấy nguồn giả định cao hơn giá trị bo đo; hệ thống có thể bỏ sót. Đây là phép thử giới hạn an toàn, không báo cáo là đã xử lý được nhiễu chéo âm. |
| LPG rõ hơn | Tăng LPG khoảng 30% | LPG WARNING, còi nhịp và LED vàng; log ghi kênh LPG. |
| Tín hiệu LPG cao | Tăng LPG trên 65% đủ lâu | LPG DANGER, LED đỏ/còi tại bo ngay cả khi mất MQTT. |
| CO tại cùng bo | Giữ LPG thấp, tăng CO ở bo 2 khoảng 30% | CO WARNING riêng, LPG SAFE; trạng thái chung WARNING. |
| Hai khí cùng đổi | Tăng cả LPG và CO | Hai trạng thái độc lập, log nêu BOTH nếu cùng chu kỳ; mức chung lấy mức nghiêm trọng hơn. |
| Nhiễu ngắn | Tăng nhanh một biến trở rồi trả lại trước thời gian duy trì | Không đổi cảnh báo do một mẫu đơn lẻ; không áp dụng nhận định này cho rò thật chưa đo. |
| Lỗi một đầu | Nhấn LPG FAULT hoặc CO FAULT >3 giây | Chỉ kênh tương ứng vào FAULT; bo chung không hiển thị SAFE. |
| ACK và kiểm tra | Nhấn ACK lúc WARNING/FAULT rồi DANGER; hạ tín hiệu thấp, chờ WATCH ổn định thêm ít nhất 10 giây rồi nhấn INSPECTED | ACK chỉ tạm tắt còi WARNING/FAULT trong 30 giây; DANGER không tắt còi. INSPECTED chỉ được nhận khi tín hiệu đã ổn định thấp, tránh xác nhận sớm lúc còn sự cố. Nhật ký ghi thao tác ACK/INSPECTED thực tế. |
| Mất mạng | Dừng MQTT/gateway rồi tăng tín hiệu | Cảnh báo tại bo vẫn chạy; sự kiện lưu tối đa 16 bản NVS, gửi lại và chờ dashboard xác nhận đã ghi SQLite. Nếu bộ đệm đầy hoặc ghi NVS lỗi, `lost_events` tăng và hiển thị trên web. |
| Mất một bo | Dừng một phiên Wokwi | Sau 10 giây dashboard báo OFFLINE cho bo đó; các bo khác vẫn gửi. |
| Môi trường | Đổi biến trở nhiệt độ/độ ẩm | Biểu đồ và sai lệch LPG giả lập thay đổi; dashboard cho thấy nguồn kịch bản khác tín hiệu đo. Không coi đó là hàm bù đã hiệu chuẩn. |
| Quên van | Đặt van mở, bếp tắt liên tục hơn 2 phút trên một bo | Dashboard nhắc kiểm tra công tắc giả lập; không điều khiển van thật. |
| Không xác nhận | Đưa LPG hoặc CO vào WARNING/DANGER và để dashboard chạy 60 giây | Sự cố được đánh dấu quá hạn trên web; bấm “Xác nhận đã xem” chỉ ghi ACK người dùng, không tắt còi bo. |
| Khoảng cách giả định | Chọn van, ống hoặc gần bếp trong thẻ “Khoảng cách trong kịch bản” | Dashboard tính khoảng cách hình học từ điểm rò **do người dùng chọn** đến nguồn cháy minh họa; không xác định điểm rò từ cảm biến và không dùng khoảng cách để tuyên bố an toàn. |

Trước phần cứng thật: đo điện áp/nguồn/heater, viết driver ADS1115/SHT31, kiểm tra lỗi dây/bus, thu dữ liệu nhiễu do hơi cồn/chất tẩy/gió/nhiệt/ẩm, hiệu chuẩn với khí chuẩn và thiết bị tham chiếu. Sau đó mới định ngưỡng và đánh giá thời gian phát hiện, báo giả, độ nhạy.

**Cách đọc bảng đánh giá trên web:** precision = cảnh báo đúng / tất cả cảnh báo; recall = cảnh báo đúng / tất cả mẫu được gắn nhãn có sự cố. “Ca nhận ra” là một đoạn nhãn TRUE liên tục có ít nhất một mẫu WATCH trở lên; trễ trung bình tính từ mẫu TRUE đầu tới mẫu cảnh báo đầu tiên của các ca đã nhận ra. Mẫu WARMUP/FAULT được loại khỏi phép so sánh khí. “Ngưỡng cố định” là bộ so sánh giả lập LPG >=12% hoặc CO >=28% trên cùng mẫu, không phải chuẩn an toàn. Chỉ dùng khi công tắc nhãn được bật/tắt đúng; các tỷ lệ và thời gian theo mẫu 2 giây còn chịu ảnh hưởng bởi thao tác người dùng, trễ lọc, độ trễ mạng và không thay thế thực nghiệm khí chuẩn.
