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
    CARD_BG, CARD_BORDER, CARD_HDR_HOLIDAY, CARD_HDR_NORMAL, CARD_HDR_WEEKEND,
    CARD_TODAY_BORDER, LINE_CHIP, OVR_FONT, OVR_STYLE, WEEKDAY_HEADERS,
    MonthSelector, StatusBar, archive_finalize_pdf_async, bind_hover_highlight,
    calendar_matrix, fg_for, member_color, next_in_cycle,
)

_WD = "一二三四五六日"

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
    """[2026-07-23 使用者整合] R（一線值班）+ VS（三線值班）合併為單一分頁：
    同一份月曆每格同時顯示一線/三線值班者（各自可點選編輯），右側兩個結算面板。
    線別以色籤區分（一線紅系、三線藍系）。"""

    def __init__(self, master, service, app):
        super().__init__(master)
        self.service = service
        self.app = app
        self._finalized = False
        self._busy_flag = False
        self._toolbar: list = []

        self._build_toolbar()
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        self._grid_holder = ttk.Frame(body, padding=4)
        self._grid_holder.pack(side="left", fill="both", expand=True)
        side = ttk.Frame(body, width=250)
        side.pack(side="right", fill="y")
        self._build_side(side)
        self._status = StatusBar(self)
        self._status.pack(fill="x", side="bottom")

        self.refresh()

    # ── 版面 ─────────────────────────────────────────────────────────────
    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self, padding=(6, 4))
        bar.pack(fill="x")
        self._selector = MonthSelector(bar, self.app.ym, self._on_month_change)
        self._selector.pack(side="left")
        self._toolbar = []
        self._auto_btns, self._clear_btns = {}, {}
        self._report_btns, self._resettle_btns = {}, {}
        # 每線一組操作鈕（自動排班/清除/報告/重算帳本），線別色籤區分
        for scope in ("r", "vs"):
            _bg, _fg, line = LINE_CHIP[scope]
            grp = ttk.LabelFrame(
                bar, text=f"{_SCOPE_TITLE[scope]}（{line}）", padding=(4, 0))
            grp.pack(side="left", padx=(10, 0))
            self._auto_btns[scope] = ttk.Button(
                grp, text="自動排班", width=8,
                command=lambda s=scope: self._on_auto(s))
            self._clear_btns[scope] = ttk.Button(
                grp, text="清除未鎖", width=8,
                command=lambda s=scope: self._on_clear_unlocked(s))
            self._report_btns[scope] = ttk.Button(
                grp, text="報告", width=5,
                command=lambda s=scope: self._on_report(s))
            self._resettle_btns[scope] = ttk.Button(
                grp, text="重算帳本", width=8,
                command=lambda s=scope: self._on_resettle(s))
            for b in (self._auto_btns[scope], self._clear_btns[scope],
                      self._report_btns[scope], self._resettle_btns[scope]):
                b.pack(side="left", padx=1)
                self._toolbar.append(b)
        # 匯出不進 _toolbar/finalized 停用集：定案月仍可匯出（唯讀輸出、不改資料）
        ttk.Button(bar, text="匯出", command=self._on_export).pack(side="left",
                                                                  padx=(10, 4))
        self._final_var = tk.BooleanVar(value=False)
        self._final_chk = ttk.Checkbutton(
            bar, text="定案", variable=self._final_var, command=self._on_finalize)
        self._final_chk.pack(side="left", padx=6)
        self._toolbar.append(self._final_chk)

    def _build_side(self, parent) -> None:
        # [2026-07-23 整合] 右側同時放 R（一線）與 VS（三線）兩個結算面板
        self._sum = {}
        cols = ("id", "m", "wd", "we", "pt", "bal")
        for scope in ("r", "vs"):
            _bg, _fg, line = LINE_CHIP[scope]
            ttk.Label(parent, text=f"{_SCOPE_TITLE[scope]}（{line}）結算",
                      font=(OVR_FONT, 10, "bold")
                      ).pack(anchor="w", padx=6, pady=(6, 0))
            tree = ttk.Treeview(parent, columns=cols, show="headings", height=5)
            for c, t, w in (("id", "代號", 44), ("m", "姓名", 58),
                            ("wd", "平日", 40), ("we", "假日", 40),
                            ("pt", "點", 38), ("bal", "帳本", 50)):
                tree.heading(c, text=t)
                tree.column(c, width=w, anchor="center")
            tree.pack(fill="x", padx=6)
            self._sum[scope] = tree
        # [2026-07-13 使用者] 週六 R2/R3 切片累計次數（存在 biopsy.json）。
        # 此帳本＝跨月累計「次數」，與上方點數帳本(結轉)不同。
        ttk.Label(parent, text="週六切片累計（R2/R3，跨月次數）",
                  font=(OVR_FONT, 10, "bold")
                  ).pack(anchor="w", padx=6, pady=(8, 0))
        self._bx = ttk.Treeview(parent, columns=("who", "cnt"),
                                show="headings", height=2)
        self._bx.heading("who", text="住院醫師")
        self._bx.column("who", width=150, anchor="w")
        self._bx.heading("cnt", text="累計次數")
        self._bx.column("cnt", width=60, anchor="center")
        self._bx.pack(fill="x", padx=6)
        ttk.Label(parent, text="警告", font=(OVR_FONT, 10, "bold")
                  ).pack(anchor="w", padx=6, pady=(8, 0))
        self._warns = tk.Listbox(parent, height=8, width=34)
        self._warns.pack(fill="both", expand=True, padx=6, pady=(0, 6))

    def _reload_biopsy_counts(self, ctx) -> None:
        """讀 biopsy.json 的累計次數，配 biopsy_pair（R 名單）認出 R2/R3 顯示。"""
        if not getattr(self, "_bx", None):
            return
        from cmuh_common.roster.saturday_biopsy import biopsy_pair
        self._bx.delete(*self._bx.get_children())
        counts = (self.service.storage.load_biopsy().get("counts") or {})
        pair, _notes = biopsy_pair(ctx.members)
        if not pair:
            self._bx.insert("", "end", values=("（名單缺 R2/R3）", "—"))
            return
        for m in pair:
            who = (f"{m.id} {m.name}（{m.level}）" if m.name
                   else f"{m.id}（{m.level}）")
            self._bx.insert("", "end", values=(who, int(counts.get(m.id, 0))))

    # ── 資料 → 畫面 ──────────────────────────────────────────────────────
    def _member_map(self, scope: str) -> dict:
        cfg = self.service.storage.load_config()
        out = {}
        for i, m in enumerate(cfg.get(f"{scope}_members") or []):
            mid = m.get("id")
            out[mid] = {"id": mid, "name": m.get("name") or mid,
                        "color": member_color(i)}
        return out

    @staticmethod
    def _who_label(pid, info) -> str:
        """月曆格/切片列的顯示：代號＋姓名（姓名空或同代號時只顯示代號）。"""
        if not info:
            return str(pid) if pid else ""
        nm = info.get("name")
        return f"{pid} {nm}" if nm and nm != pid else str(pid)

    def refresh(self) -> None:
        """重畫整個分頁（月曆格＝一線+三線 + 兩個結算 + 警告 + 定案狀態）。"""
        ym = self.app.ym
        month = self.service.storage.load_month(ym)
        self._finalized = bool(month.get("finalized"))
        self._final_var.set(self._finalized)
        ctx_r = self.service.build_context("r", ym)
        ctx_vs = self.service.build_context("vs", ym)
        holidays = ctx_r.holidays          # 假日集合=年度指定表 r/vs 鍵聯集,兩者相同
        params = ctx_r.params
        members = {"r": self._member_map("r"), "vs": self._member_map("vs")}
        duty = {"r": month.get("r_duty") or {}, "vs": month.get("vs_duty") or {}}
        biopsy = month.get("saturday_biopsy") or {}

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

        self._refresh_side(ctx_r, duty["r"], members["r"], self._sum["r"])
        self._refresh_side(ctx_vs, duty["vs"], members["vs"], self._sum["vs"])
        self._reload_biopsy_counts(ctx_r)
        self._refresh_warnings()
        self._apply_finalized_state()

    def _make_cell(self, r, c, d, duty, holidays, params, members,
                   biopsy=None) -> None:
        """[2026-07-23 整合] 每格同時顯示一線(R)/三線(VS)兩列：線別色籤＋值班者
        成員色塊＋各自 🔒；R 週六另有切片紫籤列。各列可獨立左鍵輪換/右鍵選單；
        滑鼠懸停藍框回饋（duty=members={"r":…, "vs":…}）。"""
        if d is None:
            tk.Frame(self._grid_holder).grid(row=r, column=c)
            return
        iso = d.isoformat()
        holiday = d in holidays and not is_weekend(d)
        today = (d == date.today())
        pts = day_point(d, holidays, params)

        border = CARD_TODAY_BORDER if today else CARD_BORDER
        card = tk.Frame(self._grid_holder, bg=CARD_BG,
                        highlightthickness=(2 if today else 1),
                        highlightbackground=border)
        card.grid(row=r, column=c, sticky="nsew", padx=1, pady=1)
        hbg, hfg = (CARD_HDR_HOLIDAY if holiday
                    else CARD_HDR_WEEKEND if is_weekend(d)
                    else CARD_HDR_NORMAL)
        hdr = tk.Frame(card, bg=hbg)
        hdr.pack(fill="x")
        tk.Label(hdr, text=f"{d.day}（{_WD[d.weekday()]}）"
                 + ("假" if holiday else "") + ("  ⬅今天" if today else ""),
                 bg=hbg, fg=hfg, padx=4,
                 font=(OVR_FONT, 9, "bold")).pack(side="left")
        tk.Label(hdr, text=f"{pts}點", bg=hbg, fg=hfg, padx=4,
                 font=(OVR_FONT, 8)).pack(side="right")

        for scope in ("r", "vs"):
            cell_data = (duty[scope].get(iso)) or {}
            pid = cell_data.get("person")
            locked = bool(cell_data.get("locked"))
            info = members[scope].get(pid)
            chip_bg, chip_fg, line = LINE_CHIP[scope]
            row = tk.Frame(card, bg=CARD_BG)
            row.pack(fill="x", padx=3, pady=1)
            tk.Label(row, text=line, bg=chip_bg, fg=chip_fg, padx=3,
                     font=(OVR_FONT, 8, "bold")).pack(side="left")
            if pid:
                pbg = info["color"] if info else "#9E9E9E"
                # [codex P2] width+wraplength 上限:月曆格網無捲軸,長姓名不設限會把
                # 整欄撐寬、擠爆七欄+側欄。
                tk.Label(row, text=self._who_label(pid, info)
                         + (" 🔒" if locked else ""), bg=pbg, fg=fg_for(pbg),
                         padx=4, anchor="w", width=10, wraplength=92,
                         justify="left", font=(OVR_FONT, 10, "bold")
                         ).pack(side="left", fill="x", expand=True, padx=(2, 0))
            else:
                tk.Label(row, text="—" + (" 🔒" if locked else ""), bg=CARD_BG,
                         fg="#BBBBBB", padx=4, anchor="w",
                         font=(OVR_FONT, 10)).pack(side="left", fill="x",
                                                   expand=True, padx=(2, 0))
            if not self._finalized:
                def _bind_row(w, s=scope):
                    w.bind("<Button-1>",
                           lambda _e, dd=d, ss=s: self._on_cell_left(dd, ss))
                    w.bind("<Button-3>",
                           lambda e, dd=d, ss=s: self._on_cell_right(e, dd, ss))
                    w.configure(cursor="hand2")
                    for ch in w.winfo_children():
                        _bind_row(ch, s)
                _bind_row(row)

        # [週六切片] 週六格：切片負責人（紫籤＋代號+姓名，屬 R 名單）
        bp = ((biopsy or {}).get(iso) or {}).get("person")
        if bp:
            brow = tk.Frame(card, bg=CARD_BG)
            brow.pack(fill="x", padx=3, pady=(0, 2))
            cbg, cfg2 = OVR_STYLE["biopsy"]
            tk.Label(brow, text="切片", bg=cbg, fg=cfg2, padx=3,
                     font=(OVR_FONT, 8, "bold")).pack(side="left")
            tk.Label(brow, text=self._who_label(bp, members["r"].get(bp)),
                     bg=CARD_BG, fg="#1A1A1A", padx=3, width=9,
                     wraplength=84, justify="left", anchor="w",
                     font=(OVR_FONT, 9, "bold")).pack(side="left")
        if not self._finalized:
            bind_hover_highlight(card, border)   # [UI 互動] 懸停藍框回饋

    def _refresh_warnings(self) -> None:
        """兩線警告合併顯示（[R]/[VS] 前綴）。"""
        self._warns.delete(0, tk.END)
        mark = {"error": "✗", "warn": "⚠", "info": "・"}
        for scope in ("r", "vs"):
            for ck in self.service.quick_validate(scope, self.app.ym):
                self._warns.insert(
                    tk.END,
                    f"[{scope.upper()}]{mark.get(ck.severity, '?')} {ck.msg}")
        if not self._warns.size():
            self._warns.insert(tk.END, "（無）")

    def _refresh_side(self, ctx, duty, members, tree) -> None:
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
        tree.delete(*tree.get_children())
        for mid, t in tally.items():
            if mid in members:
                name = members[mid]["name"]
                bal = f"{float(ctx.ledger.get(mid, 0.0)):+.1f}"
            else:                      # 已離名單:代號欄仍顯示 id、帳本欄不適用
                name = "（已離名單）"
                bal = "—"
            tree.insert("", "end", values=(
                mid, name, t["wd"], t["we"], t["pt"], bal))

    # ── 互動：手動改格 ───────────────────────────────────────────────────
    def _on_month_change(self, ym) -> None:
        self.app.ym = ym
        self.refresh()

    def on_shown(self) -> None:
        """由 app 在切到本分頁時呼叫：同步共用月份並重畫。"""
        if self._selector.ym != self.app.ym:
            self._selector.set_ym(self.app.ym)
        self.refresh()

    def _member_ids(self, scope: str) -> list:
        return list(self._member_map(scope).keys())

    def _on_cell_left(self, d: date, scope: str) -> None:
        if self._finalized or self._busy_flag:       # RF-17：求解中不得手排
            return
        duty = (self.service.storage.load_month(self.app.ym)
                .get(f"{scope}_duty") or {})
        cur = (duty.get(d.isoformat()) or {}).get("person")
        nxt = next_in_cycle(cur, self._member_ids(scope))
        self.service.set_cell(scope, self.app.ym, d, nxt)
        self.refresh()

    def _on_cell_right(self, event, d: date, scope: str) -> None:
        if self._finalized or self._busy_flag:       # RF-17：求解中不得開右鍵選單
            return
        _bg, _fg, line = LINE_CHIP[scope]
        menu = tk.Menu(self, tearoff=0)
        pick = tk.Menu(menu, tearoff=0)
        for mid, info in self._member_map(scope).items():
            pick.add_command(
                label=f"{mid} {info['name']}",
                command=lambda mm=mid: self._set_cell_and_refresh(d, mm, scope))
        menu.add_cascade(label=f"指定{line}人選", menu=pick)
        menu.add_command(label=f"切換{line}鎖定 🔒",
                         command=lambda: self._toggle_lock(d, scope))
        menu.add_separator()
        menu.add_command(label=f"{line}請假…",
                         command=lambda: self._open_leave_editor("leave", scope))
        menu.add_command(label=f"{line}指定值班…",
                         command=lambda: self._open_leave_editor("must", scope))
        menu.add_separator()
        menu.add_command(label=f"清空{line}此格",
                         command=lambda: self._set_cell_and_refresh(d, None,
                                                                    scope))
        menu.tk_popup(event.x_root, event.y_root)

    def _set_cell_and_refresh(self, d, mid, scope) -> None:
        self.service.set_cell(scope, self.app.ym, d, mid)
        self.refresh()

    def _toggle_lock(self, d, scope) -> None:
        self.service.toggle_lock(scope, self.app.ym, d)
        self.refresh()

    def _open_leave_editor(self, mode, scope) -> None:
        ed = LeaveEditor(self, self.service, scope, self.app.ym, mode)
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

    def _on_auto(self, scope: str) -> None:
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
            self._install_then_solve(ym, scope)
            return
        self._start_solve(ym=ym, scope=scope)

    def _install_then_solve(self, ym, scope) -> None:
        self._busy("安裝排班引擎中（首次約需 1-2 分鐘）…")

        def work():
            err = ""
            # 同匯出：ensure_dependencies 取消/失敗會 SystemExit，需一併攔截，
            # 否則 _after_install 不排程，UI 卡在「安裝中…」。
            try:
                ensure_dependencies(_ORTOOLS_DEP)
            except (Exception, SystemExit) as e:  # noqa: BLE001
                err = str(e) or "已取消或安裝失敗"
            self.after(0, lambda: self._after_install(err, ym, scope))
        threading.Thread(target=work, name="ortools-install", daemon=True).start()

    def _after_install(self, err, ym, scope) -> None:
        if err:
            self._unbusy()
            messagebox.showerror(
                "安裝失敗",
                f"排班引擎安裝失敗，請檢查網路後重試。\n詳見 dependency_install.log\n{err}")
            return
        self._start_solve(ym=ym, scope=scope)

    def _start_solve(self, allow_disable_color=False, ym=None,
                     scope: str = "r") -> None:
        self._busy(f"{_SCOPE_TITLE[scope]} 排班中…")
        ym = ym or self.app.ym                        # RF-05：一路帶著求解目標月

        def work():
            try:
                res = self.service.run_solve(
                    scope, ym, allow_disable_color=allow_disable_color)
                self.after(0, lambda: self._on_solved(res, ym, scope))
            except Exception as e:  # noqa: BLE001
                logging.exception("[roster.ui] 求解例外")
                self.after(0, lambda exc=e: self._on_solve_error(exc))
        threading.Thread(target=work, name="roster-solve", daemon=True).start()

    def _on_solve_error(self, exc) -> None:
        self._unbusy()
        messagebox.showerror("排班失敗", f"求解時發生錯誤：\n{exc}")

    def _on_solved(self, res, ym, scope) -> None:
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
            self._preview_and_accept(res, ym, scope)
        elif res.status == "need_confirm_color":
            if messagebox.askyesno(
                    "需放寬色塊連週規則",
                    "\n".join(res.diagnosis) + "\n\n是否放寬（將出現同色連週值班）？"):
                self._start_solve(allow_disable_color=True, ym=ym, scope=scope)
        else:   # precheck_failed / infeasible / error
            self._show_report_text(
                self.service.render_report(scope, ym, res),
                title=f"{_SCOPE_TITLE[scope]}（{res.status}）")

    def _preview_and_accept(self, res, ym, scope) -> None:
        text = self.service.render_report(scope, ym, res)
        win = tk.Toplevel(self)
        win.title(f"排班預覽 · {_SCOPE_TITLE[scope]} · {ym}")
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
                self.service.accept_solution(scope, ym, res)
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
    def _on_clear_unlocked(self, scope: str) -> None:
        if self._finalized or self._busy_flag:       # RF-17：求解中不得清除
            return
        if not messagebox.askyesno(
                "清除未鎖定",
                f"清除 {_SCOPE_TITLE[scope]} 所有未鎖定的排班格？"):
            return
        # RF-20：一次 load/save（非逐格 set_cell → 免 N 次驗證/git commit、UI 不凍結），
        # 並清掉舊決策報告（清除後與 report 不符 → 誤導）。
        try:
            self.service.clear_unlocked(scope, self.app.ym)
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

    def _on_resettle(self, scope: str) -> None:
        """以目前（含手動換班）排班重算帳本結轉，並刷新結算面板。"""
        if self._finalized or self._busy_flag:      # 求解中不得動帳本（避免據舊帳本套用）
            return
        try:
            self.service.resettle_from_duty(scope, self.app.ym)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("重算帳本失敗", str(e))
            return
        self.refresh()
        messagebox.showinfo("重算帳本",
                            f"已依目前 {_SCOPE_TITLE[scope]} 排班重算帳本結轉。")

    def _on_report(self, scope: str) -> None:
        month = self.service.storage.load_month(self.app.ym)
        text = month.get(f"report_{scope}") or "（本月尚未排班，無報告）"
        self._show_report_text(text, title=f"{_SCOPE_TITLE[scope]} 決策報告")

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
        # RF-16：報告鈕/定案勾選【不在此停用】——已定案月仍可看報告、可解除定案。
        state = "disabled" if (self._finalized or self._busy_flag) else "normal"
        for scope in ("r", "vs"):
            for w in (self._auto_btns[scope], self._clear_btns[scope],
                      self._resettle_btns[scope]):
                w.config(state=state)
