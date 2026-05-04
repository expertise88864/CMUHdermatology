# 中國醫皮膚科常用程式

中國醫藥大學附設醫院皮膚科自動化套件，包含四支主程式：

| 入口 | 說明 |
|---|---|
| `src/main.py` | 看診管理、掛號監控、F3/F4/F9/F10/F11 多解析度熱鍵自動化 |
| `src/scheduler.py` | 排班視覺化、值班輪替演算法 |
| `src/autoclock.py` | Selenium 自動打卡、托盤常駐、排程 |
| `src/coord_detector.py` | F8 記錄座標/顏色（除錯工具） |

## 安裝（給其他電腦）

下載 `deploy/installer.bat`，雙擊即可：

```cmd
powershell -Command "Invoke-WebRequest -Uri 'https://raw.githubusercontent.com/expertise88864/CMUHdermatology/main/deploy/installer.bat' -OutFile '%USERPROFILE%\Desktop\installer.bat'"
%USERPROFILE%\Desktop\installer.bat
```

- 有 Python 3.10+ → 直接用系統 Python
- 沒 Python → 自動下載 Embedded Python 3.12
- 兩種情況都可線上自動更新（程式碼始終是純 .py）

## 開發者：日常推送

```cmd
cd C:\Dev\CMUHdermatology
push.bat "修正 X 問題"
```

`push.bat` 會自動 bump 版本、算 SHA256、寫 manifest.json、commit、push。

## 架構

```
src/
├── main.py / scheduler.py / autoclock.py / coord_detector.py   # 入口
├── cmuh_common/        # 兩大程式共用基底（version/paths/updater/deps/icons/...）
├── network/            # 院內抓網（reg52/reg64/duty/master_schedule）
├── hotkey/             # 多解析度熱鍵（1920×1080/1280×1024/1024×768）
├── ui/                 # Tkinter 視窗
└── clock/              # 打卡程式專用（webdriver/login/perform_action/...）
```

詳見 `docs/`。

## 線上更新流程

啟動 → 拉 manifest.json → 比對 CURRENT_VERSION（tuple 比較）
→ 平行下載新版（ThreadPoolExecutor，含 SHA256 校驗）→ 全部成功才寫入
→ 失敗則保留本地舊版（不阻擋啟動）→ 有更新時提示並重啟。

`.exe` 模式只查不寫（Windows 鎖檔），跳通知請使用者去 Releases 下載新版。

## 安全性

`settings/` 目錄含明文密碼（`autoclock_config.json`），已在 `.gitignore` 排除。
本 repo 為 Public，**絕對不要把 settings/ 推上去**。`push.bat` 內 `sanity_check.py`
會在 commit 前擋下任何不慎追蹤到的 settings/。
