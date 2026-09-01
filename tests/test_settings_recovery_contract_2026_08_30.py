# -*- coding: utf-8 -*-
"""[外審第二輪 R2-P2-01] 「部分恢復」被當成「恢復完畢」。

`recover_interrupted_multiwrite()` 在有檔案還原不了時做對了三件事(保留
manifest、保留備份、記 error),★但回傳型別仍然只是一個 int★ —— 於是
「三個檔全部還原」與「兩個還原、一個被防毒鎖住」對呼叫端長得一模一樣。

而呼叫端還有第二個問題:`_settings_recovery_done = True` 設在★真正嘗試之前★,
所以這個行程之後不再重試。

兩者合起來:
    上次存檔中途被砍(第一個檔已 replace)
    → 重開 → 還原 A 成功、B 被鎖住、C 成功
    → 呼叫端不知道 → 繼續載入
    → 整個 process 活在★半舊半新★的設定組合上,而且不再重試
—— 那正是 `atomic_write_json_multi` 花那麼多程式碼要避免的狀態。

★而且不可以只是「讓呼叫端知道」★:在半舊半新的狀態上再存一次新設定,會把
下次重試需要的備份與交易紀錄永久覆蓋掉 —— 把「還救得回來」變成「救不回來」。
所以存檔路徑要擋。★閘門必須有出口★:每次載入設定都會再試一次還原,
檔案不再被鎖住就自動恢復(這條有測試)。
"""
import ast
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import app_settings as aps  # noqa: E402
from cmuh_common import atomic_io as aio  # noqa: E402
from cmuh_common.atomic_io import (  # noqa: E402
    RecoveryResult, atomic_write_json_multi, recover_interrupted_multiwrite,
)


@pytest.fixture(autouse=True)
def _reset_recovery_state(monkeypatch):
    """模組層的「已處理」旗標會跨測試汙染。"""
    monkeypatch.setattr(aps, "_settings_recovery_done", False, raising=False)
    monkeypatch.setattr(aps, "_settings_recovery_state", None, raising=False)


def _interrupted_transaction(tmp_path):
    """做出「上次多檔交易被中止」的磁碟狀態:manifest 還在 + 有 .rollback.bak。"""
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text('{"v": "old"}', encoding="utf-8")
    b.write_text('{"v": "old"}', encoding="utf-8")
    # 用真的寫入路徑產生備份與 manifest,再讓它停在「commit 沒跑完」
    for p in (a, b):
        (tmp_path / (p.name + ".rollback.bak")).write_text(
            '{"v": "old"}', encoding="utf-8")
        p.write_text('{"v": "new"}', encoding="utf-8")
    (tmp_path / ".multiwrite.manifest.json").write_text(json.dumps({
        "targets": [str(a), str(b)],
        "existed": [str(a), str(b)],
        # ★"committed" 要是 False★:那個欄位的語意是「這筆交易自己說完成了」,
        #   寫 True 等於做出一個【不該撤銷】的狀態 —— 我第一版就是這樣,
        #   於是復原正確地回報「沒有東西可撤銷」,測試卻以為是缺陷。
        "committed": False,
    }), encoding="utf-8")
    return a, b


class TestTheResultSaysWhetherItFinished:
    def test_a_full_recovery_reports_complete(self, tmp_path):
        """★對照組★:全部還原成功 → complete,而且交易紀錄被清掉。"""
        a, b = _interrupted_transaction(tmp_path)
        res = recover_interrupted_multiwrite(str(tmp_path))
        assert res.complete and bool(res) is True, res
        assert res.restored == 2 and res.stuck == (), res
        assert a.read_text(encoding="utf-8") == '{"v": "old"}'
        assert not (tmp_path / ".multiwrite.manifest.json").exists()

    def test_a_partial_recovery_is_not_complete(self, tmp_path, monkeypatch):
        """★核心★:一個檔還原不了 → ★不可以★回報成功。
        ★反例只靠「有沒有卡住」分勝負★:另一個檔照樣還原成功。"""
        a, b = _interrupted_transaction(tmp_path)
        real = aio._replace_with_retry

        def _fake(src, dst):
            if str(dst).endswith("b.json"):
                raise OSError("模擬:被防毒鎖住")
            return real(src, dst)
        monkeypatch.setattr(aio, "_replace_with_retry", _fake)
        res = recover_interrupted_multiwrite(str(tmp_path))
        assert bool(res) is False, res
        assert res.restored == 1 and len(res.stuck) == 1, res
        assert a.read_text(encoding="utf-8") == '{"v": "old"}'   # 還原到的
        assert b.read_text(encoding="utf-8") == '{"v": "new"}'   # 卡住的
        # ★下次重試需要的東西必須留著★
        assert (tmp_path / ".multiwrite.manifest.json").exists()
        assert (tmp_path / "b.json.rollback.bak").exists()

    def test_the_truthiness_means_finished_not_count(self, tmp_path,
                                                     monkeypatch):
        """★真假值刻意是「有沒有完整完成」★:舊呼叫端寫 `if recover(...)`
        時拿到的是安全的那個語意,而不是「還原了幾個」。
        (部分還原時 restored=1 —— 若真假值是計數,這裡會是 True。)"""
        _a, _b = _interrupted_transaction(tmp_path)
        real = aio._replace_with_retry
        monkeypatch.setattr(
            aio, "_replace_with_retry",
            lambda s2, d: (_ for _ in ()).throw(OSError())
            if str(d).endswith("b.json") else real(s2, d))
        res = recover_interrupted_multiwrite(str(tmp_path))
        assert res.restored == 1 and bool(res) is False, res

    def test_nothing_to_do_is_complete(self, tmp_path):
        """沒有殘留交易 → complete(不可以把「本來就沒事」講成失敗)。"""
        res = recover_interrupted_multiwrite(str(tmp_path))
        assert bool(res) is True and res.restored == 0

    def test_an_unreadable_manifest_is_not_complete(self, tmp_path):
        """★讀不到 manifest = 不知道要撤銷什麼★,不是「沒事」。"""
        (tmp_path / ".multiwrite.manifest.json").write_text(
            "{壞掉的 JSON", encoding="utf-8")
        res = recover_interrupted_multiwrite(str(tmp_path))
        assert bool(res) is False and res.pending, res


