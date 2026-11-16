# Đặc tả: Benchmark AI Agent, chạy hàng loạt và phân tích lỗi

Trạng thái: **đã làm**, bộ câu chờ chuyên gia duyệt · Ngày: 16/11/2026 · Phạm vi: AI Agent (việc 9, 10, 11 của tuần 2)

## 1. Mục tiêu

Đo AI Agent end-to-end trên dữ liệu thời tiết mẫu bằng một bộ câu hỏi có đáp án chuẩn, chạy hàng loạt qua đúng API mà
trang chat dùng, giữ lại toàn bộ trajectory, rồi phân tích các câu trượt để sửa và chạy lại.

## 2. Bộ câu hỏi (`extensions/ai-agents/benchmark/questions.jsonl`)

33 câu, mỗi dòng một câu, phủ cả bốn trục của `docs/specs/taxonomy-cau-hoi.md` và các đường xử lý:

| Nhóm | Số câu | Nội dung |
| --- | --- | --- |
| `value`, `forecast` | 7 | Một con số trên metric đã duyệt, có ngày mai |
| `list`, `ranking`, `trend`, `comparison`, `breakdown`, `range` | 10 | Từng ý định của Trục 2 |
| `parallel`, `sequential` | 3 | Câu tách thành câu con chạy song song hoặc nối tiếp |
| `follow_up` | 2 | Câu nối tiếp giữ bộ lọc và khoảng thời gian (`history` hỏi trước trong cùng cuộc trò chuyện) |
| `repair` | 2 | Tên viết sai ("Buôn Ma Thuật") và địa điểm không có trong dữ liệu ("Đà Lạt") |
| `base_tables` | 3 | Không metric nào đo được, trả lời từ dữ liệu gốc |
| `triage`, `qualitative` | 6 | Ngoài phạm vi, bị chặn (đổi chỉ dẫn, xoá dữ liệu), chào hỏi, hỏi định nghĩa, câu định tính |

Mỗi câu có:

- `expect`: nhãn Trục 1 (`triage`), các `intents` của câu con, các `metrics` phải dùng (theo tên hiển thị), `source`
  (`base_tables` khi phải trả lời từ dữ liệu gốc). Trường nào không có thì không chấm.
- `gold_sql`: SQL chuẩn trên kho `data_wh`, viết lại đúng biểu thức của metric đã duyệt (`sql_metrics` của dataset 12).
  Ngày viết bằng chỗ trống `{today}`, `{yesterday}`, `{tomorrow}`, `{week_start}`, `{week_end}`, `{last_week_start}`,
  `{last_week_end}`, `{month_start}`, `{month_end}`, điền theo hôm nay giờ Việt Nam lúc chạy. Không lưu sẵn con số vì
  Airflow nạp lại dữ liệu mỗi giờ, dự báo của ngày đã qua cũng thay đổi.
- `check`: cách so câu trả lời với kết quả SQL chuẩn (mục 4).
- `review`: "chờ chuyên gia duyệt". Đáp án chuẩn là cách hiểu của người viết bộ câu; chuyên gia nghiệp vụ duyệt từng câu
  rồi đổi trường này.

## 3. Chạy (`scripts/benchmark.py`)

```sh
uv run scripts/benchmark.py                 # cả bộ, 3 câu cùng lúc
uv run scripts/benchmark.py --only B10,B19 --workers 1
```

Cần stack đang chạy; `SUPERSET_PORT` hoặc `SUPERSET_URL` khi Superset không ở cổng 8089. Với mỗi câu, script:

1. chạy `gold_sql` trên `warehouse-db` qua `docker compose exec` (bọc bằng `json_agg` để đọc kết quả);
2. đăng nhập qua form `/login/` như `smoke.py`, mở cuộc trò chuyện mới trên dashboard 1, hỏi các câu `history` rồi đến
   câu chính qua `/chat/stream`, giữ mọi sự kiện SSE;
3. chấm điểm từ trajectory: nhãn và ý định từ kết quả bước `classify_question`; metric và nguồn từ bảng kết quả (`done`,
   kể cả `parts` của câu con); con số từ các dòng kết quả.

`--taxonomy` chỉ đo bước phân loại trên 53 câu gán nhãn của `docs/specs/taxonomy-cau-hoi.md` §10, qua endpoint chỉ
cho quản trị viên `POST /chat/classify` (một lần gọi model mỗi câu, không ghi log), và so với ngưỡng ở §11 của spec đó.

Kết quả ghi vào `extensions/ai-agents/benchmark/runs/<thời điểm>/` (không đưa vào git): `trajectories.jsonl` (mọi sự
kiện của mọi lượt hỏi), `results.json` (điểm từng câu) và `report.md` (bảng tổng hợp).

## 4. Cách chấm

Một câu đạt khi mọi kiểm tra được hỏi đều đạt: `error` (có sự kiện `done`, không `failed`), `triage`, `intents` (cùng
tập, không kể thứ tự), `metrics` (các metric mong đợi nằm trong các metric đã chạy), `source`, `value`. Con số khớp khi
lệch không quá 0,5 % (hoặc 0,01 với số gần 0); câu gió giật nới tới 2 %.

