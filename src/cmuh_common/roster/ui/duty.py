# -*- coding: utf-8 -*-
"""R/VS 排班分頁（CalendarDutyTab）+ 請假/指定編輯器（LeaveEditor）。

一個類別、scope 參數化：R 分頁=CalendarDutyTab(scope="r")、VS=scope="vs"。
所有讀寫經 RosterService；ortools 於按「自動排班」時 lazy 安裝/import。
"""
from __future__ import annotations

import logging
import os
import threading
import tkinter as tk
from datetime import date
from tkinter import filedialog, messagebox, ttk

from cmuh_common.deps_runtime import ensure_dependencies
from cmuh_common.roster.model import day_point, is_weekend
from cmuh_common.roster.ui.common import (
    WEEKDAY_HEADERS, MonthSelector, StatusBar, archive_finalize_pdf_async,
    calendar_matrix, fg_for, member_color, next_in_cycle,
)

_SCOPE_TITLE = {"r": "R 排班", "vs": "VS 排班"}
_ORTOOLS_DEP = [("ortools==9.15.6755", "ortools")]


class LeaveEditor(tk.Toplevel):
    """月曆點選式編輯請假/指定（mode="leave"/"must"）。點日期 toggle，確定即存。"""

    def __init__(self, master, service, scope, ym, mode, members=None):
        super().__init__(master)
        self.service = service
        self.scope = scope
        self.ym = ym
        self.mode = mode
        self.title(f"{'請假' if mode == 'leave' else '一定要值班'}編輯 · {ym}")
        self.resizable(False, False)
        self.transient(master)
        # members 可由呼叫端指定（PGY 當月人員 / Clerk 梯次成員）；否則抓 config
        self._members = (members if members is not None
                         else service.storage.load_config().get(f"{scope}_members")
                         or [])
        self._selected: set = set()
        self._buttons: dict = {}
        self._loaded_mid = None              # [RP3-20] 目前載入 _selected 的成員 id
        self._loaded_baseline: set = set()   # 載入當下的勾選(偵測切換時是否有變)

        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="成員").pack(side="left")
        self._mvar = tk.StringVar()
        self._combo = ttk.Combobox(
            top, width=18, state="readonly", textvariable=self._mvar,
            values=[f"{m.get('id')} {m.get('name', '')}".strip()
                    for m in self._members])
        self._combo.pack(side="left", padx=6)
        self._combo.bind("<<ComboboxSelected>>",
                         lambda _e: self._on_member_change())
        if self._members:
            self._combo.current(0)

        self._grid = ttk.Frame(self, padding=8)
        self._grid.pack()
        self._build_grid()

        btns = ttk.Frame(self, padding=8)
        btns.pack(fill="x")
        ttk.Button(btns, text="儲存", command=self._save).pack(side="right")
        ttk.Button(btns, text="取消", command=self.destroy).pack(side="right", padx=6)

        if self._members:
            self._load_member()
        self.grab_set()

    def _member_id(self):
        i = self._combo.current()
        return self._members[i].get("id") if 0 <= i < len(self._members) else None

    def _build_grid(self) -> None:
        for c, h in enumerate(WEEKDAY_HEADERS):
            ttk.Label(self._grid, text=h, width=4, anchor="center").grid(
                row=0, column=c, padx=1, pady=1)
        y, m = int(self.ym[:4]), int(self.ym[5:7])
        for r, week in enumerate(calendar_matrix(y, m), start=1):
            for c, d in enumerate(week):
                if d is None:
                    continue
                b = tk.Button(self._grid, text=str(d.day), width=4,
                              command=lambda dd=d: self._toggle(dd))
                b.grid(row=r, column=c, padx=1, pady=1)
                self._buttons[d] = b

    def _on_member_change(self) -> None:
        # [RP3-20] 切成員前先把上一位的未存變更落檔,否則切走即遺失。
        self._commit_current()
        self._load_member()

    def _commit_current(self) -> None:
        """把目前載入成員的勾選落檔（僅在相對載入基準確有變動時才寫）。"""
        if self._loaded_mid is None or self._selected == self._loaded_baseline:
            return
        if self.mode == "leave":
            self.service.set_leaves(self.scope, self.ym, self._loaded_mid,
                                    self._selected)
        else:
            self.service.set_must(self.scope, self.ym, self._loaded_mid,
                                  self._selected)
        self._loaded_baseline = set(self._selected)

    def _load_member(self) -> None:
        mid = self._member_id()
        if self.mode == "leave":                     # 請假：任一 scope 皆可
            self._selected = set(self.service.get_leaves(self.scope, self.ym, mid))
        else:                                        # 指定值班：僅 R/VS 有此概念
            ctx = self.service.build_context(self.scope, self.ym)
            self._selected = set(ctx.must_duty.get(mid) or set())
        self._loaded_mid = mid                       # [RP3-20] 記住載入者與基準
        self._loaded_baseline = set(self._selected)
        self._refresh_buttons()

    def _toggle(self, d: date) -> None:
        if d in self._selected:
            self._selected.discard(d)
        else:
            self._selected.add(d)
        self._refresh_buttons()

    def _refresh_buttons(self) -> None:
        for d, b in self._buttons.items():
            on = d in self._selected
            b.config(relief="sunken" if on else "raised",
                     bg="#F58518" if on else "SystemButtonFace")

    def _save(self) -> None:
        # [RP3-20] 存目前成員(含這次)的變更後關閉;先前切換過的成員已在切換時落檔。
        self._commit_current()
        self.destroy()


