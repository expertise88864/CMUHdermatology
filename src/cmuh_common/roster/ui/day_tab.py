# -*- coding: utf-8 -*-
"""PGY / Clerk 日排班分頁（DayScheduleTab）。

PGY 與 Clerk 共用同一份 day_slots（同時段一起填），故兩個分頁看的是同一張表；
差別只在側邊管理面板（PGY＝當月人員；Clerk＝梯次於設定頁管理）與請假 scope。

自動排班走 solve_day（純 Python，無 ortools）→ 即時；預覽警告後才落地。
"""
from __future__ import annotations

import logging
import os
import re
import threading
import tkinter as tk
from datetime import date
from tkinter import filedialog, messagebox, ttk

from cmuh_common.deps_runtime import ensure_dependencies
from cmuh_common.roster.model import ClerkBatch, batches_covering, month_dates
from cmuh_common.roster.solve_day import (
    BIOPSY, PHOTO, REST, STAT_KEYS, TREATMENT, format_course_stats,
)
from cmuh_common.roster.ui.common import (
    CARD_BG, CARD_BORDER, CARD_CANVAS_BG, CARD_HDR_NORMAL, CARD_HDR_WEEKEND,
    CARD_SEP, CARD_TODAY_BORDER, OVR_FONT, OVR_STYLE, WEEKDAY_HEADERS,
    MonthSelector, StatusBar, archive_finalize_pdf_async, bind_hover_highlight,
    calendar_matrix,
)
from cmuh_common.roster.ui.duty import LeaveEditor

_WD = "一二三四五六日"
_TITLE = "PGY / Clerk 排班"


def _split_codes(text: str) -> list:
    """代號輸入切割：支援頓號、逗號（全半形）、空白等分隔（與畫面顯示的 `、` 一致）。"""
    return [c.strip() for c in re.split(r"[、,，\s]+", text or "") if c.strip()]


_SPECIAL_SLOTS = frozenset((PHOTO, TREATMENT, BIOPSY, REST))


def _rooms_summary(slots: dict) -> str:
    """把房號格彙整成 '101:AB 102:C'（照光/治療室/切片室/放假為特殊格，不算房）。
    房號可為任意字串（如 A101/診1）→ 用「排除特殊格」判定，不假設純數字。"""
    parts = []
    for k in sorted(slots):
        if k in _SPECIAL_SLOTS:
            continue
        parts.append(f"{k}:{''.join(slots[k])}")
    return "  ".join(parts)


def _overview_cell_rows(sessions: dict) -> list:
    """[2026-07-23] 月曆總覽單日格的結構化列（純函式，供 UI 上色排版）。

    sessions={session:{slot:[代號]}} → [(session, kind, label, people)]；
    kind ∈ photo/tx/biopsy/room/rest（照/治/切固定順序在前，跟診房升冪，休最後）。
    空時段不輸出。"""
    out = []
    for session in ("上午", "下午"):
        slots = sessions.get(session) or {}
        for key, kind, lab in ((PHOTO, "photo", "照光"),
                               (TREATMENT, "tx", "治療"),
                               (BIOPSY, "biopsy", "切片")):
            if slots.get(key):
                out.append((session, kind, lab, "、".join(slots[key])))
        for k in sorted(slots):
            if k not in _SPECIAL_SLOTS and slots[k]:
                out.append((session, "room", k, "、".join(slots[k])))
        if slots.get(REST):
            out.append((session, "rest", "休", "、".join(slots[REST])))
    return out


# 總覽色籤配色/字型改由 ui.common 共用（R/VS 月曆卡片同套視覺；別名保留舊名稱）
_OVR_STYLE = OVR_STYLE
_OVR_FONT = OVR_FONT