| `check.kind` | Đạt khi |
| --- | --- |
| `number` | Giá trị metric đầu ở dòng đầu bằng giá trị chuẩn |
| `count` | Như `number`, hoặc số dòng kết quả bằng giá trị chuẩn (câu "bao nhiêu ngày" nhóm theo ngày) |
| `numbers` | Mọi giá trị chuẩn đều có trong các con số của câu trả lời, kể cả kỳ so sánh và câu con |
| `labels` | Nhãn cột nhóm đầu (đã đổi khoá thành tên) trùng danh sách chuẩn, theo thứ tự khi `ordered` |
| `top` | Nhóm xếp đầu là một trong các nhóm đồng hạng nhất của SQL chuẩn (`RANK() = 1`) |
| `series` | Mỗi điểm (ngày, vùng…) của chuẩn có trong câu trả lời với giá trị khớp |
| `no_answer` | Không có con số nào (địa điểm không có trong dữ liệu) |
| `clarify` | Hỏi lại (`missing` hoặc `clarifications` không rỗng) và không có con số |
| `none` | Không chấm số liệu (câu không cần truy vấn) |

So nhãn bỏ dấu, không phân biệt hoa thường.

## 5. Kết quả và phân tích (16/11/2026)

### Lần 1: 29/33

| Câu | Kiểm tra trượt | Nguyên nhân | Loại |
| --- | --- | --- | --- |
| B04 "Nhiệt độ thấp nhất ở TP Hồ Chí Minh ngày mai" | `intents`: `value` thay vì `forecast` | Prompt phân loại không nói câu về ngày sau hôm nay là `forecast` | Agent |
| B08 "Hà Nội có bao nhiêu ngày mưa trong tuần trước?" | `value`: 0 thay vì 5 | Lọc `ten_thanh_pho == 'Hà Nội'` (dữ liệu lưu "Ha Noi"); metric đếm trả **một dòng số 0** chứ không rỗng, nên vòng sửa giá trị không chạy | Agent |
| B10 "Thành phố nào có nhiệt độ cao nhất hôm qua?" | `value`: Cần Thơ thay vì TP Hồ Chí Minh | Hai thành phố cùng 30,9 °C; SQL chuẩn chỉ lấy một | Bộ benchmark |
| B33 "…nắng nóng gay gắt ở Hà Nội không?" | `value`: thiếu `glossary` thay vì `expert_rule` | Agent hỏi lại ngưỡng đúng mong đợi; phần thiếu đổi theo các cách hiểu người dùng đã lưu, tiêu chí chấm quá chặt | Bộ benchmark |

Sửa:

- B04: prompt phân loại thêm luật "ngày sau hôm nay là `forecast`, kể cả khi chỉ hỏi một con số".
- B08: `repair.counted_nothing`: kết quả toàn số 0 hoặc rỗng khi có bộ lọc chữ được coi như rỗng, nên giá trị lọc được
  tra lại; nếu mọi giá trị đã viết đúng như dữ liệu (`spelled_as_stored`) thì số 0 là câu trả lời thật, không gọi model
  sửa. Hai test mới trong `test_query_repair.py`.
- B10: SQL chuẩn lấy mọi thành phố đồng hạng nhất, kiểm tra `top`; B11 cũng vậy.
- B33: kiểm tra `clarify`: hỏi lại và không đưa con số nào.

### Lần 2: 33/33

### Lần 3 (đo độ ổn định): 26/33

| Câu | Nguyên nhân | Sửa |
| --- | --- | --- |
| B25, B27, B28, B31, B32, B33 | OpenRouter trả **402**: tài khoản không đủ credit cho các yêu cầu đang chạy song song | Không phải lỗi của agent. Thêm loại lỗi `model_limit` (`docs/specs/xu-ly-loi-truy-van.md`): lỗi 402 hoặc 429 từ nhà cung cấp mô hình được báo rõ cho người hỏi thay vì "không hoàn tất được câu trả lời". Chạy lại 6 câu với `--workers 1`: 6/6 |
| B19 "Nhiệt độ cao nhất ở Hà Nội hôm qua và độ ẩm…" | Câu con đầu bị gán `ranking` vì "cao nhất" nằm trong tên chỉ số; số liệu vẫn đúng | Prompt phân loại: "cao nhất", "thấp nhất" là một phần tên chỉ số thì là `value`, `ranking` chỉ khi xếp các nhóm. Chạy lại B19: đạt |

## 6. Giới hạn và việc tiếp theo

- Bộ câu còn nhỏ (33 câu) và do người viết code soạn; cần chuyên gia duyệt và bổ sung câu thật từ log ("Xử lý câu
  hỏi").
- Không chấm lời văn của câu trả lời (đơn vị, cách diễn đạt), chỉ chấm con số và lựa chọn. Một lần chạy thấy câu trả lời
  thiếu đơn vị "mm" khi metric không khai báo đơn vị.
- Model không tất định: nên chạy mỗi bộ ít nhất hai lần và theo dõi tỷ lệ đạt theo thời gian.
- Chạy song song tốn credit theo số yêu cầu cùng lúc; khi credit thấp, dùng `--workers 1`.
- Câu trả lời đã `resolved` nhưng sai (benchmark trượt `value`) nên được loại khỏi nguồn ví dụ của
  `docs/specs/truy-hoi-metric-va-faq.md`.
