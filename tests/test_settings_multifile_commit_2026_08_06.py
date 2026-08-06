# -*- coding: utf-8 -*-
"""設定頁「儲存」的多檔一致性（2026-08-06 外審 P1-07）。

【問題】設定頁一次要寫三個檔:r_doctor_settings.json / threshold_settings.json /
doctors.json。舊做法是連續三次 `atomic_write_json` —— 每個檔【各自】原子，但
三個檔【之間】不是:第二個寫失敗時第一個早就生效了，使用者只看到一個例外
(甚至什麼都沒看到)，卻不知道設定已經處於「R 醫師已更新、醫師清單還是舊的」
這種半套狀態。單檔 atomic write 不提供多檔 transaction。

【修法】`atomic_write_json_multi` 兩階段:
  Phase 1 stage : 全部寫 .tmp + fsync —— 任一失敗就清掉全部 tmp，目標檔一個都沒動。
  Phase 2 commit: 依序 os.replace —— 中途失敗會回報「已生效/未生效」清單。
呼叫端(save_all_settings)據此給出精確訊息，並且【只有成功才套用副作用】。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import atomic_io  # noqa: E402
from cmuh_common.atomic_io import (  # noqa: E402
    MultiWriteError, atomic_write_json_multi, safe_load_json,
)


def _paths(tmp_path, n=3):
    return [str(tmp_path / f"f{i}.json") for i in range(n)]


# ── happy path ──────────────────────────────────────────────────────────────
def test_all_files_written_together(tmp_path):
    a, b, c = _paths(tmp_path)
    atomic_write_json_multi([(a, {"x": 1}), (b, [1, 2]), (c, {"中文": "值"})])
    assert safe_load_json(a, None) == {"x": 1}
    assert safe_load_json(b, None) == [1, 2]
    assert safe_load_json(c, None) == {"中文": "值"}


def test_no_tmp_files_left_behind(tmp_path):
    a, b = _paths(tmp_path, 2)
    atomic_write_json_multi([(a, {"x": 1}), (b, {"y": 2})])
    leftovers = [p for p in os.listdir(tmp_path) if p.endswith(".tmp")]
    assert leftovers == [], f"殘留暫存檔:{leftovers}"


def test_existing_files_are_replaced_not_appended(tmp_path):
    a, b = _paths(tmp_path, 2)
    for p in (a, b):
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"old": True}, f)
    atomic_write_json_multi([(a, {"new": 1}), (b, {"new": 2})])
    assert safe_load_json(a, None) == {"new": 1}
    assert safe_load_json(b, None) == {"new": 2}


# ── Phase 1 失敗:一個檔都不可以變 ────────────────────────────────────────────
def test_stage_failure_changes_nothing(tmp_path, monkeypatch):
    """★核心★ 序列化/寫 tmp 失敗 → 目標檔【一個都沒動】(舊值完好)。"""
    a, b, c = _paths(tmp_path)
    for p in (a, b, c):
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"original": p}, f)

    class _Unserializable:
        pass

    with pytest.raises(MultiWriteError) as ei:
        atomic_write_json_multi([(a, {"ok": 1}), (b, {"bad": _Unserializable()}),
                                 (c, {"ok": 3})])
    assert ei.value.phase == "stage"
    assert ei.value.written == [], "stage 失敗不可以有任何檔已生效"
    for p in (a, b, c):
        assert safe_load_json(p, None) == {"original": p}, f"{p} 被動到了"
    assert [x for x in os.listdir(tmp_path) if x.endswith(".tmp")] == []


def test_disk_full_on_second_file_leaves_first_untouched(tmp_path, monkeypatch):
    """磁碟寫入錯誤(模擬空間不足)發生在第二個檔 → 第一個檔也不可以生效。"""
    a, b = _paths(tmp_path, 2)
    with open(a, "w", encoding="utf-8") as f:
        json.dump({"original": "a"}, f)

    real_fsync = atomic_io._flush_and_fsync
    calls = {"n": 0}

    def _boom(f):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(28, "No space left on device")
        return real_fsync(f)

    monkeypatch.setattr(atomic_io, "_flush_and_fsync", _boom)
    with pytest.raises(MultiWriteError) as ei:
        atomic_write_json_multi([(a, {"new": "a"}), (b, {"new": "b"})])
    assert ei.value.phase == "stage"
    assert safe_load_json(a, None) == {"original": "a"}, "第一個檔不該生效"
    assert not os.path.exists(b)


# ── Phase 2 失敗:必須精確回報哪些生效 ────────────────────────────────────────
def test_commit_failure_reports_exactly_what_landed(tmp_path, monkeypatch):
    """replace 中途失敗 → 例外要帶【已生效】與【未生效】清單,UI 才講得清楚。"""
    a, b, c = _paths(tmp_path)
    real_replace = atomic_io._replace_with_retry
    seen = {"n": 0}

    def _fail_second(src, dst):
        seen["n"] += 1
        if seen["n"] == 2:
            raise OSError("simulated replace failure")
        return real_replace(src, dst)

    monkeypatch.setattr(atomic_io, "_replace_with_retry", _fail_second)
    with pytest.raises(MultiWriteError) as ei:
        atomic_write_json_multi([(a, {"v": 1}), (b, {"v": 2}), (c, {"v": 3})])
    e = ei.value
    assert e.phase == "commit"
    assert e.written == [a], f"已生效清單不正確:{e.written}"
    assert e.pending == [b, c], f"未生效清單不正確:{e.pending}"
    assert safe_load_json(a, None) == {"v": 1}
    assert not os.path.exists(b) and not os.path.exists(c)
    # 失敗後不可留下暫存殘檔
    assert [x for x in os.listdir(tmp_path) if x.endswith(".tmp")] == []


# ── 呼叫端接線:失敗不可宣稱「已儲存」,成功才套用副作用 ──────────────────────
def _save_all_src() -> str:
    import inspect

    import main
    return inspect.getsource(main.AutomationApp.save_all_settings)


def test_save_all_settings_uses_the_multi_commit():
    src = _save_all_src()
    assert "_atomic_write_json_multi(" in src, \
        "設定頁三個檔必須一起 commit(否則又回到半套風險)"
    # 不可以再有單檔逐個寫的舊寫法
    assert "_atomic_write_json(get_conf_path(" not in src, \
        "還有單檔寫入殘留 → 三檔之間又不是 transaction 了"


def test_save_all_settings_reports_partial_commit_and_bails():
    """失敗時:要 return(不繼續)、要用 showerror 講清楚、不可顯示「設定已儲存」。"""
    src = _save_all_src()
    i_commit = src.index("_atomic_write_json_multi(")
    i_notice = src.index("設定已儲存")
    assert i_commit < i_notice, "「已儲存」提示必須在 commit 之後"

    seg = src[i_commit:i_notice]
    assert "MultiWriteError" in seg, "要攔 MultiWriteError 才能拿到已生效/未生效清單"
    assert "showerror" in seg, "失敗要明確告知使用者(不可只丟例外或無聲)"
    assert "e.written" in seg and "e.pending" in seg, \
        "必須把【哪些存了、哪些沒存】列給使用者"
    assert seg.count("return") >= 2, "stage/commit 兩種失敗都要 return,不可往下走"


def test_side_effects_only_after_successful_commit():
    """★半套的另一面★ 存檔失敗時不可以已經把 DOCTORS/畫面套成新值。"""
    src = _save_all_src()
    i_commit = src.index("_atomic_write_json_multi(")
    for token in ("self.doctors_list = new_doctors_list",
                  "DOCTORS = self.doctors_list",
                  "self.refresh_all_calendars()"):
        assert src.index(token) > i_commit, \
            f"{token} 必須排在 commit 成功之後(否則畫面已套用、磁碟沒存到)"
