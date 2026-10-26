# Challenge B: Context management

Agent tự viết (Python + `requests`, gọi OpenRouter). Lần 1–5 dùng model free `minimax/minimax-m3.1-flash:free`. Bản free này bị ngừng từ 22/10/2026, nên mặc định giờ là `nvidia/nemotron-3.5-lightning:free` (đổi bằng biến môi trường `MODEL`, ví dụ bản trả phí `minimax/minimax-m3.1-flash`). Model bị API báo ngừng/không tồn tại sẽ bị bỏ qua ngay, không retry.

| File | Nội dung |
|---|---|
| `llm.py` | OpenRouter client + agent loop native tool calling, đo `usage.prompt_tokens` mỗi request |
| `b1_compact.py` | B1: session debug dài, compact giữa chừng theo pipeline slide 26 |
| `b2_codemode.py` | B2: cùng một câu hỏi trên 10 file log, so sánh native tool calling và Code Mode |
| `test_offline.py` | Kiểm tra logic không cần API |

```
python test_offline.py
python b1_compact.py        # COMPACT_AT=20000 là ngưỡng compact (token ước lượng, đã hiệu chỉnh theo usage)
python b2_codemode.py       # both | native | code
```
Key đặt trong `.env` (`OPENROUTER_API_KEY=...`). Key free giới hạn 50 request/ngày.

## B1: Compact

Kịch bản: lỗi race condition khi refresh token. `run_tests` in ~1.100 dòng log, chỉ có 3 test fail.
Các turn: chạy test → user bắt thử tăng `TOKEN_TTL` (vô ích) → hỏi agent nhắc lại 4 ý → bảo agent sửa lỗi thật.

Pipeline (slide 26), chạy trước mỗi lần gọi LLM khi context vượt ngưỡng:
1. **Observe:** ước lượng token của context = ký tự/4 × hệ số hiệu chỉnh. Hệ số = `usage.prompt_tokens` thật ÷ số ước lượng của request agent trước (chặn trong [0,5; 3]). Ký tự/4 thuần đếm thiếu với log nhiều số/ký hiệu và không tính tool schema: ở lần 5 có 4 request 25,2–25,7k token mà không compact dù ngưỡng là 20k.
2. **Prune / truncate (slide 23):**
   - Log test chỉ giữ dòng `FAIL / AssertionError / at / note: / Tests:`. Ví dụ 1.131 dòng còn 11 dòng.
   - Output thô được lưu vào `b1_archive/`.
   - File đã đọc ở turn cũ bị cắt còn 15 dòng đầu (dễ đọc lại khi cần). File đọc trong turn hiện tại được giữ nguyên (KEEP), để agent còn sửa được.
3. **Structured checkpoint (slide 22):** một lần gọi LLM điền JSON gồm `goal, current_state, constraints, findings, failed_attempts, files, next_action`. Code bắt buộc đủ cả 7 key và `goal`, `next_action` khác rỗng, nếu không thì gọi lại (tối đa 3 lần). Lưu vào `b1_archive/checkpoint_N.json`.
4. **Reinject:** system rules + checkpoint, cộng với các message của turn hiện tại. Các turn cũ bị bỏ.
5. **Resume và validate:**
   - Đầu turn 3 (nhắc lại) luôn **ép compact** (`maybe_compact(messages, force=True)`), nên context của turn này chỉ còn system + checkpoint + câu hỏi. Cả 4 ý phải lấy từ checkpoint, không chép lại được từ turn 2.
   - Turn nhắc lại phải nêu đúng lỗi, cách đã thử, ràng buộc và bước tiếp.
   - Turn cuối phải sửa xong và test phải pass, không được lặp lại cách tăng TTL.
   - **Guard theo turn** (`EDIT_SCOPE`): turn 1 và 3 không được sửa file, turn 2 chỉ được sửa dòng `TOKEN_TTL`, turn 4 sửa tự do. Guard này do harness đặt ra, vì model nemotron đã sửa lỗi sớm và phá kịch bản (xem lần 6–7).

