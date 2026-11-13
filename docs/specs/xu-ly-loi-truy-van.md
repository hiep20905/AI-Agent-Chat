# Đặc tả: Phát hiện lỗi, tự khám phá dữ liệu và thử lại truy vấn

Trạng thái: **đang làm**, các loại lỗi đánh dấu "Có" ở mục 3 đã làm · Ngày: 13/11/2026 · Phạm vi: AI Agent (bước chạy
metric `query_metrics`)

## 1. Vấn đề

Trước đây AI Agent chạy truy vấn metric đúng một lần. Lỗi nào cũng thành `query_error`; kết quả rỗng thì trả lời "không
có dòng nào", kể cả khi rỗng chỉ vì viết sai một giá trị. Trường hợp gặp ngày 13/11/2026 trên dữ liệu thời tiết:

| Câu hỏi | Bộ lọc agent gửi | Dữ liệu lưu | Kết quả |
| --- | --- | --- | --- |
| "Nhiệt độ cao nhất ở Huế hôm qua" | `ten_thanh_pho == 'Huế'` | `Hue` | 0 dòng; đúng ra là 23,1 °C |
| "Tổng lượng mưa tháng này ở Cần Thơ" | `ten_thanh_pho == 'Cần Thơ'` | `Can Tho` | 0 dòng; đúng ra là 76,8 mm |
| "Độ ẩm trung bình ở Hà Nội" (câu con tuần tự) | `thanh_pho_key == 'Ha Noi'` | khoá số | Lỗi "phải là số" |

## 2. Nguyên tắc

1. Sửa bằng dữ liệu trước, bằng model sau. Giá trị lọc khác cách viết được đối chiếu với giá trị thật của cột, không
   cần gọi model.
2. Giữ nguyên ý câu hỏi. Không bỏ bộ lọc hay đổi metric chỉ để có kết quả.
3. Mọi lần tra và lần sửa đều theo quyền người hỏi: giá trị thật được đọc bằng chính metric đó, qua Semantic Layer.
4. Chỉ thử lại khi lần sau có thể khác lần trước. Không có quyền, ngoài khoảng dữ liệu hay đúng là không có dòng nào
   thì giải thích, không thử lại.
5. Tối đa `MAX_QUERY_ATTEMPTS` = 3 lần chạy (lần đầu và 2 lần sửa), rồi giải thích nguyên nhân cho người dùng.
6. Mỗi lần chạy, tra cứu và sửa là một tool call riêng, nên trajectory ghi đủ quá trình.

## 3. Taxonomy lỗi

| Mã | Lỗi | Cách nhận biết | Xử lý | Đã làm |
| --- | --- | --- | --- | --- |
| `value_mismatch` | Giá trị lọc không đúng cách viết trong dữ liệu (dấu, hoa thường, viết tắt, tên khác) | 0 dòng, có bộ lọc chữ trên cột không phải thời gian, không phải ngưỡng của expert rule | `explore_values`: nhóm chính metric đó theo cột, không bộ lọc, lấy giá trị thật; so khớp không dấu (mục 4) rồi chạy lại. Không khớp được thì `repair_query` với danh sách giá trị | Có |
| `invalid_plan` | Kế hoạch không qua kiểm tra: cột không lọc hay nhóm được, phép so sánh, sai kiểu (chữ cho cột khoá số), thiếu giá trị | `QueryPlanError` | `repair_query`: model sửa theo thông báo lỗi | Có |
| `rejected_query` | Superset không nhận hoặc chạy lỗi truy vấn | Semantic Layer trả 400 hoặc 500 | `repair_query` | Có |
| `transient` | Semantic Layer không phản hồi, quá tải, hết thời gian | Không kết nối được, hoặc trả 429, 502, 503, 504 | Chạy lại nguyên truy vấn | Có |
| `not_allowed` | Metric chưa duyệt hoặc hết hiệu lực, người hỏi không có quyền | Semantic Layer trả 401, 403, 404 | Không thử lại; nói chỉ số chưa được duyệt hoặc chưa có quyền | Có |
| `out_of_range` | Khoảng thời gian hỏi nằm ngoài dữ liệu | `coverage.status` là `none` | Không thử lại; nêu khoảng dữ liệu có (đã có từ trước) | Có |
| `no_match` | Đúng là không có dòng nào thoả (ngưỡng, khoảng thời gian không có sự kiện) | 0 dòng, không có bộ lọc chữ | Không thử lại; nói không có trường hợp thoả | Có |
| `suspicious_value` | Kết quả có nhưng bất thường: toàn 0 hoặc rỗng, giá trị ngoài miền hợp lý | Hiện chỉ có với metric tạm tính (`doubt`) | Cảnh báo, không khẳng định | Một phần |
| `wrong_meaning` | Chọn sai metric, sai chiều, sai khoảng thời gian dù truy vấn chạy được | Không nhận biết lúc chạy; chỉ thấy qua benchmark | Đo bằng benchmark (việc 9–11) | Chưa |
| `bad_model_output` | Model trả đầu ra hỏng | Lỗi parse đầu ra có cấu trúc | Hiện báo lỗi bước đó; nên thử lại một lần | Chưa |

