# -*- coding: utf-8 -*-
"""[2026-08-02 補審] 設定分頁的 config.json 快照會覆蓋他機剛同步進來的變更。

RF-19 已經認出這個風險,並讓 `on_shown()` 重讀了帳本/假日/週色/梯次/門診模板
五個檢視 —— **唯獨 config.json 沒有**。而 config.json 偏偏是唯一「整包覆寫」的
設定檔:`SettingsTab.__init__` 拍下 `self._cfg` 之後,任何一次
`_save_cfg()` 都把【整份記憶體快照】寫回磁碟。

另外兩條路徑也沒有讓設定頁重讀:
  * `scheduler._refresh_all_tabs`(遠端變更 callback)只重畫 duty/day 兩個分頁。
  * `on_shown()` 只重讀上述五項。

於是:A 機新增一位 R → GitSync 合併到 B 機磁碟 → B 機在設定頁動任何一個
config 支撐的控制項(名單/點數/班數範圍/診間容量/PGY 預設代號)→ A 機那位
【無聲消失】。這正是我今天早上在 gitsync_storage 修的同一個病灶
(「使用者對著舊資料繼續編輯並覆蓋掉剛拉進來的變更」),只是在 UI 這一層。

★更慘的是帳本★:名單變動後 `_sync_ledger` 會拿這份陳舊名單去 `sync_members`,
而 `sync_members` 對「不在名單裡的人」是【刪除餘額 + 清掉 history deltas】。
於是不只名單回退,他機新成員的帳本貢獻也被永久抹掉。
"""
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common.roster.service import RosterService  # noqa: E402
from cmuh_common.roster.storage import RosterStorage  # noqa: E402
from cmuh_common.roster.ui import settings as settings_mod  # noqa: E402
from cmuh_common.roster.ui.settings import SettingsTab  # noqa: E402


@pytest.fixture
def root(tk_root):
    """conftest 的共用 root（見那裡的說明：各檔自建 root 會互相把對方擠成 skip）。"""
    return tk_root


@pytest.fixture(autouse=True)
def noblock(monkeypatch):
    for name in ("askyesno",):
        monkeypatch.setattr(settings_mod.messagebox, name, lambda *a, **k: True)
    for name in ("showwarning", "showerror", "showinfo"):
        monkeypatch.setattr(settings_mod.messagebox, name, lambda *a, **k: None)


def _svc(tmp_path):
    st = RosterStorage(str(tmp_path))
    st.save_config({
        "r_members": [{"id": "A", "name": "甲", "level": "R1",
                       "fixed_weekday": 2},
                      {"id": "B", "name": "乙"}],
        "vs_members": [{"id": "D", "name": "D"}],
        "pgy_members": [{"id": "P1"}],
        "points": {"weekday": 1, "weekend": 2, "national_holiday": 1},
        "duty_range_soft": [9, 11],
    })
    st.save_holiday_duty({"r": {date(2026, 8, 15): "A"}, "vs": {}})
    st.save_clinic_template({"template": {"0": {"上午": [{"room": "101"}]}}})
    return RosterService(st)


def _other_machine_adds_r_member(svc, mid="F"):
    """模擬 GitSync 把他機的變更合併到磁碟(對本程序而言就是「檔案自己變了」)。"""
    cfg = svc.storage.load_config()
    cfg["r_members"] = list(cfg["r_members"]) + [{"id": mid, "name": "他機新人"}]
    svc.storage.save_config(cfg)
    led = svc.storage.load_ledger()
    led.setdefault("r", {})[mid] = 7.5          # 他機已經幫他結算過的餘額
    svc.storage.save_ledger(led)


def _ids(svc, scope="r"):
    return [str(m.get("id")) for m in svc.storage.load_config()[f"{scope}_members"]]