class TestTheCallerOnlyMarksDoneWhenItReallyFinished:
    def test_an_incomplete_recovery_is_retried_next_time(self, tmp_path,
                                                         monkeypatch):
        """★出口★:沒完成就不記為已處理 —— 下次載入設定會再試一次,
        檔案不再被鎖住就自動恢復(不必重開程式也不會永久卡住)。"""
        monkeypatch.setattr(aps, "get_settings_dir", lambda: str(tmp_path),
                            raising=False)
        from cmuh_common import paths
        monkeypatch.setattr(paths, "get_settings_dir", lambda: str(tmp_path))
        _interrupted_transaction(tmp_path)
        real = aio._replace_with_retry
        calls = {"n": 0}

        def _fake(src, dst):
            calls["n"] += 1
            if str(dst).endswith("b.json") and calls["n"] < 3:
                raise OSError("模擬:第一次還被鎖著")
            return real(src, dst)
        monkeypatch.setattr(aio, "_replace_with_retry", _fake)
        assert aps.settings_recovery_incomplete() is not None   # 第一次:沒完成
        assert aps._settings_recovery_done is False, "★不可以記成已處理★"
        assert aps.settings_recovery_incomplete() is None       # 第二次:成功
        assert aps._settings_recovery_done is True

    def test_a_clean_start_is_not_reported_as_a_problem(self, tmp_path,
                                                        monkeypatch):
        """★不可矯枉過正★:沒有殘留交易時,閘門必須放行。"""
        from cmuh_common import paths
        monkeypatch.setattr(paths, "get_settings_dir", lambda: str(tmp_path))
        assert aps.settings_recovery_incomplete() is None


def test_the_settings_save_path_is_gated_by_the_recovery_state():
    """★沒有呼叫端的宣稱等於沒有宣稱★:存檔路徑必須在多檔寫入【之前】
    問過復原狀態 —— 否則新設定會蓋掉下次重試需要的備份,
    把「還救得回來」變成「救不回來」。"""
    p = os.path.join(os.path.dirname(__file__), "..", "src", "main.py")
    with open(p, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef)
               and any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                       and c.func.id == "_atomic_write_json_multi"
                       for c in ast.walk(n))), None)
    assert fn is not None, "找不到多檔存檔路徑(測試失效)"
    names = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    order = [c.func.id for c in names
             if c.func.id in ("_settings_recovery_incomplete",
                              "_atomic_write_json_multi")]
    assert order[:1] == ["_settings_recovery_incomplete"], (
        f"★復原狀態要在寫入之前問★:{order}")


def test_a_normal_multiwrite_still_works(tmp_path):
    """★整批改動不可以弄壞正常存檔★:沒有殘留交易時照常寫入並清乾淨。"""
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    atomic_write_json_multi([(str(a), {"v": 1}), (str(b), {"v": 2})])
    assert json.loads(a.read_text(encoding="utf-8")) == {"v": 1}
    assert json.loads(b.read_text(encoding="utf-8")) == {"v": 2}
    assert not (tmp_path / ".multiwrite.manifest.json").exists()
    assert isinstance(recover_interrupted_multiwrite(str(tmp_path)),
                      RecoveryResult)


def test_a_partial_recovery_can_still_finish_later(tmp_path, monkeypatch):
    """★閘門必須有出口★(寫測試時抓到的自造缺陷)

    復原原本是用 `os.replace(bak, target)` 把備份【移走】的。於是部分復原之後,
    ★已經還原成功的檔沒有備份了★ —— 下一輪它會被判成「原本存在但找不到備份 →
    無法還原」,永遠 stuck:交易永遠完不成,依賴它的存檔閘門永遠打不開。
    現在還原是複製(備份留到整筆成功才清),所以重試是冪等的。
    """
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    for f in (a, b):
        f.write_text('{"v": "new"}', encoding="utf-8")
        (tmp_path / (f.name + ".rollback.bak")).write_text(
            '{"v": "old"}', encoding="utf-8")
    (tmp_path / ".multiwrite.manifest.json").write_text(json.dumps({
        "targets": [str(a), str(b)], "existed": [str(a), str(b)],
        "committed": False}), encoding="utf-8")

    real = aio._replace_with_retry
    locked = {"on": True}

    def _fake(src, dst):
        if locked["on"] and str(dst).endswith("b.json"):
            raise OSError("模擬:b 被鎖住")
        return real(src, dst)
    monkeypatch.setattr(aio, "_replace_with_retry", _fake)

    first = recover_interrupted_multiwrite(str(tmp_path))
    assert bool(first) is False and first.restored == 1, first
    assert (tmp_path / "a.json.rollback.bak").exists(), (
        "★已還原的檔仍要保留備份★,否則下一輪會把它判成無法還原")

    locked["on"] = False                      # 鎖解除(防毒放手/使用者關掉程式)
    second = recover_interrupted_multiwrite(str(tmp_path))
    assert bool(second) is True, second       # ★出口真的存在★
    assert a.read_text(encoding="utf-8") == '{"v": "old"}'
    assert b.read_text(encoding="utf-8") == '{"v": "old"}'
    assert not (tmp_path / ".multiwrite.manifest.json").exists()
    assert not (tmp_path / "a.json.rollback.bak").exists(), "全部成功才清備份"


