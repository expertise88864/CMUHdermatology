# -*- coding: utf-8 -*-
"""診間批次2/3 回歸測試(2026-07-12 未審區域計畫書補修)。

CL-02 離群裁剪改中位數;CL-03 時段切換半開區間;CL-04 止掛不誤發快滿;
CL-05 session 用語不變式釘位(不改碼);CL-06 全空回預設;CL-07 混型不拋。
FC-03/FC-05 為 Tk/執行緒路徑,以源碼層守衛防回退。
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.clinic_history import duration_stats, all_time_average_text  # noqa: E402
from cmuh_common.reg64_utils import reg64_time_code_from_local_clock  # noqa: E402
from cmuh_common.threshold_policy import is_near_alert_threshold  # noqa: E402
from cmuh_common.clinic_state import normalize_clinic_rooms, DEFAULT_CLINIC_ROOMS  # noqa: E402


# ── CL-02 中位數裁剪 ─────────────────────────────────────────────
def test_cl02_single_outlier_no_longer_inflates_average():
    # 三筆 300 秒 + 一筆卡住 36000 秒;中位數帶應排除離群 → 平均 5 分,而非放回全體的 153 分
    _all, valid, avg_min = duration_stats([300, 300, 300, 36000])
    assert 36000 not in valid, f"離群未被裁掉:{valid}"
    assert avg_min == 5.0, f"平均被離群拉高:{avg_min}"


def test_cl02_tight_distribution_unchanged():
    _all, _valid, avg_min = duration_stats([600, 600, 600])
    assert avg_min == 10.0


def test_cl02_empty():
    assert duration_stats([]) == ([], [], None)


# ── CL-03 時段切換半開區間 ───────────────────────────────────────
def test_cl03_half_open_boundaries():
    assert reg64_time_code_from_local_clock(datetime(2026, 7, 12, 12, 59, 59, 500000)) == "1"
    assert reg64_time_code_from_local_clock(datetime(2026, 7, 12, 13, 0, 0)) == "2"
    assert reg64_time_code_from_local_clock(datetime(2026, 7, 12, 17, 29, 59, 999999)) == "2"
    assert reg64_time_code_from_local_clock(datetime(2026, 7, 12, 17, 30, 0)) == "3"
    assert reg64_time_code_from_local_clock(datetime(2026, 7, 12, 0, 0, 0)) == "1"


# ── CL-04 止掛不誤發快滿 ─────────────────────────────────────────
def test_cl04_stopped_session_not_near_alert():
    tmap = {(3, "上午"): 105}
    stopped = [{"session": "上午", "count": 100, "is_stopped": True}]
    assert is_near_alert_threshold(stopped, 3, tmap, margin=10) is False
    # 對照組:未止掛、同人數 → 應觸發
    active = [{"session": "上午", "count": 100, "is_stopped": False}]
    assert is_near_alert_threshold(active, 3, tmap, margin=10) is True


# ── CL-05 session 用語不變式(釘位,不改碼) ───────────────────────
def test_cl05_session_wording_invariant():
    # 掛號來源產「上午」且門檻 key 也用「上午」→ 現行匹配成立;守住此不變式,避免日後上游改字串靜默失效
    tmap = {(3, "上午"): 105}
    assert is_near_alert_threshold([{"session": "上午", "count": 100}], 3, tmap, 10) is True


# ── CL-06 全空回預設 / 部分留空不動 ──────────────────────────────
def test_cl06_all_blank_returns_default():
    rooms, changed = normalize_clinic_rooms(["", "", "", "", ""])
    assert rooms == list(DEFAULT_CLINIC_ROOMS)
    assert changed is True


def test_cl06_full_custom_unchanged():
    rooms, changed = normalize_clinic_rooms(["101", "102", "103", "104", "105"])
    assert changed is False


def test_cl06_partial_fills_defaults():
    rooms, _changed = normalize_clinic_rooms(["201"])
    assert rooms == ["201", "102", "103", "104", "105"]


# ── CL-07 混型不拋 ───────────────────────────────────────────────
def test_cl07_mixed_type_no_typeerror():
    assert all_time_average_text((0.0, 0), ["12", 30]) != ""   # 不拋
    assert all_time_average_text((0.0, 0), ["abc"]) == "-"
    assert all_time_average_text((0.0, 0), [600]) == "10.0"


# ── FC-03 / FC-05 源碼層守衛 ─────────────────────────────────────
def _read(path_parts):
    p = os.path.join(os.path.dirname(__file__), "..", *path_parts)
    with open(p, encoding="utf-8") as f:
        return f.read()


def test_fc03_reg64_reachable_read_snapshot():
    src = _read(["src", "main.py"])
    assert "for code, reachable in list(self._reg64_room_reachable.items())" in src, \
        "FC-03 讀端未用 list() 快照"


def test_fc05_destroy_cancels_ensure_shown_and_exists_uses_destroy():
    src = _read(["src", "cmuh_common", "floating_clinic.py"])
    assert "self._ensure_shown_id = self.win.after(400" in src, "FC-05 未保存 ensure_shown after id"
    assert "self.win.after_cancel(self._ensure_shown_id)" in src, "FC-05 destroy 未取消 ensure_shown"
    # exists() 單邊死亡走 self.destroy()
    ex = src[src.find("def exists"):src.find("def destroy")]
    assert "self.destroy()" in ex, "FC-05 exists() 未改走 self.destroy()"