# ─── 主結論:他機變更被無聲覆蓋 ──────────────────────────────────────────────
def test_member_add_does_not_wipe_a_member_added_elsewhere(root, tmp_path, monkeypatch):
    """★核心★ 本機新增 C 不該讓他機剛新增的 F 消失。

    走的是使用者真的會做的動作(在設定頁按「新增」),不是直接戳 _cfg。
    """
    svc = _svc(tmp_path)
    tab = SettingsTab(root, svc)
    root.update()
    _other_machine_adds_r_member(svc)                     # ← 快照拍完之後檔案變了
    assert "F" in _ids(svc), "前提:磁碟上確實已有他機的 F"

    _fake_dialog(monkeypatch, {"id": "C", "name": "丙"})
    tab._member_add("r")

    assert _ids(svc) == ["A", "B", "F", "C"], (
        "本機新增必須疊在【磁碟現況】上;實際=" + repr(_ids(svc)))


def test_member_add_does_not_void_the_other_machines_ledger(root, tmp_path, monkeypatch):
    """★比名單回退更嚴重★ _sync_ledger 會用這份名單去 sync_members,
    而 sync_members 對「不在名單裡的人」是刪餘額 + 清 history deltas。
    名單陳舊 → 他機新成員的帳本貢獻被永久抹掉(只留一行 INFO log)。"""
    svc = _svc(tmp_path)
    tab = SettingsTab(root, svc)
    root.update()
    _other_machine_adds_r_member(svc)
    assert svc.storage.load_ledger()["r"]["F"] == 7.5     # 前提

    _fake_dialog(monkeypatch, {"id": "C", "name": "丙"})
    tab._member_add("r")

    assert svc.storage.load_ledger()["r"].get("F") == 7.5, "★他機成員的餘額被作廢★"


def test_param_save_does_not_wipe_a_member_added_elsewhere(root, tmp_path):
    """點數/班數範圍/診間容量與名單同住 config.json —— 只是動一下數字微調鈕,
    也會把整份陳舊快照寫回去。"""
    svc = _svc(tmp_path)
    tab = SettingsTab(root, svc)
    root.update()
    _other_machine_adds_r_member(svc)

    tab._p_cap.set(3)
    tab._save_params_now()

    assert svc.storage.load_config()["room_capacity"] == 3, "使用者這次的編輯要生效"
    assert "F" in _ids(svc), "★不相干的鍵不可被陳舊快照覆蓋★"


def test_param_save_preserves_a_concurrent_remote_param_change(root, tmp_path):
    """★[第3輪外審] 六個數字任一改動都會觸發一次存檔★

    本機只調「診間容量」,他機同時把「平日點數」由 1 改成 5 並同步過來 ——
    整組寫回的話,他機那格就會被本機沒碰過的舊值(1)還原。
    """
    svc = _svc(tmp_path)
    tab = SettingsTab(root, svc)
    root.update()
    cfg = svc.storage.load_config()
    cfg["points"]["weekday"] = 5
    cfg["duty_range_soft"] = [7, 8]
    svc.storage.save_config(cfg)

    tab._p_cap.set(3)                      # 本機只動這一格
    tab._save_params_now()

    out = svc.storage.load_config()
    assert out["room_capacity"] == 3, "使用者這次的編輯要生效"
    assert out["points"]["weekday"] == 5, "★他機改的點數被還原了★"
    assert out["duty_range_soft"] == [7, 8], "★他機改的班數範圍被還原了★"


def test_param_save_still_writes_the_field_the_user_changed(root, tmp_path):
    """★不可矯枉過正★ 使用者真的去改平日點數/班數範圍時,當然要寫進去。"""
    svc = _svc(tmp_path)
    tab = SettingsTab(root, svc)
    root.update()

    tab._p_wd.set(4)
    tab._p_min.set(6)
    tab._save_params_now()

    out = svc.storage.load_config()
    assert out["points"]["weekday"] == 4
    assert out["duty_range_soft"][0] == 6
    assert out["points"]["weekend"] == 2, "沒動的格子維持原值"


