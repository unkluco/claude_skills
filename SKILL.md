---
name: line-locator
description: >
  Định vị dòng trước khi đọc file hoặc tìm file trong cây thư mục — tránh đọc mù, tiết kiệm token. Dùng khi cần tìm hàm, class, symbol, hoặc bất kỳ pattern nào trong source code trước khi gọi view_range. Hai công cụ: findtree.py (tìm file nào có chứa pattern) và findtool.py (tìm dòng nào trong một file).
author: Kluco
github: unkluco
email: huylet334@gmail.com
version: 2.0
---

# Line-Locator

Hai script phối hợp để định vị chính xác trước khi đọc:

| Script | Phạm vi | Câu hỏi trả lời |
|---|---|---|
| `findtree.py` | Cả cây thư mục | *File nào* chứa pattern này? |
| `findtool.py` | Một file duy nhất | *Dòng nào* trong file này? |

**Workflow tổng quát:**
```
findtree.py (shortlist files) → findtool.py (locate lines) → view_range (read)
```

Script nằm tại: `skills/line-locator/scripts/`

---

## Output contract

Cả hai tool đều dùng **JSON mặc định**. Chỉ truyền `--text` khi cần đọc thủ công.

**Success — findtool:**
```json
{"ok": true, "mode": "mr", "result": {"matches": {"processOrder": [247, 312]}}, "engine": "literal", "ignore_case": false}
{"ok": true, "mode": "n",  "result": {"line": 248}}
{"ok": true, "mode": "b",  "result": {"line": 198}}
{"ok": true, "mode": "exists", "result": {"matched": true}}
{"ok": true, "mode": "c",  "result": {"line": 298}}
```

**Success — findtree:**
```json
{"ok": true, "engine": "literal", "matched_files": ["src/order.py", "tests/test_order.py"]}
```

**Failure (cả hai tool, ra stderr):**
```json
{"ok": false, "error": "No line matching pattern 'foo' was found after line 0."}
```

**Parse nhanh:**
```python
import json, subprocess
out = subprocess.check_output([...])
data = json.loads(out)
# findtool
lines   = data["result"]["line"]              # -n / -b / -c / -o
matches = data["result"]["matches"]["sym"]    # -mr  → list[int]
matched = data["result"]["matched"]           # -e   → bool
# findtree
files   = data["matched_files"]               # list[str] relative paths
```

---

## findtree — tìm file

```bash
python findtree.py --root ROOT --pattern PAT [options]
```

| Option | Mặc định | Mô tả |
|---|---|---|
| `--root` | *(required)* | Thư mục gốc |
| `--pattern` | *(required)* | Pattern tìm kiếm |
| `--engine` | `auto` | `auto` / `literal` / `regex` |
| `--include GLOB...` | *(tất cả)* | Chỉ tìm file khớp glob |
| `--exclude GLOB...` | *(không)* | Bỏ qua file/thư mục khớp glob |
| `--ignore-case` | off | Case-insensitive |
| `--max-results N` | *(không giới hạn)* | Dừng sau N file |
| `--show` | `matched` | `matched` / `errors` / `both` / `summary` / `all` |
| `--max-file-size` | *(không)* | Bỏ qua file lớn hơn (VD: `64K`, `10M`) |
| `--no-default-dir-excludes` | off | Tắt auto-skip `.git`, `node_modules`, v.v. |
| `--text` | off | Output text thay vì JSON |

**Auto-excluded dirs** (mặc định): `.git`, `.hg`, `node_modules`, `venv`, `.venv`, `__pycache__`, `dist`, `build`, `target`, `.mypy_cache`, `.pytest_cache`, `.tox`, `coverage`

```bash
# Tìm file chứa symbol (literal, nhanh nhất)
python findtree.py --root ./src --pattern "processOrder"

# Tìm file chứa pattern regex
python findtree.py --root ./src --pattern "class\s+\w+Service" --engine regex

# Chỉ tìm trong file .py
python findtree.py --root ./src --pattern "TODO" --include "*.py"

# Bỏ qua thư mục tests
python findtree.py --root ./src --pattern "processOrder" --exclude "tests/**"

# Dừng ngay khi tìm đủ 1 file (kiểm tra nhanh)
python findtree.py --root ./src --pattern "processOrder" --max-results 1
```

---

## findtool — tìm dòng

```bash
python findtool.py --file FILE [--engine ENGINE] [--ignore-case] FLAG... [--text]
```

