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
def test_commit_failure_rolls_everything_back(tmp_path, monkeypatch):
    """★[2026-08-08 外審] 這個測試原本把缺陷釘成通過條件★

    它斷言 `e.written == [a]` 且 `a` 的內容【已經變成新值】—— 也就是把
    「半新半舊」當成預期結果。但這個函式的 docstring 一路寫著
    「要嘛都生效、要嘛都不生效」:宣稱與實作不符,而測試站在實作那一邊,
    於是這個不一致永遠不會被發現。
    設定停在半套,使用者只看到「存檔失敗」,不會知道 R 醫師已經換了、
    醫師清單還是舊的。
    現在:commit 中途失敗要把已經換掉的檔案【還原】。
    """
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
    # 回滾成功 → 對外的語意就是「一個檔都沒變」
    assert e.phase == "stage", f"回滾成功卻仍宣稱有檔案生效:{e.phase}"
    assert e.written == [], f"回滾之後不該有『已生效』:{e.written}"
    assert not os.path.exists(a), (
        "★第一個檔沒有被還原★ 設定停在半新半舊,而使用者只看到「存檔失敗」")
    assert not os.path.exists(b) and not os.path.exists(c)
    assert not [x for x in os.listdir(tmp_path) if x.endswith(".bak")], (
        "回滾用的備份沒有清掉")
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


def test_an_existing_file_is_restored_to_its_old_content(tmp_path, monkeypatch):
    """★回滾要還原【原本的內容】,不是刪掉★ 三個設定檔平常都已經存在。"""
    a, b, c = _paths(tmp_path)
    atomic_write_json_multi([(a, {"v": "old-a"}), (b, {"v": "old-b"}),
                             (c, {"v": "old-c"})])
    real_replace = atomic_io._replace_with_retry
    seen = {"n": 0}

    def _fail_second(src, dst):
        seen["n"] += 1
        if seen["n"] == 2:
            raise OSError("simulated replace failure")
        return real_replace(src, dst)

    monkeypatch.setattr(atomic_io, "_replace_with_retry", _fail_second)
    with pytest.raises(MultiWriteError):
        atomic_write_json_multi([(a, {"v": "new-a"}), (b, {"v": "new-b"}),
                                 (c, {"v": "new-c"})])
    monkeypatch.undo()
    assert safe_load_json(a, None) == {"v": "old-a"}, (
        "★第一個檔還是新值★ 設定停在半新半舊")
    assert safe_load_json(b, None) == {"v": "old-b"}
    assert safe_load_json(c, None) == {"v": "old-c"}
    assert not [x for x in os.listdir(tmp_path)
                if x.endswith(".bak") or x.endswith(".tmp")]


def test_a_backup_failure_aborts_before_any_replace(tmp_path, monkeypatch):
    """★[2026-08-08 外審第 2 回] 備份失敗不可以照樣 commit★

    上一版只記一行 warning 然後往下走。之後若真的需要回滾,
    `backups.get(target)` 回 None 會被誤讀成「這個檔原本不存在」→
    回滾把它【刪掉】—— 使用者的設定檔就這樣沒了,舊內容也沒備份。
    "查不到備份" 與 "原本沒有這個檔" 是兩件事,不可以共用同一個表示法。
    """
    a, b, c = _paths(tmp_path)
    atomic_write_json_multi([(a, {"v": "old-a"}), (b, {"v": "old-b"}),
                             (c, {"v": "old-c"})])
    import shutil as _sh
    monkeypatch.setattr(
        atomic_io.shutil, "copy2",
        lambda *args, **kw: (_ for _ in ()).throw(OSError("no backup")))
    with pytest.raises(MultiWriteError) as ei:
        atomic_write_json_multi([(a, {"v": "new-a"}), (b, {"v": "new-b"}),
                                 (c, {"v": "new-c"})])
    monkeypatch.undo()
    assert ei.value.phase == "stage", "備份失敗要在任何 replace 之前中止"
    assert safe_load_json(a, None) == {"v": "old-a"}
    assert safe_load_json(b, None) == {"v": "old-b"}
    assert safe_load_json(c, None) == {"v": "old-c"}
    assert _sh is not None


