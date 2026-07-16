# 中國醫皮膚科常用程式

中國醫藥大學附設醫院皮膚部的診間/行政自動化套件。六支獨立程式共用一套基底
（自動更新、單例、設定、日誌、通知），單機執行、多台電腦各自部署、線上自動更新。

> ⚠ **本 repo 為 Public。** `settings/`（帳密、員編、個資）永遠不進版控；
> 推送一律走 `scripts/push_helper.py`（內含 sanity check）。詳見「安全性」一節。

## 六支程式（雙擊根目錄啟動器執行）

| 啟動器 | 原始碼 | 功能 |
|---|---|---|
| `中國醫皮膚科主程式.pyw` | `src/main.py` | 掛號人數監控（本院/東區/亞大/惠和/惠盛）、門診動態浮窗（診間燈號/候診）、止掛提醒（分級優先刷新＋Email 通知）、F1–F12 HIS 熱鍵自動化（照光醫令、UVB 劑量自動計算寫回、同意書、轉診、健保卡 OCR）、縮寫速寫、打卡狀態燈號、值班表顯示 |
| `中國醫皮膚科排班程式.pyw` | `src/scheduler.py` | 醫師排班：R/VS 值班（CP-SAT 求解，點數公平＋假日成對＋色塊連週＋連續值班軟限制）、R2/R3 週六切片輪排、PGY/Clerk 日排班（照光/治療室/切片室七步驟填充）、月曆編輯、決策報告、xlsx/docx/pdf 匯出、定案留底 |
| `中國醫皮膚科打卡程式.pyw` | `src/autoclock.py` | Selenium 自動打卡（多帳號、排班判斷、漏打警告）、托盤常駐 |
| `中國醫皮膚科守護程式.pyw` | `src/watchdog_runner.py` | 看門狗：監看會診查詢/打卡是否存活，死掉自動重啟 |
| `中國醫皮膚科會診查詢程式.pyw` | `src/consult_query.py` | 週期輪詢住院系統會診清單（隱藏桌面跑 systemftp，不干擾使用者），偵測新會診即擷取＋寄信；亦支援 Email 觸發即時查詢（IMAP） |
| `中國醫皮膚科點座標偵測程式.pyw` | `src/coord_detector.py` | F8 記錄螢幕座標/顏色（開發除錯工具） |

## 快速開始

### 安裝（新電腦）

已有完整資料夾 → 雙擊根目錄 `第一次執行先點我.bat`：自動偵測 Python（無則下載
Embedded Python）、安裝相依套件、建立桌面捷徑、接上線上自動更新。

從零開始（資料夾都沒有）→ 先下載 bootstrap 再執行：

```cmd
powershell -Command "Invoke-WebRequest -Uri 'https://raw.githubusercontent.com/expertise88864/CMUHdermatology/main/%E7%AC%AC%E4%B8%80%E6%AC%A1%E5%9F%B7%E8%A1%8C%E5%85%88%E9%BB%9E%E6%88%91.bat' -OutFile '%USERPROFILE%\Desktop\第一次執行先點我.bat'"
%USERPROFILE%\Desktop\第一次執行先點我.bat
```

安裝異常查 `settings/python_setup.log` 與 `settings/dependency_install.log`。
開機自動啟動用根目錄的 `安裝開機自動啟動.cmd`。

### 執行

雙擊對應 `.pyw` 啟動器即可（首次會自動補裝缺少的套件；ortools、openpyxl 等重依賴
採 lazy 安裝——用到該功能才裝）。命令列等價：`pythonw src\main.py`。

## 架構

```
（repo 根目錄）
├── 中國醫皮膚科*.pyw      # 6 個啟動器（兜底錯誤記錄 startup_crash.log）
├── 第一次執行先點我.bat    # 新機部署 bootstrap
├── manifest.json           # 自動更新清單（SHA256 逐檔）
├── src/
│   ├── main.py              # 主程式（單檔 Tk 應用，含 HIS 自動化/UVB 劑量引擎）
│   ├── scheduler.py         # 排班程式入口（UI 在 cmuh_common/roster/ui/）
│   ├── autoclock.py         # 打卡程式
│   ├── consult_query.py     # 會診查詢
│   ├── watchdog_runner.py   # 守護程式
│   ├── coord_detector.py    # 點座標偵測
│   ├── clock/               # 打卡專用（webdriver_setup）
│   └── cmuh_common/         # 共用基底（47 模組，~14k 行）
│       ├── updater.py / deps_runtime.py / single_instance.py / paths.py …
│       │                    # 更新鏈、lazy 依賴、單例、路徑
│       ├── abbrev_engine.py               # 縮寫速寫（低階鍵盤 hook + 原子 SendInput）
│       ├── floating_clinic.py / clinic_*.py / reg64_utils.py / threshold_policy.py
│       │                    # 門診動態浮窗、診間狀態/歷史、掛號門檻判定
│       ├── punch_status.py / chrome_options.py    # 打卡狀態查詢（headless Chrome）
│       ├── smtp_mail.py / imap_reader.py / notifications.py   # 通訊
│       ├── atomic_io.py / config_io.py / app_settings.py / sqlite_cache.py
│       │                    # 原子寫檔、設定、快取
│       ├── task_gate.py / bounded_executor.py / health.py / hotkey_guardian.py
│       │                    # 任務互斥 lease、背景執行緒池、RAM 保險絲、熱鍵看門狗
│       └── roster/          # 排班引擎套件（分層範本：model/rules/solve/service/storage/ui/export）
│           ├── model.py / rules.py / solve_rvs.py / solve_day.py   # CP-SAT 值班 + 日排班
│           ├── saturday_biopsy.py / ledger.py                      # 週六切片輪排、點數帳本
│           ├── service.py / storage.py / gitsync_storage.py        # 黏合層、檔案 IO、跨機同步
│           ├── export_xlsx.py / export_docx.py / export_pdf.py / report.py
│           └── ui/          # Tk 分頁（R/VS 月曆、PGY/Clerk、設定）
├── scripts/                 # push_helper（版本 bump+manifest+push）、sanity_check、probe_* 除錯
├── tools/                   # codex_review.sh（外審 wrapper）、dev-env-setup、視窗結構探測 .cmd
├── tests/                   # 110 檔、1500+ 測試（pytest）
├── docs/                    # 設計文件（排班規格/施工指南/審查計畫書）
└── deploy/                  # 部署輔助
```

