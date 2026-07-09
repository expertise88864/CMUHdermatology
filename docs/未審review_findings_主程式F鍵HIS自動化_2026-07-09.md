# 未審 Review — 主程式 F 鍵 HIS 自動化 findings（2026-07-09）

> 屬《未審區域review計畫書_2026-07-09.md》階段 1/3 的一部分。審查範圍：`src/main.py` 的
> F1–F12 腳本、Win32 helpers、熱鍵註冊/派工/看門狗（約 1263–5530、12903–13410），及
> `src/cmuh_common/ditto_card_ocr.py`、`src/cmuh_common/pixel_picker.py` 全文。**純審查、未改碼**。
>
> **驗證等級**：finding 未經第二層對抗驗證。**Opus 執行每條前必須先重讀該行號段落確認現況**
> （行號以審查當下工作樹為準、會漂移；不符→記錄跳過）。醫療寫入/送出方向的破口優先。
> 執行紀律見計畫書 §0（push_helper、codex、回歸測試、OneDrive 防護）。

---

## 高風險（正確性/文書安全，建議必修 → P1）

### H1. F9/F10 會對「全系統任意 #32770 對話框」自動按「是」
- `src/main.py:4546–4562、4590–4608`（`_f9_f10_round4_submit_and_confirm`）
- 用 `_wait_for_window("#32770", title_kw="")` 全域掃描、不驗行程也不驗標題，即 `PostMessageW(dlg, WM_COMMAND, IDYES)`。10 秒等待窗內，任何其他程式/HIS 其他流程跳出的標準對話框（存檔/刪除/警告）都可能被自動按「是」。dlg2 備援再補 IDYES＋IDOK，且 dlg2 掃描只 exclude popup_hwnd、沒 exclude 第一個 dlg → 同對話框可能被重複轟。
- **修法**：以 `GetWindowThreadProcessId` 比對 TFopdmain 的 PID，只對 HIS 行程的對話框動作；可再加 title 關鍵字（「警告」）雙重把關。

### H2. F9/F10：Round 2/3 失敗仍照樣 Round 4「開立電子＋自動按是」送出
- `src/main.py:5482、5487、5494`（`script_F9_F10_consent_form_adaptive`）
- `_f9_f10_round2_popup_actions`（清空所患疾病/手術原因＋勾局麻）與 `_f9_f10_round3_phrases`（兩片語）回傳值被完全忽略。欄位沒清成/局麻沒勾/片語沒選，仍自動送出電子同意書並替醫師按掉警告＝文書事故路徑。
- **修法**：round2/3 任一失敗即中止交人工，或送出前對欄位做 read-verify。

### H3. `_switch_tab_by_text` ActivePage 切換失敗仍回 True → 可能開錯類型同意書
- `src/main.py:5008–5013`（caller `5385–5389`）
- 函式註解自承「開立電子行為依視覺 ActivePage 決定→失敗會送錯同意書」，但回傳第一值只代表「sheet 找到」；swap 失敗（success=False）仍回 `(True, sheet)`，caller 不中止。歷史（v27/v28）曾產出「微小皮膚移植」而非預期同意書。
- **修法**：把 success 納入回傳、失敗即中止；或點「開立電子」前用 z-order 再驗 active sheet。

### H4. `_f23_pure_excimer_update` 確認對話框後缺 `check_stop()`，F12 取消不被尊重
- `src/main.py:2036–2048`
- 對照 `_update_uvb_dose_core` 的 CONFIRM_NEEDED（2186）與 uncertain（2295）路徑，MessageBox 回來後都有 `check_stop()`；純 excimer 沒有。醫師停在 Yes/No 時按 F12（`interrupt_automation` 13081–13091）解鎖並把舊緒加入取消集合，但舊 worker 在醫師按「是」後直接重算並 `_write_tmemo_text` 寫回處置欄、不看取消旗標；接著 F2/F3 的 `_set_身份_自費`（3816 起）也沒 check_stop，身份欄照改。
- **修法**：`_photo_confirm_yesno` 返回後、任何寫入動作前一律 `check_stop()`。

### H5. 轉診預掛表格「畫面取樣」不驗證視窗在前景/未被遮擋
- `src/main.py:3013–3026`
- F11 全程掛 ForegroundProtector（3688），使用者切到 Chrome 時 TFTunMsg 在背景被處理；`pyautogui.screenshot(region=grid_rect)` 抓的是螢幕最上層像素＝Chrome 網頁（深色文字多）→ 幾乎必然誤判「有預約」→ 誤勾「本科門診進一步追蹤治療」、漏印轉回單。同專案卡號 OCR（4107–4111）有「必須在最前景否則放棄」把關，這裡沒有。
- **修法**：取樣前確認 TFTunMsg 為前景（或 WindowFromPoint 驗證取樣點屬於該 grid），否則跳過此分支走保守「轉回原診所」路徑。

---

## 中風險（P2）