class DayScheduleTab(ttk.Frame):
    """[2026-07-23 使用者整合] PGY + 見習 Clerk 合併為單一分頁（本來就是同一份
    day_slots）：月曆總覽為預設檢視、右側同時放 PGY / Clerk 兩個週期統計面板。"""

    def __init__(self, master, service, app):
        super().__init__(master)
        self.service = service
        self.app = app
        self._finalized = False

        self._build_toolbar()
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        self._build_grid(body)
        self._build_side(body)
        self._status = StatusBar(self)
        self._status.pack(fill="x", side="bottom")
        self.refresh()

    # ── 版面 ─────────────────────────────────────────────────────────────
    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self, padding=(6, 4))
        bar.pack(fill="x")
        self._selector = MonthSelector(bar, self.app.ym, self._on_month_change)
        self._selector.pack(side="left")
        self._auto_btn = ttk.Button(bar, text="自動排班", command=self._on_auto)
        self._auto_btn.pack(side="left", padx=(12, 4))
        self._lock_btn = ttk.Button(bar, text="🔒鎖定/解鎖選取",
                                    command=self._on_toggle_lock)
        self._lock_btn.pack(side="left", padx=4)
        self._clear_btn = ttk.Button(bar, text="清除未鎖定", command=self._on_clear)
        self._clear_btn.pack(side="left", padx=4)
        ttk.Button(bar, text="報告/警告", command=self._on_report
                   ).pack(side="left", padx=4)
        # [2026-07-23 使用者] 月曆總覽改為分頁內建預設檢視;此鈕切換 列表↔月曆
        self._view_btn = ttk.Button(bar, text="切換列表檢視",
                                    command=self._on_toggle_view)
        self._view_btn.pack(side="left", padx=4)
        ttk.Button(bar, text="匯出", command=self._on_export).pack(side="left", padx=4)
        self._final_var = tk.BooleanVar(value=False)
        self._final_chk = ttk.Checkbutton(
            bar, text="定案", variable=self._final_var, command=self._on_finalize)
        self._final_chk.pack(side="left", padx=8)
        # 第二列：名單/請假/停診等編輯（PGY 與 Clerk 並列）
        bar2 = ttk.Frame(self, padding=(6, 0))
        bar2.pack(fill="x")
        self._edit_btns = []                             # 定案時一併停用的編輯鈕
        for text, cmd in (
                ("PGY 請假…", lambda: self._on_leave("pgy")),
                ("Clerk 請假…", lambda: self._on_leave("clerk")),
                ("本月停診…", self._on_clinic_closure),
                ("當月 PGY 人員…", self._edit_pgy_roster),
                ("Apply本科…", self._edit_apply_pref)):
            b = ttk.Button(bar2, text=text, command=cmd)
            b.pack(side="left", padx=4)
            self._edit_btns.append(b)
        ttk.Label(bar2, text="（Clerk 梯次/切片室開放於「設定」分頁管理）",
                  foreground="gray").pack(side="left", padx=8)

    def _build_grid(self, parent) -> None:
        """中央區＝兩個可切換檢視：月曆總覽（預設；2026-07-23 使用者：比較少看列表）
        與列表（鎖定/雙擊編輯用，按「切換列表檢視」叫出）。"""
        wrap = ttk.Frame(parent)
        wrap.pack(side="left", fill="both", expand=True)
        # ── 列表檢視（預設隱藏）─────────────────────────────────────────
        self._list_wrap = ttk.Frame(wrap)
        cols = ("date", "session", "lock", "photo", "tx", "biopsy", "rooms", "rest")
        heads = {"date": "日期", "session": "時段", "lock": "鎖",
                 "photo": "照光", "tx": "治療室", "biopsy": "切片室",
                 "rooms": "跟診診間", "rest": "放假"}
        widths = {"date": 90, "session": 44, "lock": 32, "photo": 60, "tx": 60,
                  "biopsy": 60, "rooms": 200, "rest": 90}
        self._tree = ttk.Treeview(self._list_wrap, columns=cols,
                                  show="headings", height=22)
        for c in cols:
            self._tree.heading(c, text=heads[c])
            self._tree.column(c, width=widths[c],
                              anchor="w" if c == "rooms" else "center")
        vsb = ttk.Scrollbar(self._list_wrap, orient="vertical",
                            command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        self._tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self._tree.bind("<Double-1>", self._on_edit_row)
        # ── 月曆總覽檢視（預設顯示；refresh 時重繪）───────────────────────
        self._cal_wrap = ttk.Frame(wrap)
        self._cal_canvas = tk.Canvas(self._cal_wrap, highlightthickness=0,
                                     bg=CARD_CANVAS_BG)
        cvsb = ttk.Scrollbar(self._cal_wrap, orient="vertical",
                             command=self._cal_canvas.yview)
        chsb = ttk.Scrollbar(self._cal_wrap, orient="horizontal",
                             command=self._cal_canvas.xview)
        chsb.pack(side="bottom", fill="x")
        self._cal_body = tk.Frame(self._cal_canvas, bg=CARD_CANVAS_BG)
        self._cal_body.bind(
            "<Configure>",
            lambda e: self._cal_canvas.configure(
                scrollregion=self._cal_canvas.bbox("all")))
        self._cal_canvas.create_window((0, 0), window=self._cal_body,
                                       anchor="nw")
        self._cal_canvas.configure(yscrollcommand=cvsb.set,
                                   xscrollcommand=chsb.set)
        self._cal_canvas.pack(side="left", fill="both", expand=True)
        cvsb.pack(side="right", fill="y")
        self._view_mode = "cal"
        self._cal_wrap.pack(fill="both", expand=True)

    def _show_view(self, mode: str) -> None:
        """切換中央檢視：'cal'=月曆總覽（預設）、'list'=列表（鎖定/編輯用）。"""
        if mode == self._view_mode:
            return
        if mode == "cal":
            self._list_wrap.pack_forget()
            self._cal_wrap.pack(fill="both", expand=True)
            self._view_btn.config(text="切換列表檢視")
        else:
            self._cal_wrap.pack_forget()
            self._list_wrap.pack(fill="both", expand=True)
            self._view_btn.config(text="切換月曆檢視")
        self._view_mode = mode

    def _on_toggle_view(self) -> None:
        self._show_view("list" if self._view_mode == "cal" else "cal")

    def _build_side(self, parent) -> None:
        side = ttk.Frame(parent, width=260)
        side.pack(side="right", fill="y")
        # [2026-07-23 整合] 右側同時放 PGY 與 Clerk 兩個週期統計面板。
        ttk.Label(side, text="PGY 週期統計（本月）",
                  font=(_OVR_FONT, 10, "bold")).pack(anchor="w", padx=6,
                                                     pady=(6, 0))
        self._stats_pgy = ttk.Treeview(side, columns=("c", "p", "w", "t", "f",
                                                      "r"),
                                       show="headings", height=5)
        for c, t, w in (("c", "代號", 52), ("p", "照光", 40), ("w", "週三午", 50),
                        ("t", "治療", 40), ("f", "跟診", 40), ("r", "放假", 40)):
            self._stats_pgy.heading(c, text=t)
            self._stats_pgy.column(c, width=w, anchor="center")
        self._stats_pgy.pack(fill="x", padx=6)
        ttk.Label(side, text="照光/治療室整月盡量一致；週三下午照光另獨立輪平均",
                  foreground="gray", wraplength=250,
                  justify="left").pack(anchor="w", padx=6, pady=(2, 0))
        ttk.Label(side, text="Clerk 週期統計（整梯兩週）",
                  font=(_OVR_FONT, 10, "bold")).pack(anchor="w", padx=6,
                                                     pady=(8, 0))
        self._stats_clerk = ttk.Treeview(side, columns=("c", "b", "f", "r"),
                                         show="headings", height=5)
        for c, t, w in (("c", "代號", 70), ("b", "切片", 46), ("f", "跟診", 46),
                        ("r", "放假", 46)):
            self._stats_clerk.heading(c, text=t)
            self._stats_clerk.column(c, width=w, anchor="center")
        self._stats_clerk.tag_configure("hdr", background="#E8E8E8")
        self._stats_clerk.tag_configure("miss", background="#FFD2D2")
        self._stats_clerk.pack(fill="x", padx=6)
        ttk.Label(side, text="每梯（跨月合併計）至少跟過一次切片室；紅底＝尚未排到",
                  foreground="gray", wraplength=250,
                  justify="left").pack(anchor="w", padx=6, pady=(2, 0))
        ttk.Label(side, text="警告", font=(_OVR_FONT, 10, "bold")
                  ).pack(anchor="w", padx=6, pady=(6, 0))
        self._warns = tk.Listbox(side, width=32, height=10)
        self._warns.pack(fill="both", expand=True, padx=6, pady=(0, 6))

    # ── 資料 → 畫面 ──────────────────────────────────────────────────────
    def refresh(self) -> None:
        ym = self.app.ym
        month = self.service.storage.load_month(ym)
        self._finalized = bool(month.get("finalized"))
        self._final_var.set(self._finalized)
        day_slots = month.get("day_slots") or {}
        day_locks = month.get("day_locks") or {}
        grid = self.service.build_day_input(ym).grid

        self._tree.delete(*self._tree.get_children())
        for d in sorted(grid):
            for session in ("上午", "下午"):
                slots = ((day_slots.get(d.isoformat()) or {}).get(session)) or {}
                rooms_open = (grid.get(d) or {}).get(session) or []
                rooms_disp = _rooms_summary(slots) or (
                    "（" + "、".join(rooms_open) + "）" if rooms_open else "—")
                locked = (day_locks.get(d.isoformat()) or {}).get(session)
                self._tree.insert("", "end", iid=f"{d.isoformat()}|{session}",
                                  values=(
                    f"{d.month}/{d.day}({_WD[d.weekday()]})", session,
                    "🔒" if locked else "",
                    "".join(slots.get(PHOTO, [])),
                    "".join(slots.get(TREATMENT, [])),
                    "".join(slots.get(BIOPSY, [])),
                    rooms_disp, "".join(slots.get(REST, []))))
        self._apply_finalized_state()
        # [RS-07] 手動編輯/重繪後跑快速檢查，填警告面板（warn 不擋存，符合設計 §16.4）。
        try:
            self._refresh_warnings(self.service.quick_validate_day(ym))
        except Exception:
            logging.debug("[roster.ui] quick_validate_day 失敗", exc_info=True)
        try:
            self._refresh_stats()
        except Exception:
            logging.debug("[roster.ui] 週期統計刷新失敗", exc_info=True)
        try:
            self._render_calendar(day_slots)
        except Exception:
            logging.debug("[roster.ui] 月曆總覽重繪失敗", exc_info=True)

    def _refresh_stats(self) -> None:
        """[2026-07-23] 側欄週期次數統計（PGY+Clerk 皆刷；吃存檔 day_slots）。"""
        data = self.service.day_course_stats(self.app.ym)
        t = self._stats_pgy
        t.delete(*t.get_children())
        stats, roster = data["pgy"]["stats"], data["pgy"]["roster"]
        for c in sorted({*roster, *stats}):
            st = stats.get(c) or dict.fromkeys(STAT_KEYS, 0)
            t.insert("", "end", values=(c, st["photo"], st["photo_wed_pm"],
                                        st["tx"], st["follow"], st["rest"]))
        t2 = self._stats_clerk
        t2.delete(*t2.get_children())
        for b in data["batches"]:
            t2.insert("", "end", tags=("hdr",),
                      values=(f"梯 {b['start'][5:]}~{b['end'][5:]}", "", "", ""))
            for c in sorted({*b["members"], *b["stats"]}):
                st = b["stats"].get(c) or dict.fromkeys(STAT_KEYS, 0)
                t2.insert("", "end",
                          tags=(("miss",) if st["biopsy"] == 0 else ()),
                          values=(c, st["biopsy"], st["follow"], st["rest"]))

    def _refresh_warnings(self, warnings) -> None:
        self._warns.delete(0, tk.END)
        for w in warnings:
            self._warns.insert(tk.END, f"⚠ {w}")
        if not warnings:
            self._warns.insert(tk.END, "（無警告）")

    # ── 名單 ─────────────────────────────────────────────────────────────
    def _roster_members(self, scope: str) -> list:
        """側邊/請假用的成員清單 [{id,name}]（scope="pgy"/"clerk"）。"""
        ym = self.app.ym
        if scope == "pgy":
            inp = self.service.build_day_input(ym)
            return [{"id": c, "name": ""} for c in inp.pgy_roster]
        y, m = int(ym[:4]), int(ym[5:7])
        batches = [ClerkBatch.from_dict(b)
                   for b in self.service.storage.load_clerk_batches()]
        codes = sorted({c for b in batches_covering(batches, y, m)
                        for c in b.members})
        return [{"id": c, "name": ""} for c in codes]

    # ── 互動 ─────────────────────────────────────────────────────────────
    def _on_month_change(self, ym) -> None:
        self.app.ym = ym
        self.refresh()

    def on_shown(self) -> None:
        if self._selector.ym != self.app.ym:
            self._selector.set_ym(self.app.ym)
        self.refresh()

    def _on_auto(self) -> None:
        if self._finalized:
            return
        try:
            day_slots, log, warnings = self.service.run_day_solve(self.app.ym)
        except Exception as e:  # noqa: BLE001
            logging.exception("[roster.ui] 日排班失敗")
            messagebox.showerror("排班失敗", f"排班時發生錯誤：\n{e}")
            return
        self._preview_and_accept(day_slots, log, warnings)

    def _format_report(self, log, warnings, day_slots=None) -> str:
        base = ("【警告】\n" + ("\n".join(f"  ⚠ {w}" for w in warnings) or "  （無）")
                + "\n\n【逐日過程】\n" + "\n".join(log))
        # [2026-07-23] 附週期次數統計（preview 的 day_slots 蓋掉本月、其他月讀存檔）
        try:
            data = self.service.day_course_stats(
                self.app.ym, day_slots_override=day_slots)
            base += "\n\n" + format_course_stats(
                data["pgy"]["stats"], data["pgy"]["roster"], data["batches"])
        except Exception:
            logging.debug("[roster.ui] 報告統計段生成失敗（略過）", exc_info=True)
        return base

    def _preview_and_accept(self, day_slots, log, warnings) -> None:
        # [codex P2] 統計用「鎖定合併後」的內容——與 accept_day_solution 落地的完全一致
        # (鎖定日掉出開診格網時 solver 輸出可能缺該時段,accept 會補回)。
        try:
            eff_slots = self.service.day_slots_with_locks(self.app.ym, day_slots)
        except Exception:
            logging.debug("[roster.ui] 預覽鎖定合併失敗,統計改用原始 preview",
                          exc_info=True)
            eff_slots = day_slots
        report = self._format_report(log, warnings, eff_slots)
        win = tk.Toplevel(self)
        win.title(f"日排班預覽 · {_TITLE} · {self.app.ym}")
        win.transient(self)
        txt = tk.Text(win, wrap="none", width=70, height=28, font=("Consolas", 10))
        txt.insert("1.0", report)
        txt.config(state="disabled")
        txt.pack(fill="both", expand=True, padx=6, pady=6)
        bar = ttk.Frame(win)
        bar.pack(fill="x", pady=(0, 6))

        def apply():
            try:
                # 落地時一併存下當下報告，供「報告」鈕顯示（與畫面/存檔一致）
                self.service.accept_day_solution(self.app.ym, day_slots, report)
            except Exception as e:  # noqa: BLE001
                messagebox.showerror("套用失敗", str(e), parent=win)
                return
            win.destroy()
            self.refresh()
            self._refresh_warnings(warnings)
            self._show_view("cal")   # [2026-07-23 使用者] 套用後直接顯示月曆總覽
        ttk.Button(bar, text="套用", command=apply).pack(side="right", padx=6)
        ttk.Button(bar, text="取消", command=win.destroy).pack(side="right")
        win.grab_set()

    def _toggle_lock_session(self, d: date, session: str) -> None:
        """鎖定/解鎖某(日,時段)。解鎖一律允許；只有「要新鎖定空時段」才擋
        （避免鎖住無內容的格後無法解）。列表選取與月曆格選單共用。"""
        if self._finalized:
            return
        if not self.service.is_day_locked(self.app.ym, d, session):
            slots = ((self.service.storage.load_month(self.app.ym)
                      .get("day_slots") or {}).get(d.isoformat())
                     or {}).get(session)
            if not slots:
                messagebox.showinfo("鎖定", "此時段尚未排班，無法鎖定")
                return
        self.service.toggle_day_lock(self.app.ym, d, session)
        self.refresh()

    def _on_toggle_lock(self) -> None:
        if self._finalized:
            return
        sel = self._tree.selection()
        if not sel or "|" not in sel[0]:
            if self._view_mode == "cal":     # 月曆檢視：直接點格子也可鎖定
                messagebox.showinfo(
                    "鎖定", "月曆檢視可直接點日期格 → 選「鎖定上午/下午」；\n"
                            "或按「切換列表檢視」在列表中選取時段後再按本鈕")
            return
        iso, session = sel[0].split("|", 1)
        self._toggle_lock_session(date.fromisoformat(iso), session)

    def _on_clear(self) -> None:
        if self._finalized:
            return
        if not messagebox.askyesno("清除未鎖定", "清除本月所有「未鎖定」的日排班時段？"):
            return
        try:
            self.service.clear_unlocked_day(self.app.ym)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("清除失敗", str(e))
            return
        self.refresh()
        self._refresh_warnings([])

    def _on_leave(self, scope: str) -> None:
        if self._finalized:
            return
        members = self._roster_members(scope)
        if not members:
            messagebox.showinfo("請假", "本月沒有可請假的人員（先設定 PGY 人員 / Clerk 梯次）")
            return
        ed = LeaveEditor(self, self.service, scope, self.app.ym, "leave",
                         members=members)
        self.wait_window(ed)
        self.refresh()

    def _on_clinic_closure(self) -> None:
        if self._finalized:
            return
        if not self.service.clinic_rooms_for_month(self.app.ym):
            messagebox.showinfo("本月停診", "門診週模板尚無診間（先於「設定」分頁建立）")
            return
        dlg = _ClinicClosureDialog(self, self.service, self.app.ym)
        self.wait_window(dlg)
        self.refresh()

    def _edit_pgy_roster(self) -> None:
        if self._finalized:
            return
        month = self.service.storage.load_month(self.app.ym)
        cur = month.get("pgy_month_roster")
        if cur is None:
            cfg = self.service.storage.load_config()
            cur = [str(mm.get("id")) for mm in (cfg.get("pgy_members") or [])]
        val = _prompt_codes(self, "當月 PGY 人員（代號，頓號/逗號分隔）", "、".join(cur))
        if val is None:
            return
        self.service.set_pgy_month_roster(self.app.ym, _split_codes(val))
        self.refresh()

    def _edit_apply_pref(self) -> None:
        """[2026-07-23 使用者] 勾選本月「Apply 本科」PGY（至多 2 位）：自動排班時
        週二/週五早午的 101 診在【次數平手時】優先排這些人（公平最優先）。"""
        if self._finalized:
            return
        roster = [m["id"] for m in self._roster_members("pgy")]
        if not roster:
            messagebox.showinfo("Apply本科", "本月沒有 PGY 人員（先設定當月 PGY）")
            return
        cur = set((self.service.storage.load_month(self.app.ym)
                   .get("pgy_apply_pref")) or [])
        dlg = tk.Toplevel(self)
        dlg.title(f"Apply 本科 PGY · {self.app.ym}")
        dlg.transient(self)
        dlg.resizable(False, False)
        ttk.Label(dlg, padding=8, justify="left",
                  text="勾選有意 Apply 本科的 PGY（至多 2 位）：\n"
                       "自動排班時，週二/週五（早午）的 101 診跟診會在\n"
                       "【次數平手時】優先排入勾選者。\n"
                       "公平第一：整月各項次數平均、請假優先，偏好只是最後的平手決勝。"
                  ).pack(anchor="w")
        vars_ = {}
        for c in roster:
            v = tk.BooleanVar(value=(c in cur))
            ttk.Checkbutton(dlg, text=c, variable=v).pack(anchor="w", padx=16)
            vars_[c] = v

        def ok():
            picked = [c for c, v in vars_.items() if v.get()]
            if len(picked) > 2:
                messagebox.showwarning("Apply本科", "最多選 2 位", parent=dlg)
                return
            try:
                self.service.set_pgy_apply_pref(self.app.ym, picked)
            except Exception as e:  # noqa: BLE001
                messagebox.showerror("儲存失敗", str(e), parent=dlg)
                return
            dlg.destroy()
            self.refresh()
        bar = ttk.Frame(dlg)
        bar.pack(pady=8)
        ttk.Button(bar, text="確定", command=ok).pack(side="left", padx=6)
        ttk.Button(bar, text="取消", command=dlg.destroy).pack(side="left", padx=6)
        dlg.grab_set()

    def _on_edit_row(self, _event) -> None:
        if self._finalized:
            return
        sel = self._tree.selection()
        if not sel or "|" not in sel[0]:
            return
        iso, session = sel[0].split("|", 1)
        _DayEditDialog(self, self.service, self.app.ym,
                       date.fromisoformat(iso), session, self.refresh)

    def _on_report(self) -> None:
        # 顯示「已落地/存檔」的日排班報告（非重新求解），與畫面一致
        month = self.service.storage.load_month(self.app.ym)
        text = month.get("day_report") or "（本月尚未套用日排班，無報告）"
        win = tk.Toplevel(self)
        win.title(f"{_TITLE} 報告/警告 · {self.app.ym}")
        t = tk.Text(win, wrap="none", width=70, height=30, font=("Consolas", 10))
        t.insert("1.0", text)
        t.config(state="disabled")
        t.pack(fill="both", expand=True, padx=6, pady=6)
        ttk.Button(win, text="關閉", command=win.destroy).pack(pady=(0, 6))

    def _build_overview_cell(self, parent, d, sessions) -> tk.Frame:
        """[2026-07-23 使用者美化] 單日卡片：日期標頭＋早/午分區＋角色色籤＋人名加粗；
        今日金框、早/午之間細分隔線。"""
        weekend = d.weekday() >= 5
        today = (d == date.today())
        cell = tk.Frame(parent, bg=CARD_BG,
                        highlightthickness=(2 if today else 1),
                        highlightbackground=(CARD_TODAY_BORDER if today
                                             else CARD_BORDER))
        hbg, hfg = CARD_HDR_WEEKEND if weekend else CARD_HDR_NORMAL
        hdr = tk.Label(cell, text=f"{d.day}（{_WD[d.weekday()]}）"
                       + ("  ⬅今天" if today else ""),
                       anchor="w", bg=hbg, fg=hfg,
                       font=(_OVR_FONT, 10, "bold"), padx=6)
        hdr.pack(fill="x")
        rows = _overview_cell_rows(sessions)
        if not rows:
            tk.Label(cell, text="—", bg=CARD_BG, fg="#BBBBBB",
                     font=(_OVR_FONT, 10)).pack(anchor="w", padx=8, pady=2)
            return cell
        last_session = None
        for session, kind, lab, people in rows:
            if last_session is not None and session != last_session:
                tk.Frame(cell, bg=CARD_SEP, height=1).pack(fill="x",
                                                           padx=4, pady=2)
            row = tk.Frame(cell, bg=CARD_BG)
            row.pack(fill="x", padx=4, pady=1)
            mark = ("早" if session == "上午" else "午") \
                if session != last_session else ""
            last_session = session
            tk.Label(row, text=mark, width=2, bg=CARD_BG,
                     fg=("#B26500" if session == "上午" else "#1F4E8C"),
                     font=(_OVR_FONT, 9, "bold")).pack(side="left")
            chip_bg, chip_fg = _OVR_STYLE[kind]
            tk.Label(row, text=lab, bg=chip_bg, fg=chip_fg, padx=4,
                     font=(_OVR_FONT, 8, "bold")).pack(side="left")
            tk.Label(row, text=people, bg=CARD_BG, fg="#1A1A1A", padx=4,
                     font=(_OVR_FONT, 10, "bold"), anchor="w",
                     justify="left", wraplength=140).pack(side="left",
                                                          fill="x", expand=True)
        return cell

    def _attach_cell_menu(self, cell, d) -> None:
        """[2026-07-23 使用者②] 月曆格可直接點選編輯：左/右鍵 → 選單（編輯上午/下午
        的各格＝強制指定照光/治療室/切片/跟診/放假；鎖定切換），免切列表。含懸停回饋。"""
        def _menu(event):
            if self._finalized:
                return
            m = tk.Menu(self, tearoff=0)
            for session in ("上午", "下午"):
                m.add_command(
                    label=f"編輯{session}（照光/治療室/切片/跟診/放假）…",
                    command=lambda s=session: _DayEditDialog(
                        self, self.service, self.app.ym, d, s, self.refresh))
            m.add_separator()
            for session in ("上午", "下午"):
                locked = self.service.is_day_locked(self.app.ym, d, session)
                m.add_command(
                    label=f"{'解鎖' if locked else '鎖定'}{session} 🔒",
                    command=lambda s=session: self._toggle_lock_session(d, s))
            m.tk_popup(event.x_root, event.y_root)

        def _bind_tree(w):
            w.bind("<Button-1>", _menu)
            w.bind("<Button-3>", _menu)
            w.configure(cursor="hand2")
            for ch in w.winfo_children():
                _bind_tree(ch)
        if not self._finalized:
            _bind_tree(cell)
            bind_hover_highlight(
                cell, CARD_TODAY_BORDER if d == date.today() else CARD_BORDER)

    def _render_calendar(self, day_slots: dict) -> None:
        """[2026-07-23 使用者] 內嵌月曆總覽（分頁預設檢視）：整月色籤卡片格狀重繪。
        頂列＝月份＋色籤圖例；每格＝當日早/午的照/治/切/跟診房/放假。"""
        ym = self.app.ym
        y, m = int(ym[:4]), int(ym[5:7])
        body = self._cal_body
        for w in body.winfo_children():
            w.destroy()
        legend = tk.Frame(body, bg=CARD_CANVAS_BG)
        legend.grid(row=0, column=0, columnspan=7, sticky="w",
                    padx=4, pady=(4, 2))
        tk.Label(legend, text=f"{y} 年 {m} 月", bg=CARD_CANVAS_BG,
                 font=(_OVR_FONT, 12, "bold")).pack(side="left", padx=(0, 12))
        for kind, lab in (("photo", "照光"), ("tx", "治療室"),
                          ("biopsy", "切片室"), ("room", "跟診"),
                          ("rest", "放假")):
            bg, fg = _OVR_STYLE[kind]
            tk.Label(legend, text=lab, bg=bg, fg=fg, padx=6,
                     font=(_OVR_FONT, 9, "bold")).pack(side="left", padx=3)
        for c, h in enumerate(WEEKDAY_HEADERS):
            tk.Label(body, text=h, anchor="center", bg=CARD_CANVAS_BG,
                     font=(_OVR_FONT, 10, "bold"),
                     fg="#B00020" if c >= 5 else "#2A3B50").grid(
                row=1, column=c, sticky="nsew", padx=2, pady=(2, 4))
        for r, week in enumerate(calendar_matrix(y, m), start=2):
            for c, d in enumerate(week):
                if d is None:
                    tk.Frame(body, bg=CARD_CANVAS_BG).grid(row=r, column=c)
                    continue
                cell = self._build_overview_cell(
                    body, d, day_slots.get(d.isoformat()) or {})
                cell.grid(row=r, column=c, sticky="nsew", padx=2, pady=2)
                # [2026-07-23 使用者②] 月曆格可直接點選編輯/鎖定（免切列表）
                self._attach_cell_menu(cell, d)
        for c in range(7):
            body.columnconfigure(c, weight=1, minsize=172)

    def _on_export(self) -> None:
        """[RS-01] 匯出整月班表（R/VS 月曆 + PGY/Clerk 日排班）。副檔名決定 Excel/Word；
        重依賴 lazy 安裝；沿用 duty.py 的背景執行緒 + after 回主緒模式。"""
        from cmuh_common.roster.export_common import default_filename
        data = self.service.build_export(self.app.ym)
        path = filedialog.asksaveasfilename(
            title="匯出班表", defaultextension=".xlsx",
            initialfile=default_filename(data, ".xlsx"),
            filetypes=[("Excel 活頁簿", "*.xlsx"), ("Word 文件", "*.docx")])
        if not path:
            return
        ext = os.path.splitext(path)[1].lower()
        if ext not in (".xlsx", ".docx"):
            messagebox.showerror(
                "不支援的格式",
                f"僅支援 Excel(.xlsx) 或 Word(.docx)，收到：{ext or '（無副檔名）'}")
            return
        dep = ([("openpyxl", "openpyxl")] if ext == ".xlsx"
               else [("python-docx", "docx")])

        def work():
            err = ""
            # ensure_dependencies 取消/失敗會 sys.exit(1)→SystemExit（非 Exception 子類）,
            # 一併攔截,否則 _after_export 不被排程、UI 無回饋。
            try:
                ensure_dependencies(dep)
                if ext == ".xlsx":
                    from cmuh_common.roster import export_xlsx
                    export_xlsx.export(path, data)
                else:
                    from cmuh_common.roster import export_docx
                    export_docx.export(path, data)
            except (Exception, SystemExit) as e:  # noqa: BLE001
                logging.exception("[roster.ui] 日排班匯出失敗")
                err = str(e) or "已取消或安裝失敗"
            self.after(0, lambda: self._after_export(path, err))
        threading.Thread(target=work, name="roster-day-export", daemon=True).start()

    def _after_export(self, path, err) -> None:
        if err:
            messagebox.showerror("匯出失敗", f"匯出時發生錯誤：\n{err}")
        else:
            messagebox.showinfo("匯出完成", f"已匯出：\n{path}")

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
        if on:                              # 定案 → 背景輸出 PDF 留底
            archive_finalize_pdf_async(self, self.service, self.app.ym)

    def _apply_finalized_state(self) -> None:
        state = "disabled" if self._finalized else "normal"
        for w in (self._auto_btn, self._clear_btn, self._lock_btn, *self._edit_btns):
            w.config(state=state)


def _prompt_codes(parent, title, initial) -> "str | None":
    dlg = tk.Toplevel(parent)
    dlg.title(title)
    dlg.transient(parent)
    dlg.resizable(False, False)
    ttk.Label(dlg, text=title, padding=8).pack()
    ent = ttk.Entry(dlg, width=40)
    ent.insert(0, initial)
    ent.pack(padx=8, pady=4)
    out = {"v": None}

    def ok():
        out["v"] = ent.get()
        dlg.destroy()
    bar = ttk.Frame(dlg); bar.pack(pady=8)
    ttk.Button(bar, text="確定", command=ok).pack(side="left", padx=6)
    ttk.Button(bar, text="取消", command=dlg.destroy).pack(side="left", padx=6)
    ent.focus_set()
    dlg.grab_set()
    parent.wait_window(dlg)
    return out["v"]


class _ClinicClosureDialog(tk.Toplevel):
    """本月門診停診：選診間 + 起訖日期 + 時段 → 停診/恢復（寫月檔 grid_overrides）。

    預設起訖＝整月，直接按「停診」即整月停診；縮小日期範圍則只停指定期間
    （某診 VS 請假某幾天）。自動排班會據此不把人排進停診的診間。
    """

    def __init__(self, master, service, ym):
        super().__init__(master)
        self.service = service
        self.ym = ym
        self.title(f"本月門診停診 · {ym}")
        self.resizable(False, False)
        self.transient(master)
        y, m = int(ym[:4]), int(ym[5:7])
        days = month_dates(y, m)
        pad = {"padx": 8, "pady": 4}
        r = 0
        ttk.Label(self, text="診間").grid(row=r, column=0, sticky="e", **pad)
        self._room = ttk.Combobox(self, width=16, state="readonly",
                                  values=service.clinic_rooms_for_month(ym))
        if self._room["values"]:
            self._room.current(0)
        self._room.grid(row=r, column=1, sticky="w", **pad)
        r += 1
        ttk.Label(self, text="起始日").grid(row=r, column=0, sticky="e", **pad)
        self._start = ttk.Entry(self, width=16)
        self._start.insert(0, days[0].isoformat())
        self._start.grid(row=r, column=1, sticky="w", **pad)
        r += 1
        ttk.Label(self, text="結束日").grid(row=r, column=0, sticky="e", **pad)
        self._end = ttk.Entry(self, width=16)
        self._end.insert(0, days[-1].isoformat())
        self._end.grid(row=r, column=1, sticky="w", **pad)
        r += 1
        ttk.Label(self, text="時段").grid(row=r, column=0, sticky="e", **pad)
        sf = ttk.Frame(self)
        sf.grid(row=r, column=1, sticky="w", **pad)
        self._am = tk.BooleanVar(value=True)
        self._pm = tk.BooleanVar(value=True)
        ttk.Checkbutton(sf, text="上午", variable=self._am).pack(side="left")
        ttk.Checkbutton(sf, text="下午", variable=self._pm).pack(side="left",
                                                                padx=(8, 0))
        r += 1
        ttk.Label(self, text="目前停診：" + (self._summary() or "（無）"),
                  foreground="gray", wraplength=300, justify="left").grid(
            row=r, column=0, columnspan=2, sticky="w", **pad)
        r += 1
        bar = ttk.Frame(self)
        bar.grid(row=r, column=0, columnspan=2, pady=8)
        ttk.Button(bar, text="停診", command=lambda: self._apply(True)
                   ).pack(side="left", padx=6)
        ttk.Button(bar, text="恢復開診", command=lambda: self._apply(False)
                   ).pack(side="left", padx=6)
        ttk.Button(bar, text="關閉", command=self.destroy).pack(side="left", padx=6)
        self.grab_set()

    def _summary(self) -> str:
        cur = self.service.clinic_closures(self.ym)
        parts = [f"{iso[5:]}{session[0]}:{','.join(rooms)}"
                 for iso in sorted(cur)
                 for session, rooms in sorted(cur[iso].items())]
        return "  ".join(parts[:14]) + (" …" if len(parts) > 14 else "")

    def _sessions(self) -> list:
        out = []
        if self._am.get():
            out.append("上午")
        if self._pm.get():
            out.append("下午")
        return out

    def _apply(self, closed: bool) -> None:
        room = self._room.get().strip()
        if not room:
            messagebox.showwarning("缺診間", "請選擇診間", parent=self)
            return
        try:
            start = date.fromisoformat(self._start.get().strip())
            end = date.fromisoformat(self._end.get().strip())
        except ValueError:
            messagebox.showwarning("日期格式", "日期需為 YYYY-MM-DD", parent=self)
            return
        if end < start:
            messagebox.showwarning("日期範圍", "結束日不可早於起始日", parent=self)
            return
        sessions = self._sessions()
        if not sessions:
            messagebox.showwarning("缺時段", "至少選一個時段", parent=self)
            return
        try:
            res = self.service.set_clinic_closed(self.ym, room, start, end,
                                                  sessions, closed=closed)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("失敗", str(e), parent=self)
            return
        # [RS-03/RS-05] 回報:清掉多少既有指派、以及撞到鎖定未自動移除的時段。
        res = res or {}
        cleared = res.get("cleared", 0)
        skipped = res.get("skipped_locked") or []
        msgs = []
        if cleared:
            msgs.append(f"已自現有班表移除 {cleared} 個指派，請重新自動排班。")
        if skipped:
            spans = "、".join(f"{iso} {sess}" for iso, sess in skipped)
            msgs.append(f"下列鎖定時段有停診診間的人，未自動移除（尊重鎖定），"
                        f"請自行處理：\n{spans}")
        if msgs:
            messagebox.showinfo("停診完成", "\n\n".join(msgs), parent=self)
        self.destroy()


class _DayEditDialog(tk.Toplevel):
    """手動改某日某時段的各格（照光/治療室/切片室/房號/放假），逗號分隔代號。"""

    def __init__(self, master, service, ym, d, session, on_done):
        super().__init__(master)
        self.service = service
        self.ym = ym
        self.d = d
        self.session = session
        self.on_done = on_done
        self.title(f"手動編輯 · {d.month}/{d.day} {session}")
        self.resizable(False, False)
        self.transient(master)

        month = service.storage.load_month(ym)
        slots = (((month.get("day_slots") or {}).get(d.isoformat()) or {})
                 .get(session)) or {}
        grid = service.build_day_input(ym).grid
        rooms = (grid.get(d) or {}).get(session) or []
        # 併入「已存但已從模板移除/被關閉」的房號 → 殘留指派才可編輯/清除
        stale = [k for k in slots if k not in _SPECIAL_SLOTS and k not in rooms]
        self._slots = [PHOTO, TREATMENT, BIOPSY, *sorted({*rooms, *stale}), REST]
        self._entries: dict = {}
        for i, slot in enumerate(self._slots):
            ttk.Label(self, text=slot).grid(row=i, column=0, sticky="e",
                                            padx=8, pady=3)
            ent = ttk.Entry(self, width=24)
            ent.insert(0, "、".join(slots.get(slot, [])))
            ent.grid(row=i, column=1, padx=8, pady=3)
            self._entries[slot] = ent
        bar = ttk.Frame(self)
        bar.grid(row=len(self._slots), column=0, columnspan=2, pady=8)
        ttk.Button(bar, text="儲存", command=self._save).pack(side="left", padx=6)
        ttk.Button(bar, text="取消", command=self.destroy).pack(side="left", padx=6)
        self.grab_set()

    def _save(self) -> None:
        # [RS-06] 一次覆寫整個時段（單次 load/save/git commit），取代逐格 set_day_slot。
        slots = {slot: _split_codes(ent.get())
                 for slot, ent in self._entries.items()}
        self.service.set_day_session(self.ym, self.d, self.session, slots)
        self.destroy()
        self.on_done()
