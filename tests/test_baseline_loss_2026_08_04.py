# -*- coding: utf-8 -*-
"""基準遺失／損毀不可以被當成首次安裝（2026-08-04 外審 P1-05）。

【問題】
只要基準沒建立起來，第一輪 poll 就一律：

    _save_notified(目前清單)
    return              # 不寄任何信

對真正的首次安裝那是對的（避免每次重裝收一封全清單）。但它分不出：

    真正首次安裝 / 檔案被刪 / JSON 損壞 / 防毒鎖住讀不到

後三種會把當下【所有】未回覆會診靜默標成「已通知」—— 那些會診從此不會有人收到
通知，而且沒有任何跡象。★這是無聲漏寄，臨床上最糟的方向★

【修法】
用「建立過基準」的標記檔區分首次安裝與其他三種，並在其他三種時 fail-open：
當作全部都是新的寄出去一次、信裡註明請人工核對、另寄一封系統告警給開發者。
寧可多寄一封讓人核對，不可以無聲漏掉會診。
"""
import os
import sys


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import consult_query as cq  # noqa: E402


def _state(monkeypatch, tmp_path, *, status, marker):
    monkeypatch.setattr(cq, "_notified_load_status", status, raising=False)
    monkeypatch.setattr(cq, "_INSTALL_MARKER", tmp_path / "marker.json",
                        raising=False)
    if marker:
        (tmp_path / "marker.json").write_text("{}", encoding="utf-8")


class TestTheFourStatesAreDistinguished:

    def test_a_genuine_first_install(self, monkeypatch, tmp_path):
        _state(monkeypatch, tmp_path, status="missing", marker=False)
        assert cq._baseline_absence_reason() == "first_install"

    def test_a_deleted_baseline_after_prior_runs(self, monkeypatch, tmp_path):
        """★這就是要擋的★ 建立過基準、檔案卻不見了。"""
        _state(monkeypatch, tmp_path, status="missing", marker=True)
        assert cq._baseline_absence_reason() == "missing_after_prior_run"

    def test_a_corrupt_baseline(self, monkeypatch, tmp_path):
        _state(monkeypatch, tmp_path, status="corrupt", marker=True)
        assert cq._baseline_absence_reason() == "corrupt"

    def test_a_corrupt_baseline_even_without_a_marker(self, monkeypatch,
                                                      tmp_path):
        """★內容壞掉本身就證明它存在過★ 不需要標記也不能算首次安裝。"""
        _state(monkeypatch, tmp_path, status="corrupt", marker=False)
        assert cq._baseline_absence_reason() == "corrupt"

    def test_a_locked_baseline_is_not_reported_as_missing(self, monkeypatch,
                                                          tmp_path):
        """★措辭鐵律★ 讀不到 ≠ 不見了。原檔通常還在，處置也不同。"""
        _state(monkeypatch, tmp_path, status="error", marker=True)
        assert cq._baseline_absence_reason() == "read_error"

    def test_a_read_error_without_a_marker_is_still_not_first_install(
            self, monkeypatch, tmp_path):
        _state(monkeypatch, tmp_path, status="error", marker=False)
        assert cq._baseline_absence_reason() == "read_error"


