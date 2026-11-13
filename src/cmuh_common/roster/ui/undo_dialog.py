"""[2026-11-13 使用者] 復原排班視窗:選一個還原點 → 先看會改掉什麼 → 確認才復原。

日排班與 R/VS 值班共用(差別只在 service 的範圍鍵 day / r / vs)。
"""
import tkinter as tk
from tkinter import messagebox, ttk

from .common import guard_write


class UndoDialog(tk.Toplevel):
    """`restored` = 這次有沒有真的復原(呼叫端據此重新整理畫面)。"""

    def __init__(self, parent, service, key: str, ym: str, *, title: str):
        super().__init__(parent)
        self.title(f"復原排班 · {title} · {ym}")
        self.transient(parent)
        self.service, self.key, self.ym = service, key, ym
        self.restored = False
        self._token = None
        self._entries = service.undo_list(key, ym)
        ttk.Label(
            self, padding=(10, 8), wraplength=620, justify="left",
            text=("每次自動排班、清除未鎖定或復原之前，系統會自動保留當時的班表"
                  "（最近 5 個）。\n選一個還原點可先看「復原後會改掉什麼」；"
                  "只還原班表，請假與名單不變，目前鎖定的格子也不會被改動。"
                  "手動改過的格子若會被蓋掉，會列在變動清單裡。")).pack(fill="x")
        body = ttk.Frame(self, padding=(10, 0))
        body.pack(fill="both", expand=True)
        self._list = tk.Listbox(body, width=34, height=8, exportselection=False)
        self._list.pack(side="left", fill="y")
        for e in self._entries:
            self._list.insert("end", f"{e['ts']}　「{e['action']}」之前")
        self._text = tk.Text(body, width=52, height=18, wrap="none", state="disabled")
        self._text.pack(side="left", fill="both", expand=True, padx=(8, 0))
        bar = ttk.Frame(self, padding=(10, 8))
        bar.pack(fill="x")
        ttk.Button(bar, text="關閉", command=self.destroy).pack(side="right")
        self._go = ttk.Button(bar, text="復原到這個版本", command=self._restore,
                              state="disabled")
        self._go.pack(side="right", padx=6)
        self._list.bind("<<ListboxSelect>>", lambda _e: self._show())
        if self._entries:
            self._list.selection_set(0)
            self._show()
        else:
            self._set_text("目前沒有還原點。\n（排班或清除過一次之後才會有。）")
        self.grab_set()

    def _selected(self):
        sel = self._list.curselection()
        return self._entries[sel[0]] if sel else None

    def _set_text(self, text: str) -> None:
        self._text.config(state="normal")
        self._text.delete("1.0", "end")
        self._text.insert("1.0", text)
        self._text.config(state="disabled")

    def _show(self) -> None:
        entry = self._selected()
        self._go.config(state="disabled")
        if entry is None:
            return
        try:
            preview = self.service.undo_preview(self.key, self.ym, entry["id"])
        except Exception as exc:  # noqa: BLE001
            self._set_text(f"無法讀取這個還原點：\n{exc}")
            return
        changes, self._token = preview["changes"], preview["token"]
        if not changes:
            self._set_text("這個還原點與目前的班表相同，不需要復原。\n"
                           "（目前鎖定的格子不會被復原改動。）")
            return
        self._set_text(f"復原後的變動（目前 → 復原後）共 {len(changes)} 項\n"
                       + "\n".join(f"  {c}" for c in changes))
        self._go.config(state="normal")

    def _restore(self) -> None:
        entry = self._selected()
        if entry is None:
            return
        if not messagebox.askyesno(
                "復原排班",
                f"把班表復原到 {entry['ts']}「{entry['action']}」之前的版本？\n"
                "目前的班表會保留成還原點，之後可以再復原回來。", parent=self):
            return
        if guard_write(lambda: self.service.undo_restore(
                self.key, self.ym, entry["id"], token=self._token),
                title="復原失敗", parent=self):
            self.restored = True
            self.destroy()
        else:
            self._show()        # 班表可能已變動 → 重新預覽、取得新的識別