### Tóm tắt các lần chạy

| Lần | Model | Code | Check | Ghi chú |
|---|---|---|---|---|
| 1–4 | minimax free | cũ | – | Tìm và sửa lỗi harness (xem cuối mục) |
| **5** | minimax free | cũ | **7/7** | Kết quả chính. `b1_archive/` và `b1_workspace/` là của lần này |
| 6 | nemotron free | mới, chưa có guard | 6/7, không hợp lệ | Agent sửa lỗi ngay turn 1 dù user dặn "Chưa sửa gì" |
| **7** | nemotron free | mới, guard turn 1 | 6/7 | Bằng chứng cho việc nhắc lại chỉ dựa vào checkpoint. Agent sửa lỗi ở turn 2 |
| 8 | nemotron free | mới, guard đầy đủ | – | Hết quota free/ngày ở giữa turn 1 |

Log: `b1_output_run1.txt` … `b1_output_run5.txt`, `b1_output_run6_nemotron_invalid.txt`, `b1_output_run7_nemotron_invalid.txt`, `b1_output_run8_quota.txt`.

**Chưa có lần chạy nào của code mới (đủ guard) đạt 7/7.** Lần 8 hết quota trước khi xong; guard mới chỉ được kiểm offline (`test_offline.py`).

### Sửa sau review

- **Check "test pass" từng không chứng minh được gì.** `run_tests` cũ khớp regex `lock|...` trên cả comment. Fix của agent ở lần 5 pass chỉ nhờ chữ "row lock" trong comment: bỏ comment thì 3 failed, còn chỉ thêm `// TODO lock` mà không sửa gì thì 0 failed. Giờ `run_tests` bỏ comment `//` và `/* */` trước khi khớp, và nhận diện đúng fix compare-and-swap: `UPDATE ... SET revoked = true ... AND revoked = false` có kiểm tra `rowCount`/`RETURNING`. Lock thật (`FOR UPDATE`, `pg_advisory_*lock`, mutex, in-flight map) vẫn được chấp nhận. `test_offline.py` có thêm case cho comment-only và cho fix của agent, có và không có comment.
- **Turn nhắc lại từng còn thấy turn 2.** Ở lần 5, lần compact đầu xảy ra giữa turn 2, nên câu hỏi và câu trả lời của turn 2 vẫn còn trong context. Chỉ ý "backward compatible" là thật sự lấy từ checkpoint. Giờ đầu turn 3 luôn ép compact, và check đầu tiên đổi thành "context turn nhắc lại chỉ gồm `[system, user]`". Prompt checkpoint cũng cấm `next_action` là lệnh một turn (checkpoint 1 của lần 5 ghi "Báo cáo... không thực hiện thay đổi thêm"), và bọc transcript trong `<<< >>>` để model không trả lời tiếp hội thoại thay vì trả JSON.
- **Ước lượng token được hiệu chỉnh theo usage** (xem bước 1). Ở lần 6–7, compact kích hoạt ở ~28k token thay vì để lọt như lần 5.
- `test_offline.py` giờ test cả `maybe_compact` (mock `chat`): dưới ngưỡng thì không đổi, `force=True` thì chỉ còn system + checkpoint + turn hiện tại, checkpoint thiếu key thì giữ nguyên lịch sử. Có thêm test cho guard theo turn.
- `llm.py`: model bị API báo ngừng (`retired`) bị bỏ qua ngay; hết quota ngày (`free-models-per-day`) thì dừng và báo giờ reset, không retry.
- File archive được đánh số liên tục (trước đây nhảy 005, 007, 010 vì đếm lẫn file checkpoint).

### Lần 5 (`b1_output_run5.txt`): 7/7 check PASS