| Flag | Args | Mô tả | `result` key |
|---|---|---|---|
| `-mr` | `PAT [PAT ...]` | Tất cả dòng khớp một hoặc nhiều pattern | `{"matches": {"pat": [1,5]}}` |
| `-n` | `PAT LINE` | Dòng đầu tiên khớp **sau** LINE (0 = từ đầu file) | `{"line": 45}` |
| `-b` | `PAT LINE` | Dòng cuối cùng khớp **trước** LINE (999999 = cuối file) | `{"line": 12}` |
| `-e` | `PAT` | File có chứa pattern không? | `{"matched": true}` |
| `-c` | `OPEN LINE N` | Dòng closing delimiter khớp với OPEN thứ N trên LINE | `{"line": 89}` |
| `-o` | `CLOSE LINE N` | Dòng opening delimiter khớp với CLOSE thứ N trên LINE | `{"line": 34}` |

**Delimiters `-c`/`-o`:** `{` `}` `(` `)` `[` `]`

```bash
# Tìm tất cả dòng chứa symbol
python findtool.py --file app.py -mr "processOrder"

# Tìm nhiều symbol một lần
python findtool.py --file app.py -mr "processOrder" "cancelOrder" "getUser"

# Tìm dòng { đầu tiên sau dòng 247
python findtool.py --file app.py -n "\{" 247

# Tìm dòng cuối cùng có "import" trong toàn file
python findtool.py --file app.py -b "^import" 999999

# Tìm dòng } đóng của { thứ nhất trên dòng 248
python findtool.py --file app.py -c "{" 248 1

# Kiểm tra file có TODO không
python findtool.py --file app.py -e "TODO"
```

---

## Engine

Ba chế độ áp dụng cho `-mr`, `-n`, `-b`, `-e` của **findtool** và `--pattern` của **findtree**:

| Engine | Khi nào dùng | Lưu ý |
|---|---|---|
| `auto` *(mặc định)* | Hầu hết trường hợp | Tự chọn `literal` nếu pattern không có metachar, `regex` nếu có |
| `literal` | Tìm chuỗi chính xác có ký tự đặc biệt | Không cần escape: `foo(bar)`, `a.b`, `[key]` |
| `regex` | Cần pattern phức tạp | Python `re` syntax |

```bash
# auto — tự quyết định
python findtool.py --file app.py -mr "processOrder"          # → literal
python findtool.py --file app.py -mr "def \w+"               # → regex

# literal — không cần escape ký tự đặc biệt
python findtool.py --file app.py --engine literal -mr "foo(bar)" "a.b"

# regex — pattern tường minh
python findtool.py --file app.py --engine regex -mr "(get|set)\w+"
```

> **`-c`/`-o` không dùng engine** — luôn dùng delimiter scanner riêng, tự bỏ qua string literal và comment (`//`, `#`, `/* */`, `'...'`, `"..."`, backtick, triple-quote).

---

## Workflows

### 1. Đọc một hàm đã biết tên

```bash
# Bước 1: Tìm dòng định nghĩa
python findtool.py --file app.py -mr "processOrder"
# → {"matches": {"processOrder": [247, 312]}}  → 247 là định nghĩa

# Bước 2: Tìm dòng { thực sự (KHÔNG đoán cùng dòng hay dòng dưới)
python findtool.py --file app.py -n "\{" 247
# → {"line": 248}  ← dùng con số này

# Bước 3: Tìm dòng đóng
python findtool.py --file app.py -c "{" 248 1
# → {"line": 298}

# Bước 4: Đọc
view_range [247, 298]
```

> **Tại sao `-n "\{" FUNC_LINE` thay vì `-c "{" FUNC_LINE 1` thẳng?**
> `-c` lỗi `"contains only 0 valid occurrence(s)"` nếu `{` không ở đúng dòng đó.
> `-n "\{"` luôn tìm được `{` dù cùng dòng hay dòng kế.
> Nếu kết quả cách FUNC_LINE quá xa (>2–3 dòng) → đọc thêm vài dòng kiểm tra, có thể là `{` của biểu thức khác.

> **Lưu ý escape:** `-n`/`-b`/`-mr` dùng regex → `{` phải viết `\{`. `-c`/`-o` là delimiter matching → truyền nguyên `{`.

---

### 2. Từ repository lạ → đọc một hàm

```bash
# Bước 0: Tìm file trước
python findtree.py --root ./src --pattern "processOrder"
# → {"matched_files": ["services/order.py"]}

# Bước 1-4: Tiếp tục như Workflow 1 với file đó
python findtool.py --file services/order.py -mr "processOrder"
...
```

---

### 3. Tìm nhiều symbol cùng lúc

```bash
python findtool.py --file app.py -mr "processOrder" "cancelOrder" "getUser"
# → {"matches": {"processOrder": [247], "cancelOrder": [312], "getUser": [89]}}
# Dùng kết quả để lên kế hoạch đọc toàn bộ — ít tool call hơn nhiều lần riêng lẻ
```

