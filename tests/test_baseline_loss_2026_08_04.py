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
