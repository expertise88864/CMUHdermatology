# -*- coding: utf-8 -*-
"""設定分頁：R/VS 名單、年度國定假日指定表、參數、手動週色、帳本檢視。

每一區塊變更即存檔（save_config / save_holiday_duty / save_week_colors /
save_ledger），並呼叫 on_changed() 通知排班分頁重載（名單/配色可能變）。
"""
from __future__ import annotations

import logging
import tkinter as tk
from datetime import date, datetime, timedelta
from tkinter import messagebox, ttk

from cmuh_common.roster.calendar_colors import week_colors_for_year
from cmuh_common.roster.ledger import reset_member, sync_members
from cmuh_common.roster.model import month_dates, week_key

_WD_CHOICES = ("無", "一", "二", "三", "四", "五", "六", "日")   # index-1 = weekday


def _wd_to_text(wd) -> str:
    return _WD_CHOICES[wd + 1] if isinstance(wd, int) and 0 <= wd <= 6 else "無"


def _text_to_wd(text: str):
    i = _WD_CHOICES.index(text) if text in _WD_CHOICES else 0
    return None if i == 0 else i - 1


class _MemberDialog(tk.Toplevel):
    """新增/編輯成員。回填 self.result（dict）或 None（取消）。"""

    def __init__(self, master, title, initial: dict, with_level, with_wd,
                 id_locked: bool):
        super().__init__(master)
        self.title(title)
        self.resizable(False, False)
        self.transient(master)
        self.result = None
        pad = {"padx": 8, "pady": 4}
        row = 0
        ttk.Label(self, text="代號/ID").grid(row=row, column=0, sticky="e", **pad)
        self._id = ttk.Entry(self, width=18)
        self._id.insert(0, initial.get("id", ""))
        if id_locked:
            self._id.config(state="disabled")
        self._id.grid(row=row, column=1, sticky="w", **pad)
        row += 1
        ttk.Label(self, text="姓名").grid(row=row, column=0, sticky="e", **pad)
        self._name = ttk.Entry(self, width=18)
        self._name.insert(0, initial.get("name", ""))
        self._name.grid(row=row, column=1, sticky="w", **pad)
        row += 1
        self._level = None
        if with_level:
            ttk.Label(self, text="級職").grid(row=row, column=0, sticky="e", **pad)
            self._level = ttk.Combobox(self, width=15, state="readonly",
                                       values=("", "R1", "R2", "R3", "R4"))
            self._level.set(initial.get("level", ""))
            self._level.grid(row=row, column=1, sticky="w", **pad)
            row += 1
        self._wd = None
        if with_wd:
            ttk.Label(self, text="固定值班").grid(row=row, column=0, sticky="e", **pad)
            self._wd = ttk.Combobox(self, width=15, state="readonly",
                                    values=_WD_CHOICES)
            self._wd.set(_wd_to_text(initial.get("fixed_weekday")))
            self._wd.grid(row=row, column=1, sticky="w", **pad)
            row += 1
        btns = ttk.Frame(self)
        btns.grid(row=row, column=0, columnspan=2, pady=8)
        ttk.Button(btns, text="確定", command=self._ok).pack(side="left", padx=6)
        ttk.Button(btns, text="取消", command=self.destroy).pack(side="left", padx=6)
        self._id.focus_set() if not id_locked else self._name.focus_set()
        self.grab_set()
        self.wait_window(self)

    def _ok(self) -> None:
        mid = self._id.get().strip()
        name = self._name.get().strip()
        if not mid:
            messagebox.showwarning("欄位不完整", "代號/ID 不可空白", parent=self)
            return
        out = {"id": mid, "name": name}
        if self._level is not None:
            out["level"] = self._level.get().strip()
        if self._wd is not None:
            out["fixed_weekday"] = _text_to_wd(self._wd.get())
        self.result = out
        self.destroy()