def test_a_legacy_manifest_recovers_idempotently_too(tmp_path, monkeypatch):
    """★舊格式的 manifest 也是生產形狀★(舊版寫的只有 `targets`,沒有
    `existed`)。同一條冪等性質在那條分支上也要成立 —— 否則升級後遇到
    舊交易的機器會卡在同一道沒有出口的閘門。
    """
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    for f in (a, b):
        f.write_text('{"v": "new"}', encoding="utf-8")
        (tmp_path / (f.name + ".rollback.bak")).write_text(
            '{"v": "old"}', encoding="utf-8")
    (tmp_path / ".multiwrite.manifest.json").write_text(
        json.dumps({"targets": [str(a), str(b)]}), encoding="utf-8")  # 無 existed

    real = aio._replace_with_retry
    locked = {"on": True}

    def _fake(src, dst):
        if locked["on"] and str(dst).endswith("b.json"):
            raise OSError("模擬:b 被鎖住")
        return real(src, dst)
    monkeypatch.setattr(aio, "_replace_with_retry", _fake)

    first = recover_interrupted_multiwrite(str(tmp_path))
    assert bool(first) is False and first.restored == 1, first
    assert (tmp_path / "a.json.rollback.bak").exists(), "★備份要留著★"
    locked["on"] = False
    assert bool(recover_interrupted_multiwrite(str(tmp_path))) is True
    assert a.read_text(encoding="utf-8") == '{"v": "old"}'
    assert b.read_text(encoding="utf-8") == '{"v": "old"}'