def test_an_interrupted_commit_is_recovered_on_startup(tmp_path):
    """★[2026-08-08 外審第 2 回] 行程被砍/斷電時 Python 的 rollback 不會跑★

    磁碟停在半新半舊,而且沒有任何人知道 —— 下次開機讀到的是一份
    「R 醫師已更新、醫師清單還是舊的」的設定。
    manifest 還在 = 上次的 commit 沒跑完 → 用 .bak 還原。
    """
    a, b, _c = _paths(tmp_path)
    atomic_write_json_multi([(a, {"v": "old-a"}), (b, {"v": "old-b"})])
    # 模擬「換掉第一個之後就斷電」:manifest 還在、a 是新值、a.bak 是舊值
    import json
    import shutil
    shutil.copy2(a, a + ".rollback.bak")
    shutil.copy2(b, b + ".rollback.bak")
    with open(a, "w", encoding="utf-8") as f:
        json.dump({"v": "new-a"}, f)
    with open(os.path.join(str(tmp_path), atomic_io._MANIFEST_NAME), "w",
              encoding="utf-8") as f:
        # ★manifest 也要記「交易前存不存在」★(生產的形狀)
        json.dump({"targets": [a, b], "existed": [a, b]}, f)

    n = atomic_io.recover_interrupted_multiwrite(str(tmp_path))
    assert n == 2, f"還原了 {n} 個檔"
    assert safe_load_json(a, None) == {"v": "old-a"}, "半套沒有被還原"
    assert not os.path.exists(
        os.path.join(str(tmp_path), atomic_io._MANIFEST_NAME))
    assert not [x for x in os.listdir(tmp_path) if x.endswith(".bak")]


def test_a_completed_commit_leaves_nothing_to_recover(tmp_path):
    """★反方向★ 正常完成的交易不可以在下次開機被「還原」掉。"""
    a, b, _c = _paths(tmp_path)
    atomic_write_json_multi([(a, {"v": 1}), (b, {"v": 2})])
    assert atomic_io.recover_interrupted_multiwrite(str(tmp_path)) == 0
    assert safe_load_json(a, None) == {"v": 1}


