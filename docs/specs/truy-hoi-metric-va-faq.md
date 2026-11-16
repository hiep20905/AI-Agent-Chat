# Đặc tả: Truy hồi metric và câu hỏi đã trả lời (RAG cơ bản)

Trạng thái: **đã làm** · Ngày: 16/11/2026 · Phạm vi: AI Agent (bước `understand_question`)

## 1. Vấn đề

Bước `understand_question` nhận toàn bộ catalog: mọi metric, thuật ngữ, expert rule và dataset. Với 8 metric của kho mẫu
thì đủ, nhưng có hai chỗ hỏng:

- Catalog lớn (hàng trăm metric) làm prompt dài, model dễ chọn nhầm metric gần tên.
- Model chọn metric chỉ chung một phần tên: "gió giật mạnh nhất" được trả lời bằng "Tốc độ gió tối đa" (ngày 16/11/2026),
  trong khi dữ liệu gốc có chỉ số gió giật riêng.

Câu hỏi đã trả lời đúng trước đây cũng không được dùng lại: cùng một câu hỏi lần nào cũng được hiểu từ đầu.

## 2. Cách làm

Không dùng embedding: kho mẫu nhỏ, tên metric tiếng Việt không dấu và có dấu lẫn nhau, so khớp từ khoá bỏ dấu đủ dùng
(cùng quyết định với linking, `docs/specs/schema-link.md` §8.3). Mọi thứ nằm trong `chat/services/retrieval.py`, không
gọi model.

### 2.1. Xếp hạng metric (`ranked_metrics`, `listed_metrics`)

Mỗi metric là một "tài liệu" gồm tên, mã (`nhiet_do_cao_nhat` thành "nhiet do cao nhat"), tên và từ đồng nghĩa của các
thuật ngữ liên kết với nó, định nghĩa và đơn vị. Điểm của một metric với câu hỏi:

- cụm (tên, mã, tên hay đồng nghĩa của thuật ngữ) xuất hiện nguyên vẹn trong câu hỏi: `2 × số từ` của cụm dài nhất;
- cộng độ hiếm (IDF) của mỗi từ chung, nhân đôi khi từ đó nằm trong tên chứ không chỉ trong định nghĩa ("lượng mưa"
  ra "Tổng lượng mưa" trước "Số ngày có mưa", vì định nghĩa của metric sau cũng nhắc "lượng mưa");
- từ hỏi, từ nối, từ thời gian (`STOPWORDS` của linking) không tính.

Catalog đến 40 metric được liệt kê đủ, chỉ sắp theo điểm, mỗi metric có `match` là cụm hoặc từ đã khớp. Catalog lớn hơn
chỉ liệt kê 15 metric điểm cao nhất, cộng các metric lượt trước đã truy vấn (câu nối tiếp "còn Huế thì sao?" không nêu
metric), và danh sách dataset chỉ còn các dataset của những metric đó.

Prompt nói rõ `match` chỉ là gợi ý: chọn theo nghĩa, và metric chỉ chung một phần tên mà đo thứ khác thì không chọn, để
`metrics` rỗng cho ladder đi xuống dữ liệu gốc.

### 2.2. Câu hỏi đã trả lời (FAQ, `similar_examples`)

Nguồn là log câu hỏi (`text_to_bi_agent_question`): 300 câu gần nhất có trạng thái `resolved`, không lỗi, có
`query_result`. Mỗi câu thành một ví dụ: câu hỏi (bản viết lại nếu có), metric, chiều và bộ lọc không phải thời gian.
Khoảng thời gian và các dòng kết quả bị bỏ, vì câu mới luôn tự tính lại thời gian từ `today`.

Một ví dụ được dùng khi:

- mọi metric của nó người hỏi vẫn truy vấn được (có trong catalog của người đó);
- tỷ lệ từ chung trên tổng số từ (Jaccard, bỏ từ dừng và từ thời gian) từ 0,5;
- cùng một tập từ chỉ tính một lần ("… tuần này" và "… tuần trước" là một câu).

Tối đa 3 ví dụ, gần nhất trước, vào prompt dưới tên "Câu hỏi tương tự đã trả lời". Câu mới hỏi cùng điều thì chọn như ví
dụ, khác nghĩa thì bỏ qua.

### 2.3. Hiển thị

Tool call `understand_question` mang thêm `retrieved`: số metric đã liệt kê, 3 metric khớp nhất kèm `match`, và các ví dụ
kèm độ giống. Trang AI Agent hiện đối số này trong chi tiết bước, trajectory của benchmark đọc được nó.

## 3. Giới hạn

- Một câu `resolved` nhưng chọn sai metric vẫn có thể thành ví dụ. Chưa có cách đánh dấu câu trả lời sai; khi có phản
  hồi của người dùng hoặc kết quả benchmark (việc 9–11), loại các câu đó khỏi nguồn ví dụ.
- Ví dụ có thể là câu cùng địa điểm nhưng khác chỉ số (độ ẩm và nhiệt độ ở Hà Nội chung 5/7 từ). Prompt bảo model bỏ
  qua ví dụ khác nghĩa; chạy thật ngày 16/11/2026 model chọn đúng.
- So khớp từ khoá không hiểu từ khác hoàn toàn cùng nghĩa ("ẩm ướt" và "độ ẩm"); thuật ngữ và từ đồng nghĩa trong
  Glossary là chỗ khai báo các cách gọi đó.

## 4. Kiểm chứng

`tests/test_retrieval.py`: cụm dài thắng cụm ngắn, từ trong tên nặng hơn từ trong định nghĩa, đồng nghĩa của thuật ngữ
tìm ra metric, catalog lớn chỉ giữ metric khớp và metric lượt trước, ví dụ trùng ý chỉ tính một lần, ví dụ trên metric
không truy vấn được bị bỏ, prompt có danh sách xếp hạng và ví dụ.

Chạy thật ngày 16/11/2026 trên kho mẫu:

| Câu hỏi | Truy hồi | Kết quả |
| --- | --- | --- |
| "Gió giật mạnh nhất ở Đà Nẵng hôm nay là bao nhiêu?" | "Tốc độ gió tối đa" chỉ khớp từ "gió" | Không chọn metric; trả lời từ dữ liệu gốc 38,5 km/h, đề xuất dataset |
| "Tổng lượng mưa ở Cần Thơ tuần này" | "Tổng lượng mưa" khớp nguyên cụm | 24,6 mm |
| "Lượng mưa hôm qua ở Cần Thơ" | Ví dụ "Tổng lượng mưa ở Cần Thơ tuần này" (0,8) | 10,1 mm |
| "Độ ẩm trung bình ở Hà Nội hôm qua" | "Độ ẩm trung bình" khớp nguyên cụm | 81,38 % |