# ══ 外審 deep 第 1 輪:四條失敗路徑 ═══════════════════════════════════════
class TestTheIncompleteStateReachesTheRestOfTheProgram:
    def test_the_loader_reads_the_pre_transaction_snapshot(self, tmp_path,
                                                           monkeypatch):
        """★R2:只擋寫入是不夠的★ 磁碟上可能是半舊半新(A 已還原、B 還是新的),
        而載入端讀得到也讀得懂 —— 整個執行期就用著不一致的組合做臨床判斷。
        ★而「最後一致的快照」還在★:未撤銷的交易把 `.rollback.bak` 留著。
        所以載入要讀它,不是讀那份混合的磁碟內容。
        """
        from cmuh_common import paths
        monkeypatch.setattr(paths, "get_settings_dir", lambda: str(tmp_path))
        monkeypatch.setattr(aps, "get_conf_path",
                            lambda fn: str(tmp_path / fn))
        _interrupted_transaction(tmp_path)
        real = aio._replace_with_retry
        monkeypatch.setattr(
            aio, "_replace_with_retry",
            lambda s2, d: (_ for _ in ()).throw(OSError())
            if str(d).endswith("b.json") else real(s2, d))
        # b.json 磁碟上是【新的】(還原不了),但備份是交易前的舊內容
        assert (tmp_path / "b.json").read_text(encoding="utf-8") == '{"v": "new"}'
        assert aps._path(None, "b.json").endswith(".rollback.bak"), (
            "★載入端仍指向半舊半新的那份磁碟內容★")

    def test_a_successful_read_cannot_wash_the_protection_away(self, tmp_path,
                                                               monkeypatch):
        """★標記不可以放在會被沖掉的地方★(外審 deep R2)

        我第一版把 stuck 的檔丟進 `_LOAD_FAILED_FILES` —— 但那個集合的語意是
        「這次讀不到」,而這些檔★讀得到★:下一次成功載入就 `discard` 掉它,
        標記自我消滅。真正的保護要撐得過一次成功讀取。
        """
        from cmuh_common import paths
        monkeypatch.setattr(paths, "get_settings_dir", lambda: str(tmp_path))
        monkeypatch.setattr(aps, "get_conf_path",
                            lambda fn: str(tmp_path / fn))
        _interrupted_transaction(tmp_path)
        real = aio._replace_with_retry
        monkeypatch.setattr(
            aio, "_replace_with_retry",
            lambda s2, d: (_ for _ in ()).throw(OSError())
            if str(d).endswith("b.json") else real(s2, d))
        assert aps.settings_recovery_incomplete() is not None
        aps._note_load_status("b.json", "ok")        # 一次成功讀取
        assert aps.settings_recovery_incomplete() is not None, (
            "★一次成功讀取就把保護沖掉了★")
        assert aps._path(None, "b.json").endswith(".rollback.bak")

    def test_a_stale_backup_alone_does_not_redirect(self, tmp_path,
                                                    monkeypatch):
        """★不可以恆轉向★:沒有未撤銷的交易時(只是留著一個舊備份),
        載入必須讀真正的設定檔 —— 否則使用者的新設定會被無聲忽略。"""
        from cmuh_common import paths
        monkeypatch.setattr(paths, "get_settings_dir", lambda: str(tmp_path))
        monkeypatch.setattr(aps, "get_conf_path", lambda fn: str(tmp_path / fn))
        (tmp_path / "b.json").write_text('{"v": "current"}', encoding="utf-8")
        (tmp_path / "b.json.rollback.bak").write_text('{"v": "ancient"}',
                                                      encoding="utf-8")
        assert aps.settings_recovery_incomplete() is None      # 沒有交易
        assert aps._path(None, "b.json") == str(tmp_path / "b.json")

    def test_a_file_created_by_the_transaction_reads_as_absent(
            self, tmp_path, monkeypatch):
        """★生產形狀:全新安裝第一次多檔存檔被中止★(外審 deep R3)

        契約上「交易前不存在」的目標★本來就沒有備份★。我第一版一律退回
        live 檔,而且★用測試把它釘成正確答案★ —— 那等於載入一份沒有 commit
        成功的新值,同批其他檔卻用預設值,還是一組混合快照。
        交易前的一致狀態就是「這個檔不存在」,所以要讓載入端看到「不存在」。
        """
        from cmuh_common import paths
        monkeypatch.setattr(paths, "get_settings_dir", lambda: str(tmp_path))
        monkeypatch.setattr(aps, "get_conf_path", lambda fn: str(tmp_path / fn))
        a, b = tmp_path / "a.json", tmp_path / "b.json"
        for f in (a, b):                       # 交易【新建】的檔:沒有備份
            f.write_text('{"v": "uncommitted"}', encoding="utf-8")
        (tmp_path / ".multiwrite.manifest.json").write_text(json.dumps({
            "targets": [str(a), str(b)], "existed": [], "committed": False}),
            encoding="utf-8")
        monkeypatch.setattr(
            aio, "_remove_with_retry",
            lambda _p: (_ for _ in ()).throw(OSError("模擬:刪不掉")))
        assert aps.settings_recovery_incomplete() is not None
        resolved = aps._path(None, "b.json")
        assert not os.path.exists(resolved), (
            f"★載入端讀到了沒有 commit 成功的新值★:{resolved}")

    def test_a_pre_existing_file_without_backup_is_flagged_not_silently_used(
            self, tmp_path, monkeypatch, caplog):
        """★交易前存在、備份卻不見了 → 無法證明這一份是哪一版★
        仍然照讀(把設定整個換成預設會直接毀掉使用者的門檻/收件人),
        但必須★明講★,而且寫入端的閘門仍然關著。"""
        from cmuh_common import paths
        monkeypatch.setattr(paths, "get_settings_dir", lambda: str(tmp_path))
        monkeypatch.setattr(aps, "get_conf_path", lambda fn: str(tmp_path / fn))
        _interrupted_transaction(tmp_path)
        real = aio._replace_with_retry
        monkeypatch.setattr(
            aio, "_replace_with_retry",
            lambda s2, d: (_ for _ in ()).throw(OSError())
            if str(d).endswith("b.json") else real(s2, d))
        assert aps.settings_recovery_incomplete() is not None
        os.remove(tmp_path / "b.json.rollback.bak")
        with caplog.at_level("ERROR"):
            assert aps._path(None, "b.json") == str(tmp_path / "b.json")
        assert any("無法證明" in r.getMessage() for r in caplog.records), (
            caplog.text)
        assert aps.settings_recovery_incomplete() is not None, "閘門要仍然關著"

    def test_a_legacy_manifest_never_infers_the_file_was_created(
            self, tmp_path, monkeypatch):
        """★舊格式的 manifest 沒有 `existed` 欄位 → 不可以推斷★

        把「沒有這個欄位」讀成「空集合」的話,舊交易的每個目標都會被當成
        【交易新建的檔】→ 載入端看到「不存在」→ ★整份設定變成預設值★。
        那正是這個 repo 反覆修過的「讀檔失敗被當成沒有資料」的同一形狀,
        只是換成 metadata 的版本。
        """
        from cmuh_common import paths
        monkeypatch.setattr(paths, "get_settings_dir", lambda: str(tmp_path))
        monkeypatch.setattr(aps, "get_conf_path", lambda fn: str(tmp_path / fn))
        a, b = tmp_path / "a.json", tmp_path / "b.json"
        for f in (a, b):
            f.write_text('{"v": "new"}', encoding="utf-8")
        # a 有備份(還原會失敗 → stuck,交易因此未完成);b 沒有備份
        (tmp_path / "a.json.rollback.bak").write_text('{"v": "old"}',
                                                      encoding="utf-8")
        (tmp_path / ".multiwrite.manifest.json").write_text(
            json.dumps({"targets": [str(a), str(b)]}), encoding="utf-8")
        monkeypatch.setattr(
            aio, "_replace_with_retry",
            lambda _s, _d: (_ for _ in ()).throw(OSError("模擬:鎖住")))
        assert aps.settings_recovery_incomplete() is not None
        assert aps._path(None, "b.json") == str(tmp_path / "b.json"), (
            "★舊格式被推斷成「交易新建」→ 設定會整份變預設值★")

    def test_a_new_transaction_in_the_same_process_is_noticed(self, tmp_path,
                                                              monkeypatch):
        """★R1-2 不可以永久快取「沒有交易」★:乾淨啟動記成已處理之後,
        ★同一個行程裡★後來的多檔寫入若留下未撤銷的交易,閘門仍然要看得到 ——
        否則只能靠重開程式,與「每次載入都重試」的契約不符。"""
        from cmuh_common import paths
        monkeypatch.setattr(paths, "get_settings_dir", lambda: str(tmp_path))
        assert aps.settings_recovery_incomplete() is None      # 乾淨 → 記為已處理
        assert aps._settings_recovery_done is True
        _interrupted_transaction(tmp_path)                     # 之後才出現的交易
        real = aio._replace_with_retry
        monkeypatch.setattr(
            aio, "_replace_with_retry",
            lambda s2, d: (_ for _ in ()).throw(OSError())
            if str(d).endswith("b.json") else real(s2, d))
        assert aps.settings_recovery_incomplete() is not None, (
            "★舊快取讓新的未撤銷交易看不見★")

    def test_an_unreadable_probe_does_not_reuse_the_cached_answer(
            self, tmp_path, monkeypatch):
        """★「看不出來」不可以沿用「沒事」的舊答案★:探測 manifest 在不在
        本身失敗時(ACL / 暫時 IO),必須重跑復原,而不是靠快取直接放行 ——
        否則同一個行程裡新出現的未撤銷交易會被漏掉。"""
        from cmuh_common import paths
        monkeypatch.setattr(paths, "get_settings_dir", lambda: str(tmp_path))
        assert aps.settings_recovery_incomplete() is None      # 乾淨 → 快取
        _interrupted_transaction(tmp_path)
        real_exists = os.path.exists

        def _boom(path):
            if str(path).endswith(".multiwrite.manifest.json"):
                raise OSError("模擬:連探測都失敗")
            return real_exists(path)
        monkeypatch.setattr(aps.os.path, "exists", _boom)
        real = aio._replace_with_retry
        monkeypatch.setattr(
            aio, "_replace_with_retry",
            lambda s2, d: (_ for _ in ()).throw(OSError())
            if str(d).endswith("b.json") else real(s2, d))
        assert aps.settings_recovery_incomplete() is not None, (
            "★探測失敗卻沿用了『沒事』的快取★")

    def test_the_gate_clears_once_the_situation_is_fixed(self, tmp_path,
                                                         monkeypatch):
        """★出口★:復原嘗試拋錯之後把狀態記成未完成是對的,但★也要解除
        已處理旗標★ —— 否則狀況恢復(交易紀錄已經不在了)之後,快取會讓
        它直接早退,那個「未完成」永遠清不掉,閘門從此打不開。
        """
        from cmuh_common import paths
        monkeypatch.setattr(paths, "get_settings_dir", lambda: str(tmp_path))
        # ★前提要有一次乾淨啟動★:旗標得先是 True,否則「有沒有解除它」
        #   在這條路徑上不會有任何差別(我第一版就是這樣,突變量不到)。
        assert aps.settings_recovery_incomplete() is None
        assert aps._settings_recovery_done is True
        real_exists = os.path.exists

        def _boom(path):
            if str(path).endswith(".multiwrite.manifest.json"):
                raise OSError("模擬:探測失敗")
            return real_exists(path)
        monkeypatch.setattr(aps.os.path, "exists", _boom)
        assert aps.settings_recovery_incomplete() is not None   # 先卡住
        # ★不可以用 monkeypatch.undo()★:它會撤銷【這個測試裡的所有】patch,
        #   包含 autouse fixture 設的那兩個狀態變數 —— 等於把受測狀態本身
        #   重置掉,測試於是恆綠(突變量不到)。只還原我們要還原的那一個。
        monkeypatch.setattr(aps.os.path, "exists", real_exists)
        assert aps.settings_recovery_incomplete() is None, (
            "★狀況已恢復,閘門卻打不開(沒有出口)★")