### M1. HIS 選單 command ID 寫死，改版整批位移已是既成事實
- `MENU_ID_代碼輸入=219 @1306`、`MENU_ID_同意書=669 @4231`、`MENU_ID_FINISH_NO_PRINT=277 @3518`
- 2026-06-29 改版所有 ID +1 事故已發生過一次。「完成不印」owner-drawn 動態文字解析必失敗→永遠走 hardcode id；同意書路徑 25s 逾時後還「重送一次」同一未知命令（5368–5373）。下次改版這些 WM_COMMAND 會觸發未知選單功能。
- **修法**：啟動時讀主視窗 title 的 HIS 版本字串比對已校正版本，不符即停用 F 鍵並明確警告，而非照送。

### M2. `_wait_for_code_input_focus` 在 `previous_focus=0` 時放寬條件，醫令代碼可能打進病歷內文
- `src/main.py:1413–1428、1566–1592`
- `_get_thread_focus` 失敗回 0 時「任何 input-like 焦點」立即通過（`not previous_focus` 分支）。若同時選單命令沒生效（如 M1 的 ID 漂移），焦點仍停在醫師剛打字的病歷 TMemo → `51019`+Enter 進病歷文字。且整段代碼輸入無 read-back 驗證，回 True 只代表 PostMessage 成功。
- **修法**：previous_focus 讀不到時改嚴格判準（必須觀察到焦點變化、class 限縮為 grid inplace-edit），並考慮事後驗證。

### M3. `_f11_popup_watcher` 的 `handled`/`retry_counter` 以裸 hwnd 為 key，HWND 會被回收重用
- `src/main.py:3300–3304、3395–3399`
- 60 秒 watcher 期間，已關 popup 的 hwnd 被新 popup 重用 → 誤判 already-handled 永久跳過（「popup 沒被按」）。同 class 第二個實例在第一個（handled 未關）存在期間也掃不到。
- **修法**：handled 項目定期以 `IsWindow` 清理，或 key 加上 class。

### M4. `_replace_edit_text` fallback 以螢幕座標實體點擊＋typewrite，無前景/遮擋檢查
- `src/main.py:3891–3935`
- HIS 被蓋住時 click 落在覆蓋視窗上、把「01」/療程值打進別的應用程式（read-verify 事後會失敗警告，但輸入副作用已發生）。
- **修法**：fallback 前先 `_ensure_hospital_foreground` 並用 WindowFromPoint 驗點擊點確為目標欄位。

### M5. `_f11_precheck_card_for_phototherapy` 未做全形數字正規化
- `src/main.py:3649–3651`（vs 主流程 `_f11_read_course_value` 3535 有 `_f11_normalize_course_value`）
- precheck 沒 normalize：療程欄若是全形「２」會跳過卡號檢查、仍走照光「完成不印」→ 卡號空白照樣完成。
- **修法**：兩處共用同一 normalize。

### M6. ditto_card_ocr 在診間機器 runtime 背景 `pip install winsdk`
- `src/cmuh_common/ditto_card_ocr.py:277–291`
- 生產環境對 PyPI 做 runtime 安裝：供應鏈風險；pythonw 下 subprocess 未帶 CREATE_NO_WINDOW 會閃 console；裝進共用環境影響其他功能。
- **修法**：部署時預先打包、移除 runtime 安裝路徑。

---

## 低風險 / 備註（P3）

- **L1.** 未滿 18 Information dialog（`main.py:5439–5447`）直接點 `buttons[0]`，沒驗「只有一顆按鈕」；若出現 Yes/No 變體會誤點。
- **L2.** `pixel_picker.py` 全支無呼叫端（只在 manifest 部署清單）＝死碼；`pick_pixel`（30% alpha）抓到的 RGB 是隔黑 overlay 的混色值（docstring 自承），日後啟用只該用 `pick_pixel_with_accurate_color`；其 73–79 行為無作用死碼。
- **L3.** OCR 臨時 PNG（含病人資料截圖）寫入共用 `%TEMP%`（`main.py:4121`、`ditto_card_ocr.py:366`），異常中斷/save_debug 會留 PHI 檔；建議專屬目錄＋try/finally 保證清除。
- **L4.** `_hotkey_awaiting_user` module-level bool；F12 解鎖＋新熱鍵重疊邊界下，舊緒 scope 退出會蓋掉新流程 awaiting 狀態——僅影響看門狗訊息分類，無資料風險。
- **L5.** `HOTKEY_LENIENT_CLASSES` 含 `#32770`（`main.py:13205`）：任何程式的標準對話框在前景時 F9–F12 都可觸發（guard 只看 class 不看行程）。
- **L6.** `_update_uvb_dose_core` 寫回後 read-back 為空（逾時）時直接跳過 verify 續跑 51019（`main.py:2320–2321`）——與 W7 註解一致屬刻意 best-effort，列出供知悉。

---

## 正面觀察（穩健，不要動）

卡號 OCR 完整把關鏈（僅空欄才填→前景驗證→4 碼＋同卡交叉→貼表頭幾何檢查→寫後 verify→fail-open）；`_set_身份_自費` 正向樣式把關（1–3 位數字才寫）＋read-verify；看門狗「worker 活著絕不解鎖」W1 設計；全面 SMTO_ABORTIFHUNG 的 `_wm_settext/gettext_timeout`；`_find_hospital_main_window` 的 `call_with_timeout` 防凍結；`SendMessageTimeoutW` argtypes 與 abbrev_engine 對齊。

**建議優先序**：H1–H5（自動送出/寫入方向破口）> M1（HIS 版本守門）> 其餘。
