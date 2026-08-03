# 住院醫囑系統：Borland Database Engine 初始化失敗（error $250E）

> 2026-08-03 實機事故紀錄與處置。會診查詢連續失敗 22 次、每次告警都寫「請確認
> 帳號密碼是否被院方改過/停用」，手動開程式才看到真正的畫面是 BDE 初始化失敗。
> **帳號密碼完全沒問題**，方向被錯誤訊息帶偏 —— 訊息已於 v2026.08.03.11 修正。

## 這個錯誤代表什麼

住院醫囑系統（`C:\admc\systemftp.exe`）是 Delphi + BDE（Borland Database Engine）
寫的。這個對話框出現時，**程式根本還沒起來**：BDE 初始化失敗 → 主視窗與登入視窗
被那個 modal 擋成 disabled。自動化看到的畫面是：

```
TFrmLogin(vis=1, en=0)        ← 登入視窗在，但是被停用（有 modal 蓋著）
TFMTimeOut_1(vis=1, en=1)     ← 唯一 enabled 的視窗
TmrWindowClass / PsockWindowClass / TPUtilWindow (vis=0)   ← BDE/Delphi 的隱藏基礎視窗
```

外觀與「帳密不對所以停在登入頁」**一模一樣**，這是舊訊息誤導的原因。

## 處置（由快到慢，通常第 1 步就好）

### 1. 重新開機（最常見、成功率最高）
$250E 幾乎都是**資源/鎖檔殘留**造成的：BDE 的鎖檔（`PDOXUSRS.NET`、`*.LCK`）
被已經死掉的行程佔著，或系統 handle 用盡。重開機把兩者一起清掉。

重開機前可以先試「輕量版」：關掉所有 HIS 相關行程再開一次。

```bash
tasklist | findstr /i systemftp
```

若列出多個 `systemftp.exe`，全部結束後再開：

```bash
taskkill /IM systemftp.exe /F
```

### 2. 清掉 BDE 鎖檔
找出 BDE 的 NET DIR（鎖檔放置處）：

```bash
reg query "HKLM\SOFTWARE\WOW6432Node\Borland\Database Engine\Settings\SYSTEM\INIT" /v NET DIR
```

到該目錄刪除 `PDOXUSRS.NET`（**確定沒有人在用 HIS 時才刪**；它會自動重建）。
若 NET DIR 指向網路磁碟機而該磁碟機當下連不上，也會出現這個錯誤 —— 這種情況要
恢復網路磁碟機或請資訊室把 NET DIR 改到本機路徑。

### 3. 權限
BDE 需要對這兩個地方有寫入權：
- BDE 安裝目錄（通常 `C:\Program Files (x86)\Common Files\Borland Shared\BDE`）
- 登錄機碼 `HKLM\SOFTWARE\WOW6432Node\Borland\Database Engine`

以系統管理員身分執行 HIS 可以繞過大部分權限問題；長期解要請資訊室調權限。

### 4. 磁碟空間 / 暫存目錄
`C:` 或 `TEMP` 滿了也會讓 BDE 起不來。確認可用空間：

```bash
wmic logicaldisk get caption,freespace,size
```

### 5. 以上都無效 → 請資訊室重裝／修復 BDE
這時多半是 `IDAPI32.DLL` 或 BDE 設定損毀，不是我們能在應用層處理的。

## 程式端已經做的事（v2026.08.03.11）

- **認得這個錯誤**：偵測到 `Borland Database Engine` 字樣就丟 `HISStartupBlocked`，
  告警信改寫「住院醫囑系統自己沒起來（error $250E），★這不是帳號密碼問題★」。
  只擷取錯誤碼、不記錄任何視窗文字（隱私邊界同 `_describe_windows_for_diag`）。
- **不重試**：BDE 起不來時再登一百次也一樣，重試只會浪費時間。
- 連續失敗告警本來就有節流（最多 6 小時一封，狀態已落地故重啟不會歸零）。

## 判斷「是不是這一種」

看 `settings/consult_query.log`：

| log 訊息 | 意思 | 該做什麼 |
|---|---|---|
| `住院醫囑系統起不來 → 不重試(與帳密無關)` | BDE 初始化失敗 | 照本文處置 |
| `登入沒有完成 → 不重試` | 真的停在登入頁 | 查帳號密碼／HIS 連線 |
| `等不到住院醫囑主畫面` | 登入過了但主畫面沒出來 | 看視窗清單，多半是有 modal 擋著 |