def test_the_restore_defaults_path_is_gated_too():
    """★R1-3★ 還原預設是單檔寫入,原本完全繞過閘門:未撤銷的交易還在時去覆蓋
    那些檔,之後的復原會把剛寫進去的預設值再撤銷掉,而 UI 已經回報「已還原」
    —— 一個假的成功。它也要在★寫任何檔案之前★問同一道閘門。"""
    p = os.path.join(os.path.dirname(__file__), "..", "src", "main.py")
    with open(p, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef)
               and n.name == "_restore_settings_defaults"
               and any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                       and c.func.id == "_restore_settings_defaults"
                       for c in ast.walk(n))), None)
    assert fn is not None, "找不到還原預設路徑(測試失效)"
    order = [c.func.id for c in ast.walk(fn)
             if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
             and c.func.id in ("_settings_recovery_incomplete",
                               "_restore_settings_defaults")]
    assert order[:1] == ["_settings_recovery_incomplete"], (
        f"★復原狀態要在寫入之前問★:{order}")


def test_a_failed_backup_open_does_not_leak_the_temp_file(tmp_path,
                                                          monkeypatch):
    """★R1-4★ `mkstemp()` 成功但開啟備份失敗時,原本的寫法讓 fd 永遠不會被關
    (`os.fdopen(fd)` 還沒執行),Windows 上那個暫存檔因此也刪不掉 ——
    反覆重試會累積 handle 與 `.restore-*` 殘檔。"""
    a = tmp_path / "a.json"
    a.write_text('{"v": "new"}', encoding="utf-8")
    bak = tmp_path / "a.json.rollback.bak"
    bak.write_text('{"v": "old"}', encoding="utf-8")
    real_open = open

    def _fake_open(path, mode="r", *args, **kw):
        if str(path).endswith(".rollback.bak"):
            raise OSError("模擬:備份被共用鎖擋住")
        return real_open(path, mode, *args, **kw)
    monkeypatch.setattr("builtins.open", _fake_open)
    with pytest.raises(OSError):
        aio._restore_from_backup(str(bak), str(a))
    monkeypatch.undo()
    leftovers = [f for f in os.listdir(tmp_path) if ".restore-" in f]
    assert leftovers == [], f"★暫存檔沒有被清掉(fd 還開著)★:{leftovers}"