class SettingsTab(ttk.Frame):
    def __init__(self, master, service, on_changed=None):
        super().__init__(master)
        self.service = service
        self.on_changed = on_changed
        self._cfg = self.service.storage.load_config()

        # 整頁可捲動（區塊多）
        canvas = tk.Canvas(self, highlightthickness=0)
        vsb = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        self._body = ttk.Frame(canvas)
        self._body.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self._body, anchor="nw")
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self._member_trees: dict = {}
        self._build_members("r", "R 住院醫師名單", with_level=True, with_wd=True)
        self._build_members("vs", "VS 主治醫師名單", with_level=False, with_wd=False)
        self._build_holiday_table()
        self._build_params()
        self._build_pgy_defaults()
        self._build_clinic_template()
        self._build_clerk_batches()
        self._build_week_colors()
        self._build_ledger_view()

    # ── 共用 ─────────────────────────────────────────────────────────────
    def _save_cfg(self) -> None:
        self.service.storage.save_config(self._cfg)
        if self.on_changed:
            self.on_changed()

    def _notify(self) -> None:
        if self.on_changed:
            self.on_changed()

    # ── 區塊 1/2：R / VS 名單 ────────────────────────────────────────────
    def _members(self, scope: str) -> list:
        return self._cfg.setdefault(f"{scope}_members", [])

    def _build_members(self, scope, title, with_level, with_wd) -> None:
        lf = ttk.LabelFrame(self._body, text=title, padding=8)
        lf.pack(fill="x", padx=10, pady=6)
        cols = ["id", "name"]
        heads = {"id": "代號", "name": "姓名"}
        if with_level:
            cols.append("level"); heads["level"] = "級職"
        if with_wd:
            cols.append("wd"); heads["wd"] = "固定值班"
        tree = ttk.Treeview(lf, columns=cols, show="headings", height=4)
        for c in cols:
            tree.heading(c, text=heads[c])
            tree.column(c, width=90, anchor="center")
        tree.pack(fill="x")
        self._member_trees[scope] = (tree, with_level, with_wd)
        self._reload_members(scope)

        bar = ttk.Frame(lf)
        bar.pack(fill="x", pady=(6, 0))
        ttk.Button(bar, text="新增", command=lambda: self._member_add(scope)
                   ).pack(side="left")
        ttk.Button(bar, text="編輯", command=lambda: self._member_edit(scope)
                   ).pack(side="left", padx=4)
        ttk.Button(bar, text="刪除", command=lambda: self._member_del(scope)
                   ).pack(side="left")

    def _reload_members(self, scope) -> None:
        tree, with_level, with_wd = self._member_trees[scope]
        tree.delete(*tree.get_children())
        for mem in self._members(scope):
            vals = [mem.get("id", ""), mem.get("name", "")]
            if with_level:
                vals.append(mem.get("level", ""))
            if with_wd:
                vals.append(_wd_to_text(mem.get("fixed_weekday")))
            tree.insert("", "end", iid=mem.get("id", ""), values=vals)

    def _member_add(self, scope) -> None:
        _tree, with_level, with_wd = self._member_trees[scope]
        dlg = _MemberDialog(self, "新增成員", {}, with_level, with_wd,
                            id_locked=False)
        if not dlg.result:
            return
        if any(m.get("id") == dlg.result["id"] for m in self._members(scope)):
            messagebox.showwarning("重複", f"代號 {dlg.result['id']} 已存在")
            return
        self._members(scope).append(dlg.result)
        self._save_cfg()
        self._reload_members(scope)
        self._sync_ledger(scope)

    def _member_edit(self, scope) -> None:
        tree, with_level, with_wd = self._member_trees[scope]
        sel = tree.selection()
        if not sel:
            return
        members = self._members(scope)
        idx = next((i for i, m in enumerate(members) if m.get("id") == sel[0]), -1)
        if idx < 0:
            return
        dlg = _MemberDialog(self, "編輯成員", members[idx], with_level, with_wd,
                            id_locked=True)   # id 是帳本鍵，不可改
        if not dlg.result:
            return
        dlg.result["id"] = members[idx]["id"]        # 保險：鎖住 id
        members[idx] = dlg.result
        self._save_cfg()
        self._reload_members(scope)

    def _member_del(self, scope) -> None:
        tree, _wl, _ww = self._member_trees[scope]
        sel = tree.selection()
        if not sel:
            return
        if not messagebox.askyesno(
                "刪除成員",
                f"刪除 {sel[0]}？該員在 {scope.upper()} 的帳本餘額將一併作廢。"):
            return
        self._cfg[f"{scope}_members"] = [
            m for m in self._members(scope) if m.get("id") != sel[0]]
        self._save_cfg()
        self._reload_members(scope)
        self._sync_ledger(scope)

    def _sync_ledger(self, scope) -> None:
        """名單變動 → 帳本同步（新人補 0、離開者作廢）。"""
        ledger = self.service.storage.load_ledger()
        sync_members(ledger, scope, [m.get("id") for m in self._members(scope)])
        self.service.storage.save_ledger(ledger)
        self._reload_ledger()

    # ── 區塊 3：年度國定假日指定表 ───────────────────────────────────────
    def _build_holiday_table(self) -> None:
        lf = ttk.LabelFrame(
            self._body, text="年度國定假日指定表（此表的日期＝當年度國定假日清單）",
            padding=8)
        lf.pack(fill="x", padx=10, pady=6)
        cols = ("date", "r", "vs")
        self._hol_tree = ttk.Treeview(lf, columns=cols, show="headings", height=4)
        for c, t, w in (("date", "日期", 110), ("r", "R 指定", 80),
                        ("vs", "VS 指定", 80)):
            self._hol_tree.heading(c, text=t)
            self._hol_tree.column(c, width=w, anchor="center")
        self._hol_tree.pack(fill="x")
        self._reload_holidays()

        bar = ttk.Frame(lf)
        bar.pack(fill="x", pady=(6, 0))
        ttk.Label(bar, text="日期 YYYY-MM-DD").pack(side="left")
        self._hol_date = ttk.Entry(bar, width=12)
        self._hol_date.pack(side="left", padx=4)
        ttk.Label(bar, text="R").pack(side="left")
        self._hol_r = ttk.Entry(bar, width=6)
        self._hol_r.pack(side="left", padx=2)
        ttk.Label(bar, text="VS").pack(side="left")
        self._hol_vs = ttk.Entry(bar, width=6)
        self._hol_vs.pack(side="left", padx=2)
        ttk.Button(bar, text="新增/更新", command=self._holiday_put
                   ).pack(side="left", padx=4)
        ttk.Button(bar, text="刪除選取", command=self._holiday_del
                   ).pack(side="left")

    def _load_holiday_map(self) -> dict:
        return self.service.storage.load_holiday_duty()

    def _reload_holidays(self) -> None:
        self._hol_tree.delete(*self._hol_tree.get_children())
        table = self._load_holiday_map()
        all_dates = sorted(set(table["r"]) | set(table["vs"]))
        for d in all_dates:
            iso = d.isoformat()
            self._hol_tree.insert("", "end", iid=iso, values=(
                iso, table["r"].get(d, ""), table["vs"].get(d, "")))

    def _holiday_put(self) -> None:
        raw = self._hol_date.get().strip()
        try:
            d = date.fromisoformat(raw)
        except ValueError:
            messagebox.showwarning("日期格式", "請輸入 YYYY-MM-DD")
            return
        table = self._load_holiday_map()
        r, vs = self._hol_r.get().strip(), self._hol_vs.get().strip()
        if r:
            table["r"][d] = r
        else:
            table["r"].pop(d, None)
        if vs:
            table["vs"][d] = vs
        else:
            table["vs"].pop(d, None)
        self.service.storage.save_holiday_duty(table)
        self._reload_holidays()
        self._notify()

    def _holiday_del(self) -> None:
        sel = self._hol_tree.selection()
        if not sel:
            return
        table = self._load_holiday_map()
        for iso in sel:
            d = date.fromisoformat(iso)
            table["r"].pop(d, None)
            table["vs"].pop(d, None)
        self.service.storage.save_holiday_duty(table)
        self._reload_holidays()
        self._notify()

    # ── 區塊 4：參數 ─────────────────────────────────────────────────────
    def _build_params(self) -> None:
        lf = ttk.LabelFrame(self._body, text="參數", padding=8)
        lf.pack(fill="x", padx=10, pady=6)
        pts = self._cfg.setdefault(
            "points", {"weekday": 1, "weekend": 2, "national_holiday": 1})
        rng = self._cfg.setdefault("duty_range_soft", [9, 11])
        self._cfg.setdefault("room_capacity", 2)

        def spin(parent, frm, to, init):
            var = tk.IntVar(value=int(init))
            ttk.Spinbox(parent, from_=frm, to=to, width=5, textvariable=var,
                        command=self._save_params).pack(side="left", padx=(2, 12))
            var.trace_add("write", lambda *_a: self._save_params())
            return var

        rowa = ttk.Frame(lf); rowa.pack(fill="x", pady=2)
        ttk.Label(rowa, text="點數 平日").pack(side="left")
        self._p_wd = spin(rowa, 0, 9, pts.get("weekday", 1))
        ttk.Label(rowa, text="週末").pack(side="left")
        self._p_we = spin(rowa, 0, 9, pts.get("weekend", 2))
        ttk.Label(rowa, text="平日國定假日").pack(side="left")
        self._p_hol = spin(rowa, 0, 9, pts.get("national_holiday", 1))

        rowb = ttk.Frame(lf); rowb.pack(fill="x", pady=2)
        ttk.Label(rowb, text="R 班數範圍").pack(side="left")
        self._p_min = spin(rowb, 0, 31, rng[0])
        ttk.Label(rowb, text="—").pack(side="left")
        self._p_max = spin(rowb, 0, 31, rng[1])
        ttk.Label(rowb, text="診間容量").pack(side="left")
        self._p_cap = spin(rowb, 1, 9, self._cfg.get("room_capacity", 2))

    def _save_params(self) -> None:
        try:
            self._cfg["points"] = {
                "weekday": self._p_wd.get(), "weekend": self._p_we.get(),
                "national_holiday": self._p_hol.get()}
            self._cfg["duty_range_soft"] = [self._p_min.get(), self._p_max.get()]
            self._cfg["room_capacity"] = self._p_cap.get()
        except (tk.TclError, ValueError):
            return                                   # 輸入中的暫態非數字
        self._save_cfg()

    # ── PGY 預設代號 ─────────────────────────────────────────────────────
    def _build_pgy_defaults(self) -> None:
        lf = ttk.LabelFrame(self._body, text="PGY 預設代號（每月可於 PGY 分頁再調整）",
                            padding=8)
        lf.pack(fill="x", padx=10, pady=6)
        self._pgy_entry = ttk.Entry(lf, width=40)
        self._pgy_entry.insert(0, "、".join(
            str(mm.get("id")) for mm in (self._cfg.get("pgy_members") or [])))
        self._pgy_entry.pack(side="left", padx=(0, 6))
        ttk.Button(lf, text="儲存", command=self._save_pgy_defaults
                   ).pack(side="left")

    def _save_pgy_defaults(self) -> None:
        codes = [c.strip() for c in self._pgy_entry.get().replace("，", ",")
                 .replace("、", ",").split(",") if c.strip()]
        self._cfg["pgy_members"] = [{"id": c} for c in codes]
        self._save_cfg()

    # ── 門診週模板（開診格網來源）─────────────────────────────────────────
    def _build_clinic_template(self) -> None:
        lf = ttk.LabelFrame(
            self._body, text="門診週模板（週幾×時段開哪些診間；自費診勾選後不排學生）",
            padding=8)
        lf.pack(fill="x", padx=10, pady=6)
        cols = ("wd", "session", "room", "doctor", "paid")
        self._tpl_tree = ttk.Treeview(lf, columns=cols, show="headings", height=5)
        for c, t, w in (("wd", "週幾", 50), ("session", "時段", 60),
                        ("room", "診間", 70), ("doctor", "醫師", 80),
                        ("paid", "自費", 50)):
            self._tpl_tree.heading(c, text=t)
            self._tpl_tree.column(c, width=w, anchor="center")
        self._tpl_tree.pack(fill="x")
        bar = ttk.Frame(lf); bar.pack(fill="x", pady=(6, 0))
        ttk.Button(bar, text="新增", command=self._template_add).pack(side="left")
        ttk.Button(bar, text="刪除選取", command=self._template_del
                   ).pack(side="left", padx=4)
        self._reload_template()

    def _load_template(self) -> dict:
        return self.service.storage.load_clinic_template()

    def _reload_template(self) -> None:
        self._tpl_tree.delete(*self._tpl_tree.get_children())
        tpl = (self._load_template().get("template") or {})
        for wd in sorted(tpl):
            for session in ("上午", "下午"):
                for i, e in enumerate(tpl[wd].get(session) or []):
                    self._tpl_tree.insert("", "end", iid=f"{wd}|{session}|{i}",
                                          values=(
                        _WD_CHOICES[int(wd) + 1], session, e.get("room", ""),
                        e.get("doctor", ""), "✓" if e.get("is_self_paid") else ""))

    def _template_add(self) -> None:
        dlg = _ClinicRoomDialog(self)
        if not dlg.result:
            return
        wd, session, room, doctor, paid = dlg.result
        data = self._load_template()
        tpl = data.setdefault("template", {})
        entry = {"room": room, "doctor": doctor}
        if paid:
            entry["is_self_paid"] = True
        tpl.setdefault(str(wd), {}).setdefault(session, []).append(entry)
        self.service.storage.save_clinic_template(data)
        self._reload_template()
        self._notify()

    def _template_del(self) -> None:
        sel = self._tpl_tree.selection()
        if not sel or "|" not in sel[0]:
            return
        wd, session, idx = sel[0].split("|")
        data = self._load_template()
        lst = ((data.get("template") or {}).get(wd) or {}).get(session) or []
        try:
            lst.pop(int(idx))
        except (ValueError, IndexError):
            return
        self.service.storage.save_clinic_template(data)
        self._reload_template()
        self._notify()

    # ── Clerk 梯次（兩週一梯，起始必週一）+ 切片室開放格網 ──────────────────
    def _build_clerk_batches(self) -> None:
        lf = ttk.LabelFrame(self._body, text="Clerk 梯次（兩週一梯，起始必為週一）",
                            padding=8)
        lf.pack(fill="x", padx=10, pady=6)
        cols = ("start", "members")
        self._batch_tree = ttk.Treeview(lf, columns=cols, show="headings", height=4)
        self._batch_tree.heading("start", text="起始週一")
        self._batch_tree.heading("members", text="成員代號")
        self._batch_tree.column("start", width=110, anchor="center")
        self._batch_tree.column("members", width=200, anchor="w")
        self._batch_tree.pack(fill="x")
        bar = ttk.Frame(lf); bar.pack(fill="x", pady=(6, 0))
        ttk.Button(bar, text="新增", command=self._batch_add).pack(side="left")
        ttk.Button(bar, text="編輯", command=self._batch_edit
                   ).pack(side="left", padx=4)
        ttk.Button(bar, text="刪除", command=self._batch_del).pack(side="left")
        ttk.Button(bar, text="切片室開放…", command=self._batch_biopsy
                   ).pack(side="left", padx=(12, 0))
        self._reload_batches()

    @staticmethod
    def _batch_key(b: dict) -> str:
        """樹列 iid 與查找共用的鍵（id 缺失的舊資料退回 start_monday，前後一致）。"""
        return str(b.get("id") or b.get("start_monday") or "")

    def _reload_batches(self) -> None:
        self._batch_tree.delete(*self._batch_tree.get_children())
        for b in self.service.storage.load_clerk_batches():
            self._batch_tree.insert("", "end", iid=self._batch_key(b),
                                    values=(b.get("start_monday", ""),
                                            "、".join(b.get("members") or [])))

    def _batch_add(self) -> None:
        dlg = _ClerkBatchDialog(self, {})
        if dlg.result:
            batches = self.service.storage.load_clerk_batches()
            batches.append(dlg.result)
            self.service.storage.save_clerk_batches(batches)
            self._seed_biopsy_from_prev(dlg.result, batches)   # 預設複製上一梯次模式
            self._reload_batches()
            self._notify()

    def _seed_biopsy_from_prev(self, new_batch: dict, batches: list) -> None:
        """新梯次切片格網預設＝複製「前一梯次」的模式（依相對週幾對齊，C3 定案）。"""
        grid_all = self.service.storage.load_biopsy_grid()
        if grid_all.get(new_batch["id"]):
            return                                   # 已有設定就不覆蓋
        prevs = [b for b in batches
                 if b.get("id") and b["id"] != new_batch["id"]
                 and b.get("start_monday", "") < new_batch["start_monday"]]
        if not prevs:
            return
        prev = max(prevs, key=lambda b: b["start_monday"])
        prev_grid = grid_all.get(prev["id"]) or {}
        if not prev_grid:
            return
        ns = date.fromisoformat(new_batch["start_monday"])
        ps = date.fromisoformat(prev["start_monday"])
        seeded: dict = {}
        for i in range(14):                          # 相對第 i 天對齊（皆週一起 → 同週幾）
            pg = prev_grid.get((ps + timedelta(days=i)).isoformat())
            if pg:
                seeded[(ns + timedelta(days=i)).isoformat()] = dict(pg)
        if seeded:
            grid_all[new_batch["id"]] = seeded
            self.service.storage.save_biopsy_grid(grid_all)

    def _batch_edit(self) -> None:
        sel = self._batch_tree.selection()
        if not sel:
            return
        batches = self.service.storage.load_clerk_batches()
        cur = next((b for b in batches if self._batch_key(b) == sel[0]), None)
        if cur is None:
            return
        old_start = cur.get("start_monday")
        dlg = _ClerkBatchDialog(self, cur)
        if dlg.result:
            cur.update(dlg.result)
            self.service.storage.save_clerk_batches(batches)
            if cur.get("id") and cur.get("start_monday") != old_start:
                self._shift_biopsy_grid(cur["id"], old_start, cur["start_monday"])
            self._reload_batches()
            self._notify()

    def _shift_biopsy_grid(self, batch_id, old_start, new_start) -> None:
        """改梯次起始日 → 把切片格網整組平移相同天數（否則新窗覆蓋不到、資料失效）。"""
        grid_all = self.service.storage.load_biopsy_grid()
        g = grid_all.get(batch_id)
        if not g or not old_start or not new_start:
            return
        try:
            delta = (date.fromisoformat(new_start)
                     - date.fromisoformat(old_start)).days
        except ValueError:
            return
        if delta == 0:
            return
        shifted: dict = {}
        for iso, sess in g.items():
            try:
                nd = date.fromisoformat(iso) + timedelta(days=delta)
            except ValueError:
                continue
            shifted[nd.isoformat()] = sess
        grid_all[batch_id] = shifted
        self.service.storage.save_biopsy_grid(grid_all)

    def _batch_del(self) -> None:
        sel = self._batch_tree.selection()
        if not sel or not messagebox.askyesno("刪除梯次", f"刪除梯次 {sel[0]}？"):
            return
        batches = [b for b in self.service.storage.load_clerk_batches()
                   if self._batch_key(b) != sel[0]]
        self.service.storage.save_clerk_batches(batches)
        self._reload_batches()
        self._notify()

    def _batch_biopsy(self) -> None:
        sel = self._batch_tree.selection()
        if not sel:
            messagebox.showinfo("切片室開放", "請先選一個梯次")
            return
        batch = next((b for b in self.service.storage.load_clerk_batches()
                      if self._batch_key(b) == sel[0]), None)
        if not batch:
            return
        if not batch.get("id"):     # 切片格網以 id 為鍵（build_day_input 亦然）
            messagebox.showinfo("切片室開放",
                                "此梯次尚無 id，請先按「編輯」儲存一次以指派 id")
            return
        _BiopsyGridDialog(self, self.service, batch)
        self._notify()

    # ── 區塊 5：行事曆週色（決定性自動套色，可手動覆蓋）─────────────────────
    def _build_week_colors(self) -> None:
        lf = ttk.LabelFrame(
            self._body,
            text="行事曆週色（依 115 行事曆 4 週交替自動套色；雙擊該週可手動覆蓋）",
            padding=8)
        lf.pack(fill="x", padx=10, pady=6)
        bar = ttk.Frame(lf); bar.pack(fill="x")
        ttk.Label(bar, text="年").pack(side="left")
        self._wc_year = tk.IntVar(value=date.today().year)
        ttk.Spinbox(bar, from_=2020, to=2100, width=6, textvariable=self._wc_year,
                    command=self._reload_week_colors).pack(side="left", padx=4)
        ttk.Label(bar, text="（自動色往後年度自動延續，通常免手動）",
                  foreground="gray").pack(side="left", padx=8)
        cols = ("week", "range", "color")
        self._wc_tree = ttk.Treeview(lf, columns=cols, show="headings", height=6)
        for c, t, w in (("week", "ISO 週", 90), ("range", "起訖", 150),
                        ("color", "色", 80)):
            self._wc_tree.heading(c, text=t)
            self._wc_tree.column(c, width=w, anchor="center")
        self._wc_tree.pack(fill="x", pady=(6, 0))
        self._wc_tree.bind("<Double-1>", self._week_color_cycle)
        self._reload_week_colors()

    def _year_weeks(self, year: int) -> list:
        """該年所有 ISO 週 → [(week_key, 起, 訖)]，起訖取落在該年的日期範圍。"""
        buckets: dict = {}
        for m in range(1, 13):
            for d in month_dates(year, m):
                buckets.setdefault(week_key(d), []).append(d)
        out = []
        for wk, days in buckets.items():
            out.append((wk, min(days), max(days)))
        return sorted(out, key=lambda t: t[1])

    def _reload_week_colors(self) -> None:
        self._wc_tree.delete(*self._wc_tree.get_children())
        year = self._wc_year.get()
        auto = week_colors_for_year(year)
        manual = self.service.storage.load_week_colors()
        label = {"pink": "粉", "green": "綠"}
        for wk, lo, hi in self._year_weeks(year):
            eff = manual.get(wk, auto.get(wk, ""))
            tag = "（手動）" if wk in manual else "（自動）"
            self._wc_tree.insert("", "end", iid=wk, values=(
                wk, f"{lo.month}/{lo.day}–{hi.month}/{hi.day}",
                f"{label.get(eff, '?')}{tag}"))

    def _week_color_cycle(self, _event) -> None:
        """雙擊循環：自動 → 手動粉 → 手動綠 → 自動（移除覆蓋回歸決定性色）。"""
        sel = self._wc_tree.selection()
        if not sel:
            return
        wk = sel[0]
        manual = self.service.storage.load_week_colors()   # 全年度攤平覆蓋集
        nxt = {None: "pink", "pink": "green", "green": None}[manual.get(wk)]
        if nxt:
            manual[wk] = nxt
        else:
            manual.pop(wk, None)                            # 移除覆蓋→回自動色
        # replace=True：整組取代（否則 merge 無法真正刪掉已移除的覆蓋）
        self.service.storage.save_week_colors(
            self._wc_year.get(), manual, source="manual", replace=True)
        self._reload_week_colors()
        self._notify()

    # ── 區塊 6：帳本檢視 ─────────────────────────────────────────────────
    def _build_ledger_view(self) -> None:
        lf = ttk.LabelFrame(self._body, text="點數帳本（結轉；正=多值 負=欠）",
                            padding=8)
        lf.pack(fill="x", padx=10, pady=6)
        cols = ("scope", "member", "balance")
        self._led_tree = ttk.Treeview(lf, columns=cols, show="headings", height=5)
        for c, t, w in (("scope", "類別", 70), ("member", "成員", 90),
                        ("balance", "餘額", 90)):
            self._led_tree.heading(c, text=t)
            self._led_tree.column(c, width=w, anchor="center")
        self._led_tree.pack(fill="x")
        ttk.Button(lf, text="選取者歸零", command=self._ledger_reset
                   ).pack(side="left", pady=(6, 0))
        self._reload_ledger()

    def _reload_ledger(self) -> None:
        if not hasattr(self, "_led_tree"):
            return
        self._led_tree.delete(*self._led_tree.get_children())
        ledger = self.service.storage.load_ledger()
        for scope in ("r", "vs"):
            for mid, bal in sorted((ledger.get(scope) or {}).items()):
                self._led_tree.insert("", "end", iid=f"{scope}:{mid}", values=(
                    scope.upper(), mid, f"{float(bal):+.2f}"))

    def _ledger_reset(self) -> None:
        sel = self._led_tree.selection()
        if not sel or ":" not in sel[0]:
            return
        scope, mid = sel[0].split(":", 1)
        if not messagebox.askyesno("歸零", f"將 {scope.upper()}/{mid} 帳本歸零？"):
            return
        ledger = self.service.storage.load_ledger()
        reset_member(ledger, scope, mid)
        self.service.storage.save_ledger(ledger)
        self._reload_ledger()
        logging.info("[roster.ui] 帳本歸零 %s/%s", scope, mid)