class CalendarDutyTab(ttk.Frame):
    def __init__(self, master, service, scope, app):
        super().__init__(master)
        self.service = service
        self.scope = scope
        self.app = app
        self._finalized = False
        self._busy_flag = False
        self._toolbar: list = []

        self._build_toolbar()
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        self._grid_holder = ttk.Frame(body, padding=4)
        self._grid_holder.pack(side="left", fill="both", expand=True)
        side = ttk.Frame(body, width=240)
        side.pack(side="right", fill="y")
        self._build_side(side)
        self._status = StatusBar(self)
        self._status.pack(fill="x", side="bottom")

        self.refresh()

    # ── 版面 ─────────────────────────────────────────────────────────────
    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self, padding=(6, 6))
        bar.pack(fill="x")
        self._selector = MonthSelector(bar, self.app.ym, self._on_month_change)
        self._selector.pack(side="left")
        self._auto_btn = ttk.Button(bar, text="自動排班", command=self._on_auto)
        self._auto_btn.pack(side="left", padx=(12, 4))
        self._clear_btn = ttk.Button(bar, text="清除未鎖定",
                                     command=self._on_clear_unlocked)
        self._clear_btn.pack(side="left", padx=4)
        self._report_btn = ttk.Button(bar, text="報告", command=self._on_report)
        self._report_btn.pack(side="left", padx=4)
        self._resettle_btn = ttk.Button(bar, text="重算帳本",
                                        command=self._on_resettle)
        self._resettle_btn.pack(side="left", padx=4)
        # 匯出不進 _toolbar/finalized 停用集：定案月仍可匯出（唯讀輸出、不改資料）
        ttk.Button(bar, text="匯出", command=self._on_export).pack(side="left", padx=4)
        self._final_var = tk.BooleanVar(value=False)
        self._final_chk = ttk.Checkbutton(
            bar, text="定案", variable=self._final_var, command=self._on_finalize)
        self._final_chk.pack(side="left", padx=12)
        self._toolbar = [self._auto_btn, self._clear_btn, self._report_btn,
                         self._resettle_btn, self._final_chk]

    def _build_side(self, parent) -> None:
        ttk.Label(parent, text="結算", font=("Microsoft JhengHei UI", 10, "bold")
                  ).pack(anchor="w", padx=6, pady=(6, 0))
        cols = ("m", "wd", "we", "pt", "bal")
        self._sum = ttk.Treeview(parent, columns=cols, show="headings", height=8)
        for c, t, w in (("m", "成員", 60), ("wd", "平日", 42), ("we", "假日", 42),
                        ("pt", "點", 42), ("bal", "帳本", 54)):
            self._sum.heading(c, text=t)
            self._sum.column(c, width=w, anchor="center")
        self._sum.pack(fill="x", padx=6)
        ttk.Label(parent, text="警告", font=("Microsoft JhengHei UI", 10, "bold")
                  ).pack(anchor="w", padx=6, pady=(8, 0))
        self._warns = tk.Listbox(parent, height=10, width=34)
        self._warns.pack(fill="both", expand=True, padx=6, pady=(0, 6))

    # ── 資料 → 畫面 ──────────────────────────────────────────────────────
    def _member_map(self) -> dict:
        cfg = self.service.storage.load_config()
        out = {}
        for i, m in enumerate(cfg.get(f"{self.scope}_members") or []):
            out[m.get("id")] = {"name": m.get("name") or m.get("id"),
                                "color": member_color(i)}
        return out

    def refresh(self) -> None:
        """重畫整個分頁（月曆格 + 結算 + 警告 + 定案狀態）。"""
        ym = self.app.ym
        month = self.service.storage.load_month(ym)
        self._finalized = bool(month.get("finalized"))
        self._final_var.set(self._finalized)
        ctx = self.service.build_context(self.scope, ym)
        holidays = ctx.holidays
        params = ctx.params
        members = self._member_map()
        duty = month.get(f"{self.scope}_duty") or {}
        # [週六切片] R 分頁的週六格附註(值班連動/次數平衡由 service 排定)
        biopsy = (month.get("saturday_biopsy") or {}) if self.scope == "r" else {}

        for w in self._grid_holder.winfo_children():
            w.destroy()
        for c, h in enumerate(WEEKDAY_HEADERS):
            wknd = c >= 5
            ttk.Label(self._grid_holder, text=h, anchor="center",
                      foreground="#B00" if wknd else "#000").grid(
                row=0, column=c, sticky="nsew", padx=1, pady=1)
        y, m = int(ym[:4]), int(ym[5:7])
        for r, week in enumerate(calendar_matrix(y, m), start=1):
            for c, d in enumerate(week):
                self._make_cell(r, c, d, duty, holidays, params, members,
                                biopsy)
        for c in range(7):
            self._grid_holder.columnconfigure(c, weight=1)

        self._refresh_side(ctx, duty, members)
        self._apply_finalized_state()

    def _make_cell(self, r, c, d, duty, holidays, params, members,
                   biopsy=None) -> None:
        if d is None:
            tk.Frame(self._grid_holder).grid(row=r, column=c)
            return
        iso = d.isoformat()
        cell = duty.get(iso) or {}
        pid = cell.get("person")
        locked = bool(cell.get("locked"))
        info = members.get(pid)
        bg = info["color"] if info else ("#F0E7D8" if d in holidays else "#FFFFFF")
        fg = fg_for(bg) if info else "#000000"
        name = info["name"] if info else ""
        pts = day_point(d, holidays, params)
        mark = " 🔒" if locked else ""
        hol = "假 " if (d in holidays and not is_weekend(d)) else ""
        text = f"{hol}{d.day}\n{name}\n{pts}點{mark}"
        # [週六切片] R 分頁週六格第 4 行顯示切片負責人
        bp = ((biopsy or {}).get(iso) or {}).get("person")
        height = 3
        if bp:
            binfo = members.get(bp)
            text += f"\n切:{binfo['name'] if binfo else bp}"
            height = 4
        lbl = tk.Label(self._grid_holder, text=text, bg=bg, fg=fg,
                       width=8, height=height, relief="ridge", justify="center",
                       font=("Microsoft JhengHei UI", 9))
        lbl.grid(row=r, column=c, sticky="nsew", padx=1, pady=1)
        if not self._finalized:
            lbl.bind("<Button-1>", lambda _e, dd=d: self._on_cell_left(dd))
            lbl.bind("<Button-3>", lambda e, dd=d: self._on_cell_right(e, dd))

    def _refresh_side(self, ctx, duty, members) -> None:
        # 結算：由目前格子即時統計
        assigned: dict = {}
        for iso, cell in duty.items():
            p = cell.get("person")
            if p:
                try:
                    assigned[date.fromisoformat(iso)] = p
                except (ValueError, TypeError):
                    continue
        tally = {mid: {"wd": 0, "we": 0, "pt": 0} for mid in members}
        for d, p in assigned.items():
            # [RS-02/RF-11] 已離目前名單的值班者(換血/改 id 後檢視歷史月)也動態納入,
            # 否則月曆排得出人、結算卻整列消失、數字對不上。比照 member_tally 的 setdefault。
            t = tally.setdefault(p, {"wd": 0, "we": 0, "pt": 0})
            # [RS-02] 平日的國定假日算「假日班」,與 member_tally / solve_rvs 一致。
            if is_weekend(d) or d in ctx.holidays:
                t["we"] += 1
            else:
                t["wd"] += 1
            t["pt"] += day_point(d, ctx.holidays, ctx.params)
        self._sum.delete(*self._sum.get_children())
        for mid, t in tally.items():
            if mid in members:
                name = members[mid]["name"]
                bal = f"{float(ctx.ledger.get(mid, 0.0)):+.1f}"
            else:                      # 已離名單:顯示 id、帳本欄不適用
                name = mid
                bal = "—"
            self._sum.insert("", "end", values=(
                name, t["wd"], t["we"], t["pt"], bal))

        self._warns.delete(0, tk.END)
        mark = {"error": "✗", "warn": "⚠", "info": "・"}
        for ck in self.service.quick_validate(self.scope, self.app.ym):
            self._warns.insert(tk.END, f"{mark.get(ck.severity, '?')} {ck.msg}")
        if not self._warns.size():
            self._warns.insert(tk.END, "（無）")

    # ── 互動：手動改格 ───────────────────────────────────────────────────
    def _on_month_change(self, ym) -> None:
        self.app.ym = ym
        self.refresh()

    def on_shown(self) -> None:
        """由 app 在切到本分頁時呼叫：同步共用月份並重畫。"""
        if self._selector.ym != self.app.ym:
            self._selector.set_ym(self.app.ym)
        self.refresh()

    def _member_ids(self) -> list:
        return list(self._member_map().keys())

    def _on_cell_left(self, d: date) -> None:
        if self._finalized or self._busy_flag:       # RF-17：求解中不得手排
            return
        duty = (self.service.storage.load_month(self.app.ym)
                .get(f"{self.scope}_duty") or {})
        cur = (duty.get(d.isoformat()) or {}).get("person")
        nxt = next_in_cycle(cur, self._member_ids())
        self.service.set_cell(self.scope, self.app.ym, d, nxt)
        self.refresh()

    def _on_cell_right(self, event, d: date) -> None:
        if self._finalized or self._busy_flag:       # RF-17：求解中不得開右鍵選單
            return
        menu = tk.Menu(self, tearoff=0)
        pick = tk.Menu(menu, tearoff=0)
        for mid, info in self._member_map().items():
            pick.add_command(
                label=f"{mid} {info['name']}",
                command=lambda mm=mid: self._set_cell_and_refresh(d, mm))
        menu.add_cascade(label="指定人選", menu=pick)
        menu.add_command(label="切換鎖定 🔒",
                         command=lambda: self._toggle_lock(d))
        menu.add_separator()
        menu.add_command(label="設為請假…",
                         command=lambda: self._open_leave_editor("leave"))
        menu.add_command(label="設為指定值班…",
                         command=lambda: self._open_leave_editor("must"))
        menu.add_separator()
        menu.add_command(label="清空此格",
                         command=lambda: self._set_cell_and_refresh(d, None))
        menu.tk_popup(event.x_root, event.y_root)

    def _set_cell_and_refresh(self, d, mid) -> None:
        self.service.set_cell(self.scope, self.app.ym, d, mid)
        self.refresh()

    def _toggle_lock(self, d) -> None:
        self.service.toggle_lock(self.scope, self.app.ym, d)
        self.refresh()

    def _open_leave_editor(self, mode) -> None:
        ed = LeaveEditor(self, self.service, self.scope, self.app.ym, mode)
        self.wait_window(ed)
        self.refresh()

    # ── 自動排班（threaded）──────────────────────────────────────────────
    def _busy(self, text) -> None:
        self._busy_flag = True
        self._status.set(text)
        self._selector.set_enabled(False)        # RF-05：求解中鎖住月份切換
        for w in self._toolbar:
            w.config(state="disabled")

    def _unbusy(self) -> None:
        # RF-16：無條件恢復再套定案狀態——否則求解結束時若停在已定案月，「報告」鈕與
        # 「定案」勾選會被永久停用（切回未定案月也救不回）。
        self._busy_flag = False
        self._status.set("就緒")
        self._selector.set_enabled(True)
        for w in self._toolbar:
            w.config(state="normal")
        self._apply_finalized_state()

    def _on_auto(self) -> None:
        if self._finalized or self._busy_flag:
            return
        ym = self.app.ym                             # RF-05：捕捉觸發當下的月份
        try:
            import ortools  # noqa: F401
        except ImportError:
            if not messagebox.askyesno(
                    "需要安裝排班引擎",
                    "首次使用自動排班需下載 Google OR-Tools（約 30MB）。現在安裝？"):
                return
            self._install_then_solve(ym)
            return
        self._start_solve(ym=ym)

    def _install_then_solve(self, ym) -> None:
        self._busy("安裝排班引擎中（首次約需 1-2 分鐘）…")

        def work():
            err = ""
            # 同匯出：ensure_dependencies 取消/失敗會 SystemExit，需一併攔截，
            # 否則 _after_install 不排程，UI 卡在「安裝中…」。
            try:
                ensure_dependencies(_ORTOOLS_DEP)
            except (Exception, SystemExit) as e:  # noqa: BLE001
                err = str(e) or "已取消或安裝失敗"
            self.after(0, lambda: self._after_install(err, ym))
        threading.Thread(target=work, name="ortools-install", daemon=True).start()

    def _after_install(self, err, ym) -> None:
        if err:
            self._unbusy()
            messagebox.showerror(
                "安裝失敗",
                f"排班引擎安裝失敗，請檢查網路後重試。\n詳見 dependency_install.log\n{err}")
            return
        self._start_solve(ym=ym)

    def _start_solve(self, allow_disable_color=False, ym=None) -> None:
        self._busy("排班中…")
        ym = ym or self.app.ym                        # RF-05：一路帶著求解目標月
        scope = self.scope

        def work():
            try:
                res = self.service.run_solve(
                    scope, ym, allow_disable_color=allow_disable_color)
                self.after(0, lambda: self._on_solved(res, ym))
            except Exception as e:  # noqa: BLE001
                logging.exception("[roster.ui] 求解例外")
                self.after(0, lambda exc=e: self._on_solve_error(exc))
        threading.Thread(target=work, name="roster-solve", daemon=True).start()

    def _on_solve_error(self, exc) -> None:
        self._unbusy()
        messagebox.showerror("排班失敗", f"求解時發生錯誤：\n{exc}")

    def _on_solved(self, res, ym) -> None:
        self._unbusy()
        # RF-05：求解期間若月份已被切走，結果屬「別的月」→ 一律捨棄不套用/不預覽，
        # 避免 need_confirm_color 對切換後的月份放寬重解、或預覽混搭錯月內容。
        if ym != self.app.ym:
            messagebox.showinfo(
                "排班結果已捨棄",
                f"求解期間月份已由 {ym} 切至 {self.app.ym}，結果未套用，"
                f"請切回 {ym} 重新排班。")
            return
        if res.status == "ok":
            self._preview_and_accept(res, ym)
        elif res.status == "need_confirm_color":
            if messagebox.askyesno(
                    "需放寬色塊連週規則",
                    "\n".join(res.diagnosis) + "\n\n是否放寬（將出現同色連週值班）？"):
                self._start_solve(allow_disable_color=True, ym=ym)
        else:   # precheck_failed / infeasible / error
            self._show_report_text(
                self.service.render_report(self.scope, ym, res),
                title=f"{_SCOPE_TITLE[self.scope]}（{res.status}）")

    def _preview_and_accept(self, res, ym) -> None:
        text = self.service.render_report(self.scope, ym, res)
        win = tk.Toplevel(self)
        win.title(f"排班預覽 · {_SCOPE_TITLE[self.scope]} · {ym}")
        win.transient(self)
        txt = tk.Text(win, wrap="none", width=64, height=28,
                      font=("Consolas", 10))
        txt.insert("1.0", text)
        txt.config(state="disabled")
        txt.pack(fill="both", expand=True, padx=6, pady=6)
        bar = ttk.Frame(win)
        bar.pack(fill="x", pady=(0, 6))

        def apply():
            try:
                self.service.accept_solution(self.scope, ym, res)
            except ValueError as e:
                messagebox.showwarning("結果已過期", str(e), parent=win)
                win.destroy()
                return
            except Exception as e:  # noqa: BLE001
                messagebox.showerror("套用失敗", str(e), parent=win)
                return
            win.destroy()
            self.refresh()
        ttk.Button(bar, text="套用", command=apply).pack(side="right", padx=6)
        ttk.Button(bar, text="取消", command=win.destroy).pack(side="right")
        win.grab_set()

    # ── 其他工具鈕 ───────────────────────────────────────────────────────
    def _on_clear_unlocked(self) -> None:
        if self._finalized or self._busy_flag:       # RF-17：求解中不得清除
            return
        if not messagebox.askyesno("清除未鎖定", "清除所有未鎖定的排班格？"):
            return
        # RF-20：一次 load/save（非逐格 set_cell → 免 N 次驗證/git commit、UI 不凍結），
        # 並清掉舊決策報告（清除後與 report 不符 → 誤導）。
        try:
            self.service.clear_unlocked(self.scope, self.app.ym)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("清除失敗", str(e))
            return
        self.refresh()

    def _on_export(self) -> None:
        """匯出整月班表（R+VS）。副檔名決定 Excel/Word；重依賴 lazy 安裝。"""
        from cmuh_common.roster.export_common import default_filename
        data = self.service.build_export(self.app.ym)
        path = filedialog.asksaveasfilename(
            title="匯出班表", defaultextension=".xlsx",
            initialfile=default_filename(data, ".xlsx"),
            filetypes=[("Excel 活頁簿", "*.xlsx"), ("Word 文件", "*.docx")])
        if not path:
            return
        ext = os.path.splitext(path)[1].lower()
        if ext not in (".xlsx", ".docx"):        # 只認這兩種，免寫出錯副檔名的檔
            messagebox.showerror(
                "不支援的格式",
                f"僅支援 Excel(.xlsx) 或 Word(.docx)，收到：{ext or '（無副檔名）'}")
            return
        dep = ([("openpyxl", "openpyxl")] if ext == ".xlsx"
               else [("python-docx", "docx")])
        self._status.set("匯出中…")

        def work():
            err = ""
            # ensure_dependencies 取消/失敗會 sys.exit(1)→SystemExit（非 Exception 子類），
            # 必須一併攔截，否則 _after_export 不會被排程，UI 卡在「匯出中…」。
            try:
                ensure_dependencies(dep)
                if ext == ".xlsx":
                    from cmuh_common.roster import export_xlsx
                    export_xlsx.export(path, data)
                else:
                    from cmuh_common.roster import export_docx
                    export_docx.export(path, data)
            except (Exception, SystemExit) as e:  # noqa: BLE001
                logging.exception("[roster.ui] 匯出失敗")
                err = str(e) or "已取消或安裝失敗"
            self.after(0, lambda: self._after_export(path, err))
        threading.Thread(target=work, name="roster-export", daemon=True).start()

    def _after_export(self, path, err) -> None:
        self._status.set("就緒")
        if err:
            messagebox.showerror("匯出失敗", f"匯出時發生錯誤：\n{err}")
        else:
            messagebox.showinfo("匯出完成", f"已匯出：\n{path}")

    def _on_resettle(self) -> None:
        """以目前（含手動換班）排班重算帳本結轉，並刷新結算面板。"""
        if self._finalized or self._busy_flag:      # 求解中不得動帳本（避免據舊帳本套用）
            return
        try:
            self.service.resettle_from_duty(self.scope, self.app.ym)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("重算帳本失敗", str(e))
            return
        self.refresh()
        messagebox.showinfo("重算帳本", "已依目前排班重算帳本結轉。")

    def _on_report(self) -> None:
        month = self.service.storage.load_month(self.app.ym)
        text = month.get(f"report_{self.scope}") or "（本月尚未排班，無報告）"
        self._show_report_text(text, title=f"{_SCOPE_TITLE[self.scope]} 決策報告")

    def _show_report_text(self, text, title) -> None:
        win = tk.Toplevel(self)
        win.title(title)
        win.transient(self)
        t = tk.Text(win, wrap="none", width=64, height=30,
                    font=("Consolas", 10))
        t.insert("1.0", text)
        t.config(state="disabled")
        t.pack(fill="both", expand=True, padx=6, pady=6)
        ttk.Button(win, text="關閉", command=win.destroy).pack(pady=(0, 6))

    def _on_finalize(self) -> None:
        on = bool(self._final_var.get())
        try:
            self.service.finalize(self.app.ym, on)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("定案失敗", str(e))
            self._final_var.set(not on)
            return
        self._finalized = on
        self._apply_finalized_state()
        self.refresh()
        if on:                              # 定案 → 背景輸出 PDF 留底
            archive_finalize_pdf_async(self, self.service, self.app.ym)

    def _apply_finalized_state(self) -> None:
        # RF-17：求解中（_busy_flag）也要保持停用——refresh（切月/切分頁/設定變更）
        # 會走到這裡，若只看 _finalized 會把編輯鈕在求解中重新啟用。
        state = "disabled" if (self._finalized or self._busy_flag) else "normal"
        for w in (self._auto_btn, self._clear_btn, self._resettle_btn):
            w.config(state=state)
