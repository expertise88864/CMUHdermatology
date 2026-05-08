# -*- coding: utf-8 -*-
"""[熱鍵步驟全圖形化編輯器]

GUI 編輯熱鍵腳本：新增/刪除/上下移/編輯步驟，存到 settings/hotkey_overrides.json。
不修改 main.py — 程式啟動時若 overrides 存在，主程式可選擇套用。

⚠️ 注意：此編輯器目前只**編輯與儲存** override JSON，主程式套用 override 的
邏輯需要 main.py 配合（後續迭代）。本階段先讓使用者能完整視覺化編輯。
"""
from __future__ import annotations

import json
import logging
import os
import tkinter as tk
from copy import deepcopy
from tkinter import messagebox, ttk
from typing import Optional

from cmuh_common.hotkey_viewer import (
    ClickAction,
    HotkeyScript,
    HotkeyStep,
    MatchCondition,
    parse_all_hotkeys,
)
from cmuh_common.paths import get_conf_path

OVERRIDE_FILE = "hotkey_overrides.json"


def _override_path() -> str:
    """本機私有 override（settings/，gitignored）。"""
    return get_conf_path(OVERRIDE_FILE)


def _shared_override_path() -> str:
    """共用 override（repo root，會被 git 追蹤、推到 GitHub）。"""
    from cmuh_common.paths import get_app_dir
    return os.path.join(get_app_dir(), OVERRIDE_FILE)


def load_overrides() -> dict:
    """優先讀本機 settings/ 版本，沒有再讀 repo root 共用版本。"""
    for p in (_override_path(), _shared_override_path()):
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                logging.warning("讀取 %s 失敗", p, exc_info=True)
    return {}


def save_overrides(data: dict) -> bool:
    """儲存到本機（settings/）。"""
    try:
        from cmuh_common.atomic_io import atomic_write_json
        atomic_write_json(_override_path(), data)
        return True
    except Exception:
        logging.error("儲存 hotkey_overrides.json 失敗", exc_info=True)
        return False


def push_to_shared(data: dict) -> bool:
    """[O34] 把 override 寫到 repo root（待 push.bat 推到 GitHub 後其他電腦會收到）。"""
    try:
        from cmuh_common.atomic_io import atomic_write_json
        atomic_write_json(_shared_override_path(), data)
        return True
    except Exception:
        logging.error("寫入共用 override 失敗", exc_info=True)
        return False


def _script_to_dict(script: HotkeyScript) -> dict:
    """把 HotkeyScript 轉成可序列化 dict（給 override 用）。"""
    if script.is_sequential:
        return {"mode": "sequential", "actions": [
            {"type": _action_type_name(a), **_action_to_dict(a)}
            for a in script.sequential_actions
        ]}
    out = {"mode": "loop"}
    if script.init_action:
        out["init_action"] = {"type": "click", **_action_to_dict(script.init_action)}
    out["steps"] = []
    for s in script.steps:
        out["steps"].append({
            "name": s.name,
            "matches": [
                {"x": m.x, "y": m.y, "rgb": [m.r, m.g, m.b], "tolerance": m.tolerance}
                for m in s.matches
            ],
            "actions": [
                {"type": _action_type_name(a), **_action_to_dict(a)}
                for a in s.actions
            ],
        })
    return out


def _action_type_name(a) -> str:
    return {
        "ClickAction": "click",
        "TypeAction": "type",
        "WaitColorAction": "wait_color",
        "CheckColorAction": "check_color",
        "SleepAction": "sleep",
    }.get(type(a).__name__, type(a).__name__)


def _action_to_dict(a) -> dict:
    if isinstance(a, ClickAction):
        return {"x": a.x, "y": a.y, "delay": a.delay}
    name = type(a).__name__
    if name == "TypeAction":
        return {"text": getattr(a, "text", "")}
    if name == "WaitColorAction":
        return {"x": getattr(a, "x", 0), "y": getattr(a, "y", 0)}
    if name == "CheckColorAction":
        return {"x": a.x, "y": a.y}
    if name == "SleepAction":
        return {"seconds": a.seconds}
    return {}