Bước `repair_query` lỗi (model không phản hồi) thì dừng thử và giải thích lỗi đang có. Bước `explore_values` lỗi thì
model vẫn được nhờ sửa, chỉ là không có danh sách giá trị.

## 4. So khớp giá trị không dấu

`normalized_text` bỏ dấu (NFD và `đ` → `d`), chữ thường, bỏ dấu câu: "TP. Hồ Chí Minh" thành "tp ho chi minh".
`closest_value` nhận một giá trị khi và chỉ khi nó đứng cho đúng một giá trị trong dữ liệu, xét lần lượt:

1. Giống hệt sau khi bỏ dấu ("Huế" là "Hue").
2. Tên này nằm trọn từ trong tên kia và dài ít nhất 60% tên kia ("TP Hồ Chí Minh" là "Ho Chi Minh"; "Phú" không là
   "Phu Quoc", "Nam" không là "Mien Nam").
3. Gần giống, độ giống từ 0,85 trở lên (`difflib`): "Buôn Ma Thuật" là "Buon Ma Thuot".

Bước 2 và 3 chỉ xét giá trị có **cùng các con số**: mã lệch một chữ số là bản ghi khác ("DH000123" không bao giờ là
"DH000124"). Bước nào ra từ hai giá trị trở lên thì không chọn ("Ha" có thể là "Ha Noi" hay "Ha Long"); việc chọn
chuyển cho model. Giá trị nào đổi thì ghi vào `result.notes`: "Đã đổi giá trị lọc theo cách viết trong dữ liệu: “Huế”
→ “Hue”."

Cột dài (khách hàng, sản phẩm): Semantic Layer cắt danh sách ở giới hạn dòng (`SEMANTIC_QUERY_MAX_ROWS`, mặc định
1.000) và báo `truncated`. Khi đó giá trị được hỏi có thể nằm ngoài danh sách, nên chỉ nhận bước 1; giá trị gần giống
chuyển cho model, và câu trả lời nói "không tìm thấy trong phần đã tra" thay vì "dữ liệu không có". Model và giao diện
thấy `count`, `truncated` và 50 giá trị **giống giá trị được hỏi nhất** (`closest`), không phải 50 giá trị đầu theo
thứ tự chữ cái.

## 5. Luồng

```text
query_metrics (lần 1)
   ├── có dòng, hoặc out_of_range / no_match ──► nhận kết quả
   ├── not_allowed ──────────────────────────► dừng, giải thích
   ├── transient ────────────────────────────► query_metrics (lần sau, cùng truy vấn)
   ├── value_mismatch ──► explore_values ──┬── khớp được ──► query_metrics (lần sau)
   │                                       └── không khớp ─► repair_query ─┐
   └── invalid_plan / rejected_query ─────────────────────► repair_query ─┴─► query_metrics (lần sau)
                                                              (query null ► dừng, giải thích)
Sau lần thứ 3: dừng, `query_error` là lý do của lần cuối; câu trả lời giải thích theo `attempts`.
```

`AgentOutcome.attempts` ghi mỗi lần lỗi: `attempt`, `kind`, `detail`, `fix` (đã sửa gì) hoặc `unfixable` (vì sao model
không sửa được, ví dụ dữ liệu không có thành phố Đà Lạt). Câu trả lời cuối nhận
`attempts`: có số liệu thì chỉ nhắc ngắn giá trị đã đổi; không có thì giải thích theo `kind` của lần cuối và gợi ý
hỏi lại.

## 6. Ngoài phạm vi, để sau

- Nhánh metric tạm tính (`draft_metric`) và nhánh schema link (SQL nhiều bảng, việc 4) chưa đi qua vòng thử lại này.
  Khi nối schema link vào luồng chat, lỗi SQL dùng cùng taxonomy.
- `suspicious_value` cho metric đã duyệt: cần ngưỡng hợp lý theo đơn vị, khai báo ở Semantic Layer.
- Cột khoá chưa có `labelled_values` (như `thanh_pho_key` trên cài đặt mới): agent lọc qua cột tên. Cách bền hơn là
  khai báo cặp khoá–nhãn trong Data Dictionary.