---

### 4. Scan cấu trúc tổng thể file

```bash
python findtool.py --file service.py -mr "\bclass\s+\w+" "def \w+" "^import\s"
# → {"matches": {"\bclass\s+\w+": [1], "def \\w+": [12,45,89,134], "^import\\s": [1,2,3]}}
# Dùng để lên kế hoạch đọc mà không cần view toàn bộ file
```

---

### 5. Đọc imports

```bash
python findtool.py --file index.ts -mr "^import\s"
# → {"matches": {"^import\\s": [1,2,3,4,5,14,15]}}
# min=1, max=15 → view_range [1, 15]
```

---

### 6. Duyệt từng hàm khi chưa biết tên

```bash
# Bước 1: Lấy tất cả dòng có {
python findtool.py --file foo.py -mr "\{"
# → {"matches": {"\\{": [12, 25, 67, 89, ...]}} → OPEN_LINE đầu = 12

# Bước 2: Đọc tên hàm ở dòng đó (và dòng trên nếu { đứng riêng)
view_range [11, 12]

# Bước 3: Nhảy qua body
python findtool.py --file foo.py -c "{" 12 1  → 67

# Bước 4: Tìm { tiếp theo sau dòng đóng
python findtool.py --file foo.py -n "\{" 67  → 70
view_range [69, 70]

# Lặp bước 3–4 đến hết file
```

---

### 7. Xác định dòng đang thuộc scope nào

```bash
# Tìm { mở gần nhất phía trên dòng X
python findtool.py --file dao.py -b "\{" X
# → 134

# Kiểm tra X có nằm trong scope đó không
python findtool.py --file dao.py -c "{" 134 1
# → 189  (nếu 189 > X → X đúng là trong scope này)

# Đọc tên: nếu dòng 134 chỉ có { → tên ở dòng 133
view_range [133, 135]
```

---

### 8. Tìm cặp bao quanh trực tiếp một dòng

```bash
python findtool.py --file app.py -b "\{" X   → OPEN_LINE
python findtool.py --file app.py -n "\}" X   → CLOSE_LINE
python findtool.py --file app.py -c "{" OPEN_LINE 1
# Nếu kết quả == CLOSE_LINE → đây là cặp bao trực tiếp
```

---

### 9. Leo scope chain

```bash
python findtool.py --file app.py -b "\{" 245  →  198
python findtool.py --file app.py -b "\{" 198  →  134
python findtool.py --file app.py -b "\{" 134  →  12
python findtool.py --file app.py -b "\{" 12   →  error → top-level
# Đọc dòng tìm được + dòng trên để biết tên scope
```

---

### 10. Argument list dài nhiều dòng

```bash
python findtool.py --file builder.py -c "(" LINE 1  → CLOSE_LINE
view_range [LINE, CLOSE_LINE]
```

---

### 11. Tìm else / catch / finally

```bash
python findtool.py --file handler.py -c "{" IF_LINE 1  → 78
python findtool.py --file handler.py -n "else|catch|finally" 78
# → 79 → đây là else/catch của block đó nếu kết quả ngay sau 78
```

---

### 12. Decorator / annotation trước hàm

```bash
python findtool.py --file routes.py -b "@\w+" FUNC_LINE
# → Nếu kết quả >= FUNC_LINE - 3 → có decorator trực tiếp
# → Nếu kết quả < FUNC_LINE - 3  → decorator của hàm khác, bỏ qua

view_range [DECORATOR_LINE, CLOSE_LINE]
```

---

### 13. Tìm file theo nhiều tiêu chí (findtree kết hợp)

```bash
# Tìm file test có dùng một service cụ thể
python findtree.py --root ./tests --pattern "OrderService" --include "*.py"

# Tìm file có TODO trong src, bỏ qua vendor
python findtree.py --root . --pattern "TODO" --include "*.ts" "*.js" --exclude "vendor/**" "dist/**"

# Kiểm tra nhanh pattern có tồn tại không, dừng ngay khi tìm thấy 1 file
python findtree.py --root . --pattern "deprecated_api" --max-results 1
# → {"ok":true,"engine":"literal","matched_files":["src/legacy.py"]} hoặc []
```

---

## ⚠️ `{` có thể không cùng dòng với tên hàm

Đây là điểm dễ gây lỗi nhất. Hai style tồn tại song song:

```
# Style 1 — { cùng dòng
processOrder(params) {            ← dòng 247: tên hàm VÀ {

# Style 2 — { xuống dòng riêng
processOrder(params)              ← dòng 247: tên hàm
{                                 ← dòng 248: chỉ có {
```

