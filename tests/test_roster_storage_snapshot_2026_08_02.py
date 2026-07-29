# -*- coding: utf-8 -*-
"""[2026-08-02 補審] 快照保護只蓋到四個檔,另外五個「寫壞就沒有回頭路」。

`_snapshot` 原本是各個 `save_*` 自己呼叫的,結果只有 config / ledger / biopsy /
月檔有;`save_week_colors` / `save_holiday_duty` / `save_clinic_template` /
`save_clerk_batches` / `save_biopsy_grid` 一個都沒有。

這不是理論上的風險 —— 2026-07-25 的審查註解就寫過「config.json 是唯一沒有快照
保護的存檔路徑…誤刪最痛」,當時補了 config 卻沒有回頭看還有誰漏。而年度假日表
最要緊:它的鍵集合【就是】國定假日清單,一旦被清空,整年的點數計算與週末連休
區塊全部跟著算錯,而且無從回溯。

修法:把 `_snapshot` 移進 `_save()` —— 唯一的寫入出口。這樣不是「補上五個呼叫」
而是「以後不可能再漏」,新增的 `save_*` 自動有保護。
`.bak-*` 已在 GitSync 的 .gitignore 內,不會同步出去、不產生 git 雜訊。
"""
import glob
import io
import json
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common.roster.storage import KEEP_SNAPSHOTS, RosterStorage  # noqa: E402


@pytest.fixture
def st(tmp_path):
    return RosterStorage(str(tmp_path))


def _baks(path: str) -> list:
    return sorted(glob.glob(f"{path}.bak-*"))


def _read(path: str) -> dict:
    return json.loads(io.open(path, encoding="utf-8-sig").read())


# ─── 每一條 save_* 都要留下前一版 ────────────────────────────────────────────
def test_every_save_path_snapshots_the_previous_content(st):
    """★整層契約,不是單一函式★ 逐一走過九條 save_*:第二次存檔之後,
    磁碟上必須留得到【第一次】的內容。"""
    cases = [
        ("week_colors.json",
         lambda: st.save_week_colors(2026, {"2026-W31": "pink"}),
         lambda: st.save_week_colors(2026, {"2026-W99": "green"}, replace=True),
         lambda d: d["weeks"] == {"2026-W31": "pink"}),
        ("holiday_duty.json",
         lambda: st.save_holiday_duty({"r": {date(2026, 1, 1): "A"}, "vs": {}}),
         lambda: st.save_holiday_duty({"r": {}, "vs": {}}),
         lambda d: d["r"] == {"2026-01-01": "A"}),
        ("clinic_template.json",
         lambda: st.save_clinic_template({"template": {"0": {"上午": [{"room": "101"}]}}}),
         lambda: st.save_clinic_template({"template": {}}),
         lambda d: d["template"] != {}),
        ("clerk_batches.json",
         lambda: st.save_clerk_batches([{"id": "b1", "start_monday": "2026-08-03"}]),
         lambda: st.save_clerk_batches([]),
         lambda d: d["batches"] != []),
        ("biopsy_grid.json",
         lambda: st.save_biopsy_grid({"b1": {"2026-08-03": {"上午": True}}}),
         lambda: st.save_biopsy_grid({}),
         lambda d: d["grid"] != {}),
        ("config.json",
         lambda: st.save_config({"r_members": [{"id": "A"}]}),
         lambda: st.save_config({"r_members": []}),
         lambda d: d["r_members"] == [{"id": "A"}]),
        ("ledger.json",
         lambda: st.save_ledger({"r": {"A": 3.0}}),
         lambda: st.save_ledger({"r": {}}),
         lambda d: d["r"] == {"A": 3.0}),
        ("biopsy.json",
         lambda: st.save_biopsy({"counts": {"A": 2}}),
         lambda: st.save_biopsy({"counts": {}}),
         lambda d: d["counts"] == {"A": 2}),
    ]
    for name, first, second, check_old in cases:
        p = os.path.join(st.base_dir, name)
        first()
        assert not _baks(p), f"{name}: 首次建檔不該有快照"
        second()
        baks = _baks(p)
        assert baks, f"★{name} 沒有留下前一版★"
        assert check_old(_read(baks[-1])), f"{name} 的快照內容不是前一版"

    p = st._month_path("2026-08")
    st.save_month("2026-08", {"r_duty": {"2026-08-01": {"person": "A"}}})
    st.save_month("2026-08", {"r_duty": {}})
    assert _read(_baks(p)[-1])["r_duty"], "★月檔沒有留下前一版★"


def test_snapshot_lives_in_the_single_write_exit(st):
    """★不是「補上五個呼叫」而是「以後不可能再漏」★

    `_snapshot` 要掛在唯一的寫入出口 `_save()` 上,新增的 save_* 自動有保護;
    留在各個 save_* 裡的話,下一個新增的存檔路徑照樣會忘。
    """
    import inspect

    from cmuh_common.roster import storage as mod
    assert "self._snapshot(" in inspect.getsource(mod.RosterStorage._save)
    for name, fn in vars(mod.RosterStorage).items():
        if name.startswith("save_") and callable(fn):
            assert "_snapshot(" not in inspect.getsource(fn), \
                f"{name} 不該自己再呼叫一次(會存成兩份快照)"


def test_only_one_snapshot_per_save(st):
    """把 _snapshot 移進 _save 時,原本各 save_* 裡的呼叫必須拿掉,否則每存一次
    就留兩份一模一樣的快照,20 份的保留額度一半被浪費掉。"""
    p = os.path.join(st.base_dir, "config.json")
    st.save_config({"r_members": [{"id": "A"}]})
    st.save_config({"r_members": [{"id": "B"}]})
    assert len(_baks(p)) == 1, f"一次存檔只該留一份快照,實際 {len(_baks(p))}"


def test_old_snapshots_are_pruned(st):
    p = os.path.join(st.base_dir, "week_colors.json")
    for i in range(KEEP_SNAPSHOTS + 5):
        st.save_week_colors(2026, {f"2026-W{i:02d}": "pink"})
    assert len(_baks(p)) == KEEP_SNAPSHOTS


# ─── 不可矯枉過正 ──────────────────────────────────────────────────────────
def test_no_snapshot_when_the_write_is_refused(st):
    """寫入被守門擋下(檔案被鎖住)時不該留下快照 —— 什麼都沒改,留一份只是浪費
    保留額度,把真正有用的歷史擠掉。"""
    st.save_week_colors(2026, {"2026-W31": "pink"})
    p = os.path.join(st.base_dir, "week_colors.json")
    before = len(_baks(p))

    def _locked(_path):
        raise ValueError("模擬：被防毒/同步軟體鎖住")
    st._guard_overwrite = _locked
    with pytest.raises(ValueError):
        st.save_week_colors(2026, {"2026-W33": "green"})

    assert len(_baks(p)) == before, "被拒寫時不該留快照"


def test_first_ever_write_makes_no_snapshot(st):
    """檔案還不存在時沒有東西可備份,不該產生空快照。"""
    st.save_biopsy_grid({"b1": {}})
    assert not _baks(os.path.join(st.base_dir, "biopsy_grid.json"))
