# -*- coding: utf-8 -*-
"""HIS 熱鍵 ID 修正工具（給不會寫程式的人用）。

什麼時候用：醫院 HIS（西醫門診醫師作業）改版後，
  * 按 F1～F5，游標沒有跳到下方「醫令代碼」輸入欄；或
  * 按 F9／F10，沒有開出「同意書開立作業」（或開成別的視窗，例如診斷書）。
這通常是 HIS 的「選單編號（ID）」位移了。本工具一步一步帶你找出新的編號，
寫進這台電腦的「本機快速修正」，重開主程式就生效，不必等程式更新。

怎麼開：雙擊程式資料夾裡的「修正HIS熱鍵ID.cmd」（會跳出「是否允許變更」→ 按「是」）。
詳細步驟見同資料夾的「HIS熱鍵ID修正工具_使用說明.txt」。

安全設計：
  * 測試只會「開啟對話框／視窗」，看完按取消即可，不會送出或完成病歷；
  * 測試範圍只限「代碼輸入」與「同意書」附近的安全編號，★刻意不含「完成」選單★；
  * 寫入前一律用主程式自己的驗證規則檢查（his_contract.parse_override），
    檔案原子寫入；正式校正推版後，本機快速修正會自動失效，不會蓋掉新的正式值。
（2026-10-21 由 scripts/test_yiling_menu_id.py 改寫並移到程式資料夾。）
"""
from __future__ import annotations

import ctypes
import datetime
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))

TARGET_CLASS = "TFopdmain"
TARGET_TITLE_KW = "西醫門診醫師作業"
WM_COMMAND = 0x0111
GUIDE_FILE = "HIS熱鍵ID修正工具_使用說明.txt"
OVERRIDE_FILE = os.path.join("settings", "his_menu_override.json")

#: 可以修正的功能。range = 測試按鈕允許的安全範圍（只會開對話框/視窗）。
FUNCTIONS = (
    {"key": "MENU_ID_代碼輸入", "name": "代碼輸入", "hotkeys": "F1、F2、F3、F4、F5",
     "symptom": "按 F1～F5 後，游標沒有跳到下方「醫令代碼」輸入欄",
     "success": "主程式畫面的游標跳到下方「醫令代碼」輸入欄（可以直接打字的那一格）",
     "range": (213, 234)},
    {"key": "MENU_ID_同意書", "name": "同意書", "hotkeys": "F9、F10",
     "symptom": "按 F9／F10 後沒有開出「同意書開立作業」，或開成別的視窗（例如診斷書）",
     "success": "跳出「同意書開立作業」視窗",
     "range": (663, 679)},
)


# ─── 純函式（有測試）──────────────────────────────────────────────────────
def parse_his_version(title: str):
    """HIS 標題 → (主版本, 完整版本)；讀不到回 (None, None)。例：V.1151020.01 → ("1151020","1151020.01")。"""
    m = re.search(r"V\.?\s*(\d{6,8})(?:\.(\d+))?", title or "")
    if not m:
        return None, None
    major = m.group(1)
    return major, (f"{major}.{m.group(2)}" if m.group(2) else major)


def clamp_step(current: int, step: int, rng) -> int:
    """在安全範圍內往前/往後一格（到邊界就停在邊界）。"""
    lo, hi = rng
    return max(lo, min(hi, int(current) + step))


def build_override(hc, existing, confirmed: dict, record_version: "str | None"):
    """組本機快速修正檔內容 → (data, error)。

    existing: 磁碟上現有的檔(可能為 None/壞掉/過期);confirmed: {MENU_ID_*: id}。
    ★只有戳記仍是現任的舊內容才保留★:過期檔裡的其他鍵是上一個校正時代的 id,
    合併後蓋上新戳記等於把過期值復活 —— 熱鍵打到別的功能。"""
    marker = hc.override_marker()
    data = {}
    if isinstance(existing, dict) and existing.get("for_calibration") == marker:
        data = {k: v for k, v in existing.items()}
    data.update({k: int(v) for k, v in confirmed.items()})
    if record_version:
        data["CALIBRATED_VERSION"] = str(record_version)
    data["for_calibration"] = marker
    data["note"] = f"HIS熱鍵ID修正工具 實測 {datetime.date.today().isoformat()}"
    _updates, err = hc.parse_override(data, marker)
    return (data, "") if not err else ({}, err)