# ─── Phase 3 設定用對話框 ───────────────────────────────────────────────────
class _ClinicRoomDialog(tk.Toplevel):
    """新增一筆門診格（週幾×時段×診間）。回填 self.result 或 None。"""
    _WD5 = ("一", "二", "三", "四", "五")

    def __init__(self, master):
        super().__init__(master)
        self.title("新增門診格")
        self.resizable(False, False)
        self.transient(master)
        self.result = None
        pad = {"padx": 8, "pady": 4}
        ttk.Label(self, text="週幾").grid(row=0, column=0, sticky="e", **pad)
        self._wd = ttk.Combobox(self, width=8, state="readonly", values=self._WD5)
        self._wd.current(0)
        self._wd.grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(self, text="時段").grid(row=1, column=0, sticky="e", **pad)
        self._sess = ttk.Combobox(self, width=8, state="readonly",
                                  values=("上午", "下午"))
        self._sess.current(0)
        self._sess.grid(row=1, column=1, sticky="w", **pad)
        ttk.Label(self, text="診間房號").grid(row=2, column=0, sticky="e", **pad)
        self._room = ttk.Entry(self, width=12)
        self._room.grid(row=2, column=1, sticky="w", **pad)
        ttk.Label(self, text="醫師（選填）").grid(row=3, column=0, sticky="e", **pad)
        self._doc = ttk.Entry(self, width=12)
        self._doc.grid(row=3, column=1, sticky="w", **pad)
        self._paid = tk.BooleanVar(value=False)
        ttk.Checkbutton(self, text="自費/美容診（不排學生）", variable=self._paid
                        ).grid(row=4, column=0, columnspan=2, **pad)
        bar = ttk.Frame(self)
        bar.grid(row=5, column=0, columnspan=2, pady=8)
        ttk.Button(bar, text="確定", command=self._ok).pack(side="left", padx=6)
        ttk.Button(bar, text="取消", command=self.destroy).pack(side="left", padx=6)
        self._room.focus_set()
        self.grab_set()
        self.wait_window(self)

    def _ok(self):
        room = self._room.get().strip()
        if not room:
            messagebox.showwarning("欄位", "請填診間房號", parent=self)
            return
        self.result = (self._wd.current(), self._sess.get(), room,
                       self._doc.get().strip(), bool(self._paid.get()))
        self.destroy()