- Compact 4 lần. Lần 1 (giữa turn 2): **~30k → ~1k** token ước lượng. 3 lần sau trong turn sửa lỗi: ~20–30k → ~8–14k, vì file và log của turn hiện tại được KEEP. Mỗi lần `run_tests` thêm ~42k ký tự vào turn hiện tại, nên turn 4 compact 3 lần (mỗi lần tốn 1 request checkpoint 8–13k input). Peak input 25,7k token thật, vượt ngưỡng 20k vì ước lượng ký tự/4 đếm thiếu (đã sửa sau đó).
- Checkpoint đủ 7 trường (lúc đó code chưa bắt buộc điều này). `failed_attempts` ghi TTL 900 → 3600 vẫn 3 fail. `next_action` ở checkpoint 2: `db.tx` + `SELECT ... FOR UPDATE` + revoke có điều kiện `revoked=false`. Checkpoint 3 bịa chi tiết: ghi "khóa dòng FOR UPDATE" dù fix thật không dùng FOR UPDATE.
- Câu trả lời nhắc lại đúng cả 4 ý. Nhưng 3/4 ý còn nằm nguyên trong turn 2 chưa bị nén, nên lần này chưa chứng minh được là nhờ checkpoint (lần 7 bổ sung phần này).
- Turn cuối: agent sửa `refreshToken()` thành `UPDATE ... AND revoked = false` có điều kiện trong `db.tx` + kiểm tra `rowCount !== 1` (compare-and-swap, đúng về ngữ nghĩa), chữ ký giữ nguyên, revert TTL về 900. Không thử lại cách tăng TTL. Các lần sửa lưu ở `b1_archive/edits.json`.
- **Chấm lại bằng checker mới:** `b1_workspace/session.ts` cuối lần 5 vẫn cho **0 failed**, kể cả khi bỏ hết comment. Vậy check "test pass" của lần 5 vẫn đứng được, dù lúc chạy nó pass vì lý do sai.

### Lần 7 (`b1_output_run7_nemotron_invalid.txt`): nhắc lại chỉ từ checkpoint, 6/7

- Compact 3 lần: ~28,5k → ~13,2k (turn 1), ~21,1k → ~0,9k (giữa turn 2), và **ép compact đầu turn 3: ~26,7k → ~0,7k**.
- Turn 3 chạy với context chỉ gồm `[system + checkpoint, câu hỏi]`, **877 token input**. Agent vẫn nêu đúng: race condition trong `refreshToken` (1), tăng TTL 900 → 3600 không giải quyết được (2), giữ API backward compatible (3). Ba ý này chắc chắn đến từ checkpoint.
- Turn 1: agent định sửa 2 lần, đều bị guard chặn.
- **Phát hiện:** sau khi compact giữa turn 2, agent **làm theo `next_action` trong checkpoint thay vì yêu cầu của user**. User chỉ bảo tăng TTL rồi báo kết quả, nhưng agent tự sửa `refreshToken` bằng `db.tx` + `SELECT ... FOR UPDATE` và không revert TTL. Checkpoint là "chỉ thị" mạnh với model yếu: compact giữa turn có thể làm agent mất phân biệt giữa "việc tiếp theo của cả task" và "việc user đang yêu cầu". Guard cho turn 2 được thêm sau lần này.
- Check FAIL: ý (4) "bước tiếp" thành "chạy lại test cho chắc", vì lỗi đã được sửa từ turn 2. Turn 4 chỉ chạy test xác nhận (0 failed; checker mới chấp nhận `FOR UPDATE` trong code thật).

### Lần 6 (`b1_output_run6_nemotron_invalid.txt`): không hợp lệ

Ngay turn 1, agent sửa `refreshToken` bằng `db.tx` + `SELECT ... FOR UPDATE`, dù user dặn "Chưa sửa gì". Turn 2 (tăng TTL) vì thế cho 0 failed, không còn là cách thử thất bại. Sau lần này mới thêm guard cho turn 1.