# ══ B 案(使用者定案 2026-08-31):無法證明版本 → 停用止掛提醒 ═══════════
class TestUnprovableSettingsSuspendStopAlerts:
    def _unprovable_state(self, tmp_path, monkeypatch):
        """交易前存在、備份遺失 → live 檔無法證明是哪一版。"""
        from cmuh_common import paths
        monkeypatch.setattr(paths, "get_settings_dir", lambda: str(tmp_path))
        _interrupted_transaction(tmp_path)
        os.remove(tmp_path / "b.json.rollback.bak")
        real = aio._replace_with_retry
        monkeypatch.setattr(
            aio, "_replace_with_retry",
            lambda s2, d: (_ for _ in ()).throw(OSError())
            if str(d).endswith("a.json") else real(s2, d))

    def test_the_unprovable_file_is_named(self, tmp_path, monkeypatch):
        self._unprovable_state(tmp_path, monkeypatch)
        assert aps.unprovable_settings() == ("b.json",)

    def test_a_restorable_stuck_file_is_not_unprovable(self, tmp_path,
                                                       monkeypatch):
        """★分得開兩種卡住★:備份還在、只是 replace 失敗 → 可證明
        (載入讀 .bak),不停提醒。"""
        from cmuh_common import paths
        monkeypatch.setattr(paths, "get_settings_dir", lambda: str(tmp_path))
        _interrupted_transaction(tmp_path)
        real = aio._replace_with_retry
        monkeypatch.setattr(
            aio, "_replace_with_retry",
            lambda s2, d: (_ for _ in ()).throw(OSError())
            if str(d).endswith("b.json") else real(s2, d))
        assert aps.settings_recovery_incomplete() is not None   # 有卡住
        assert aps.unprovable_settings() == ()                  # 但可證明

    def test_the_exit_clears_automatically(self, tmp_path, monkeypatch):
        """★出口★:備份回來 → 下一次還原成功 → 清單自動回空。"""
        self._unprovable_state(tmp_path, monkeypatch)
        assert aps.unprovable_settings() != ()
        # 狀況解除:樁全部撤掉(undo 也會清 autouse 的旗標,重設它們)
        monkeypatch.undo()
        from cmuh_common import paths
        monkeypatch.setattr(paths, "get_settings_dir", lambda: str(tmp_path))
        monkeypatch.setattr(aps, "_settings_recovery_done", False,
                            raising=False)
        monkeypatch.setattr(aps, "_settings_recovery_state", None,
                            raising=False)
        (tmp_path / "b.json.rollback.bak").write_text('{"v": "old"}',
                                                      encoding="utf-8")
        assert aps.unprovable_settings() == ()

    def test_a_clean_dir_reports_nothing(self, tmp_path, monkeypatch):
        from cmuh_common import paths
        monkeypatch.setattr(paths, "get_settings_dir", lambda: str(tmp_path))
        assert aps.unprovable_settings() == ()

    def test_a_corrupt_manifest_is_not_reported_as_clean(self, tmp_path,
                                                         monkeypatch):
        """★查不出來不可以說成「沒有」★(閘門會因此放行)。

        ★用生產的失敗形狀★(外審 R5-2 點名):第一版 monkeypatch 掉
        `_pending_tx_info` —— 但生產的失敗是【manifest 檔壞掉/讀不到】,
        而那個 helper 自己把例外吞成 None(=「確定沒有」),被繞過的正是
        要測的那一層。改成直接把壞檔寫上磁碟。"""
        from cmuh_common import paths
        monkeypatch.setattr(paths, "get_settings_dir", lambda: str(tmp_path))
        (tmp_path / ".multiwrite.manifest.json").write_text(
            "{壞掉的 JSON", encoding="utf-8")
        assert aps.unprovable_settings() != (), (
            "★manifest 壞掉被當成『沒有 pending 交易』,閘門會放行★")

    def test_all_backups_quarantined_keeps_the_manifest_and_suspends(
            self, tmp_path, monkeypatch):
        """★R5-1★ 未 commit 的交易、existed 目標的備份【全部】被外力拿走
        (防毒整批隔離 .bak):原本被判成「上次已完成」→ manifest 被刪 →
        B 案閘門從此查不到、寫入閘門也開了。正解:保留 manifest、
        列為 stuck、止掛暫停。"""
        from cmuh_common import paths
        monkeypatch.setattr(paths, "get_settings_dir", lambda: str(tmp_path))
        _interrupted_transaction(tmp_path)
        os.remove(tmp_path / "a.json.rollback.bak")
        os.remove(tmp_path / "b.json.rollback.bak")     # ★全部★不見
        res = recover_interrupted_multiwrite(str(tmp_path))
        assert bool(res) is False and len(res.stuck) == 2, res
        assert (tmp_path / ".multiwrite.manifest.json").exists(), (
            "★manifest 被當成已完成而刪掉了★")
        assert aps.unprovable_settings() == ("a.json", "b.json")

    def test_a_committed_residue_does_not_suspend_alerts(self, tmp_path,
                                                         monkeypatch):
        """★R5-3★ commit 成功、備份已合法刪除、只剩 manifest 刪不掉:
        那不是 pending 交易 —— 判成無法證明會把止掛提醒★無限期★停掉。"""
        from cmuh_common import paths
        monkeypatch.setattr(paths, "get_settings_dir", lambda: str(tmp_path))
        for n in ("a.json", "b.json"):
            (tmp_path / n).write_text('{"v": "committed"}', encoding="utf-8")
        (tmp_path / ".multiwrite.manifest.json").write_text(json.dumps({
            "targets": [str(tmp_path / "a.json"), str(tmp_path / "b.json")],
            "existed": [str(tmp_path / "a.json"), str(tmp_path / "b.json")],
            "committed": True}), encoding="utf-8")
        # ★前提:manifest 刪不掉★ —— 不擋刪除的話,復原的清理路徑會順手把
        #   它清掉,探測根本看不到 committed 殘留(我第一版就是這樣,
        #   突變假綠燈:判準拆掉也量不到)。
        real_rm = aio._remove_with_retry
        monkeypatch.setattr(
            aio, "_remove_with_retry",
            lambda p2: (_ for _ in ()).throw(OSError("模擬:刪除被拒"))
            if str(p2).endswith(".multiwrite.manifest.json") else real_rm(p2))
        assert aps.unprovable_settings() == (), (
            "★合法的 committed 殘留把止掛提醒無限期停掉了★")
        assert (tmp_path / ".multiwrite.manifest.json").exists(), "前提:殘留仍在"

    def test_a_committed_residue_recovers_as_complete(self, tmp_path,
                                                      monkeypatch):
        """★R5-1 的邊界★:committed 殘留 + 備份合法不見 → 復原判【已完成】
        並清掉 manifest —— 「備份全失=保留」只適用未 commit 的交易,
        commit 過的備份本來就該不見(不可矯枉過正,否則合法殘留永遠清不掉)。"""
        from cmuh_common import paths
        monkeypatch.setattr(paths, "get_settings_dir", lambda: str(tmp_path))
        for n in ("a.json", "b.json"):
            (tmp_path / n).write_text('{"v": "committed"}', encoding="utf-8")
        (tmp_path / ".multiwrite.manifest.json").write_text(json.dumps({
            "targets": [str(tmp_path / "a.json"), str(tmp_path / "b.json")],
            "existed": [str(tmp_path / "a.json"), str(tmp_path / "b.json")],
            "committed": True}), encoding="utf-8")
        res = recover_interrupted_multiwrite(str(tmp_path))
        assert bool(res) is True, res
        assert not (tmp_path / ".multiwrite.manifest.json").exists()

    def test_both_alert_paths_consult_the_gate(self):
        """★兩條止掛路徑都要問閘門★(外審第 2 輪在 transport_note 上抓過
        「只標了一條路徑」的形狀 —— 這裡直接把兩條都釘住)。"""
        p = os.path.join(os.path.dirname(__file__), "..", "src", "main.py")
        with open(p, encoding="utf-8") as f:
            src = f.read()
        tree = ast.parse(src)
        hits = []
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef):
                continue
            if any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                   and c.func.id == "_stop_alerts_suspended_reason"
                   for c in ast.walk(fn)):
                hits.append(fn.name)
        assert "_update_grid_data" in hits, hits          # 本週行事曆路徑
        assert "_dispatch_future_stop_alert" in hits, hits  # 遠期掃描路徑

    def test_the_future_path_releases_the_claim(self):
        """★擋下來也要釋放寄送權★:nk 的 claim 在呼叫端已取得,靜靜 return
        會讓該診次卡在 in-flight —— 狀態解除後也永遠寄不出來(沒有出口)。"""
        p = os.path.join(os.path.dirname(__file__), "..", "src", "main.py")
        with open(p, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "_dispatch_future_stop_alert")
        # 閘門的 if 區塊裡必須呼叫 _release_alert_email_claim
        for node in ast.walk(fn):
            if (isinstance(node, ast.If)
                    and any(isinstance(c, ast.Call)
                            and isinstance(c.func, ast.Name)
                            and c.func.id == "_stop_alerts_suspended_reason"
                            for c in ast.walk(node.test))):
                assert any(isinstance(c, ast.Call)
                           and isinstance(c.func, ast.Attribute)
                           and c.func.attr == "_release_alert_email_claim"
                           for c in ast.walk(node)), "★閘門沒釋放寄送權★"
                break
        else:
            raise AssertionError("找不到遠期路徑的閘門")