def test_the_settings_loader_runs_recovery():
    """★接線★ 沒人呼叫的話,半套狀態會一直留著
    (「有 API」不等於「會發生」)。"""
    import ast
    import inspect
    import textwrap

    from cmuh_common import app_settings
    src = textwrap.dedent(inspect.getsource(app_settings.load_threshold_settings))
    names = {n.func.id for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_recover_interrupted_settings_write" in names


def test_a_hard_kill_mid_commit_is_recovered_through_the_real_path(tmp_path):
    """★這個測試才真的量到 manifest★(突變驗證教的)

    上面那個 recovery 測試是【手工造出】manifest 與 .bak 的,所以把生產程式碼
    裡的 `_write_manifest` 拿掉,它照樣綠 —— 量到的只是 recovery 函式本身。
    這裡用「不被 `except Exception` 接住的中止」模擬行程被砍:
    交易走的是真正的生產路徑,manifest 也是它自己寫的。
    """
    a, b, _c = _paths(tmp_path)
    atomic_write_json_multi([(a, {"v": "old-a"}), (b, {"v": "old-b"})])
    real = atomic_io._replace_with_retry
    seen = {"n": 0}

    def _kill_after_first(src, dst):
        seen["n"] += 1
        if seen["n"] == 2:
            raise KeyboardInterrupt("simulated hard kill")   # 不是 Exception
        return real(src, dst)

    atomic_io._replace_with_retry = _kill_after_first
    try:
        with pytest.raises(KeyboardInterrupt):
            atomic_write_json_multi([(a, {"v": "new-a"}), (b, {"v": "new-b"})])
    finally:
        atomic_io._replace_with_retry = real

    assert os.path.exists(
        os.path.join(str(tmp_path), atomic_io._MANIFEST_NAME)), (
        "★沒有寫交易 manifest★ 行程被砍就沒有任何線索可以還原")
    assert safe_load_json(a, None) == {"v": "new-a"}, "前提:第一個檔已經換掉了"

    assert atomic_io.recover_interrupted_multiwrite(str(tmp_path)) >= 1
    assert safe_load_json(a, None) == {"v": "old-a"}, "半套沒有被還原"


def test_a_successful_commit_leaves_no_manifest_behind(tmp_path):
    """★M9★ 完成後沒撤掉 manifest 的話,下次開機會把一份【好好的】設定
    「還原」回舊值。"""
    a, b, _c = _paths(tmp_path)
    atomic_write_json_multi([(a, {"v": 1}), (b, {"v": 2})])
    assert not os.path.exists(
        os.path.join(str(tmp_path), atomic_io._MANIFEST_NAME)), (
        "★交易完成卻留著 manifest★ 下次開機會誤把新設定還原掉")


def test_a_manifest_write_failure_aborts_the_commit(tmp_path, monkeypatch):
    """★[2026-08-08 外審第 3 回]★ manifest 是整個復原機制的前提。
    它沒有確定落地就開始換檔案的話,之後 replace 中途終止就沒有有效的
    manifest —— 設定永遠停在半新半舊,而且沒有人知道。"""
    a, b, _c = _paths(tmp_path)
    atomic_write_json_multi([(a, {"v": "old-a"}), (b, {"v": "old-b"})])
    monkeypatch.setattr(
        atomic_io, "_write_manifest",
        lambda *args, **kw: (_ for _ in ()).throw(OSError("locked")))
    with pytest.raises(MultiWriteError) as ei:
        atomic_write_json_multi([(a, {"v": "new-a"}), (b, {"v": "new-b"})])
    monkeypatch.undo()
    assert ei.value.phase == "stage"
    assert safe_load_json(a, None) == {"v": "old-a"}, "manifest 寫不出還是換了檔"
    assert not [x for x in os.listdir(tmp_path)
                if x.endswith(".bak") or x.endswith(".tmp")]


def test_a_failed_restore_keeps_the_manifest_and_backups(tmp_path,
                                                         monkeypatch):
    """★[2026-08-08 外審第 3 回]★ 還原失敗還把 manifest 與備份刪掉的話,
    設定停在不一致,而下次重試需要的舊資料已經被自己刪光,永遠回不去。"""
    a, b, _c = _paths(tmp_path)
    atomic_write_json_multi([(a, {"v": "old-a"}), (b, {"v": "old-b"})])
    import json
    import shutil
    shutil.copy2(a, a + ".rollback.bak")
    with open(a, "w", encoding="utf-8") as f:
        json.dump({"v": "new-a"}, f)
    with open(os.path.join(str(tmp_path), atomic_io._MANIFEST_NAME), "w",
              encoding="utf-8") as f:
        json.dump({"targets": [a], "existed": [a]}, f)

    monkeypatch.setattr(
        atomic_io, "_replace_with_retry",
        lambda src, dst: (_ for _ in ()).throw(OSError("locked")))
    atomic_io.recover_interrupted_multiwrite(str(tmp_path))
    monkeypatch.undo()

    assert os.path.exists(a + ".rollback.bak"), (
        "★還原失敗卻把備份刪掉了★ 舊內容永久消失,下次也救不回來")
    assert os.path.exists(
        os.path.join(str(tmp_path), atomic_io._MANIFEST_NAME)), (
        "★還原失敗卻把交易紀錄刪掉了★ 下次開機不會再試")
    # 下一次(檔案鎖解除後)要能真的救回來
    assert atomic_io.recover_interrupted_multiwrite(str(tmp_path)) == 1
    assert safe_load_json(a, None) == {"v": "old-a"}


def test_recovery_undoes_files_that_did_not_exist_before(tmp_path):
    """★[2026-08-08 外審]★ 全新安裝第一次儲存時三個檔都還不存在,沒有 .bak。
    復原若只會「換回備份」,第一個【已經建出來】的檔就永遠留著,
    其他檔仍不存在 —— 交易永久停在部分生效。"""
    a, b, _c = _paths(tmp_path)
    real = atomic_io._replace_with_retry
    seen = {"n": 0}

    def _kill_after_first(src, dst):
        seen["n"] += 1
        if seen["n"] == 2:
            raise KeyboardInterrupt("simulated hard kill")
        return real(src, dst)

    atomic_io._replace_with_retry = _kill_after_first
    try:
        with pytest.raises(KeyboardInterrupt):
            atomic_write_json_multi([(a, {"v": 1}), (b, {"v": 2})])
    finally:
        atomic_io._replace_with_retry = real
    assert os.path.exists(a), "前提:第一個檔已經被建出來了"

    atomic_io.recover_interrupted_multiwrite(str(tmp_path))
    assert not os.path.exists(a), (
        "★交易前不存在的檔沒有被撤銷★ 設定永久停在部分生效")
    assert not os.path.exists(b)


def test_a_new_transaction_is_refused_while_a_manifest_remains(tmp_path):
    """★[2026-08-08 外審]★ 備份檔名是固定的。復原失敗後若讓新交易照跑,
    它會用【目前這份半新半舊的內容】覆寫那個備份 ——
    唯一一份完整的舊設定就此永久消失,而且再也復原不了。"""
    a, b, _c = _paths(tmp_path)
    atomic_write_json_multi([(a, {"v": "old"}), (b, {"v": "old"})])
    import json
    import shutil
    # ★要擋的是【還救得回來】的未完成交易★(第 2 回外審)
    #   只有 manifest、沒有備份的殘留代表「上次其實已完成」,那種擋下去
    #   會讓之後每一次存檔都被永久拒絕。所以這裡要造出真的有備份的狀態。
    shutil.copy2(a, a + ".rollback.bak")
    with open(os.path.join(str(tmp_path), atomic_io._MANIFEST_NAME), "w",
              encoding="utf-8") as f:
        json.dump({"targets": [a, b], "existed": [a, b]}, f)
    with pytest.raises(MultiWriteError) as ei:
        atomic_write_json_multi([(a, {"v": "new"}), (b, {"v": "new"})])
    assert ei.value.phase == "stage"
    assert safe_load_json(a, None) == {"v": "old"}
    assert not [x for x in os.listdir(tmp_path) if x.endswith(".tmp")]


def test_a_legacy_manifest_never_deletes_the_settings(tmp_path):
    """★[2026-08-08 外審第 2 回] 最危險的一個★

    舊實作寫的 manifest 只有 `targets`、沒有 `existed`。把「沒有這個欄位」
    讀成「全部都不存在」的話,復原會【刪掉每一個目標】——
    三個設定檔一起消失。「欄位不存在」與「空清單」是兩件事。
    """
    a, b, _c = _paths(tmp_path)
    atomic_write_json_multi([(a, {"v": "old-a"}), (b, {"v": "old-b"})])
    import json
    import shutil
    shutil.copy2(a, a + ".rollback.bak")          # 只有 a 有備份
    with open(os.path.join(str(tmp_path), atomic_io._MANIFEST_NAME), "w",
              encoding="utf-8") as f:
        json.dump({"targets": [a, b]}, f)          # ★舊格式:沒有 existed★

    atomic_io.recover_interrupted_multiwrite(str(tmp_path))
    assert os.path.exists(b), (
        "★舊格式的 manifest 讓復原刪掉了設定檔★ 使用者的設定就這樣沒了")
    assert safe_load_json(a, None) == {"v": "old-a"}


def test_a_successful_save_of_a_new_file_survives_a_stuck_manifest(tmp_path):
    """★[2026-08-08 外審第 3 回] 存檔回報成功,設定卻在重開之後消失★

    情境:這筆交易裡有「交易前不存在」的檔(全新安裝、或新增一個設定檔)。
    commit 全部成功、備份被刪掉,但 manifest 剛好刪不掉。
    那個新檔【現在存在】→ 上一版的復原判定「有東西可撤銷」→ 把一個剛剛
    才存好的設定檔刪掉。
    「這筆交易完成了」是一個事實,必須自己寫下來,不能從別的痕跡推。
    """
    a, b, _c = _paths(tmp_path)
    real_remove = atomic_io._remove_manifest

    def _stuck(path):
        return False                       # 模擬 manifest 刪不掉(被鎖住)

    atomic_io._remove_manifest = _stuck
    try:
        atomic_write_json_multi([(a, {"v": 1}), (b, {"v": 2})])   # 兩個都是新檔
    finally:
        atomic_io._remove_manifest = real_remove

    assert os.path.exists(
        os.path.join(str(tmp_path), atomic_io._MANIFEST_NAME)), "前提:紀錄還在"
    atomic_io.recover_interrupted_multiwrite(str(tmp_path))
    assert safe_load_json(a, None) == {"v": 1}, (
        "★存檔回報成功,重開之後設定檔被自己刪掉了★")
    assert safe_load_json(b, None) == {"v": 2}


def test_a_committed_manifest_does_not_block_the_next_save(tmp_path):
    """已標記完成的殘留紀錄不可以擋住之後的存檔。"""
    a, b, _c = _paths(tmp_path)
    real_remove = atomic_io._remove_manifest
    atomic_io._remove_manifest = lambda path: False
    try:
        atomic_write_json_multi([(a, {"v": 1}), (b, {"v": 2})])
    finally:
        atomic_io._remove_manifest = real_remove
    atomic_write_json_multi([(a, {"v": 3}), (b, {"v": 4})])
    assert safe_load_json(a, None) == {"v": 3}


def test_an_uncompletable_transaction_reports_failure_not_success(tmp_path):
    """★[2026-08-08 外審第 4 回] 假的成功比失敗嚴重★

    上一版兩條收尾都失敗時只記 error 然後正常返回。於是
    `save_all_settings` 照樣套用新的 live state、跳出「設定已儲存」——
    而開機時的復原會把設定還原到存檔前。使用者確認過的存檔在重開之後
    無聲消失;失敗他會再存一次,假成功他不會。
    """
    a, b, _c = _paths(tmp_path)
    atomic_write_json_multi([(a, {"v": "old"}), (b, {"v": "old"})])
    real_mark = atomic_io._mark_manifest_committed
    real_remove = atomic_io._remove_manifest
    atomic_io._mark_manifest_committed = lambda path: False
    atomic_io._remove_manifest = lambda path: False
    try:
        with pytest.raises(MultiWriteError) as ei:
            atomic_write_json_multi([(a, {"v": "new"}), (b, {"v": "new"})])
    finally:
        atomic_io._mark_manifest_committed = real_mark
        atomic_io._remove_manifest = real_remove
    assert ei.value.phase == "stage", "回滾成功 → 對外語意是「一個檔都沒變」"
    assert safe_load_json(a, None) == {"v": "old"}, "★沒有當場回滾★"
    assert safe_load_json(b, None) == {"v": "old"}
