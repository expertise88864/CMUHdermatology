# requirements.txt 的取捨說明

`requirements.txt` 本身**只能有 ASCII**，所以中文說明放在這裡。

## ★為什麼那個檔案不能有中文★

`pip` 讀 requirements 檔是用**系統 locale 的編碼**（`pip._internal.utils.encoding.auto_decode`），
不是固定 UTF-8。所以只要檔案裡有任何非 ASCII 位元組，在 locale 不是 UTF-8 的機器上
（cp936 / cp1252 / …）`pip install -r requirements.txt` 會直接死於
`UnicodeDecodeError` —— 程式**根本裝不起來**。

2026-07-30 實測確認：舊版 `requirements.txt`（含中文註解）在一台 cp936 的機器上
用乾淨 venv 安裝，pip 就是這樣掛掉的。CI 之所以一直是綠的，只是因為 GitHub runner
的 locale 剛好吃得下 —— **那是運氣，不是設計**。

同一個坑本 repo 已經踩過另一個版本：`pip-audit` 的 requirements 解析器也用 locale
編碼，所以 `scripts/audit_deps.py` 刻意改成掃「已安裝的環境」而不是掃 requirements 檔。

## 版本上限的原則

上限 pin 在**下一個 major**：避免新機器（或 runtime fallback 安裝）抓到破壞性新版。
要升級時手動把上限往上調，不要自動放寬。
0.x 與 date-based 版號的套件（`keyboard` / `pyautogui` / `pystray` / `pywin32`）
不 pin 上限 —— 語意化版號對它們不適用，pin 了只會擋住修正。

## Pillow：上限從 `<12` 提到 `<13`（2026-07-30，外審 P2-07）

Pillow 11.x 帶著 20 幾個已知弱點（PYSEC-2026-2250 / 2253 / 2254 / 2255 / 2256 /
2257 / 2874 / 3451 / 3453 / 3454 / 3493 / 3494 / 3495 / 3496 …），而**修正版是
12.3.0** —— 舊的 `<12` 上限恰好把修正擋在外面。這種「上限把自己的修正擋住」是
最容易被忽略的一種相依風險：`pip install -U` 看起來很正常，掃描器卻一直紅。

不直接把 20 幾個 CVE 加進 `security/pip_audit_allowlist.json` 的理由很簡單：
**能升就升**，允許清單是留給真的升不上去的情況。

升級風險評估（本專案只用這些 Pillow API）：

| API | Pillow 12 狀態 |
|---|---|
| `Image.open` / `Image.new` / `Image.resize` | 不變 |
| `Image.Resampling.LANCZOS`（含 `reducing_gap`） | 不變（本專案**早已**改用 `Resampling`，不是被移除的 `Image.ANTIALIAS`） |
| `ImageDraw.Draw` | 不變 |
| `ImageGrab.grab(bbox=..., all_screens=True)` | 不變 |
| `ImageTk` | 不變 |

已在乾淨 venv 實裝 Pillow 12.3.0 逐項執行驗證過（非只看文件）。

## 不在這個檔案裡的相依（刻意）

以下是 **lazy 安裝**：使用者用到那個功能時才裝，`requirements.txt` 刻意不含，
以免每台機器都背這些重量級套件。CI 會另外裝它們（見 `.github/workflows/ci.yml`），
否則對應測試會**靜默 skip＝等於沒測**。

* `ortools`（排班求解器，版本釘在 `roster/__init__.py` 的 `ORTOOLS_PINNED_VERSION`）
* `openpyxl` / `python-docx` / `reportlab`（排班匯出）
