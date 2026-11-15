# Chạy full test trên Windows

Tài liệu này dành cho người clone nhánh `Thien` để chạy bộ kiểm thử đầy đủ. Không gửi token hoặc mật khẩu vào chat, email công khai, source code hay commit Git.

## Lưu ý về số PASS

`full` **không tiếp tục một phiên trước từ PASS 51**. Mỗi lần chạy tạo thư mục kết quả mới theo thời gian trong `test-results/`; chế độ full chạy lại các kiểm tra quick trước, sau đó mới chạy các bước bổ sung. Xem số PASS trong `summary.md` của đúng lần chạy; không cộng kết quả của nhiều lần với nhau.

## 1. Tải nhánh

```powershell
git clone -b Thien https://github.com/nhattoan235/Smart-Gas-Alert-System.git
Set-Location Smart-Gas-Alert-System
```

## 2. Cài công cụ cần thiết

- Python 3 và PlatformIO Core để build firmware.
- Gói Python dùng cho kiểm tra chứng chỉ:

```powershell
python -m pip install platformio cryptography
```

- Wokwi CLI: xem [hướng dẫn cài chính thức cho Windows](https://docs.wokwi.com/wokwi-ci/cli-installation). Token dùng với CLI được tạo riêng ở bước tiếp theo.
- Để có thêm test PASS: cài Node.js và một trình biên dịch C++ host (`g++`, `clang++`, hoặc `cl.exe`). Thiếu compiler sẽ khiến các test C++ bị đánh dấu BLOCKED.
- Một số runtime cần VS Code, extension Wokwi và Private IoT Gateway. Test tương thích broker cần `mosquitto.exe` cùng `mosquitto_passwd.exe`.

Full runner không tự cài/nâng cấp bộ PlatformIO đã khóa phiên bản. Trên máy mới, preflight có thể báo BLOCKED nếu các package/compiler PlatformIO cần thiết chưa có hoặc không khớp; xem `platformio-toolchain-preflight.log` trong thư mục kết quả.

## 3. Tạo Wokwi CLI token

1. Đăng nhập tài khoản Wokwi của người chạy test.
2. Mở [Wokwi CI Dashboard](https://wokwi.com/dashboard/ci) và tạo API token.
3. Token hợp lệ bắt đầu bằng `wok_`; tài liệu Wokwi ghi token dài 44 ký tự. Giữ token riêng tư và không lưu vào repo. Xem [hướng dẫn xác thực Wokwi CLI](https://docs.wokwi.com/wokwi-ci/cli-usage).

## 4. Chuẩn bị thông tin MQTT

Cần mật khẩu MQTT cho tài khoản broker mà người chạy được phép sử dụng. Đây là **mật khẩu broker**, không phải Wokwi token. Nếu dùng broker riêng, cấu hình host và username phù hợp trong `firmware/platformio.ini`; bảo đảm Wokwi có thể tới broker nếu kịch bản cần kết nối mạng. Không dùng hoặc chia sẻ mật khẩu broker của người khác khi chưa được cho phép.

## 5. Nhập token và mật khẩu an toàn

Trong PowerShell, tại thư mục repository, chạy:

```powershell
.\tools\save-test-secrets.ps1
```

Script hỏi lần lượt:

1. `Wokwi CLI token`: nhập token vừa tạo.
2. `MQTT build password`: nhập mật khẩu broker.

Hai ô nhập là dạng bảo mật nên ký tự có thể không hiện trên màn hình. Nhập xong mỗi giá trị thì nhấn Enter. Script lưu chúng đã mã hóa theo tài khoản Windows hiện tại tại `%LOCALAPPDATA%\SmartGasAlertSystem`; không đưa file này lên GitHub và không chép file đã mã hóa sang máy Windows khác.

## 6. Chạy full

```powershell
.\tools\run-tests-saved.ps1 -Mode full
```

Runner sẽ tạo thư mục `test-results/<run-id>/`. Mở `summary.md` để xem tổng số PASS/FAIL/BLOCKED/NOT_TESTED; mở `failed-tests.md` và log cùng tên để xem nguyên nhân. Token không được ghi vào log.

BLOCKED hoặc NOT_TESTED không đồng nghĩa với PASS hay FAIL: thường là thiếu toolchain, cấu hình broker, Wokwi extension/Private Gateway, hoặc runtime không được chạy. Bộ test trên mô phỏng không thay thế kiểm thử phần cứng thật.

## Tài liệu chính thức

- [Wokwi CLI Installation](https://docs.wokwi.com/wokwi-ci/cli-installation)
- [Wokwi CLI Usage và token](https://docs.wokwi.com/wokwi-ci/cli-usage)