class TestTheMarkerIsWrittenOnlyAfterASuccessfulSave:
    """★不可以在啟動時寫標記★

    啟動時寫的話，真正的首次安裝在第一輪 poll 就已經有標記，會被判成
    「基準遺失」而寄出整份清單 —— 首次安裝寄整份正是當初要避免的事。
    """

    def test_saving_the_baseline_creates_the_marker(self, monkeypatch,
                                                    tmp_path):
        monkeypatch.setattr(cq, "_NOTIFIED_FILE", tmp_path / "n.json")
        monkeypatch.setattr(cq, "_INSTALL_MARKER", tmp_path / "marker.json")
        assert not (tmp_path / "marker.json").exists()

        cq._save_notified({"1111111|06/25|09:30"})

        assert (tmp_path / "marker.json").exists(), (
            "存了基準卻沒留標記 → 之後檔案不見了會被誤判成首次安裝")

    def test_the_marker_is_not_rewritten_every_time(self, monkeypatch,
                                                    tmp_path):
        """標記記的是「第一次」，不可以每輪覆蓋掉那個時間。"""
        monkeypatch.setattr(cq, "_NOTIFIED_FILE", tmp_path / "n.json")
        monkeypatch.setattr(cq, "_INSTALL_MARKER", tmp_path / "marker.json")
        cq._save_notified({"1111111"})
        first = (tmp_path / "marker.json").read_text(encoding="utf-8")
        cq._save_notified({"2222222"})
        assert (tmp_path / "marker.json").read_text(encoding="utf-8") == first

    def test_a_failed_marker_write_does_not_break_saving(self, monkeypatch,
                                                         tmp_path):
        """標記寫不出來不可以害基準也存不了(基準才是主線)。"""
        monkeypatch.setattr(cq, "_NOTIFIED_FILE", tmp_path / "n.json")
        monkeypatch.setattr(cq, "_mark_baseline_established",
                            lambda: (_ for _ in ()).throw(OSError("寫不了")))
        try:
            cq._save_notified({"1111111"})
        except OSError:
            raise AssertionError("標記寫入失敗把主線也拖垮了") from None


