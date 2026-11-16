# Hướng dẫn chạy kiểm thử trên Windows

Tài liệu này dành cho người chạy repo ở nhánh `Thien`. Chạy lệnh trong PowerShell, tại thư mục repo. Không gửi Wokwi token hoặc mật khẩu MQTT qua chat, email, source code hay commit Git.

## Chọn quick hay full

### Quick

Chạy nhóm kiểm thử nhanh tại máy, không build firmware và không chạy Wokwi runtime. **Không cần Wokwi token hay mật khẩu MQTT.**

```powershell
.\tools\run-all-tests.ps1 -Mode quick
```

### Full

Chạy các kiểm tra quick trước, sau đó build firmware và thực hiện các kiểm thử bổ sung, gồm các kịch bản Wokwi runtime. Cần cài công cụ ở mục 2 và lưu Wokwi token cùng mật khẩu MQTT theo mục 5.

```powershell
.\tools\run-tests-saved.ps1 -Mode full
```

**Không thể tiếp tục một lượt full từ PASS 51.** Runner không có checkpoint/resume: mỗi lần chạy tạo thư mục kết quả mới và full chạy lại từ đầu. Giữ nguyên thư mục kết quả cũ trong `test-results/` nếu muốn lưu lượt đã chạy; lượt mới có `run-id` riêng để so sánh.

## 1. Tải nhánh `Thien`

Nếu chưa có repo trên máy:

```powershell
git clone --branch Thien --single-branch https://github.com/nhattoan235/Smart-Gas-Alert-System.git
Set-Location Smart-Gas-Alert-System
```

Nếu đã clone repo rồi:

```powershell
git switch Thien
git pull --ff-only origin Thien
```

## 2. Cài công cụ

- Python 3 và PlatformIO Core để build firmware:

  ```powershell
  python -m pip install platformio cryptography
  ```

- Wokwi CLI theo [hướng dẫn cài cho Windows](https://docs.wokwi.com/wokwi-ci/cli-installation).
- Full có thể cần VS Code, extension Wokwi và Private IoT Gateway cho các kiểm thử mô phỏng. Thiếu compiler C++ host hoặc Node.js sẽ khiến một số kiểm tra bị `BLOCKED`.
- Trên máy mới, preflight có thể báo `BLOCKED` nếu phiên bản PlatformIO/compiler chưa đúng. Xem `platformio-toolchain-preflight.log` trong thư mục kết quả của lượt chạy.

Quick chỉ cần môi trường chạy được PowerShell và các công cụ mà từng kiểm tra cần; thiếu công cụ được báo riêng trong kết quả.

## 3. Lấy Wokwi token

1. Người chạy đăng nhập tài khoản Wokwi của mình.
2. Mở [Wokwi CI Dashboard](https://wokwi.com/dashboard/ci), tạo API token và sao chép token.
3. Giữ token riêng tư. Mỗi người chạy nên tự tạo token cho tài khoản của mình; không commit token vào repo.

Xem thêm [hướng dẫn xác thực Wokwi CLI](https://docs.wokwi.com/wokwi-ci/cli-usage).

## 4. Chuẩn bị mật khẩu MQTT

Mật khẩu MQTT **khác với** Wokwi token. Full cần mật khẩu của tài khoản broker mà người chạy được phép sử dụng để build firmware. Nếu dùng broker riêng, cấu hình host và username tương ứng trong `firmware/platformio.ini`; broker phải truy cập được từ môi trường Wokwi khi kịch bản cần kết nối.

Không dùng mật khẩu của người khác nếu chưa được cho phép. Nếu chỉ chạy quick thì bỏ qua bước lấy token và mật khẩu này.

## 5. Nhập và lưu thông tin cho full

Tại thư mục repo, chạy:

```powershell
.\tools\save-test-secrets.ps1
```

Script lần lượt hỏi:

1. `Wokwi CLI token`: dán token lấy ở mục 3 rồi nhấn Enter.
2. `MQTT build password`: nhập mật khẩu MQTT ở mục 4 rồi nhấn Enter.

Ô nhập là dạng bảo mật nên ký tự sẽ không hiện ra khi gõ hoặc dán; đó là bình thường. Script lưu hai giá trị đã mã hóa theo tài khoản Windows hiện tại tại `%LOCALAPPDATA%\SmartGasAlertSystem\test-secrets.clixml`. Không gửi hoặc chép file này sang máy/tài khoản Windows khác; bạn của m cần tự tạo token và nhập thông tin của bạn ấy trên máy bạn ấy.

Nếu cần cập nhật riêng một giá trị sau khi đã tạo kho bí mật, dùng `-Update WokwiToken` hoặc `-Update MqttPassword`. Lần đầu phải chạy lệnh không tham số để lưu cả hai.

## 6. Chạy và xem kết quả

Quick:

```powershell
.\tools\run-all-tests.ps1 -Mode quick
```

Full:

```powershell
.\tools\run-tests-saved.ps1 -Mode full
```

Mỗi lượt tạo thư mục `test-results/<run-id>/`. Đọc `summary.md` để xem tổng `PASS`, `FAIL`, `BLOCKED` và `NOT_TESTED`; mở log tương ứng để xem chi tiết. Lưu lại thư mục của lượt PASS 51 nếu cần đối chiếu. Một lượt full mới bắt đầu lại từ đầu, không cộng số PASS giữa các lượt.

`BLOCKED` và `NOT_TESTED` không đồng nghĩa với PASS hoặc FAIL; thường có nghĩa là thiếu công cụ, cấu hình hoặc runtime cần thiết. Kiểm thử mô phỏng không thay thế kiểm thử phần cứng thật.

## Tài liệu Wokwi

- [Wokwi CLI Installation](https://docs.wokwi.com/wokwi-ci/cli-installation)
- [Wokwi CLI Usage và token](https://docs.wokwi.com/wokwi-ci/cli-usage)
