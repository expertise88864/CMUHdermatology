# -*- coding: utf-8 -*-
"""[人性化熱鍵步驟檢視器]

從 main.py 的 script_F<N>_<res>() 函式自動解析步驟，以人類可讀的格式顯示：

  起始動作: 左鍵點擊 (1144, 938) 延遲 0.1s

  第 1 步: 門診病史徵候確認事項
    條件:
      像素 (932, 456) ≈ #FFFFE1 (容差 ±10)
      像素 (934, 571) ≈ #F0F0F0 (容差 ±10)
    動作:
      左鍵點擊 (754, 585) 延遲 0.1s

不修改原始 script 函式，純讀取與顯示，保證對既有行為零影響。
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional

# === 解析用的 regex 模式 ===
# px.match_rgb(932, 456, (255, 255, 225), 10)
_RE_MATCH_RGB = re.compile(
    r"px\.match_rgb\(\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)\s*,\s*(\d+)\s*\)"
)
# click_point_1280(754, 585, after_delay=0.1)
_RE_CLICK = re.compile(
    r"click_point_(\d+)\(\s*(-?\d+)\s*,\s*(-?\d+)\s*(?:,\s*after_delay\s*=\s*([\d.]+))?\s*\)"
)
# logging.info("F11: 步驟描述")
_RE_LOG = re.compile(r'logging\.info\(\s*[fr]?"([^"]*?)"')
# # (註記:xxx) 中文註解
_RE_NOTE = re.compile(r"#\s*\(?註記[:：]?\s*([^)）#]+)")
# type_digits_1280(...) / type_text_1024(...) / wait_for_color_*
_RE_TYPE = re.compile(r"type_(?:digits|text)_\d+\(\s*([^)]+)\)")
_RE_WAIT_COLOR = re.compile(
    r"wait_for_(?:multiple_)?colors?_\d+\("
)


@dataclass
class MatchCondition:
    x: int
    y: int
    r: int
    g: int
    b: int
    tolerance: int

    @property
    def hex_color(self) -> str:
        return f"#{self.r:02X}{self.g:02X}{self.b:02X}"

    def to_human(self) -> str:
        return (f"像素 ({self.x:>4}, {self.y:>4}) ≈ "
                f"{self.hex_color} (RGB {self.r},{self.g},{self.b}) "
                f"容差 ±{self.tolerance}")


@dataclass
class ClickAction:
    x: int
    y: int
    delay: float = 0.0

    def to_human(self) -> str:
        delay_part = f"，延遲 {self.delay}s" if self.delay > 0 else ""
        return f"左鍵點擊 ({self.x:>4}, {self.y:>4}){delay_part}"


@dataclass
class TypeAction:
    text: str

    def to_human(self) -> str:
        return f"鍵盤輸入「{self.text}」"


@dataclass
class WaitColorAction:
    """像素等候（簡化顯示）。"""

    def to_human(self) -> str:
        return "等候像素出現..."


@dataclass
class HotkeyStep:
    name: str
    line_number: int
    matches: list = field(default_factory=list)
    actions: list = field(default_factory=list)


@dataclass
class HotkeyScript:
    hotkey: str               # F3 / F4 / F9 / F10 / F11
    resolution: str           # "1280x1024"
    function_name: str
    line_start: int           # 函式起始行
    line_end: int
    init_action: Optional[ClickAction] = None
    steps: list[HotkeyStep] = field(default_factory=list)


def _read_main_source() -> Optional[str]:
    """讀取 main.py 原始碼。"""
    # main.py 在 src/main.py 或本模組所在 src/ 同層
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.normpath(os.path.join(here, "..", "main.py")),
        os.path.join(here, "main.py"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            try:
                with open(c, "r", encoding="utf-8") as f:
                    return f.read()
            except Exception:
                logging.debug("讀取 main.py 失敗: %s", c, exc_info=True)
                return None
    return None


def _find_function_block(source: str, func_name: str) -> Optional[tuple[int, int, str]]:
    """找出 def func_name(): 函式體（含起訖行號 1-based 與內文）。"""
    pattern = rf"^def {re.escape(func_name)}\([^)]*\):"
    m = re.search(pattern, source, re.MULTILINE)
    if not m:
        return None
    start_pos = m.start()
    # 取下一個 ^def / ^class / 檔尾
    after = source[m.end():]
    next_m = re.search(r"^(def |class |if __name__)", after, re.MULTILINE)
    end_pos = m.end() + next_m.start() if next_m else len(source)
    body = source[start_pos:end_pos]
    line_start = source.count("\n", 0, start_pos) + 1
    line_end = line_start + body.count("\n")
    return line_start, line_end, body


def parse_hotkey_script(source: str, hotkey: str, resolution: str) -> Optional[HotkeyScript]:
    """從 main.py 原始碼解析單一熱鍵腳本。"""
    func_name = f"script_{hotkey}_{resolution}"
    block = _find_function_block(source, func_name)
    if not block:
        return None
    line_start, line_end, body = block

    script = HotkeyScript(
        hotkey=hotkey,
        resolution=resolution,
        function_name=func_name,
        line_start=line_start,
        line_end=line_end,
    )

    lines = body.split("\n")

    # 把整個 body 視為一個流：
    #   - while True: 出現之前的 click_point_<res>(...) → 起始動作
    #   - while True: 之後的 if/elif px.match_rgb 區塊 → step
    in_loop = False
    current_step: Optional[HotkeyStep] = None
    init_done = False

    for idx, raw_line in enumerate(lines):
        line = raw_line.rstrip()
        line_no = line_start + idx

        # 偵測 while 迴圈起始
        if not in_loop and re.match(r"\s*while\s+True\s*:", line):
            in_loop = True
            continue

        # 偵測「if/elif px.match_rgb」開新 step
        if "px.match_rgb" in line and re.match(r"\s*(if|elif)\s+\(?\s*px\.match_rgb", line):
            # finalise previous
            if current_step:
                script.steps.append(current_step)
            # 在此行往前看最近的 # 註記行作為步驟名稱
            step_name = _peek_back_for_note(lines, idx)
            current_step = HotkeyStep(name=step_name, line_number=line_no)
            # 抓本行的所有 match_rgb
            for m in _RE_MATCH_RGB.finditer(line):
                current_step.matches.append(MatchCondition(
                    x=int(m.group(1)), y=int(m.group(2)),
                    r=int(m.group(3)), g=int(m.group(4)), b=int(m.group(5)),
                    tolerance=int(m.group(6)),
                ))
            continue

        # 後續行也可能含 match_rgb（多行 if 條件）
        if current_step is not None and "px.match_rgb" in line and not re.match(
            r"\s*(if|elif|else|click|logging|action_taken|return|continue)", line
        ):
            for m in _RE_MATCH_RGB.finditer(line):
                current_step.matches.append(MatchCondition(
                    x=int(m.group(1)), y=int(m.group(2)),
                    r=int(m.group(3)), g=int(m.group(4)), b=int(m.group(5)),
                    tolerance=int(m.group(6)),
                ))
            continue

        # 抓 click_point
        click_m = _RE_CLICK.search(line)
        if click_m:
            click = ClickAction(
                x=int(click_m.group(2)),
                y=int(click_m.group(3)),
                delay=float(click_m.group(4) or 0.0),
            )
            if not in_loop and not init_done:
                # 迴圈前的 click → 起始動作
                script.init_action = click
                init_done = True
            elif current_step is not None:
                current_step.actions.append(click)
            continue

        # type_*
        type_m = _RE_TYPE.search(line)
        if type_m and current_step is not None:
            current_step.actions.append(TypeAction(text=type_m.group(1).strip()))
            continue

        # wait_for_color
        if _RE_WAIT_COLOR.search(line) and current_step is not None:
            current_step.actions.append(WaitColorAction())
            continue

        # 'return' / 'break' 表示 step 結束 + 整個 script 終止條件
        if current_step and re.match(r"\s*(return|break)\b", line):
            if "Termination" in line or "terminat" in line.lower() or "終止" in line:
                current_step.name = current_step.name or "終止條件達成"
            script.steps.append(current_step)
            current_step = None

    if current_step is not None:
        script.steps.append(current_step)

    return script


def _peek_back_for_note(lines: list, idx: int, max_back: int = 4) -> str:
    """從目前行往前找最近的『# (註記:xxx)』中文註解作為步驟名稱。"""
    for i in range(idx - 1, max(0, idx - max_back) - 1, -1):
        line = lines[i].strip()
        if not line:
            continue
        if line.startswith("#"):
            m = _RE_NOTE.search(line)
            if m:
                return m.group(1).strip().rstrip("）)")
            # 不含「註記」標記但仍是註解，視為步驟說明
            text = line.lstrip("#").strip()
            # 跳過明顯不是名稱的（例如 "1. " 開頭的純編號）
            if text and len(text) < 60:
                return text
        else:
            # 遇到非註解、非空白 → 停止
            break
    return "（未命名步驟）"