def test_the_poll_path_branches_on_the_reason():
    """★接線本身也要被測到★（本 session 這個形狀已出現五次）

    上面每一支都直接呼叫 `_baseline_absence_reason`，所以就算 poll 那條路仍然
    無條件靜默建基準，它們照樣全綠 —— 而那正是漏寄還在的樣子。
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(cq._do_full_job)))
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_baseline_absence_reason" in called, (
        "poll 沒有分辨基準為什麼不在 → 遺失/損毀仍會被靜默吞掉")
    assert "_alert_baseline_lost" in called, (
        "基準遺失沒有告警 → 沒有人會知道這台機器出過事")


class TestAFailedMarkerNeverLooksLikeAFreshInstall:
    """★[2026-08-08 外審]★ 標記是「這台建立過基準」的唯一憑據，而它的寫入是
    fail-open 的（防毒鎖檔／權限／磁碟錯誤都只記一行 log）。標記沒寫成、
    基準檔之後又遺失時，會判成 `first_install` —— 那條路是【把當下所有未回覆
    會診靜默記成已通知然後不寄】。那批會診從此沒有人收到通知。

    ★補救的位置★（外審第 4 回教的）我前兩版試圖事後用「產物痕跡」
    （log／截圖目錄／去重檔）推斷「這台跑過沒有」。那個啟發式兩個方向都會錯：
    `--configure` 開個設定就留下 log（全新機器被誤判成跑過）；
    email 觸發會建出去重檔與截圖，但它【明確不更新團隊基準】（沒有基準卻被
    當成有過）。真正的補救在【建立基準的那一刻就不要宣稱成功】。
    """

    def _no_marker(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cq, "_INSTALL_MARKER", tmp_path / "marker.json")
        monkeypatch.setattr(cq, "_notified_load_status", "missing",
                            raising=False)

    def test_a_genuinely_fresh_machine_is_still_first_install(self, monkeypatch,
                                                             tmp_path):
        """★反方向★ 真的第一次跑,仍要走首次安裝(否則每次重裝都收一封全清單)。"""
        self._no_marker(monkeypatch, tmp_path)
        monkeypatch.setattr(cq, "LOG_FILE", tmp_path / "nope1")
        monkeypatch.setattr(cq, "SHOTS_DIR", tmp_path / "nope2")
        monkeypatch.setattr(cq, "_TRIGGER_DEDUP_STATE_FILE", tmp_path / "nope3")
        assert cq._baseline_absence_reason() == "first_install"

    def test_the_marker_helper_reports_failure(self, monkeypatch, tmp_path):
        """★成敗要說得出來★ 舊版把例外吞掉、回 None —— 呼叫端無從得知
        「這台機器之後會被誤判成首次安裝」。"""
        monkeypatch.setattr(cq, "_INSTALL_MARKER", tmp_path / "m.json")
        import cmuh_common.atomic_io as aio
        monkeypatch.setattr(
            aio, "atomic_write_json",
            lambda *a, **k: (_ for _ in ()).throw(OSError("locked")))
        monkeypatch.setattr(cq, "atomic_write_json", aio.atomic_write_json,
                            raising=False)
        assert cq._mark_baseline_established() is False

    def test_a_successful_mark_reports_true(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cq, "_INSTALL_MARKER", tmp_path / "m.json")
        assert cq._mark_baseline_established() is True




class TestAFirstBaselineIsNotClaimedWithoutADurableMarker:
    """★[2026-08-08 外審第 4 回]★ 補救的位置在【建立基準的那一刻】。

    首次基準存檔成功、但標記沒能寫入時，不可以宣稱「建立成功、本輪不寄信」——
    因為下次基準檔一旦遺失，就會被判成首次安裝而【靜默】把當下所有未回覆會診
    記成已通知。改成 fail-open：照常寄整份清單 + 告警，並且原因要是
    `marker_not_durable`（不是 `first_install`，那會報錯原因）。
    """

    def _poll_src(self):
        import inspect
        import textwrap
        return textwrap.dedent(inspect.getsource(cq._do_full_job))

    def test_the_baseline_is_not_committed_before_the_marker(self):
        """★核心(第 5 回)★ 順序必須是「先確認標記能落地 → 才建基準」。

        反過來的話:基準已經 commit,而 fail-open 那封信若寄不出去,
        下一輪就認為所有會診都通知過而什麼都不寄 —— 那批會診永遠送不出去。
        基準只能在【送達之後】才可以前進。
        """
        import ast
        src = self._poll_src()
        tree = ast.parse(src)
        mark_line = save_line = None
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call) or not isinstance(n.func, ast.Name):
                continue
            if n.func.id == "_mark_baseline_established":
                mark_line = n.lineno
            if (n.func.id == "_save_notified_if_eligible"
                    and any(isinstance(k.value, ast.Constant)
                            and k.value.value == "建立首次基準"
                            for k in n.keywords)):
                save_line = n.lineno
        assert mark_line and save_line, f"{mark_line} {save_line}"
        assert mark_line < save_line, (
            "★先建基準才檢查標記★ 基準已經 commit,而 fail-open 那封信"
            "若寄不出去,那批會診永遠送不出去")

    def test_the_failure_reason_is_not_first_install(self):
        src = self._poll_src()
        assert '_why = "marker_not_durable"' in src, (
            "★標記沒落地卻仍沿用 first_install 當原因★ 告警會報錯的原因")
        assert '_lost = "marker_not_durable"' not in src, (
            "★設 _lost 會被下面的 `_lost = _why` 覆寫★ 告警仍報 first_install")

    def test_an_ineligible_first_roster_leaves_no_marker(self):
        """★核心(第 6 回)★ 首輪清單「未經回讀確認」時，標記與基準都不可以出現。

        只寫標記卻沒建基準的話，下一輪會看到「有標記、沒基準」→ 判成基準遺失
        → 把整份既有清單寄出去 + 一封假的遺失告警。
        """
        import ast
        src = self._poll_src()
        tree = ast.parse(src)
        may_line = mark_line = None
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call) or not isinstance(n.func, ast.Name):
                continue
            if n.func.id == "_may_update_baseline" and may_line is None:
                may_line = n.lineno
            if n.func.id == "_mark_baseline_established":
                mark_line = n.lineno
        assert may_line and mark_line, f"{may_line} {mark_line}"
        assert may_line < mark_line, (
            "★先寫標記才檢查清單合不合格★ 不合格時會留下「有標記、沒基準」,"
            "下一輪就是一封假的基準遺失告警 + 整份清單")
