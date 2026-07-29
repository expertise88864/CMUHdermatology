# -*- coding: utf-8 -*-
"""[2026-08-02 第二輪外審 P2-04] 快照失敗只記 warning,然後照樣覆寫。

`_snapshot()` 的 `copy2` 失敗時只寫一行 "快照失敗(續存)",接著 `_save()` 照常把新
內容寫上去。對「每改一格就存一次」的月檔這是合理的(不能因為備份不成就不讓人排班),
但對【失去備份就再也回不來】的那幾份就不是同一件事:

  * `config.json` —— 全體 R/VS 成員名單
  * `holiday_duty.json` —— 它的鍵集合【就是】整年的國定假日清單,錯了之後點數與
    週末連休區塊全部跟著算錯
  * 已定案月份的 `force=True` 覆寫 —— 那是留底用的定案快照本身

同一份程式對這三者與對一般自動存檔用同一套風險政策,是不一致的。

修法:`_save(..., backup=REQUIRE_BACKUP)`。備份不成就拒寫並說清楚,而不是「寫了、
但你沒有回頭路」。刻意【不】把 ledger.json 納入:它的餘額可由月檔的實排經
`resettle_from_duty` 重算出來(仍在保留窗內的月份),而且它每次套用排班都會寫,
拉高拒寫門檻的代價大於好處。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common.roster.storage import RosterStorage  # noqa: E402


@pytest.fixture
def st(tmp_path):
    return RosterStorage(str(tmp_path))


def _break_snapshot(monkeypatch):
    """讓快照複製一律失敗(模擬檔案被鎖住/磁碟滿)。"""
    import shutil
    monkeypatch.setattr(shutil, "copy2",
                        lambda *a, **k: (_ for _ in ()).throw(
                            OSError("模擬：快照無法建立")))


def _content(st, name):
    import io
    import json
    return json.loads(io.open(os.path.join(st.base_dir, name),
                              encoding="utf-8-sig").read())


# ─── 高後果寫入:備份不成就拒寫 ─────────────────────────────────────────────
def test_member_list_is_not_overwritten_without_a_backup(st, monkeypatch):
    """★核心★ 全體成員名單失去備份就回不來 —— 寧可拒寫。"""
    st.save_config({"r_members": [{"id": "A"}, {"id": "B"}]})
    _break_snapshot(monkeypatch)

    with pytest.raises(ValueError, match="備份"):
        st.save_config({"r_members": []})

    assert _content(st, "config.json")["r_members"] == [{"id": "A"}, {"id": "B"}]


def test_holiday_table_is_not_overwritten_without_a_backup(st, monkeypatch):
    from datetime import date
    st.save_holiday_duty({"r": {date(2026, 1, 1): "A"}, "vs": {}})
    _break_snapshot(monkeypatch)

    with pytest.raises(ValueError, match="備份"):
        st.save_holiday_duty({"r": {}, "vs": {}})

    assert st.holidays_set() == {date(2026, 1, 1)}


def test_force_overwriting_a_finalized_month_requires_a_backup(st, monkeypatch):
    """定案月的 force 覆寫是「把留底本身改掉」——沒有備份不可以做。"""
    st.save_month("2026-08", {"r_duty": {"2026-08-01": {"person": "A"}}})
    m = st.load_month("2026-08")
    m["finalized"] = True
    st.save_month("2026-08", m, force=True)
    _break_snapshot(monkeypatch)

    with pytest.raises(ValueError, match="備份"):
        st.save_month("2026-08", {"r_duty": {}}, force=True)

    assert st.load_month("2026-08")["r_duty"], "定案內容必須原封不動"


# ─── 不可矯枉過正 ──────────────────────────────────────────────────────────
def test_ordinary_month_saves_still_go_through(st, monkeypatch):
    """★每改一格就存一次的路徑不可以被拉高門檻★
    備份不成仍要能排班(否則備份軟體鎖檔的那幾分鐘整個程式等於停擺)。"""
    st.save_month("2026-08", {"r_duty": {"2026-08-01": {"person": "A"}}})
    _break_snapshot(monkeypatch)

    st.save_month("2026-08", {"r_duty": {"2026-08-02": {"person": "B"}}})

    assert "2026-08-02" in st.load_month("2026-08")["r_duty"]


def test_the_ledger_is_deliberately_best_effort(st, monkeypatch):
    """帳本餘額可由月檔實排經 resettle_from_duty 重算 → 刻意不納入 REQUIRE。
    (這是取捨,不是漏掉;寫在測試裡免得日後被當成疏忽而「順手補上」。)"""
    st.save_ledger({"r": {"A": 1.0}})
    _break_snapshot(monkeypatch)

    st.save_ledger({"r": {"A": 2.0}})

    assert _content(st, "ledger.json")["r"]["A"] == 2.0


def test_first_ever_write_needs_no_backup(st, monkeypatch):
    """檔案還不存在 → 沒有東西可備份 → 不可因此拒絕建檔。"""
    _break_snapshot(monkeypatch)
    st.save_config({"r_members": [{"id": "A"}]})
    assert _content(st, "config.json")["r_members"]


def test_a_successful_backup_still_allows_the_write(st):
    st.save_config({"r_members": [{"id": "A"}]})
    st.save_config({"r_members": [{"id": "B"}]})
    assert _content(st, "config.json")["r_members"] == [{"id": "B"}]


# ─── [外審第5輪] 拒寫必須被使用者看見 ───────────────────────────────────────
def test_settings_holiday_edit_reports_a_refused_write(tmp_path, monkeypatch):
    """★我自己造的新洞★

    把 save_holiday_duty 改成 REQUIRE_BACKUP 之後它會拋,但設定頁的假日新增/刪除
    是裸呼叫 —— Tk 只把例外寫進 log,使用者以為存好了。這正是我今早才在
    duty.py/day_tab.py 修過的同一個病灶(guard_write),自己又造了一個。

    這裡不建 Tk 視窗,直接檢查那幾條寫入路徑都經過 guard_write。
    """
    import inspect

    from cmuh_common.roster.ui import settings as mod
    for fn in (mod.SettingsTab._holiday_put, mod.SettingsTab._holiday_del,
               mod.SettingsTab._template_add, mod.SettingsTab._template_del,
               mod.SettingsTab._wc_toggle if hasattr(mod.SettingsTab, "_wc_toggle")
               else mod.SettingsTab._holiday_put):
        src = inspect.getsource(fn)
        if "storage.save_" in src:
            assert "guard_write" in src, f"{fn.__name__} 的寫入沒有包 guard_write"


def test_every_storage_write_in_the_settings_tab_is_guarded():
    """整檔掃一次 —— 這不是單一函式的疏漏,是整層的契約。
    (_save_cfg / _sync_ledger 有自己的錯誤處理,豁免。)"""
    import io
    import os as _os
    p = _os.path.join(_os.path.dirname(__file__), "..", "src", "cmuh_common",
                      "roster", "ui", "settings.py")
    lines = io.open(p, encoding="utf-8").read().split("\n")
    bad = []
    for i, ln in enumerate(lines):
        if "storage.save_" not in ln:
            continue
        window = "\n".join(lines[max(0, i - 4):i + 1])
        if "guard_write" in window:
            continue
        if "save_config" in ln or "save_ledger" in ln:
            continue                      # 有自己的 try/except
        bad.append(f"{i + 1}: {ln.strip()}")
    assert not bad, "這些寫入沒有包 guard_write：\n" + "\n".join(bad)


def test_finalize_does_not_resettle_the_ledger_when_it_cannot_save(tmp_path,
                                                                   monkeypatch):
    """★[外審第9輪] 多步驟落地要讓失敗發生在第一步★

    finalize 是先重算帳本(寫 ledger.json)、再 force 覆寫月檔。自從 force 覆寫
    改成 REQUIRE_BACKUP,月檔那一步可能拒寫 —— 於是 UI 報「定案失敗」並把勾選
    還原,而帳本【已經被重新結算過了】。
    """
    from cmuh_common.roster.service import RosterService
    from cmuh_common.roster.storage import RosterStorage

    st = RosterStorage(str(tmp_path))
    st.save_config({"r_members": [{"id": "A"}, {"id": "B"}],
                    "vs_members": [],
                    "points": {"weekday": 1, "weekend": 2,
                               "national_holiday": 1}})
    st.save_holiday_duty({"r": {}, "vs": {}})
    st.save_ledger({"r": {"A": 0.0, "B": 0.0}, "vs": {}, "history": []})
    st.save_month("2026-08", {"r_duty": {"2026-08-03": {"person": "A"}}})
    svc = RosterService(st)
    before = st.load_ledger()["r"]

    _break_snapshot(monkeypatch)
    with pytest.raises(ValueError, match="備份"):
        svc.finalize("2026-08", True)

    assert st.load_ledger()["r"] == before, "★帳本被動過了,但月份沒定案★"
    assert st.load_month("2026-08")["finalized"] is False


def test_finalize_still_works_normally(tmp_path):
    """★不可矯枉過正★ 一切正常時定案要照常完成(含帳本重算)。"""
    from cmuh_common.roster.service import RosterService
    from cmuh_common.roster.storage import RosterStorage

    st = RosterStorage(str(tmp_path))
    st.save_config({"r_members": [{"id": "A"}, {"id": "B"}],
                    "vs_members": [],
                    "points": {"weekday": 1, "weekend": 2,
                               "national_holiday": 1}})
    st.save_holiday_duty({"r": {}, "vs": {}})
    st.save_month("2026-08", {"r_duty": {"2026-08-03": {"person": "A"}}})
    svc = RosterService(st)

    svc.finalize("2026-08", True)

    assert st.load_month("2026-08")["finalized"] is True
    assert st.load_ledger()["r"]["A"] != 0.0, "帳本要有被重算"


def test_finalize_does_not_need_a_second_snapshot(tmp_path, monkeypatch):
    """★[外審第10輪] 預檢做了第一次快照,save_month 又要第二次★

    兩次之間檔案被鎖住,就仍會留下「帳本已重算、月份沒定案」的半套 ——
    預檢的意義就沒了。這裡讓【第二次】copy2 失敗來釘住這件事。
    """
    import shutil

    from cmuh_common.roster.service import RosterService
    from cmuh_common.roster.storage import RosterStorage

    st = RosterStorage(str(tmp_path))
    st.save_config({"r_members": [{"id": "A"}, {"id": "B"}], "vs_members": [],
                    "points": {"weekday": 1, "weekend": 2,
                               "national_holiday": 1}})
    st.save_holiday_duty({"r": {}, "vs": {}})
    st.save_month("2026-08", {"r_duty": {"2026-08-03": {"person": "A"}}})
    svc = RosterService(st)

    real = shutil.copy2
    calls = {"n": 0}

    def _fail_second(*a, **k):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise OSError("模擬：第二次備份時檔案被鎖住")
        return real(*a, **k)

    monkeypatch.setattr(shutil, "copy2", _fail_second)
    svc.finalize("2026-08", True)

    assert st.load_month("2026-08")["finalized"] is True, \
        "★預檢已經留過快照,不該再被第二次失敗擋下★"