def format_script_for_display(script: HotkeyScript) -> list[str]:
    """把 HotkeyScript 轉為人類可讀的字串列表（每行一條）。"""
    lines: list[str] = []
    title = f"=== 熱鍵 {script.hotkey} @ {script.resolution} ==="
    lines.append(title)
    lines.append(f"  原始碼位置: main.py 第 {script.line_start}–{script.line_end} 行")
    lines.append("")

    if script.init_action:
        lines.append(f"起始動作:  {script.init_action.to_human()}")
        lines.append("")

    if not script.steps:
        lines.append("  (此熱鍵無迴圈步驟，僅執行起始動作)")
        return lines

    lines.append(f"迴圈步驟（共 {len(script.steps)} 個，依序判斷）:")
    lines.append("")
    for i, step in enumerate(script.steps, 1):
        lines.append(f"  ── 第 {i} 步: {step.name}  (main.py L{step.line_number})")
        if step.matches:
            lines.append(f"     【偵測條件】（全部符合才執行）:")
            for cond in step.matches:
                lines.append(f"        · {cond.to_human()}")
        if step.actions:
            lines.append(f"     【執行動作】:")
            for j, act in enumerate(step.actions, 1):
                lines.append(f"        {j}. {act.to_human()}")
        else:
            lines.append("     【執行動作】: (此步驟僅判斷終止條件)")
        lines.append("")

    return lines


def parse_all_hotkeys(resolutions=("1920x1080", "1280x1024", "1024x768"),
                      hotkeys=("F3", "F4", "F9", "F10", "F11")) -> dict:
    """全解析。回傳 {res: {hotkey: HotkeyScript or None}}。"""
    source = _read_main_source()
    if not source:
        return {}
    out = {}
    for res in resolutions:
        out[res] = {}
        for hk in hotkeys:
            script = parse_hotkey_script(source, hk, res)
            out[res][hk] = script
    return out