def override_status(hc, ov) -> dict:
    """步驟 1「本機快速修正」那一行的文字與顏色(用主程式同一套規則判斷有沒有效)。"""
    if ov is None:
        return {"text": "本機快速修正：目前沒有", "foreground": "#000000"}
    shown = "、".join(f"{k.replace('MENU_ID_', '')}={v}" for k, v in
                     (ov.items() if isinstance(ov, dict) else ())
                     if k.startswith("MENU_ID_") or k == "CALIBRATED_VERSION")
    err = ""
    if hc is not None:
        _updates, err = hc.parse_override(ov, hc.override_marker())
    if err:
        return {"text": f"✖ 本機快速修正檔無效，主程式不會使用（{err}）。"
                        "請重新測試後套用，或按「清除快速修正」", "foreground": "#B00020"}
    return {"text": f"本機快速修正：生效中 → {shown or '（無）'}（重開主程式後套用）",
            "foreground": "#006400"}


def report_text(version_full, ids: dict) -> str:
    """回報給開發者的一行文字（讓正式版本也跟上）。"""
    parts = [f"{f['name']}={ids[f['key']]}" for f in FUNCTIONS if f["key"] in ids]
    ver = f"V.{version_full}" if version_full else "（版本讀不到）"
    return (f"HIS {ver} 熱鍵 ID 實測：{'、'.join(parts) or '（無）'}"
            f"（HIS熱鍵ID修正工具，{datetime.date.today().isoformat()}）")


# ─── Windows / 主程式 ─────────────────────────────────────────────────────
def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def relaunch_as_admin() -> bool:
    """以系統管理員身分重新開啟本工具（會跳出 Windows 的允許視窗）。"""
    try:
        exe = sys.executable.replace("python.exe", "pythonw.exe")
        if not os.path.isfile(exe):
            exe = sys.executable
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", exe, f'"{os.path.abspath(__file__)}"', ROOT, 1)
        return int(rc) > 32
    except Exception:  # noqa: BLE001
        return False