**Hệ quả:**
- Luôn dùng `-n "\{" FUNC_LINE` để tìm dòng `{` thực tế — không đoán
- Khi `-c` báo `"contains only 0 valid occurrence(s)"` → `{` không ở dòng đó, dùng `-n "\{" FUNC_LINE`
- Khi nhận được dòng chỉ có `{` → tên hàm/class nằm ở **dòng trên**

---

## Bảng quyết định nhanh

| Tình huống | Dùng gì |
|---|---|
| Chưa biết file nào cần đọc | `findtree --root ... --pattern ...` |
| Biết tên hàm, muốn đọc body | `-mr "name"` → `-n "\{" LINE` → `-c "{"` |
| Nhiều hàm cùng lúc | `-mr "fn1" "fn2" "fn3"` |
| Pattern có ký tự đặc biệt, tìm literal | `--engine literal -mr "foo(bar)"` |
| Tìm theo pattern (get/set, class...) | `--engine regex -mr "pattern"` |
| `{` đầu tiên trong file | `-mr "\{"` → `result[0]` |
| Duyệt từng hàm không biết tên | `-mr "\{"` → `-c "{"` → `-n "\{"` → lặp |
| Ở dòng X, muốn biết thuộc hàm nào | `-b "\{" X` → `-c "{"` verify |
| Đọc hết imports | `-mr "^import\s"` → min/max → `view_range` |
| Argument list dài nhiều dòng | `-c "("` |
| Tìm else/catch/finally | `-c "{"` rồi `-n "else\|catch\|finally"` |
| Có decorator/annotation không? | `-b "@\w+" FUNC_LINE` → check khoảng cách |
| Code lồng sâu, tìm scope ngoài cùng | `-b "\{"` lặp đến error |
| File có chứa pattern không? | `-e "pattern"` |

---

## Escape ký tự

Áp dụng cho `-mr`, `-n`, `-b`, `-e` (regex mode). **Không áp dụng cho `-c`/`-o`** (delimiter matching).
Dùng `--engine literal` để tránh escape hoàn toàn.

| Muốn tìm | Regex pattern | Hoặc dùng literal |
|---|---|---|
| `{` | `\{` | `--engine literal -mr "{"` |
| `}` | `\}` | |
| `(` | `\(` | |
| `)` | `\)` | |
| `[` | `\[` | |
| `.` (dấu chấm) | `\.` | |
| `foo(bar)` | `foo\(bar\)` | `--engine literal -mr "foo(bar)"` |
| `a.b` | `a\.b` | `--engine literal -mr "a.b"` |
| Tên hàm thường | `processOrder` | không cần escape |

---

## Xử lý lỗi

| Lỗi | Nguyên nhân | Cách xử lý |
|---|---|---|
| `"For -n, LINE must be >= 0"` | Truyền số âm vào `-n` | Dùng 0 để tìm từ đầu file |
| `"For -b, LINE must be >= 1"` | Truyền 0 vào `-b` | Dùng 999999 để tìm từ cuối file |
| `"contains only 0 valid occurrence(s) of '{'"` | `{` không nằm trên dòng đó | Dùng `-n "\{" FUNC_LINE` để tìm dòng `{` thực tế |
| `"does not have a valid matching"` | Delimiter không có cặp | File có syntax lỗi, hoặc delimiter trong string/comment đã bị bỏ qua |
| `"No line matching pattern ... was found"` | Pattern không khớp trong phạm vi | Kiểm tra lại regex/spelling; với `-b` có thể tăng LINE lên |
| `"Invalid regex pattern"` | Regex sai cú pháp | Hay gặp khi quên escape `(`, `)`, `{` — hoặc dùng `--engine literal` |
| `"Root directory not found"` | `--root` không tồn tại | Kiểm tra lại đường dẫn |

---

## Ví dụ end-to-end

**Bài toán:** Đọc hàm `validateToken` trong codebase 50 file, không biết nó nằm ở đâu.

```bash
# 1. Tìm file
python findtree.py --root ./src --pattern "validateToken"
# → {"matched_files": ["auth/service.py"]}

# 2. Tìm dòng định nghĩa
python findtool.py --file auth/service.py -mr "validateToken"
# → {"matches": {"validateToken": [312, 467]}}  → 312 là định nghĩa

# 3. Kiểm tra decorator
python findtool.py --file auth/service.py -b "@\w+" 312
# → {"line": 310}  → có decorator ở dòng 310

# 4. Tìm dòng {
python findtool.py --file auth/service.py -n "\{" 312
# → {"line": 313}

# 5. Tìm dòng đóng
python findtool.py --file auth/service.py -c "{" 313 1
# → {"line": 389}

# 6. Đọc
view_range [310, 389]
```

**Token tiêu thụ:** ~80 dòng thay vì hàng nghìn dòng. Tiết kiệm >90%.