設計慣例（全 repo 一致）：

- **醫療保守**：UVB 劑量/醫令寫回「不確定就不動、交醫師」；寫入 fail-closed、唯讀 fail-open；
  寫回後 round-trip 驗證；F12 全鏈可取消。
- **決定性**：排班 CP-SAT 固定 seed、單 worker、ortools 釘版（`9.15.6755`）——同輸入同輸出。
- **對外禮貌**：所有院方網站輪詢帶隨機抖動（反固定節拍）、退避（backoff）、夜間降頻。
- **Tk 只在主緒碰**；背景工作經 UI queue 回主緒；跨行程單例（mutex）。

## 線上自動更新

啟動與定時檢查 → 拉 `manifest.json` → 版本比較 → 平行下載（SHA256 逐檔校驗、
OS 級跨行程檔案鎖、防降版）→ 全部成功才寫入，失敗保留舊版不阻擋啟動 →
需要重啟時走「熱鍵閒置閘門」（自動化進行中不重啟，避免醫令打到一半被砍）。

## 開發流程

```cmd
python -m pytest -q                 # 全套測試（必須全綠）
ruff check src scripts tests        # lint（F/E9 真 bug 類）
python -m pyright                   # 型別檢查
tools/codex_review.sh targeted HEAD ctx.txt   # 外部 read-only 審查（GPT-5.6）
python scripts\push_helper.py "訊息"          # bump 版本 + manifest + commit + push
```

- **上線閘門**：非 docs-only 變更須經 `tools/codex_review.sh` 外審，**跑到 APPROVE 才 push**
  （REQUEST_CHANGES → 驗證 findings、只修 CONFIRMED、resume 同 session 續審）。
- **絕不手動 `git commit`+`push` src**——一律走 `push_helper.py`（含 sanity check 擋
  settings/個資、自動 bump `cmuh_common/version.py`、重生 manifest）。
- CI（GitHub Actions, windows-latest, Python 3.13）：ruff + pyright + 全套 pytest
  （含 ortools 釘版安裝，排班 solver 測試真跑）。
- 高風險檔（`main.py`、`uvb_dose.py`）改動慣例：小步提交、patch 備份、
  回歸測試紅→綠、涉醫療劑量用 `deep` 模式外審。

## 測試慣例

- 純邏輯（劑量計算、排班規則、門檻判定）→ 直接單元測試。
- Win32/Tk/Selenium 路徑（headless 無法跑）→ 原始碼守門測試（AST/inspect 斷言關鍵
  守衛存在且順序正確）＋假物件行為測試。
- 排班 solver 測試依賴決定性（固定 seed），驗證精確指派結果。

## 安全性（Public repo 紅線）

- `settings/`（帳密、員編、Email、個人設定）已在 `.gitignore`，**絕對不推上去**；
  `push_helper.py` 的 sanity check 會在 commit 前擋下誤加。
- 病人資料零落地原則：OCR 暫存 try/finally 保證清除；log 不記病歷內容；
  匯出檔（含醫師姓名）不進版控。
- 自動更新完整性：manifest SHA256 逐檔校驗 + 防降版 + 原子寫入。

## 文件

| 文件 | 內容 |
|---|---|
| `docs/排班程式設計文件.md` | 排班完整規格（規則定案表 G/R/V/P/C 系列） |
| `docs/排班程式審查與施工指南.md` | 排班施工分期與 service/UI 介面 |
| `docs/排班_roster套件API地圖.md` | roster 套件對外 API 速查 |
| `docs/未審區域review計畫書_2026-07-09.md` | 五程式全面審查 findings 與修正進度（§8A） |
| `docs/五程式完整回顧審查優化計畫書_2026-07-08.md` | 上一輪全面審查記錄 |