### Lần 1–4: lỗi harness tìm ra và đã sửa

Lỗi sửa ở lần 3–4:
- Checkpoint rỗng: lần 3, model trả content rỗng (`"raw_summary": ""`) mà lịch sử vẫn bị xóa. Giờ checkpoint không hợp lệ thì gọi lại, tối đa 3 lần. Vẫn hỏng thì chỉ prune, không xóa lịch sử.
- Agent hết step khi đi tìm `pool.ts` (lần 3): `db.ts` giờ khai báo `pool`; `read_file` báo danh sách file có thật khi file không tồn tại; `max_steps` 8 → 10.
- Check "không lặp TTL" báo nhầm (lần 4): edit chứa sẵn dòng `TOKEN_TTL = 3600` bị đếm là tăng TTL. Giờ chỉ đếm edit thật sự đổi giá trị TTL.

Kết quả lần chạy 1 (`b1_output_run1.txt`):
- Compact 4 lần, mỗi lần **~23–26k → ~1,2–2,2k token**. Peak input giữ ở ~20k.
- Câu trả lời sau compact nhắc đúng cả 4 ý: race condition, TTL thất bại, backward compatible, bước tiếp là transaction/lock.

Lỗi tìm ra ở lần 1 và đã sửa trong code:
- **Thrash:** prune cắt luôn file của turn hiện tại → agent đọc lại → context phình lại → compact tiếp. Agent hết step trước khi kịp sửa. Đã sửa: KEEP file đọc trong turn hiện tại.
- **Constraint sai:** checkpoint chép lệnh tạm thời "Chưa sửa gì" thành constraint → mâu thuẫn với turn "hãy sửa". Đã sửa prompt: constraints chỉ gồm ràng buộc lâu dài.
- **Hallucinate:** checkpoint ghi TTL "đã revert" dù agent không hề revert. Đây đúng là rủi ro của Summary nêu ở slide 21.

## B2: Code Mode

10 file × 400 dòng log (297 KB). Câu hỏi: top 5 endpoint có nhiều status 500 nhất. Ground truth tính bằng Python.

| Chế độ | Request | Tổng input token | Peak input | Ký tự tool → LLM | Đúng top 5 |
|---|---:|---:|---:|---:|---:|
| native (`read_file`) | 4 | 216.343 | 143.051 | 292.865 | 5/5 |
| Code Mode (`run_code`) | 3 | **3.129** | **1.451** | **494** | 5/5 |

Code Mode ít hơn **~69 lần** về token (216.343 ÷ 3.129). Log thô không đi vào context: script chạy trong subprocess (`python -I`, cwd = `b2_logs/`, timeout 30s, stdout bị cắt ở 2.000 ký tự), chỉ in kết quả cuối.

Hai dòng của bảng đến từ **hai lần chạy riêng**. Log giống hệt nhau vì cùng seed (`random.Random(2026)`).
- `b2_output.txt`: native. Code Mode trong lần chạy này **thất bại** (hết step, 0/5).
- `b2_output_codemode.txt`: Code Mode chạy lại sau khi thêm bước cuối buộc trả lời (tắt tool ở step cuối).

`score()` chỉ kiểm tra endpoint + số lần, không kiểm tra thứ tự. Dữ liệu có 3 endpoint hòa ở 42 lần, nên thứ tự trong nhóm đó không xác định.

Rủi ro bảo mật (slide 31): subprocess không phải sandbox. Script do LLM viết chạy với quyền của user: đọc được mọi file, kể cả `../.env` chứa `OPENROUTER_API_KEY`, gọi được network, ghi được file. Code đã truyền env tối thiểu (`PATH`, `SYSTEMROOT`) nên script không thừa kế API key hay biến môi trường khác, nhưng vẫn đọc được file `.env`. Production cần container/VM, filesystem chỉ đọc chứa đúng thư mục log, chặn network, giới hạn CPU/RAM.