def test_pgy_defaults_save_does_not_wipe_a_member_added_elsewhere(root, tmp_path):
    svc = _svc(tmp_path)
    tab = SettingsTab(root, svc)
    root.update()
    _other_machine_adds_r_member(svc)

    tab._pgy_entry.delete(0, "end")
    tab._pgy_entry.insert(0, "P1、P2")
    tab._save_pgy_defaults()

    cfg = svc.storage.load_config()
    assert [m["id"] for m in cfg["pgy_members"]] == ["P1", "P2"]
    assert "F" in _ids(svc), "★不相干的鍵不可被陳舊快照覆蓋★"


def test_member_delete_only_removes_the_selected_one(root, tmp_path):
    """刪除 B 時不可順手把他機的 F 一起帶走。"""
    svc = _svc(tmp_path)
    tab = SettingsTab(root, svc)
    root.update()
    _other_machine_adds_r_member(svc)

    tab._member_trees["r"][0].selection_set("B")
    tab._member_del("r")

    assert _ids(svc) == ["A", "F"], repr(_ids(svc))


def test_member_edit_applies_only_the_fields_the_user_changed(root, tmp_path,
                                                              monkeypatch):
    """★[第2輪外審] 對話框本身也是一份快照★

    我第一版只把陳舊窗口從「開程式起」縮到「開對話框起」,沒有消掉它:
    _MemberDialog 回的是【全部欄位】,值都預填自開窗當下的紀錄,整包蓋回去
    就會把開窗期間他機改的其他欄位、用使用者根本沒動過的舊值悄悄還原。

    情境:B 開著編輯視窗只想改姓名;此時 A 把同一人的級職 R1 → R3 並同步過來。
    """
    svc = _svc(tmp_path)
    tab = SettingsTab(root, svc)
    root.update()
    cfg = svc.storage.load_config()
    cfg["r_members"][0]["level"] = "R3"          # ← 他機在對話框開著時改了級職
    svc.storage.save_config(cfg)

    tab._member_trees["r"][0].selection_set("A")
    # 對話框回傳:姓名被改了,級職/固定值班是開窗當下的舊值(使用者沒動)
    _fake_dialog(monkeypatch, {"id": "A", "name": "甲改", "level": "R1",
                               "fixed_weekday": 2})
    tab._member_edit("r")

    m = svc.storage.load_config()["r_members"][0]
    assert m["name"] == "甲改", "使用者真的改的欄位要生效"
    assert m["level"] == "R3", "★他機改的欄位不可被對話框的舊值蓋回去★"
    assert m["fixed_weekday"] == 2


def test_member_edit_still_applies_a_real_field_change(root, tmp_path,
                                                       monkeypatch):
    """★不可矯枉過正★ 使用者真的把級職改掉時,當然要寫進去。"""
    svc = _svc(tmp_path)
    tab = SettingsTab(root, svc)
    root.update()
    tab._member_trees["r"][0].selection_set("A")
    _fake_dialog(monkeypatch, {"id": "A", "name": "甲", "level": "R4",
                               "fixed_weekday": None})
    tab._member_edit("r")

    m = svc.storage.load_config()["r_members"][0]
    assert m["level"] == "R4"
    assert m["fixed_weekday"] is None, "清掉固定值班也是一次真的變更"


# ─── 顯示面:切回設定頁要看得到他機的變更 ────────────────────────────────────
def test_on_shown_reloads_config_like_the_other_five_views(root, tmp_path):
    """RF-19 讓 on_shown 重讀了帳本/假日/週色/梯次/模板;config 是第六個,
    漏掉它等於「畫面上根本看不到他機的新人,卻照樣被寫回去」。"""
    svc = _svc(tmp_path)
    tab = SettingsTab(root, svc)
    root.update()
    _other_machine_adds_r_member(svc)

    tab.on_shown()
    root.update()

    assert "F" in set(tab._member_trees["r"][0].get_children()), "名單樹要跟上磁碟"
    assert [m["id"] for m in tab._cfg["r_members"]] == ["A", "B", "F"]


