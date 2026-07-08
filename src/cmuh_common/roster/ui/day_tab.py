# -*- coding: utf-8 -*-
"""PGY / Clerk 日排班分頁（DayScheduleTab）。

PGY 與 Clerk 共用同一份 day_slots（同時段一起填），故兩個分頁看的是同一張表；
差別只在側邊管理面板（PGY＝當月人員；Clerk＝梯次於設定頁管理）與請假 scope。

自動排班走 solve_day（純 Python，無 ortools）→ 即時；預覽警告後才落地。
"""
from __future__ import annotations

import logging
import re
import tkinter as tk
from datetime import date
from tkinter import messagebox, ttk

from cmuh_common.roster.model import ClerkBatch, batches_covering, month_dates
from cmuh_common.roster.solve_day import BIOPSY, PHOTO, REST, TREATMENT
from cmuh_common.roster.ui.common import (
    MonthSelector, StatusBar, archive_finalize_pdf_async,
)
from cmuh_common.roster.ui.duty import LeaveEditor

_WD = "一二三四五六日"
_TITLE = {"pgy": "PGY 排班", "clerk": "Clerk 排班"}


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


class DayScheduleTab(ttk.Frame):
    def __init__(self, master, service, scope, app):
        super().__init__(master)
        self.service = service
        self.scope = scope           # "pgy" / "clerk"
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
        bar = ttk.Frame(self, padding=(6, 6))
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
        self._edit_btns = []                             # 定案時一併停用的編輯鈕
        self._leave_btn = ttk.Button(bar, text="請假…", command=self._on_leave)
        self._leave_btn.pack(side="left", padx=4)
        self._edit_btns.append(self._leave_btn)
        self._closure_btn = ttk.Button(bar, text="本月停診…",
                                       command=self._on_clinic_closure)
        self._closure_btn.pack(side="left", padx=4)
        self._edit_btns.append(self._closure_btn)
        if self.scope == "pgy":
            pb = ttk.Button(bar, text="當月 PGY 人員…", command=self._edit_pgy_roster)
            pb.pack(side="left", padx=4)
            self._edit_btns.append(pb)
        ttk.Button(bar, text="報告/警告", command=self._on_report
                   ).pack(side="left", padx=4)
        self._final_var = tk.BooleanVar(value=False)
        self._final_chk = ttk.Checkbutton(
            bar, text="定案", variable=self._final_var, command=self._on_finalize)
        self._final_chk.pack(side="left", padx=12)

    def _build_grid(self, parent) -> None:
        wrap = ttk.Frame(parent)
        wrap.pack(side="left", fill="both", expand=True)
        cols = ("date", "session", "lock", "photo", "tx", "biopsy", "rooms", "rest")
        heads = {"date": "日期", "session": "時段", "lock": "鎖",
                 "photo": "照光", "tx": "治療室", "biopsy": "切片室",
                 "rooms": "跟診診間", "rest": "放假"}
        widths = {"date": 90, "session": 44, "lock": 32, "photo": 60, "tx": 60,
                  "biopsy": 60, "rooms": 200, "rest": 90}
        self._tree = ttk.Treeview(wrap, columns=cols, show="headings", height=22)
        for c in cols:
            self._tree.heading(c, text=heads[c])
            self._tree.column(c, width=widths[c],
                              anchor="w" if c == "rooms" else "center")
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        self._tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self._tree.bind("<Double-1>", self._on_edit_row)

    def _build_side(self, parent) -> None:
        side = ttk.Frame(parent, width=220)
        side.pack(side="right", fill="y")
        ttk.Label(side, text="警告", font=("Microsoft JhengHei UI", 10, "bold")
                  ).pack(anchor="w", padx=6, pady=(6, 0))
        self._warns = tk.Listbox(side, width=32, height=18)
        self._warns.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        if self.scope == "clerk":
            ttk.Label(side, text="（Clerk 梯次/切片室開放\n於「設定」分頁管理）",
                      foreground="gray").pack(anchor="w", padx=6)

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

    def _refresh_warnings(self, warnings) -> None:
        self._warns.delete(0, tk.END)
        for w in warnings:
            self._warns.insert(tk.END, f"⚠ {w}")
        if not warnings:
            self._warns.insert(tk.END, "（無警告）")

    # ── 名單 ─────────────────────────────────────────────────────────────
    def _roster_members(self) -> list:
        """側邊/請假用的成員清單 [{id,name}]。"""
        ym = self.app.ym
        if self.scope == "pgy":
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

    def _format_report(self, log, warnings) -> str:
        return ("【警告】\n" + ("\n".join(f"  ⚠ {w}" for w in warnings) or "  （無）")
                + "\n\n【逐日過程】\n" + "\n".join(log))

    def _preview_and_accept(self, day_slots, log, warnings) -> None:
        report = self._format_report(log, warnings)
        win = tk.Toplevel(self)
        win.title(f"日排班預覽 · {_TITLE[self.scope]} · {self.app.ym}")
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
        ttk.Button(bar, text="套用", command=apply).pack(side="right", padx=6)
        ttk.Button(bar, text="取消", command=win.destroy).pack(side="right")
        win.grab_set()

    def _on_toggle_lock(self) -> None:
        if self._finalized:
            return
        sel = self._tree.selection()
        if not sel or "|" not in sel[0]:
            return
        iso, session = sel[0].split("|", 1)
        d = date.fromisoformat(iso)
        # 解鎖一律允許；只有「要新鎖定空時段」才擋（避免鎖住無內容的格後無法解）
        if not self.service.is_day_locked(self.app.ym, d, session):
            slots = ((self.service.storage.load_month(self.app.ym).get("day_slots")
                      or {}).get(iso) or {}).get(session)
            if not slots:
                messagebox.showinfo("鎖定", "此時段尚未排班，無法鎖定")
                return
        self.service.toggle_day_lock(self.app.ym, d, session)
        self.refresh()

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

    def _on_leave(self) -> None:
        if self._finalized:
            return
        members = self._roster_members()
        if not members:
            messagebox.showinfo("請假", "本月沒有可請假的人員（先設定 PGY 人員 / Clerk 梯次）")
            return
        ed = LeaveEditor(self, self.service, self.scope, self.app.ym, "leave",
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
        win.title(f"{_TITLE[self.scope]} 報告/警告 · {self.app.ym}")
        t = tk.Text(win, wrap="none", width=70, height=30, font=("Consolas", 10))
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
        for slot, ent in self._entries.items():
            self.service.set_day_slot(self.ym, self.d, self.session, slot,
                                      _split_codes(ent.get()))
        self.destroy()
        self.on_done()
