# -*- coding: utf-8 -*-
"""[2026-07-25 完整 code review] 「讀檔失敗被當成沒有資料，然後被正常寫回覆蓋」

打卡 autoclock_config.json 與排班 config.json/月檔踩的是同一個病灶，兩者都不可逆：
  - 打卡：防毒/OneDrive 短暫鎖住設定檔 → 舊版讀成 [] 與「首次啟動未設定」無法區分
    → ①當次完全不啟動排程(整天漏打卡) ②開出空設定視窗，關窗按「儲存」即把帳密與
    整張班表寫成 [] 永久毀掉(atomic_write_json 無 .bak)。
  - 排班：config.json 有 git conflict marker / 被鎖住 → _load_json 靜默回 {} →
    設定頁名單全空 → 改任一參數(去抖自動存檔)就把成員名單永久清掉；月檔同理，
    還會因為讀到 finalized=False 而靜默解除定案。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import autoclock as ac  # noqa: E402
from cmuh_common.roster.storage import RosterStorage  # noqa: E402


# ── 打卡：設定檔 ────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _reset_autoclock_state():
    yield
    ac._config_load_failed = False
    ac.accounts_data = []


def test_transient_read_error_is_not_treated_as_unconfigured(monkeypatch,
                                                             tmp_path):
    """暫時性讀取失敗(status="error") → 標記失敗旗標，不得與「未設定」混為一談。"""
    monkeypatch.setattr(ac, "CONFIG_FILE", tmp_path / "autoclock_config.json")
    monkeypatch.setattr(ac, "safe_load_json_ex",
                        lambda *a, **k: ([], "error"))
    ac.load_config()
    assert ac._config_load_failed is True


def test_missing_config_is_normal_first_run(monkeypatch, tmp_path):
    """首次啟動(missing)/壞檔已被搬走(corrupt) → 視為空、可正常編輯儲存。"""
    monkeypatch.setattr(ac, "CONFIG_FILE", tmp_path / "autoclock_config.json")
    for status in ("missing", "corrupt"):
        monkeypatch.setattr(ac, "safe_load_json_ex",
                            lambda *a, _s=status, **k: ([], _s))
        ac.load_config()
        assert ac._config_load_failed is False, f"{status} 不該被當成讀取失敗"


def test_save_config_refuses_to_overwrite_after_read_failure(monkeypatch,
                                                             tmp_path):
    """★核心：讀取失敗後絕不可用記憶體中的空資料覆寫磁碟上的好檔。"""
    cfg = tmp_path / "autoclock_config.json"
    good = [{"username": "u1", "password": "p1", "schedule": {"mon": "1"}}]
    cfg.write_text(json.dumps(good), encoding="utf-8")
    monkeypatch.setattr(ac, "CONFIG_FILE", cfg)
    monkeypatch.setattr(ac, "safe_load_json_ex", lambda *a, **k: ([], "error"))

    ac.load_config()                      # 模擬檔案被鎖住
    assert ac.save_config() is False, "讀取失敗後必須拒絕存檔"
    assert json.loads(cfg.read_text(encoding="utf-8")) == good, \
        "磁碟上的帳密/班表必須原封不動"


def test_save_config_works_normally_when_load_ok(monkeypatch, tmp_path):
    cfg = tmp_path / "autoclock_config.json"
    monkeypatch.setattr(ac, "CONFIG_FILE", cfg)
    monkeypatch.setattr(ac, "safe_load_json_ex", lambda *a, **k: ([], "missing"))
    ac.load_config()
    ac.accounts_data = [{"username": "u2"}]
    assert ac.save_config() is True
    assert json.loads(cfg.read_text(encoding="utf-8"))[0]["username"] == "u2"


def test_startup_aborts_instead_of_opening_empty_settings_window():
    """源碼守門：讀取失敗時 main() 必須中止，不可落到「開空設定視窗」那條路
    （那條路會 ①不啟動排程 ②讓使用者一按儲存就清空設定）。"""
    import inspect
    src = inspect.getsource(ac.main)
    i_guard = src.index("_config_load_failed")
    i_empty = src.index("if not accounts_data:")
    assert i_guard < i_empty, "讀取失敗的防護必須排在『視為未設定』分支之前"
    assert "return" in src[i_guard:i_empty], "讀取失敗應直接中止本次啟動"


def test_save_config_failure_is_surfaced_to_user():
    """源碼守門：四個呼叫端都必須看回傳值（舊版全部忽略 → UI 謊報成功）。"""
    import inspect
    for fn in (ac.ClockApp.save_account, ac.ClockApp.delete_account,
               ac.ClockApp.save_and_bg, ac.ClockApp.on_closing):
        src = inspect.getsource(fn)
        assert ("ok = save_config()" in src
                or "not save_config()" in src), \
            f"{fn.__name__} 忽略了 save_config 的回傳值"


# ── 排班：storage 寫入防護 ──────────────────────────────────────────────────
def test_locked_file_refuses_overwrite_and_keeps_original(tmp_path,
                                                          monkeypatch):
    """OSError(被防毒/同步軟體鎖住,原檔通常完好) → 拒寫並拋 ValueError。"""
    st = RosterStorage(str(tmp_path))
    st.save_config({"r_members": [{"id": "A"}]})
    before = (tmp_path / "config.json").read_text(encoding="utf-8")

    real_open = open

    def _locked(path, *a, **k):
        if str(path).endswith("config.json") and (not a or "r" in str(a[0])):
            raise PermissionError("locked by AV")
        return real_open(path, *a, **k)
    monkeypatch.setattr("builtins.open", _locked)
    with pytest.raises(ValueError, match="暫時無法讀取"):
        st._guard_overwrite(str(tmp_path / "config.json"))
    monkeypatch.undo()
    assert (tmp_path / "config.json").read_text(encoding="utf-8") == before


def test_corrupt_file_is_backed_up_then_overwrite_allowed(tmp_path):
    """壞檔(git conflict marker/壞 JSON) → 先備份 .corrupt-* 再允許覆寫,
    使用者不會被永久卡住無法存檔,舊內容也留得下來。"""
    st = RosterStorage(str(tmp_path))
    p = tmp_path / "config.json"
    p.write_text("<<<<<<< HEAD\n{\"r_members\": []}\n=======\n", encoding="utf-8")
    st.save_config({"r_members": [{"id": "NEW"}]})
    assert json.loads(p.read_text(encoding="utf-8"))["r_members"][0]["id"] == "NEW"
    baks = list(tmp_path.glob("config.json.corrupt-*"))
    assert baks, "壞檔必須先備份"
    assert "<<<<<<<" in baks[0].read_text(encoding="utf-8")


def test_save_config_keeps_snapshot(tmp_path):
    """config.json 存的是全部成員名單,誤刪最痛 → 必須有快照(其他檔早就有)。"""
    st = RosterStorage(str(tmp_path))
    st.save_config({"r_members": [{"id": "A"}]})
    st.save_config({"r_members": []})                 # 模擬誤刪
    snaps = sorted(tmp_path.glob("config.json.bak-*"))
    assert snaps, "save_config 應留快照"
    assert json.loads(snaps[-1].read_text(encoding="utf-8"))["r_members"], \
        "快照裡要保有誤刪前的名單"


def test_locked_month_file_does_not_silently_unfinalize(tmp_path, monkeypatch):
    """月檔被鎖住時不得寫入——否則讀到的 finalized=False 會靜默解除定案。"""
    st = RosterStorage(str(tmp_path))
    st.save_month("2026-08", {"r_duty": {"2026-08-03": {"person": "A"}},
                              "finalized": True})
    path = str(st._month_path("2026-08"))
    real_open = open

    def _locked(p, *a, **k):
        if str(p) == path and (not a or "r" in str(a[0])):
            raise PermissionError("locked")
        return real_open(p, *a, **k)
    monkeypatch.setattr("builtins.open", _locked)
    with pytest.raises(ValueError):
        st._guard_overwrite(path)
    monkeypatch.undo()
    with open(path, encoding="utf-8") as f:
        assert json.load(f)["finalized"] is True


# ── 會診：_flow_lock 不得洩漏 ───────────────────────────────────────────────
def test_consult_flow_lock_released_when_com_init_fails():
    """源碼守門：import pythoncom / CoInitialize() 必須在 try 內。
    舊版放在 acquire 與 try 之間,只要拋一次例外鎖就永久洩漏 → 之後每次輪詢都只印
    INFO「已有任務進行中」跳過,log 看起來正常但會診查詢再也不會執行。"""
    import inspect

    import consult_query as cq
    src = inspect.getsource(cq._do_full_job)
    i_acq = src.index("_flow_lock.acquire")
    i_try = src.index("try:", i_acq)
    i_imp = src.index("import pythoncom")
    i_coinit = src.index("CoInitialize()")
    assert i_try < i_imp and i_try < i_coinit, \
        "import pythoncom / CoInitialize 必須在 try 之後（否則鎖會洩漏）"
    assert "_flow_lock.release()" in src[src.index("finally:", i_try):], \
        "finally 必須釋放 _flow_lock"


# ── codex deep 第一輪 findings 的回歸測試 ──────────────────────────────────
def test_corrupt_backup_failure_reports_error_not_corrupt(tmp_path,
                                                          monkeypatch):
    """[codex] "corrupt" 的契約是「原檔已被 rename 移走、可安全覆寫」。備份失敗時
    原檔還在,若仍回 "corrupt",呼叫端就會把使用者的檔覆蓋掉且毫無備份 → 應回 "error"。"""
    from cmuh_common import atomic_io
    p = tmp_path / "x.json"
    p.write_text("{not json", encoding="utf-8")

    def _fail_rename(*a, **k):
        raise PermissionError("rename denied")
    monkeypatch.setattr(atomic_io, "_replace_with_retry", _fail_rename)
    _val, status = atomic_io.safe_load_json_ex(str(p), default={})
    assert status == "error", "備份失敗 → 不可回報成可安全覆寫的 corrupt"
    assert p.exists(), "原檔仍在"


def test_guard_rejects_valid_json_with_non_dict_root(tmp_path):
    """[codex] 語法正確但根不是 object(如 [])→ _load_json 一樣轉成 {} 顯示為空,
    屬結構性壞檔;必須先備份再允許覆寫,否則無快照的檔(週色/年度假日)會無備份消失。"""
    st = RosterStorage(str(tmp_path))
    p = tmp_path / "week_colors.json"
    p.write_text('["2026-W31", "pink"]', encoding="utf-8")
    st.save_week_colors(2026, {"2026-W31": "pink"})
    baks = list(tmp_path.glob("week_colors.json.corrupt-*"))
    assert baks, "非 dict 根必須先備份"
    assert "2026-W31" in baks[0].read_text(encoding="utf-8")


def test_resettle_refuses_when_month_file_unreadable(tmp_path, monkeypatch):
    """[codex] 帳本從【讀不到的月檔】推導 → 全月算 0 點、回滾掉真正的舊分錄,
    而 save_ledger 寫的是另一個檔(守門看不到來源有問題)→ 帳本被清成零還報成功。"""
    from cmuh_common.roster.service import RosterService
    st = RosterStorage(str(tmp_path))
    st.save_config({"r_members": [{"id": "A", "name": "甲"}],
                    "points": {"weekday": 1, "weekend": 2,
                               "national_holiday": 1}})
    svc = RosterService(st)
    svc.set_cell("r", "2026-08", __import__("datetime").date(2026, 8, 3), "A")
    svc.resettle_from_duty("r", "2026-08")
    before = dict(st.load_ledger().get("r") or {})
    assert before, "先建立一筆真實結算"

    path = str(st._month_path("2026-08"))
    real_open = open

    def _locked(p, *a, **k):
        if str(p) == path and (not a or "r" in str(a[0])):
            raise PermissionError("locked")
        return real_open(p, *a, **k)
    monkeypatch.setattr("builtins.open", _locked)
    with pytest.raises(ValueError):
        svc.resettle_from_duty("r", "2026-08")
    monkeypatch.undo()
    assert dict(st.load_ledger().get("r") or {}) == before, "帳本不得被清成零"


def test_consult_uninitializes_com_only_when_init_succeeded():
    """[codex] pythoncom 只代表 import 成功。CoInitialize 拋例外(RPC_E_CHANGED_MODE)
    時若仍 CoUninitialize,等於拆掉該緒別處建立的 apartment。"""
    import inspect

    import consult_query as cq
    src = inspect.getsource(cq._do_full_job)
    assert "com_initialized = False" in src and "com_initialized = True" in src
    i_init = src.index("com_initialized = True")
    i_coinit = src.index("CoInitialize()")
    assert i_coinit < i_init, "旗標必須在 CoInitialize 成功【之後】才設"
    assert "if com_initialized:" in src, "finally 應以旗標判斷,而非 pythoncom is not None"


def test_settings_save_failure_is_surfaced_and_reloads():
    """[codex] storage 新增的守門會拋 ValueError;設定頁若不接,Tk callback 例外只
    進 log(不跳窗)→ 使用者以為存好了,畫面與磁碟不一致。"""
    import inspect

    from cmuh_common.roster.ui.settings import SettingsTab
    src = inspect.getsource(SettingsTab._save_cfg)
    assert "except" in src and "showerror" in src, "存檔失敗必須跳錯誤給使用者"
    assert "load_config()" in src and "on_shown()" in src, \
        "失敗後應把畫面拉回磁碟真值"


def test_settings_save_failure_never_wipes_cfg_or_ledger():
    """[codex R2] ★存檔失敗的「復原」不可比原 bug 更糟：
    檔案仍被鎖住時,非嚴格 load_config() 回 {} → 若拿它蓋掉 self._cfg,呼叫端接著跑的
    _sync_ledger() 會用空名單呼叫 sync_members,把【可寫的】帳本餘額與歷史永久刪掉。
    另:只有 r/vs 兩棵成員樹,重載 "pgy" 會 KeyError,反而讓復原半途中斷。"""
    import inspect

    from cmuh_common.roster.ui.settings import SettingsTab
    src = inspect.getsource(SettingsTab._save_cfg)
    assert "assert_readable" in src, "只有嚴格可讀時才可重讀覆蓋 self._cfg"
    i_assert = src.index("assert_readable")
    i_assign = src.index("self._cfg = ")
    assert i_assert < i_assign, "assert_readable 必須在覆蓋 self._cfg 之前"
    assert "for scope in self._member_trees" in src, \
        "只能重載真的存在的成員樹（寫死 pgy 會 KeyError）"
    assert "return False" in src and src.rstrip().endswith("return True"), \
        "_save_cfg 必須回傳成功與否,讓呼叫端能停手"

    # 會動帳本的兩個呼叫端必須在存檔失敗時中止,不得走到 _sync_ledger
    # ★錨在【性質】上,不是在某個寫法上★(2026-08-19 RS-5):這兩處已改用
    #   `change_members_and_sync_ledger(mutator)`(名單+帳本在同一個臨界區內,
    #   而且 ids 由寫成功後重讀的 config 推導),舊錨 `if not self._save_cfg()`
    #   因此消失 —— 但要守的規則沒變:先寫成功,才可以動帳本;寫失敗要 return。
    for fn in (SettingsTab._member_add, SettingsTab._member_del):
        body = inspect.getsource(fn)
        i_write = body.index("change_members_and_sync_ledger(")
        i_sync = body.index("self._reload_ledger()")   # 寫入之後才重畫
        assert i_write < i_sync, f"{fn.__name__} 必須先確認存檔成功再同步帳本"
        seg = body[i_write:i_sync]
        assert "except Exception" in seg and "return" in seg, \
            f"{fn.__name__} 存檔失敗必須 return,不可繼續改帳本"