def test_reload_keeps_unsaved_pgy_typing(root, tmp_path):
    """★[第2輪外審] 這一欄沒有自動存檔★ 使用者打到一半、還沒按「儲存」時,
    一次不相干的遠端同步不可以把他打的字洗掉(而且他不會知道發生了什麼)。"""
    svc = _svc(tmp_path)
    tab = SettingsTab(root, svc)
    root.update()
    tab._pgy_entry.delete(0, "end")
    tab._pgy_entry.insert(0, "P7、P8、P9")      # 打到一半,還沒按儲存
    _other_machine_adds_r_member(svc)

    tab.on_shown()                              # ← 遠端變更觸發的重讀
    root.update()

    assert tab._pgy_entry.get() == "P7、P8、P9", "★使用者的輸入被洗掉了★"


def test_reload_updates_pgy_entry_when_untouched(root, tmp_path):
    """★不可矯枉過正★ 沒動過的話,還是要跟上他機改的 PGY 預設代號。"""
    svc = _svc(tmp_path)
    tab = SettingsTab(root, svc)
    root.update()
    cfg = svc.storage.load_config()
    cfg["pgy_members"] = [{"id": "P1"}, {"id": "P5"}]
    svc.storage.save_config(cfg)

    tab.on_shown()
    root.update()

    assert tab._pgy_entry.get() == "P1、P5"


def test_reloading_does_not_trigger_a_spurious_save(root, tmp_path):
    """★不可矯枉過正★ 重讀會 set 數字微調鈕的 IntVar,而它掛著 trace → 存檔。
    若不擋,單純切回設定頁就會產生一次 commit/push(而且是把剛讀到的值再寫一次)。"""
    svc = _svc(tmp_path)
    tab = SettingsTab(root, svc)
    root.update()
    saved = []
    orig = svc.storage.save_config
    svc.storage.save_config = lambda d: (saved.append(1), orig(d))[1]

    tab.on_shown()
    root.update()
    tab._flush_params()                       # 把去抖中的存檔逼出來(若有的話)

    assert saved == [], f"重讀不該寫檔,實際寫了 {len(saved)} 次"


def test_corrupt_config_must_not_become_an_empty_overwrite(root, tmp_path, monkeypatch):
    """★2026-07-25 那條教訓不可因為「改成重讀」而重新引進★

    非嚴格的 load_config() 對壞檔/鎖檔靜默回 {}。若拿它當讀-改-寫的基底,
    一次新增成員就會把整份設定清成只剩那一個人 —— 比原本的陳舊覆蓋更慘。
    正確行為:讀不到就中止這次編輯,磁碟原封不動。
    """
    svc = _svc(tmp_path)
    tab = SettingsTab(root, svc)
    root.update()
    raw = os.path.join(str(tmp_path), "config.json")
    with open(raw, "w", encoding="utf-8") as f:
        f.write("{ 這不是 JSON")
    before = open(raw, encoding="utf-8").read()

    _fake_dialog(monkeypatch, {"id": "C", "name": "丙"})
    tab._member_add("r")                       # 不可拋、不可寫

    assert open(raw, encoding="utf-8").read() == before, "壞檔時磁碟必須原封不動"


def test_remote_change_refreshes_the_settings_tab_too(root, tmp_path):
    """scheduler 的遠端變更 callback 只重畫 duty/day —— 設定頁同樣讀 storage,
    也必須跟著重畫,否則使用者看著舊名單繼續編輯。"""
    import inspect

    import scheduler
    src = inspect.getsource(scheduler.ScheduleApp._refresh_all_tabs)
    assert "_settings_tab" in src or "設定" in src, (
        "遠端變更後設定分頁沒有被重畫:" + src)


# ─── 小工具 ────────────────────────────────────────────────────────────────
def _fake_dialog(monkeypatch, result):
    """把 _MemberDialog 換成「直接回傳指定結果」——測的是存檔路徑,不是對話框。"""
    class _D:
        def __init__(self, *a, **k):
            self.result = dict(result)
    monkeypatch.setattr(settings_mod, "_MemberDialog", _D)
