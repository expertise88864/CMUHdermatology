# -*- coding: utf-8 -*-
"""F11 療程欄的讀值分類。
（2026-08-01 外部 code review P1-03）

【問題】
原本 `_f11_read_course_value()` 回一個字串，而且對三種完全不同的情況都回 `""`：

  * 找不到療程欄（控制項定位失敗）
  * 讀取時拋例外
  * 真的讀到空白（合法：這一診沒有療程）

呼叫端只能看到「空字串」，於是分不出「HIS 說沒有」與「我們沒讀到」。
更糟的是它把**讀到的原始內容**寫進 `automation_ui.log`：定位一漂到姓名欄，
病人姓名就進了一個 5MB×3 輪替、沒有保存期限、而且常整包交給開發者除錯的檔案。

【★使用者定案（2026-08-01）：不 fail-closed★】
讀到無法辨識的內容時，F11 **照舊按「全部完成」**，不擋、不跳窗 —— 與金絲雀
「改版＝寄信通知不擋不跳窗」同一套原則（診間動線優先）。
但必須做到三件事：
  1. log 只能寫「療程讀值 invalid，長度=N」，**絕不可寫原值**；
  2. 記一筆 typed violation 進稽核帳本（帳本本身也不存原值）；
  3. 寄一封通知信，讓維護者知道「定位可能漂了」。

也就是說：**臨床行為不變，可觀測性補上**。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# 讀到的欄位內容：找到了嗎、合法嗎
OK_VALUE = "ok_value"        # 讀到而且是合法療程值
OK_EMPTY = "ok_empty"        # 讀到，而且是空的（合法：這一診沒有療程）
INVALID = "invalid"          # 讀到了，但不是合法療程值 ← 定位漂移的訊號
NOT_FOUND = "not_found"      # 找不到療程欄
READ_FAILED = "read_failed"  # 讀取過程拋例外

# ★療程值的值域＝單一數字★
#   與 `audit_events._CODE_DOMAINS["療程"]`（`\A\d?\Z`）同一個契約。
#   實務上只會是 1/2/3（F1/F2/F3 三段照光課程），但這裡不寫死那三個 ——
#   HIS 哪天多一段課程時，那應該是「新的合法值」而不是「疑似定位漂移」。
_COURSE_RE = re.compile(r"\A\d\Z")

_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")


@dataclass(frozen=True)
class CourseReadResult:
    """★不含原值★ 這個物件會流到 log／稽核帳本／告警信，所以它只帶得動長度。

    `value` 只有在 `status == OK_VALUE` 時才有內容 —— 那時它已經確定是單一數字，
    不可能是姓名或病歷號。其餘狀態一律空字串，想描述「讀到什麼」只能用
    `observed_length`。
    """
    status: str
    value: str = ""
    observed_length: int = 0

    @property
    def is_phototherapy_2_or_3(self) -> bool:
        """要不要走「完成不印」那條路。"""
        return self.status == OK_VALUE and self.value in ("2", "3")

    @property
    def needs_attention(self) -> bool:
        """要不要記 violation + 寄通知（★不影響按哪顆按鈕★）。

        `OK_EMPTY` 不算：沒有療程是正常的。
        """
        return self.status in (INVALID, NOT_FOUND, READ_FAILED)

    def describe(self) -> str:
        """給 log／信件用的一句話 —— 保證不含原值。"""
        if self.status == OK_VALUE:
            return f"療程={self.value}"
        if self.status == OK_EMPTY:
            return "療程=(空白)"
        if self.status == INVALID:
            return f"療程讀值 invalid，長度={self.observed_length}"
        if self.status == NOT_FOUND:
            return "找不到療程欄"
        return "讀療程欄失敗"


def normalize_course_value(raw_value) -> str:
    """全形數字轉半形 + 去頭尾空白。★不做任何把關★（把關在 classify）。"""
    value = str(raw_value or "").strip()
    if not value:
        return ""
    return value.translate(_FULLWIDTH_DIGITS)


def classify_course_value(raw_value) -> CourseReadResult:
    """把讀到的原始內容分類成 typed 結果。

    ★這支是唯一碰得到原值的地方★ 回傳值不帶原值，所以呼叫端就算想印也印不出來
    —— 那正是重點：讓「不小心把病人姓名寫進 log」在型別上做不到。
    """
    value = normalize_course_value(raw_value)
    if not value:
        return CourseReadResult(OK_EMPTY)
    if _COURSE_RE.match(value):
        return CourseReadResult(OK_VALUE, value=value, observed_length=len(value))
    return CourseReadResult(INVALID, observed_length=len(value))