class _ClerkBatchDialog(tk.Toplevel):
    """新增/編輯 Clerk 梯次（起始必週一）。回填 self.result 或 None。"""

    def __init__(self, master, initial: dict):
        super().__init__(master)
        self.title("Clerk 梯次")
        self.resizable(False, False)
        self.transient(master)
        self.result = None
        self._initial_id = initial.get("id")     # 編輯時沿用原 id（切片格網掛在 id 上）
        pad = {"padx": 8, "pady": 4}
        ttk.Label(self, text="起始日（週一）YYYY-MM-DD").grid(
            row=0, column=0, sticky="e", **pad)
        self._start = ttk.Entry(self, width=14)
        self._start.insert(0, initial.get("start_monday", ""))
        self._start.grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(self, text="成員代號（頓/逗號分隔）").grid(
            row=1, column=0, sticky="e", **pad)
        self._members = ttk.Entry(self, width=22)
        self._members.insert(0, "、".join(initial.get("members") or []))
        self._members.grid(row=1, column=1, sticky="w", **pad)
        bar = ttk.Frame(self)
        bar.grid(row=2, column=0, columnspan=2, pady=8)
        ttk.Button(bar, text="確定", command=self._ok).pack(side="left", padx=6)
        ttk.Button(bar, text="取消", command=self.destroy).pack(side="left", padx=6)
        self.grab_set()
        self.wait_window(self)

    def _ok(self):
        raw = self._start.get().strip()
        try:
            d = date.fromisoformat(raw)
        except ValueError:
            messagebox.showwarning("日期", "請輸入 YYYY-MM-DD", parent=self)
            return
        if d.weekday() != 0:
            messagebox.showwarning("起始日", "梯次起始必為週一", parent=self)
            return
        members = [c.strip() for c in self._members.get().replace("，", ",")
                   .replace("、", ",").split(",") if c.strip()]
        # 穩定唯一 id（不綁 start_monday）→ 同週一可多梯不撞、改起始日不斷開切片格網
        bid = self._initial_id or ("b" + datetime.now().strftime("%Y%m%d%H%M%S%f"))
        self.result = {"id": bid, "start_monday": raw, "members": members}
        self.destroy()