# =============================================================================
# 編輯器主視窗
# =============================================================================
class HotkeyEditorWindow(tk.Toplevel):
    """熱鍵步驟全圖形化編輯器。"""

    RESOLUTIONS = ("1280x1024", "1920x1080", "1024x768")
    HOTKEYS = ("F3", "F4", "F9", "F10", "F11")

    def __init__(self, parent: tk.Misc):
        super().__init__(parent)
        self.title("熱鍵步驟編輯器（新增 / 刪除 / 編輯）")
        self.geometry("980x700")
        self.minsize(900, 600)

        self._original_scripts = parse_all_hotkeys()
        self._overrides = load_overrides()
        self._current_data: Optional[dict] = None  # 當前載入到編輯區的 dict
        self._dirty = False  # 有未儲存變更

        self._build_ui()
        self._refresh()

    # ------ UI 構建 ------
    def _build_ui(self) -> None:
        top = ttk.Frame(self, padding=8)
        top.pack(fill=tk.X)

        ttk.Label(top, text="解析度:").pack(side=tk.LEFT)
        self.res_var = tk.StringVar(value="1280x1024")
        ttk.Combobox(top, textvariable=self.res_var, values=self.RESOLUTIONS,
                     state="readonly", width=12).pack(side=tk.LEFT, padx=4)

        ttk.Label(top, text="  熱鍵:").pack(side=tk.LEFT, padx=(8, 0))
        self.hk_var = tk.StringVar(value="F11")
        for hk in self.HOTKEYS:
            ttk.Radiobutton(top, text=hk, variable=self.hk_var, value=hk,
                            command=self._refresh).pack(side=tk.LEFT, padx=2)

        self.res_var.trace_add("write", lambda *a: self._refresh())

        ttk.Button(top, text="還原為原始（清除 override）",
                   command=self._revert_current).pack(side=tk.RIGHT, padx=4)
        # [O34] 推送共用：把當前 override 推到 GitHub，其他電腦自動同步
        ttk.Button(top, text="📤 推送共用 (GitHub)",
                   command=self._push_shared).pack(side=tk.RIGHT, padx=4)
        ttk.Button(top, text="儲存全部",
                   command=self._save_all,
                   style="Primary.TButton" if "Primary.TButton" in self.tk.call("ttk::style", "element", "names")
                   else "TButton").pack(side=tk.RIGHT)

        body = ttk.Panedwindow(self, orient="horizontal")
        body.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        # 左：步驟列表
        left = ttk.LabelFrame(body, text="步驟列表", padding=4)
        body.add(left, weight=1)

        self.steps_tree = ttk.Treeview(left, columns=("name",), show="headings",
                                       selectmode="browse")
        self.steps_tree.heading("name", text="步驟")
        self.steps_tree.column("name", width=240, anchor="w")
        self.steps_tree.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        sb = ttk.Scrollbar(left, orient="vertical", command=self.steps_tree.yview)
        sb.pack(side=tk.LEFT, fill=tk.Y)
        self.steps_tree.configure(yscrollcommand=sb.set)
        self.steps_tree.bind("<<TreeviewSelect>>", lambda *_: self._on_step_selected())

        btn_row = ttk.Frame(left)
        btn_row.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(btn_row, text="↑", width=3, command=self._move_up).pack(side=tk.LEFT)
        ttk.Button(btn_row, text="↓", width=3, command=self._move_down).pack(side=tk.LEFT)
        ttk.Button(btn_row, text="＋ 新增", command=self._add_step).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_row, text="複製", command=self._dup_step).pack(side=tk.LEFT)
        ttk.Button(btn_row, text="刪除", command=self._delete_step).pack(side=tk.LEFT, padx=4)

        # 右：編輯區
        right = ttk.LabelFrame(body, text="步驟詳細（編輯 / 新增條件、動作）", padding=8)
        body.add(right, weight=2)

        self._build_step_editor(right)

        # 底部說明
        bottom = ttk.Frame(self, padding=(8, 0, 8, 8))
        bottom.pack(fill=tk.X)
        ttk.Label(
            bottom,
            text="提示：編輯後別忘了「儲存全部」。儲存後重啟主程式才生效。",
            foreground="#888"
        ).pack(side=tk.LEFT)
        ttk.Button(bottom, text="關閉", command=self._on_close).pack(side=tk.RIGHT)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_step_editor(self, parent: ttk.Frame) -> None:
        # 步驟名稱
        name_row = ttk.Frame(parent)
        name_row.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(name_row, text="步驟名稱:").pack(side=tk.LEFT, padx=(0, 4))
        self.name_var = tk.StringVar()
        ttk.Entry(name_row, textvariable=self.name_var, width=40).pack(
            side=tk.LEFT, fill=tk.X, expand=True)
        self.name_var.trace_add("write", lambda *a: self._on_name_changed())

        # 條件區
        cond_lf = ttk.LabelFrame(parent, text="偵測條件（全部符合才執行）", padding=4)
        cond_lf.pack(fill=tk.BOTH, expand=False, pady=(0, 6))
        self.cond_tree = ttk.Treeview(
            cond_lf, columns=("x", "y", "rgb", "tol"), show="headings", height=4)
        for col, text, w in [("x", "X", 60), ("y", "Y", 60),
                             ("rgb", "RGB / Hex", 180), ("tol", "容差", 60)]:
            self.cond_tree.heading(col, text=text)
            self.cond_tree.column(col, width=w, anchor="center")
        self.cond_tree.pack(fill=tk.X)
        cond_btn = ttk.Frame(cond_lf)
        cond_btn.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(cond_btn, text="＋ 新增條件",
                   command=self._add_condition).pack(side=tk.LEFT)
        ttk.Button(cond_btn, text="編輯選取",
                   command=self._edit_condition).pack(side=tk.LEFT, padx=4)
        ttk.Button(cond_btn, text="刪除選取",
                   command=self._delete_condition).pack(side=tk.LEFT)

        # 動作區
        act_lf = ttk.LabelFrame(parent, text="執行動作（依序）", padding=4)
        act_lf.pack(fill=tk.BOTH, expand=True)
        self.act_tree = ttk.Treeview(
            act_lf, columns=("type", "params"), show="headings", height=4)
        self.act_tree.heading("type", text="類型")
        self.act_tree.heading("params", text="參數")
        self.act_tree.column("type", width=120, anchor="w")
        self.act_tree.column("params", width=300, anchor="w")
        self.act_tree.pack(fill=tk.BOTH, expand=True)
        act_btn = ttk.Frame(act_lf)
        act_btn.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(act_btn, text="＋ 新增動作",
                   command=self._add_action).pack(side=tk.LEFT)
        ttk.Button(act_btn, text="編輯選取",
                   command=self._edit_action).pack(side=tk.LEFT, padx=4)
        ttk.Button(act_btn, text="刪除選取",
                   command=self._delete_action).pack(side=tk.LEFT)

    # ------ 資料載入 ------
    def _refresh(self) -> None:
        res = self.res_var.get()
        hk = self.hk_var.get()
        # 優先讀 override，沒有就從原始解析
        ov = self._overrides.get(res, {}).get(hk)
        if ov:
            self._current_data = deepcopy(ov)
        else:
            script = self._original_scripts.get(res, {}).get(hk)
            if script is None:
                self._current_data = {"mode": "loop", "steps": []}
            else:
                self._current_data = _script_to_dict(script)
        self._populate_steps()

    def _populate_steps(self) -> None:
        for iid in self.steps_tree.get_children():
            self.steps_tree.delete(iid)
        if not self._current_data:
            return
        if self._current_data.get("mode") == "sequential":
            actions = self._current_data.get("actions", [])
            for i, a in enumerate(actions, 1):
                self.steps_tree.insert("", "end", iid=str(i),
                                       values=(f"第 {i} 步: {self._action_human(a)}",))
        else:
            steps = self._current_data.get("steps", [])
            for i, s in enumerate(steps, 1):
                self.steps_tree.insert("", "end", iid=str(i),
                                       values=(f"第 {i} 步: {s.get('name', '')}",))
        self._clear_editor()

    def _action_human(self, a: dict) -> str:
        t = a.get("type", "?")
        if t == "click":
            return f"點擊 ({a.get('x', 0)}, {a.get('y', 0)})"
        if t == "wait_color":
            return f"等候顏色 ({a.get('x', 0)}, {a.get('y', 0)})"
        if t == "check_color":
            return f"檢查顏色 ({a.get('x', 0)}, {a.get('y', 0)})"
        if t == "sleep":
            return f"等候 {a.get('seconds', 0)}s"
        if t == "type":
            return f"輸入「{a.get('text', '')}」"
        return t

    # ------ 步驟編輯區 ------
    def _selected_step_idx(self) -> Optional[int]:
        sel = self.steps_tree.selection()
        if not sel:
            return None
        try:
            return int(sel[0]) - 1
        except ValueError:
            return None

    def _on_step_selected(self) -> None:
        idx = self._selected_step_idx()
        if idx is None or self._current_data is None:
            self._clear_editor()
            return
        if self._current_data.get("mode") == "sequential":
            # 順序腳本沒有 step name + matches；只顯示動作
            self.name_var.set("（順序動作）")
            self._populate_conditions([])
            actions = self._current_data.get("actions", [])
            if 0 <= idx < len(actions):
                self._populate_actions([actions[idx]])
            return
        steps = self._current_data.get("steps", [])
        if idx < 0 or idx >= len(steps):
            return
        step = steps[idx]
        self.name_var.set(step.get("name", ""))
        self._populate_conditions(step.get("matches", []))
        self._populate_actions(step.get("actions", []))

    def _populate_conditions(self, conds: list) -> None:
        for iid in self.cond_tree.get_children():
            self.cond_tree.delete(iid)
        # 為了顯示色塊，動態加入 tag 並 configure
        for i, c in enumerate(conds):
            rgb = c.get("rgb", [0, 0, 0])
            hex_c = f"#{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}"
            tag_name = f"swatch_{i}"
            try:
                # [O30] 用 row tag 染整列背景近似的方式不可行（ttk 限制）；
                # 改用「□ 」前綴 + RGB 文字。完整色塊預覽留在編輯對話框。
                self.cond_tree.insert("", "end", iid=str(i),
                                      values=(c.get("x", 0), c.get("y", 0),
                                              f"■ {hex_c}  ({rgb[0]},{rgb[1]},{rgb[2]})",
                                              c.get("tolerance", 10)),
                                      tags=(tag_name,))
                # 設定 tag 前景色為該 RGB → 讓「■」顯示為色塊
                self.cond_tree.tag_configure(tag_name, foreground=hex_c)
            except Exception:
                self.cond_tree.insert("", "end", iid=str(i),
                                      values=(c.get("x", 0), c.get("y", 0),
                                              f"({rgb[0]},{rgb[1]},{rgb[2]}) {hex_c}",
                                              c.get("tolerance", 10)))

    def _populate_actions(self, acts: list) -> None:
        for iid in self.act_tree.get_children():
            self.act_tree.delete(iid)
        for i, a in enumerate(acts):
            self.act_tree.insert("", "end", iid=str(i),
                                 values=(a.get("type", ""), self._action_human(a)))

    def _clear_editor(self) -> None:
        self.name_var.set("")
        self._populate_conditions([])
        self._populate_actions([])

    def _on_name_changed(self) -> None:
        idx = self._selected_step_idx()
        if idx is None or self._current_data is None:
            return
        if self._current_data.get("mode") != "loop":
            return
        steps = self._current_data.get("steps", [])
        if 0 <= idx < len(steps):
            new_name = self.name_var.get()
            if steps[idx].get("name") != new_name:
                steps[idx]["name"] = new_name
                self._dirty = True
                # 更新左側列表顯示
                self.steps_tree.item(str(idx + 1),
                                     values=(f"第 {idx + 1} 步: {new_name}",))

    # ------ 步驟操作（新增/刪除/移動/複製） ------
    def _add_step(self) -> None:
        if self._current_data is None:
            return
        if self._current_data.get("mode") == "sequential":
            actions = self._current_data.setdefault("actions", [])
            actions.append({"type": "click", "x": 0, "y": 0, "delay": 0.0})
        else:
            steps = self._current_data.setdefault("steps", [])
            steps.append({"name": "新步驟", "matches": [], "actions": []})
        self._dirty = True
        self._populate_steps()

    def _delete_step(self) -> None:
        idx = self._selected_step_idx()
        if idx is None or self._current_data is None:
            return
        if not messagebox.askyesno("確認", f"刪除第 {idx + 1} 步？", parent=self):
            return
        if self._current_data.get("mode") == "sequential":
            self._current_data.get("actions", []).pop(idx)
        else:
            self._current_data.get("steps", []).pop(idx)
        self._dirty = True
        self._populate_steps()

    def _dup_step(self) -> None:
        idx = self._selected_step_idx()
        if idx is None or self._current_data is None:
            return
        if self._current_data.get("mode") == "sequential":
            arr = self._current_data.get("actions", [])
        else:
            arr = self._current_data.get("steps", [])
        if 0 <= idx < len(arr):
            arr.insert(idx + 1, deepcopy(arr[idx]))
            self._dirty = True
            self._populate_steps()
            self.steps_tree.selection_set(str(idx + 2))

    def _move_up(self) -> None:
        idx = self._selected_step_idx()
        if idx is None or idx <= 0 or self._current_data is None:
            return
        arr = (self._current_data.get("actions") if self._current_data.get("mode") == "sequential"
               else self._current_data.get("steps"))
        if arr:
            arr[idx - 1], arr[idx] = arr[idx], arr[idx - 1]
            self._dirty = True
            self._populate_steps()
            self.steps_tree.selection_set(str(idx))

    def _move_down(self) -> None:
        idx = self._selected_step_idx()
        if idx is None or self._current_data is None:
            return
        arr = (self._current_data.get("actions") if self._current_data.get("mode") == "sequential"
               else self._current_data.get("steps"))
        if arr and idx < len(arr) - 1:
            arr[idx], arr[idx + 1] = arr[idx + 1], arr[idx]
            self._dirty = True
            self._populate_steps()
            self.steps_tree.selection_set(str(idx + 2))

    # ------ 條件編輯 ------
    def _add_condition(self) -> None:
        idx = self._selected_step_idx()
        if idx is None or self._current_data is None:
            return
        if self._current_data.get("mode") == "sequential":
            messagebox.showinfo("提示", "順序動作腳本沒有條件欄位。", parent=self)
            return
        c = self._prompt_condition({"x": 0, "y": 0, "rgb": [255, 255, 255], "tolerance": 10})
        if c:
            self._current_data["steps"][idx].setdefault("matches", []).append(c)
            self._dirty = True
            self._on_step_selected()

    def _edit_condition(self) -> None:
        step_idx = self._selected_step_idx()
        if step_idx is None or self._current_data is None:
            return
        sel = self.cond_tree.selection()
        if not sel:
            return
        cond_idx = int(sel[0])
        try:
            conds = self._current_data["steps"][step_idx]["matches"]
        except (KeyError, IndexError):
            return
        if 0 <= cond_idx < len(conds):
            new = self._prompt_condition(conds[cond_idx])
            if new:
                conds[cond_idx] = new
                self._dirty = True
                self._on_step_selected()

    def _delete_condition(self) -> None:
        step_idx = self._selected_step_idx()
        if step_idx is None or self._current_data is None:
            return
        sel = self.cond_tree.selection()
        if not sel:
            return
        cond_idx = int(sel[0])
        try:
            conds = self._current_data["steps"][step_idx]["matches"]
        except (KeyError, IndexError):
            return
        if 0 <= cond_idx < len(conds):
            conds.pop(cond_idx)
            self._dirty = True
            self._on_step_selected()

    def _prompt_condition(self, defaults: dict) -> Optional[dict]:
        dlg = tk.Toplevel(self)
        dlg.title("編輯條件")
        dlg.geometry("420x300")
        dlg.transient(self)
        dlg.grab_set()
        result = {}

        def add_row(row, label, default):
            ttk.Label(dlg, text=label).grid(row=row, column=0, padx=6, pady=4, sticky="e")
            v = tk.StringVar(value=str(default))
            e = ttk.Entry(dlg, textvariable=v, width=14)
            e.grid(row=row, column=1, padx=6, pady=4, sticky="w")
            return v

        rgb = defaults.get("rgb", [255, 255, 255])
        x_var = add_row(0, "X 座標:", defaults.get("x", 0))
        y_var = add_row(1, "Y 座標:", defaults.get("y", 0))
        r_var = add_row(2, "R (0-255):", rgb[0])
        g_var = add_row(3, "G (0-255):", rgb[1])
        b_var = add_row(4, "B (0-255):", rgb[2])
        tol_var = add_row(5, "容差 (推薦 10):", defaults.get("tolerance", 10))

        # [O30] 色塊預覽（即時跟著 RGB 欄位變動）
        swatch = tk.Label(dlg, text="  ", bg="#FFFFFF", relief="ridge", bd=2,
                          width=10, height=4)
        swatch.grid(row=2, column=2, rowspan=3, padx=10, pady=4, sticky="ns")

        def update_swatch(*_a):
            try:
                r = int(r_var.get())
                g = int(g_var.get())
                b = int(b_var.get())
                r = max(0, min(255, r))
                g = max(0, min(255, g))
                b = max(0, min(255, b))
                swatch.configure(bg=f"#{r:02X}{g:02X}{b:02X}")
            except (ValueError, tk.TclError):
                pass

        for v in (r_var, g_var, b_var):
            v.trace_add("write", update_swatch)
        update_swatch()

        # [O29] 視覺取座標按鈕
        def pick_visually():
            from cmuh_common.pixel_picker import pick_pixel_with_accurate_color
            def on_picked(x, y, r, g, b):
                x_var.set(str(x))
                y_var.set(str(y))
                r_var.set(str(r))
                g_var.set(str(g))
                b_var.set(str(b))
            pick_pixel_with_accurate_color(dlg, on_picked)

        ttk.Button(dlg, text="🎯 視覺取座標 / 顏色", command=pick_visually).grid(
            row=6, column=0, columnspan=3, pady=8, padx=6, sticky="ew")

        def ok():
            try:
                result.update({
                    "x": int(x_var.get()),
                    "y": int(y_var.get()),
                    "rgb": [int(r_var.get()), int(g_var.get()), int(b_var.get())],
                    "tolerance": int(tol_var.get()),
                })
                dlg.destroy()
            except ValueError:
                messagebox.showerror("格式錯誤", "X/Y/RGB/容差 須為整數", parent=dlg)

        btn_row = ttk.Frame(dlg)
        btn_row.grid(row=7, column=0, columnspan=3, pady=8)
        ttk.Button(btn_row, text="確定", command=ok).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_row, text="取消", command=dlg.destroy).pack(side=tk.LEFT)

        self.wait_window(dlg)
        return result if result else None

    # ------ 動作編輯 ------
    def _add_action(self) -> None:
        idx = self._selected_step_idx()
        if idx is None or self._current_data is None:
            return
        new = self._prompt_action({"type": "click", "x": 0, "y": 0, "delay": 0.0})
        if new is None:
            return
        if self._current_data.get("mode") == "sequential":
            self._current_data["actions"].insert(idx + 1, new)
        else:
            self._current_data["steps"][idx].setdefault("actions", []).append(new)
        self._dirty = True
        self._populate_steps()
        self.steps_tree.selection_set(str(idx + 1))

    def _edit_action(self) -> None:
        step_idx = self._selected_step_idx()
        if step_idx is None or self._current_data is None:
            return
        sel = self.act_tree.selection()
        if not sel:
            return
        a_idx = int(sel[0])
        if self._current_data.get("mode") == "sequential":
            arr = self._current_data.get("actions", [])
            if 0 <= step_idx < len(arr):
                # 順序腳本：編輯目前選中的那一步
                new = self._prompt_action(arr[step_idx])
                if new:
                    arr[step_idx] = new
                    self._dirty = True
                    self._populate_steps()
                    self.steps_tree.selection_set(str(step_idx + 1))
            return
        try:
            acts = self._current_data["steps"][step_idx]["actions"]
        except (KeyError, IndexError):
            return
        if 0 <= a_idx < len(acts):
            new = self._prompt_action(acts[a_idx])
            if new:
                acts[a_idx] = new
                self._dirty = True
                self._on_step_selected()

    def _delete_action(self) -> None:
        step_idx = self._selected_step_idx()
        if step_idx is None or self._current_data is None:
            return
        sel = self.act_tree.selection()
        if not sel:
            return
        if self._current_data.get("mode") == "sequential":
            return  # 順序腳本只能透過刪除步驟
        a_idx = int(sel[0])
        try:
            acts = self._current_data["steps"][step_idx]["actions"]
        except (KeyError, IndexError):
            return
        if 0 <= a_idx < len(acts):
            acts.pop(a_idx)
            self._dirty = True
            self._on_step_selected()

    def _prompt_action(self, defaults: dict) -> Optional[dict]:
        dlg = tk.Toplevel(self)
        dlg.title("編輯動作")
        dlg.geometry("400x260")
        dlg.transient(self)
        dlg.grab_set()
        result = {}

        ttk.Label(dlg, text="類型:").grid(row=0, column=0, padx=6, pady=4, sticky="e")
        type_var = tk.StringVar(value=defaults.get("type", "click"))
        type_combo = ttk.Combobox(
            dlg, textvariable=type_var,
            values=["click", "wait_color", "check_color", "sleep", "type"],
            state="readonly", width=22,
        )
        type_combo.grid(row=0, column=1, padx=6, pady=4, sticky="ew")

        # 動態欄位
        fields_frame = ttk.Frame(dlg)
        fields_frame.grid(row=1, column=0, columnspan=2, sticky="ew", padx=6)
        dlg.columnconfigure(1, weight=1)
        field_vars: dict[str, tk.StringVar] = {}

        def render_fields(*_):
            for w in fields_frame.winfo_children():
                w.destroy()
            field_vars.clear()
            t = type_var.get()
            row = 0
            def add(label, key, default):
                nonlocal row
                ttk.Label(fields_frame, text=label).grid(row=row, column=0, padx=2, pady=4, sticky="e")
                v = tk.StringVar(value=str(default))
                ttk.Entry(fields_frame, textvariable=v, width=18).grid(
                    row=row, column=1, padx=2, pady=4, sticky="w")
                fields_frame.columnconfigure(1, weight=1)
                field_vars[key] = v
                row += 1
            if t in ("click", "wait_color", "check_color"):
                add("X 座標:", "x", defaults.get("x", 0))
                add("Y 座標:", "y", defaults.get("y", 0))
                if t == "click":
                    add("延遲 (秒):", "delay", defaults.get("delay", 0.0))
                elif t == "wait_color":
                    # [O33] wait_color 完整：加目標 RGB + 容差 + timeout
                    target_rgb = defaults.get("target_rgb", [255, 255, 0])
                    add("目標 R:", "tr", target_rgb[0] if len(target_rgb) > 0 else 255)
                    add("目標 G:", "tg", target_rgb[1] if len(target_rgb) > 1 else 255)
                    add("目標 B:", "tb", target_rgb[2] if len(target_rgb) > 2 else 0)
                    add("容差:", "tolerance", defaults.get("tolerance", 5))
                    add("逾時 (秒):", "timeout", defaults.get("timeout", 15))
                    # 色塊預覽
                    pv = tk.Label(fields_frame, text="  ", bg="#FFFFFF",
                                  relief="ridge", bd=2, width=8, height=2)
                    pv.grid(row=2, column=2, rowspan=3, padx=8, sticky="ns")
                    def upd_pv(*_a):
                        try:
                            r = max(0, min(255, int(field_vars["tr"].get())))
                            g = max(0, min(255, int(field_vars["tg"].get())))
                            b = max(0, min(255, int(field_vars["tb"].get())))
                            pv.configure(bg=f"#{r:02X}{g:02X}{b:02X}")
                        except (ValueError, tk.TclError):
                            pass
                    for k in ("tr", "tg", "tb"):
                        field_vars[k].trace_add("write", upd_pv)
                    upd_pv()
                # [O29] 視覺取座標按鈕（適用於 click / wait_color / check_color）
                def pick_visually():
                    from cmuh_common.pixel_picker import pick_pixel_with_accurate_color
                    def on_picked(x, y, r, g, b):
                        if "x" in field_vars: field_vars["x"].set(str(x))
                        if "y" in field_vars: field_vars["y"].set(str(y))
                        # 對 wait_color 也順便填目標 RGB
                        if "tr" in field_vars: field_vars["tr"].set(str(r))
                        if "tg" in field_vars: field_vars["tg"].set(str(g))
                        if "tb" in field_vars: field_vars["tb"].set(str(b))
                    pick_pixel_with_accurate_color(dlg, on_picked)
                ttk.Button(fields_frame, text="🎯 視覺取座標",
                           command=pick_visually).grid(
                    row=row, column=0, columnspan=2, pady=6, padx=2, sticky="ew")
                row += 1
            elif t == "sleep":
                add("秒數:", "seconds", defaults.get("seconds", 0.5))
            elif t == "type":
                add("輸入文字:", "text", defaults.get("text", ""))

        type_combo.bind("<<ComboboxSelected>>", render_fields)
        render_fields()

        def ok():
            try:
                t = type_var.get()
                d: dict = {"type": t}
                if t in ("click", "wait_color", "check_color"):
                    d["x"] = int(field_vars["x"].get())
                    d["y"] = int(field_vars["y"].get())
                    if t == "click":
                        d["delay"] = float(field_vars["delay"].get())
                    elif t == "wait_color":
                        # [O33] 完整 wait_color 欄位
                        d["target_rgb"] = [
                            int(field_vars["tr"].get()),
                            int(field_vars["tg"].get()),
                            int(field_vars["tb"].get()),
                        ]
                        d["tolerance"] = int(field_vars["tolerance"].get())
                        d["timeout"] = float(field_vars["timeout"].get())
                elif t == "sleep":
                    d["seconds"] = float(field_vars["seconds"].get())
                elif t == "type":
                    d["text"] = field_vars["text"].get()
                result.update(d)
                dlg.destroy()
            except ValueError:
                messagebox.showerror("格式錯誤", "請填寫正確的數值", parent=dlg)

        btn_row = ttk.Frame(dlg)
        btn_row.grid(row=2, column=0, columnspan=2, pady=10)
        ttk.Button(btn_row, text="確定", command=ok).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_row, text="取消", command=dlg.destroy).pack(side=tk.LEFT)

        self.wait_window(dlg)
        return result if result else None

    # ------ 整體儲存 ------
    def _save_all(self) -> None:
        # 把 _current_data 寫回 _overrides（只覆寫當前 res/hk 的）
        if self._current_data is None:
            return
        res = self.res_var.get()
        hk = self.hk_var.get()
        self._overrides.setdefault(res, {})[hk] = deepcopy(self._current_data)
        if save_overrides(self._overrides):
            messagebox.showinfo(
                "已儲存並立即生效",
                f"override 已存到 settings/{OVERRIDE_FILE}\n\n"
                "✅ 主程式會於下次按 F3/F4/F9/F10/F11 時自動讀取最新 override（mtime cache）。\n"
                "→ 不需重啟，直接按熱鍵即可看到效果。\n\n"
                "若要還原為原始 main.py 版本，按「還原為原始」即可。",
                parent=self,
            )
            self._dirty = False
        else:
            messagebox.showerror("儲存失敗", "無法寫入 override 檔。", parent=self)

    def _push_shared(self) -> None:
        """[O34] 把目前所有 override 推到 GitHub 共用區。"""
        if self._current_data is None:
            messagebox.showinfo("提示", "目前沒有可推送的 override", parent=self)
            return
        # 確保本機已存
        res = self.res_var.get()
        hk = self.hk_var.get()
        self._overrides.setdefault(res, {})[hk] = deepcopy(self._current_data)
        save_overrides(self._overrides)

        if not messagebox.askyesno(
            "推送共用 override",
            "把目前所有 override 推送到 GitHub，所有使用本程式的電腦都會收到（自動更新時）。\n\n"
            "確定推送？",
            parent=self,
        ):
            return

        # 寫到 repo root 並呼叫 push.bat
        if not push_to_shared(self._overrides):
            messagebox.showerror("失敗", "寫入 repo root 失敗。", parent=self)
            return

        # 嘗試呼叫 push.bat（背景）
        from cmuh_common.paths import get_app_dir
        push_bat = os.path.join(get_app_dir(), "push.bat")
        if not os.path.isfile(push_bat):
            messagebox.showinfo(
                "已寫入 repo root",
                "已將 override 寫入 repo 根目錄的 hotkey_overrides.json。\n\n"
                "找不到 push.bat，請手動執行 push.bat 推到 GitHub。",
                parent=self,
            )
            return

        import subprocess
        try:
            subprocess.Popen([push_bat, f'sync hotkey_overrides ({res}/{hk})'],
                             cwd=get_app_dir(), shell=True)
            messagebox.showinfo(
                "已啟動推送",
                "已啟動 push.bat 在背景推送到 GitHub。\n\n"
                "其他電腦下次啟動會自動收到更新（manifest cache ~5 分鐘）。",
                parent=self,
            )
        except Exception as e:
            logging.error("啟動 push.bat 失敗", exc_info=True)
            messagebox.showerror(
                "啟動推送失敗",
                f"無法執行 push.bat:\n{e}\n\n請手動執行 push.bat。",
                parent=self,
            )

    def _revert_current(self) -> None:
        res = self.res_var.get()
        hk = self.hk_var.get()
        if res in self._overrides and hk in self._overrides[res]:
            if not messagebox.askyesno(
                "還原確認",
                f"清除 {res} {hk} 的 override，回到 main.py 的原始版本？",
                parent=self,
            ):
                return
            del self._overrides[res][hk]
            if not self._overrides[res]:
                del self._overrides[res]
            save_overrides(self._overrides)
        self._refresh()

    def _on_close(self) -> None:
        if self._dirty:
            if not messagebox.askyesno(
                "未儲存",
                "有變更尚未儲存，確定關閉？",
                parent=self,
            ):
                return
        self.destroy()