class TestR6TerminalStateBeforeDroppingBackups:
    """[外審第二輪 R6/P1] 回滾成功後,★終態要先落地才能刪備份★。

    我在 R5-1 加的隔離守衛,判準是「未 commit + existed 備份全不見」。
    但活體回滾的清理順序是【先刪備份、再 best-effort 刪 manifest】——
    manifest 剛好被鎖住的話,磁碟上留下的正是那個形狀,守衛把一筆
    ★已經完整回滾★的交易永久判成 stuck(存檔全擋+止掛無限期暫停)。
    上一輪的修法自己造出它要防的災難(a-fix-must-be-judged-in-combination)。

    正解(外審原文):rollback 成功後,必須先持久化「已回滾」終態
    (`rolled_back`)或成功移除 manifest,才能清除最後的備份;
    若終態無法落盤,應保留可★冪等重試的完整備份★。
    「完整」是關鍵:活體回滾原本是搬移式(os.replace 消耗備份),
    留下的備份組天生不完整 → 一併改成與開機復原同一支複製式還原。
    """

    def _two_old_files(self, tmp_path):
        a, b = tmp_path / "a.json", tmp_path / "b.json"
        a.write_text('{"v": "old"}', encoding="utf-8")
        b.write_text('{"v": "old"}', encoding="utf-8")
        return a, b

    def _fail_commit_on_b(self, mp):
        """讓 commit 在換 b.json 時失敗(生產形狀:目標檔被鎖/IO 錯)。

        只攔【換上 b.json】那一次;備份、tmp、回滾用的 replace 全走真的。
        """
        real = aio._replace_with_retry
        mp.setattr(
            aio, "_replace_with_retry",
            lambda s, d: (_ for _ in ()).throw(OSError("locked"))
            if str(d).endswith("b.json") else real(s, d))

    def test_a_wedged_manifest_gets_a_durable_rolled_back_marker(
            self, tmp_path, monkeypatch):
        """manifest 刪不掉 → 寫入 rolled_back 終態,備份才可以刪;
        下次啟動把它認成已了結(不是 R5-1 的隔離形狀)。"""
        a, b = self._two_old_files(tmp_path)
        with pytest.MonkeyPatch.context() as mp:
            self._fail_commit_on_b(mp)
            mp.setattr(aio, "_remove_manifest", lambda p: False)
            with pytest.raises(aio.MultiWriteError) as ei:
                atomic_write_json_multi([(str(a), {"v": "new"}),
                                         (str(b), {"v": "new"})])
            assert ei.value.phase == "stage", "回滾完整,錯誤應說『設定未變更』"
        # 磁碟終態:兩檔都是舊內容、備份已清、manifest 帶 rolled_back 標記
        assert json.loads(a.read_text(encoding="utf-8")) == {"v": "old"}
        assert json.loads(b.read_text(encoding="utf-8")) == {"v": "old"}
        assert not os.path.exists(str(a) + ".rollback.bak")
        assert not os.path.exists(str(b) + ".rollback.bak")
        mpath = tmp_path / ".multiwrite.manifest.json"
        m = json.loads(mpath.read_text(encoding="utf-8"))
        assert m.get("rolled_back") is True, m
        # 下次啟動:認成終態 → complete、殘留 manifest 清掉,不是 stuck
        res = recover_interrupted_multiwrite(str(tmp_path))
        assert bool(res) is True and res.stuck == (), res
        assert not mpath.exists()
        # B 案閘門也不會啟動
        from cmuh_common import paths
        monkeypatch.setattr(paths, "get_settings_dir", lambda: str(tmp_path))
        assert aps.unprovable_settings() == ()

    def test_when_no_terminal_state_lands_the_backup_set_stays_complete(
            self, tmp_path):
        """★連終態都寫不成 → 整套留著,而且備份組是【完整】的★。

        搬移式回滾在這裡留下的備份組缺了已回滾的那幾個檔 → 下次復原把
        它們判成「existed 卻沒備份 → stuck」,又是一道永久假警報。
        複製式回滾後備份齊全,復原冪等重試 → 自動了結。"""
        a, b = self._two_old_files(tmp_path)
        with pytest.MonkeyPatch.context() as mp:
            self._fail_commit_on_b(mp)
            mp.setattr(aio, "_remove_manifest", lambda p: False)
            mp.setattr(aio, "_mark_manifest_rolled_back", lambda p: False)
            with pytest.raises(aio.MultiWriteError):
                atomic_write_json_multi([(str(a), {"v": "new"}),
                                         (str(b), {"v": "new"})])
        # a 已被活體回滾(replace 過才回滾)—— 它的備份必須★還在★
        assert os.path.exists(str(a) + ".rollback.bak"), (
            "★備份被搬移式回滾消耗掉了 → fallback 的備份組不完整★")
        assert os.path.exists(str(b) + ".rollback.bak")
        assert (tmp_path / ".multiwrite.manifest.json").exists()
        assert json.loads(a.read_text(encoding="utf-8")) == {"v": "old"}
        # 下次啟動:冪等重試 → 完整了結,不留任何殘骸
        res = recover_interrupted_multiwrite(str(tmp_path))
        assert bool(res) is True and res.stuck == (), res
        assert json.loads(a.read_text(encoding="utf-8")) == {"v": "old"}
        assert json.loads(b.read_text(encoding="utf-8")) == {"v": "old"}
        assert not (tmp_path / ".multiwrite.manifest.json").exists()
        assert not os.path.exists(str(a) + ".rollback.bak")
        assert not os.path.exists(str(b) + ".rollback.bak")

    def test_the_write_path_probe_must_not_delete_the_quarantine_shape(
            self, tmp_path):
        """★R5-1 的第二道門★:寫入路徑的殘留清理看到同一個隔離形狀
        (未 commit + existed 備份全不見)也會把 manifest 當「上次已完成」
        刪掉 —— 從寫入路徑把 B 案的證據銷毀,然後放行覆寫。"""
        a, b = _interrupted_transaction(tmp_path)
        os.remove(str(a) + ".rollback.bak")
        os.remove(str(b) + ".rollback.bak")     # ★全部★被外力拿走
        mpath = str(tmp_path / ".multiwrite.manifest.json")
        assert aio._manifest_is_recoverable(mpath) is True, (
            "★隔離形狀被寫入路徑判成殘留★")
        assert os.path.exists(mpath), "★證據 manifest 被寫入路徑刪掉了★"
        # 而且整筆新存檔被擋下、什麼都沒動
        with pytest.raises(aio.MultiWriteError):
            atomic_write_json_multi([(str(a), {"v": "clobber"}),
                                     (str(b), {"v": "clobber"})])
        assert json.loads(a.read_text(encoding="utf-8")) == {"v": "new"}
        assert os.path.exists(mpath)

    def test_a_rolled_back_residue_does_not_block_the_write_path(
            self, tmp_path):
        """終態標記的另一半:寫入路徑要認得 rolled_back=已了結,
        清掉殘留後放行 —— 否則標記反而把存檔永久擋住。"""
        a, b = self._two_old_files(tmp_path)
        (tmp_path / ".multiwrite.manifest.json").write_text(json.dumps({
            "targets": [str(a), str(b)], "existed": [str(a), str(b)],
            "committed": False, "rolled_back": True,
        }), encoding="utf-8")
        atomic_write_json_multi([(str(a), {"v": "new"}),
                                 (str(b), {"v": "new"})])
        assert json.loads(a.read_text(encoding="utf-8")) == {"v": "new"}
        assert not (tmp_path / ".multiwrite.manifest.json").exists()

    def test_the_commit_teardown_rollback_follows_the_same_contract(
            self, tmp_path):
        """★同一個 P1 的第二個實例★:commit 全部成功、mark_committed 與
        remove_manifest 都失敗 → 當場回滾那條收尾,原本也是先刪備份再刪
        (已知刪不掉的)manifest —— 自產隔離形狀。外審點名的舊測試
        (`test_an_uncompletable_transaction_reports_failure_not_success`)
        走的正是這條路,只是沒接著跑 recovery。"""
        a, b = self._two_old_files(tmp_path)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(aio, "_mark_manifest_committed", lambda p: False)
            mp.setattr(aio, "_remove_manifest", lambda p: False)
            mp.setattr(aio, "_mark_manifest_rolled_back", lambda p: False)
            with pytest.raises(aio.MultiWriteError) as ei:
                atomic_write_json_multi([(str(a), {"v": "new"}),
                                         (str(b), {"v": "new"})])
            assert ei.value.phase == "stage"
        # 備份組必須★完整★留著(兩檔都被 replace 過又都被回滾)
        assert os.path.exists(str(a) + ".rollback.bak")
        assert os.path.exists(str(b) + ".rollback.bak")
        assert (tmp_path / ".multiwrite.manifest.json").exists()
        # 下次啟動:冪等重試 → 了結
        res = recover_interrupted_multiwrite(str(tmp_path))
        assert bool(res) is True and res.stuck == (), res
        assert json.loads(a.read_text(encoding="utf-8")) == {"v": "old"}
        assert json.loads(b.read_text(encoding="utf-8")) == {"v": "old"}
        assert not (tmp_path / ".multiwrite.manifest.json").exists()

    def test_the_commit_teardown_can_land_the_rolled_back_marker(
            self, tmp_path):
        """收尾路徑的另一分支:終態標記寫得成 → 備份可刪,
        下次啟動認成已了結。"""
        a, b = self._two_old_files(tmp_path)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(aio, "_mark_manifest_committed", lambda p: False)
            mp.setattr(aio, "_remove_manifest", lambda p: False)
            with pytest.raises(aio.MultiWriteError):
                atomic_write_json_multi([(str(a), {"v": "new"}),
                                         (str(b), {"v": "new"})])
        mpath = tmp_path / ".multiwrite.manifest.json"
        m = json.loads(mpath.read_text(encoding="utf-8"))
        assert m.get("rolled_back") is True, m
        assert not os.path.exists(str(a) + ".rollback.bak")
        res = recover_interrupted_multiwrite(str(tmp_path))
        assert bool(res) is True and res.stuck == (), res
        assert not mpath.exists()
        assert json.loads(a.read_text(encoding="utf-8")) == {"v": "old"}