class _BiopsyGridDialog(tk.Toplevel):
    """某梯次 14 天的切片室開放格網（週三下午恆關、週末不排）。存即生效。"""

    def __init__(self, master, service, batch: dict):
        super().__init__(master)
        self.service = service
        self.batch_id = batch.get("id")
        start = date.fromisoformat(batch["start_monday"])
        self.title(f"切片室開放 · 梯次 {self.batch_id}")
        self.resizable(False, False)
        self.transient(master)
        cur = service.storage.load_biopsy_grid().get(self.batch_id) or {}
        self._vars: dict = {}
        ttk.Label(self, text="勾選＝該時段切片室開放（週三下午恆關）",
                  padding=6).grid(row=0, column=0, columnspan=3)
        row = 1
        for i in range(14):
            d = start + timedelta(days=i)
            if d.weekday() >= 5:
                continue
            iso = d.isoformat()
            ttk.Label(self, text=f"{d.month}/{d.day}"
                      f"({'一二三四五六日'[d.weekday()]})").grid(
                row=row, column=0, sticky="w", padx=8, pady=1)
            sess_cur = cur.get(iso) or {}
            for c, session in enumerate(("上午", "下午"), start=1):
                wed_pm = (d.weekday() == 2 and session == "下午")
                v = tk.BooleanVar(value=bool(sess_cur.get(session)) and not wed_pm)
                ttk.Checkbutton(self, text=session, variable=v,
                                state="disabled" if wed_pm else "normal").grid(
                    row=row, column=c, padx=4)
                if not wed_pm:
                    self._vars[(iso, session)] = v
            row += 1
        bar = ttk.Frame(self)
        bar.grid(row=row, column=0, columnspan=3, pady=8)
        ttk.Button(bar, text="儲存", command=self._save).pack(side="left", padx=6)
        ttk.Button(bar, text="取消", command=self.destroy).pack(side="left", padx=6)
        self.grab_set()
        self.wait_window(self)

    def _save(self):
        grid_all = self.service.storage.load_biopsy_grid()
        newg: dict = {}
        for (iso, session), v in self._vars.items():
            if v.get():
                newg.setdefault(iso, {})[session] = True
        grid_all[self.batch_id] = newg
        self.service.storage.save_biopsy_grid(grid_all)
        self.destroy()