def find_his_window():
    """→ (hwnd, title)；找不到回 (0, "")。"""
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    found = [0, ""]
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _text(hwnd):
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(max(n, 0) + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value

    @proc
    def cb(hwnd, _lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            if cls.value != TARGET_CLASS:
                return True
            title = _text(hwnd)
            if TARGET_TITLE_KW in title:
                found[0], found[1] = hwnd, title
                return False
        except Exception:  # noqa: BLE001
            pass
        return True

    user32.EnumWindows(cb, 0)
    return found[0], found[1]


def send_menu_command(hwnd: int, cmd_id: int) -> None:
    """送選單指令給 HIS（同步 SendMessage；需要管理員權限，否則 Windows 會擋掉）。"""
    ctypes.windll.user32.SendMessageW(hwnd, WM_COMMAND, int(cmd_id), 0)


def load_running_contract():
    """載入【正在使用的那一份】程式的 his_contract（部署機由 current.txt 指定版本）。
    → (module, 錯誤訊息)。失敗時工具仍可開啟，但不能寫入快速修正。"""
    os.environ.setdefault("CMUH_APP_DIR", ROOT)   # 讓 cmuh_common.paths 錨在程式資料夾
    try:
        src_dir = os.path.join(ROOT, "src")
        vp_path = os.path.join(ROOT, "version_pointer.py")
        if os.path.isfile(vp_path):
            import importlib.util
            spec = importlib.util.spec_from_file_location("_cmuh_vp_tool", vp_path)
            vp = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(vp)
            src_dir = vp.resolve_src(ROOT, "his_menu_fix_tool").src_dir
        if src_dir not in sys.path:
            sys.path.insert(0, src_dir)
        import cmuh_common.his_contract as hc
        if not hasattr(hc, "override_marker"):
            return None, ("目前執行中的程式版本太舊，還沒有「本機快速修正」功能；"
                          "請先重開主程式讓它自動更新，再用本工具")
        return hc, ""
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


def override_path(hc=None) -> str:
    """主程式讀的那個檔(his_contract.override_path);載入失敗時退回程式資料夾的 settings。"""
    if hc is not None:
        try:
            return hc.override_path()
        except Exception:  # noqa: BLE001
            pass
    return os.path.join(ROOT, OVERRIDE_FILE)


def read_override(hc=None):
    try:
        with open(override_path(hc), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:  # noqa: BLE001 —— 壞掉的檔:回一個必然驗證失敗的內容,畫面才會顯示「無效」
        return {"_讀取失敗": type(e).__name__}


def write_override(hc, data: dict) -> str:
    """原子寫入（寫到一半中斷也不會留下半截檔）→ 錯誤訊息（空字串＝成功）。"""
    try:
        path = override_path(hc)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        from cmuh_common.atomic_io import atomic_write_json  # noqa: PLC0415
        atomic_write_json(path, data, ensure_ascii=False, indent=2)
        return ""
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}"


# ─── 畫面 ─────────────────────────────────────────────────────────────────
def main() -> int:  # noqa: C901 —— 單一視窗的版面,拆開反而難讀
    import tkinter as tk
    from tkinter import messagebox, ttk

    hc, hc_err = load_running_contract()
    root = tk.Tk()
    root.title("HIS 熱鍵 ID 修正工具")
    root.geometry("860x900")
    root.attributes("-topmost", True)
    F = "Microsoft JhengHei UI"
    style = ttk.Style(root)
    style.configure("Big.TButton", font=(F, 11, "bold"), padding=6)
    style.configure("TLabelframe.Label", font=(F, 11, "bold"))

    canvas = tk.Canvas(root, highlightthickness=0)
    bar = ttk.Scrollbar(root, orient="vertical", command=canvas.yview)
    body = ttk.Frame(canvas)
    body.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.create_window((0, 0), window=body, anchor="nw")
    canvas.configure(yscrollcommand=bar.set)
    canvas.pack(side="left", fill="both", expand=True)
    bar.pack(side="right", fill="y")
    canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(-int(e.delta / 120), "units"))

    def label(parent, text, **kw):
        kw.setdefault("font", (F, 10))
        kw.setdefault("wraplength", 800)
        kw.setdefault("justify", "left")
        w = ttk.Label(parent, text=text, **kw)
        w.pack(anchor="w", padx=8, pady=2)
        return w

    # ── 說明 ──
    intro = tk.Frame(body, bg="#FFF8DC", bd=1, relief="solid")
    intro.pack(fill="x", padx=10, pady=(10, 6))
    for line, bold in (
            ("什麼時候用這個工具？", True),
            ("醫院 HIS 改版後：按 F1～F5 游標沒有跳到「醫令代碼」輸入欄，或按 F9／F10 "
             "開不出「同意書」（開成別的視窗）。這代表 HIS 的「選單編號」變了，"
             "用這個工具找出新編號、寫進這台電腦，重開主程式就能用。", False),
            ("安全嗎？測試只會「開啟對話框／視窗」，看完按「取消」或關掉即可，不會送出或完成病歷。"
             "本工具不測試「完成」選單（那可能會完成門診），F11「完成不印」異常請直接回報開發者。",
             False)):
        tk.Label(intro, text=line, bg="#FFF8DC", justify="left", wraplength=800,
                 font=(F, 11 if bold else 10, "bold" if bold else "normal")
                 ).pack(anchor="w", padx=8, pady=2)
    ttk.Button(body, text="開啟詳細使用說明", command=lambda: os.startfile(
        os.path.join(ROOT, GUIDE_FILE)) if os.path.isfile(os.path.join(ROOT, GUIDE_FILE))
        else messagebox.showinfo("找不到說明檔", f"程式資料夾裡沒有 {GUIDE_FILE}")
    ).pack(anchor="w", padx=12)

    # ── 步驟 1：狀態 ──
    s1 = ttk.LabelFrame(body, text="步驟 1　確認狀態（全部要是 ✔ 才能測試）")
    s1.pack(fill="x", padx=10, pady=6)
    st_admin = label(s1, "")
    st_his = label(s1, "")
    st_ver = label(s1, "")
    st_prog = label(s1, "")
    st_ovr = label(s1, "")
    btns1 = ttk.Frame(s1)
    btns1.pack(anchor="w", padx=8, pady=4)
    state = {"hwnd": 0, "major": None, "full": None, "confirmed": {}}

    def refresh_status():
        admin = is_admin()
        st_admin.config(text=("✔ 已用系統管理員身分開啟" if admin else
                              "✖ 沒有系統管理員權限 → 測試按鈕會「沒有反應」。"
                              "請按右下「以系統管理員身分重新開啟」，或改用雙擊「修正HIS熱鍵ID.cmd」"),
                        foreground="#006400" if admin else "#B00020")
        hwnd, title = find_his_window()
        state["hwnd"] = hwnd
        state["major"], state["full"] = parse_his_version(title)
        st_his.config(text=("✔ 已找到 HIS「西醫門診醫師作業」視窗" if hwnd else
                            "✖ 找不到 HIS 視窗 → 請先打開「西醫門診醫師作業」並掛入一位病人，"
                            "再按「重新偵測」"),
                      foreground="#006400" if hwnd else "#B00020")
        st_ver.config(text=(f"HIS 版本：V.{state['full']}" if state["full"] else
                            "HIS 版本：（讀不到）"))
        if hc is None:
            st_prog.config(text=f"✖ 讀不到程式設定，無法寫入快速修正：{hc_err}",
                           foreground="#B00020")
        else:
            st_prog.config(text=(f"程式目前使用（開啟本工具時）：代碼輸入＝{hc.MENU_ID_代碼輸入}、"
                                 f"同意書＝{hc.MENU_ID_同意書}（校正版本 {hc.CALIBRATED_VERSION}）"),
                           foreground="#000000")
        st_ovr.config(**override_status(hc, read_override(hc)))
        test_btn.config(state="normal" if (admin and hwnd) else "disabled")

    ttk.Button(btns1, text="重新偵測", command=lambda: refresh_status()).pack(side="left")
    ttk.Button(btns1, text="以系統管理員身分重新開啟",
               command=lambda: (relaunch_as_admin() and root.destroy())).pack(side="left", padx=8)

    # ── 步驟 2：選功能 ──
    s2 = ttk.LabelFrame(body, text="步驟 2　選擇要修正的功能")
    s2.pack(fill="x", padx=10, pady=6)
    func_var = tk.StringVar(value=FUNCTIONS[0]["key"])
    for f in FUNCTIONS:
        ttk.Radiobutton(s2, text=f"{f['name']}（{f['hotkeys']}）── 症狀：{f['symptom']}",
                        variable=func_var, value=f["key"],
                        command=lambda: on_func_change()).pack(anchor="w", padx=8, pady=2)
    success_lbl = label(s2, "", foreground="#004080", font=(F, 10, "bold"))

    # ── 步驟 3：測試 ──
    s3 = ttk.LabelFrame(body, text="步驟 3　逐一測試，直到 HIS 出現正確的畫面")
    s3.pack(fill="x", padx=10, pady=6)
    label(s3, "做法：按「送出測試」→ 看 HIS 畫面 → 不對就先把 HIS 跳出的視窗關掉／按取消，"
              "再按「試下一個」或「試上一個」，然後再按一次「送出測試」。"
              "第一次測試建議從「程式目前使用」的編號開始（已自動帶入）。")
    cand_var = tk.IntVar(value=0)
    row = ttk.Frame(s3)
    row.pack(pady=6)
    ttk.Button(row, text="◀ 試上一個", command=lambda: step(-1)).pack(side="left", padx=6)
    ttk.Label(row, text="目前要測試的編號：", font=(F, 12)).pack(side="left")
    ttk.Label(row, textvariable=cand_var, font=("Consolas", 22, "bold"),
              foreground="#8B0000", width=5).pack(side="left")
    ttk.Button(row, text="試下一個 ▶", command=lambda: step(+1)).pack(side="left", padx=6)
    test_btn = ttk.Button(s3, text="送出測試", style="Big.TButton",
                          command=lambda: send_test())
    test_btn.pack(pady=4)
    sent_var = tk.StringVar(value="（還沒送出）")
    ttk.Label(s3, textvariable=sent_var, font=(F, 10, "bold"),
              foreground="#004080").pack()
    judge = ttk.Frame(s3)
    judge.pack(pady=6)
    yes_btn = ttk.Button(judge, text="✔ 畫面正確，就是這個編號", style="Big.TButton",
                         state="disabled", command=lambda: confirm_current())
    yes_btn.pack(side="left", padx=8)
    ttk.Button(judge, text="✖ 不對，試下一個", style="Big.TButton",
               command=lambda: (step(+1), sent_var.set(
                   "請先把 HIS 剛才跳出的視窗關掉，再按「送出測試」"))).pack(side="left", padx=8)
    last_sent = {"key": None, "id": None}

    def current_func():
        return next(f for f in FUNCTIONS if f["key"] == func_var.get())

    def on_func_change():
        f = current_func()
        start = getattr(hc, f["key"], f["range"][0]) if hc is not None else f["range"][0]
        cand_var.set(clamp_step(start, 0, f["range"]))
        success_lbl.config(text=f"正確的畫面：{f['success']}")
        last_sent.update(key=None, id=None)
        yes_btn.config(state="disabled")
        sent_var.set(f"（安全測試範圍 {f['range'][0]}～{f['range'][1]}）")

    def step(delta):
        f = current_func()
        cand_var.set(clamp_step(cand_var.get(), delta, f["range"]))
        yes_btn.config(state="disabled")

    def send_test():
        if not state["hwnd"]:
            refresh_status()
            if not state["hwnd"]:
                messagebox.showwarning("找不到 HIS", "請先打開「西醫門診醫師作業」並掛入一位病人。")
                return
        cid = cand_var.get()
        send_menu_command(state["hwnd"], cid)
        last_sent.update(key=current_func()["key"], id=cid)
        yes_btn.config(state="normal")
        sent_var.set(f"已送出編號 {cid} → 請看 HIS 畫面是不是「{current_func()['success']}」")

    def confirm_current():
        if last_sent["key"] != current_func()["key"] or last_sent["id"] != cand_var.get():
            messagebox.showwarning("請先送出測試", "請先按「送出測試」並確認 HIS 畫面正確。")
            return
        state["confirmed"][last_sent["key"]] = last_sent["id"]
        refresh_summary()
        messagebox.showinfo("已記下", f"{current_func()['name']} ＝ {last_sent['id']}\n\n"
                                      "請把 HIS 剛跳出的視窗關掉。需要的話再選另一個功能測試，"
                                      "全部找完後到「步驟 4」套用。")

    # ── 步驟 4：套用 ──
    s4 = ttk.LabelFrame(body, text="步驟 4　套用修正（寫進這台電腦，重開主程式生效）")
    s4.pack(fill="x", padx=10, pady=6)
    summary_var = tk.StringVar()
    ttk.Label(s4, textvariable=summary_var, font=(F, 11, "bold"),
              foreground="#8B0000").pack(anchor="w", padx=8, pady=4)
    ver_var = tk.BooleanVar(value=False)
    ver_chk = ttk.Checkbutton(
        s4, variable=ver_var,
        text="我已確認 F1～F11 全部熱鍵都正常 → 一併把目前 HIS 版本記為校正版本"
             "（可省掉「偵測到 HIS 改版」的通知；不確定就不要勾）")
    ver_chk.pack(anchor="w", padx=8)
    btns4 = ttk.Frame(s4)
    btns4.pack(anchor="w", padx=8, pady=6)
    apply_var = tk.StringVar()

    def refresh_summary():
        c = state["confirmed"]
        summary_var.set("已確認：" + ("、".join(
            f"{f['name']}＝{c[f['key']]}" for f in FUNCTIONS if f["key"] in c) or "（還沒有）"))

    def apply_fix():
        if hc is None:
            messagebox.showerror("無法寫入", f"讀不到程式設定：{hc_err}")
            return
        if not state["confirmed"] and not ver_var.get():
            messagebox.showwarning("沒有要寫的內容", "請先在步驟 3 確認至少一個功能的正確編號。")
            return
        record = state["major"] if ver_var.get() else None
        if ver_var.get() and not record:
            messagebox.showwarning("讀不到 HIS 版本", "讀不到 HIS 版本，請取消勾選後再套用。")
            return
        data, err = build_override(hc, read_override(hc), state["confirmed"], record)
        if err:
            apply_var.set(f"❌ 沒寫入：{err}")
            return
        what = "、".join([f"{f['name']}＝{state['confirmed'][f['key']]}" for f in FUNCTIONS
                         if f["key"] in state["confirmed"]]
                        + ([f"校正版本＝{record}"] if record else []))
        if not messagebox.askyesno(
                "確定要套用嗎？",
                f"將寫入這台電腦的本機快速修正：\n\n{what}\n\n"
                "⚠ 編號錯誤會讓熱鍵打到別的功能，請確定剛才 HIS 開出的就是正確的畫面。"):
            return
        werr = write_override(hc, data)
        if werr:
            apply_var.set(f"❌ 寫入失敗（原本的檔沒有被動到）：{werr}")
            return
        apply_var.set("✔ 已寫入！請【關閉並重新開啟「中國醫皮膚科主程式」】才會生效。"
                      "之後程式正式更新時，這個快速修正會自動失效。"
                      "也請按「複製回報內容」傳給開發者，讓正式版本跟上。")
        refresh_status()

    def clear_fix():
        if not messagebox.askyesno("清除快速修正",
                                   "要刪除這台電腦的本機快速修正嗎？\n"
                                   "刪除後重開主程式，會回到程式內建的編號。"):
            return
        try:
            os.remove(override_path(hc))
            apply_var.set("✔ 已刪除本機快速修正（重開主程式後回到程式內建的編號）")
        except FileNotFoundError:
            apply_var.set("（本來就沒有本機快速修正）")
        except OSError as e:
            apply_var.set(f"❌ 刪不掉：{e}")
        refresh_status()

    def copy_report():
        text = report_text(state["full"], state["confirmed"])
        root.clipboard_clear()
        root.clipboard_append(text)
        apply_var.set(f"已複製：{text}")

    ttk.Button(btns4, text="✅ 套用修正", style="Big.TButton",
               command=apply_fix).pack(side="left")
    ttk.Button(btns4, text="複製回報內容", command=copy_report).pack(side="left", padx=8)
    ttk.Button(btns4, text="清除快速修正", command=clear_fix).pack(side="left")
    ttk.Label(s4, textvariable=apply_var, wraplength=800, font=(F, 10, "bold"),
              foreground="#004080", justify="left").pack(anchor="w", padx=8, pady=4)

    on_func_change()
    refresh_summary()
    refresh_status()
    root.mainloop()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 —— 讓不會程式的人也看得到錯誤
        import traceback
        tb = traceback.format_exc()
        try:
            with open(os.path.join(ROOT, "startup_crash.log"), "a", encoding="utf-8") as f:
                f.write(f"\n===== {datetime.datetime.now():%Y-%m-%d %H:%M:%S} "
                        f"HIS熱鍵ID修正工具 錯誤 =====\n{tb}")
        except OSError:
            pass
        try:
            ctypes.windll.user32.MessageBoxW(
                0, f"HIS 熱鍵 ID 修正工具發生錯誤：\n{sys.exc_info()[1]}\n\n"
                   "詳見程式資料夾裡的 startup_crash.log", "錯誤", 0x10)
        except Exception:  # noqa: BLE001
            print(tb)
        sys.exit(1)
