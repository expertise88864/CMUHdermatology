# -*- coding: utf-8 -*-
"""會診識別要認得「同一病人的另一張會診」（2026-08-04 外審 P1-04）。

【問題】
`_consult_signature_from_roster()` 回傳的是【病歷號集合】。它只能回答

    這位病人在不在清單上？

回答不了

    這是不是同一位病人的【另一張】會診？

因為 `{"12345678", "12345678"} == {"12345678"}`。同一病人的第二張會診被集合
吸收掉 → ★漏寄★，臨床上最糟的方向。

【修法】識別改成 `病歷號|日期|時間`。缺日期/時間時退回只用病歷號（＝舊行為），
所以鑑別力只會變好或持平。

★不採納外審建議把病房/床號放進識別★：病人轉床很常見，而轉床不是新的會診 ——
把床號放進去會在每次轉床多寄一封（誤寄）。真正屬於「這張會診單」的是申請時間：
轉床時不變，新開一張時必然不同。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import consult_query as cq  # noqa: E402


class TestASecondConsultForTheSamePatientIsDetected:

    def test_two_consults_same_patient_are_two_ids(self):
        """★這就是漏寄的來源★ 同一病人兩張會診不可以塌成一個。"""
        rows = ["王小明C16(1)1234567(沈)06/25(09:30)",
                "王小明C16(1)1234567(許)06/25(14:10)"]
        ids = cq._consult_signature_from_roster(rows)
        assert len(ids) == 2, f"同一病人的第二張會診被吸收掉了：{ids}"

    def test_the_same_consult_seen_twice_is_one_id(self):
        """★反方向:不可以每輪都變成新的★ 同一張會診兩次讀到要一樣。"""
        row = "王小明C16(1)1234567(沈)06/25(09:30)"
        assert (cq._consult_signature_from_roster([row])
                == cq._consult_signature_from_roster([row]))

    def test_moving_bed_does_not_look_like_a_new_consult(self):
        """★不採納把床號放進識別的理由★ 轉床不是新會診，不可以多寄一封。"""
        before = cq._consult_signature_from_roster(
            ["王小明C16(1)1234567(沈)06/25(09:30)"])
        after = cq._consult_signature_from_roster(
            ["王小明B7(18A)1234567(沈)06/25(09:30)"])
        assert before == after, (
            f"轉床被當成新會診 → 會多寄一封：{before} vs {after}")

    def test_a_row_without_a_time_falls_back_to_chart_only(self):
        """沒有日期/時間 → 退回病歷號（＝舊行為），鑑別力持平不變差。"""
        ids = cq._consult_signature_from_roster(["王小明C16(1)1234567(沈)"])
        assert ids == {"1234567"}

    def test_the_chart_is_recoverable_from_the_id(self):
        """升級時要拿識別回推病歷號跟舊基準比對。"""
        assert cq._chart_of_consult_id("1234567|06/25|09:30") == "1234567"
        assert cq._chart_of_consult_id("1234567") == "1234567"


class TestUnparsableRowsKeepTheOldBehaviour:
    """★這是我第一版改壞的地方，由既有測試抓到★

    解析不到結構的列，舊碼用 `findall` 掃出該列【全部】病歷號。我第一版改成只取
    第一個 —— 實機上會有整塊文字被當成一列傳進來的情況（見
    `test_poll_first_startup_builds_baseline_silently` 的 fixture），只取第一個
    會把後面的會診整個丟掉。那是漏寄，比識別粒度不夠更嚴重。
    """

    def test_a_blob_row_still_yields_every_chart(self):
        blob = "會診清單(2 位):\n1. 甲C16(1)1111111(沈)06/25\n2. 乙C16(2)2222222(許)06/25"
        ids = cq._consult_signature_from_roster([blob])
        assert ids == {"1111111", "2222222"}, (
            f"★整塊文字當一列時漏掉了會診★：{ids}")

    def test_empty_and_none_are_still_empty(self):
        assert cq._consult_signature_from_roster(None) == set()
        assert cq._consult_signature_from_roster([]) == set()
        assert cq._consult_signature_from_roster([""]) == set()


class TestUpgradingMustNotResendEverything:
    """★升級當下最危險的事★

    舊基準只有病歷號。若拿新識別（`病歷號|日期|時間`）直接對它做集合相減，
    【每一張既有會診都會變成「新的」】→ 對團隊整份重寄。
    """

    def _baseline(self, monkeypatch, mem, legacy):
        monkeypatch.setattr(cq, "_notified_memory", set(mem))
        monkeypatch.setattr(cq, "_notified_initialized", True)
        monkeypatch.setattr(cq, "_notified_is_legacy", legacy)

    def test_a_legacy_baseline_does_not_make_everything_new(self,
                                                            monkeypatch):
        self._baseline(monkeypatch, {"1111111", "2222222"}, legacy=True)
        current = {"1111111|06/25|09:30", "2222222|06/25|10:00"}

        assert cq._new_consult_ids(current) == set(), (
            "★升級當下把既有會診全判成新的 → 整份重寄★")

    def test_a_genuinely_new_patient_is_still_detected_during_migration(
            self, monkeypatch):
        """★反方向:不可以升級那輪把真的新會診也吞掉★"""
        self._baseline(monkeypatch, {"1111111"}, legacy=True)
        current = {"1111111|06/25|09:30", "3333333|06/25|11:00"}

        assert cq._new_consult_ids(current) == {"3333333|06/25|11:00"}

    def test_after_migration_full_discrimination_is_restored(self,
                                                             monkeypatch):
        """基準寫成新格式之後，同一病人的第二張會診就認得出來了。"""
        self._baseline(monkeypatch, {"1111111|06/25|09:30"}, legacy=False)
        current = {"1111111|06/25|09:30", "1111111|06/25|14:10"}

        assert cq._new_consult_ids(current) == {"1111111|06/25|14:10"}

    def test_saving_clears_the_legacy_flag(self, monkeypatch, tmp_path):
        """寫下去的就是新格式 → 旗標要跟著關掉，否則永遠停在降級比對。"""
        monkeypatch.setattr(cq, "_NOTIFIED_FILE", tmp_path / "n.json")
        monkeypatch.setattr(cq, "_notified_is_legacy", True)
        cq._save_notified({"1111111|06/25|09:30"})
        assert cq._notified_is_legacy is False

    def test_the_file_still_carries_charts_for_older_versions(
            self, monkeypatch, tmp_path):
        """★診間電腦不是同時更新的★ 新版寫的檔可能被還沒更新的舊版讀到。

        舊版只認得 `charts`，少了它會把整份清單當成新會診重寄。
        """
        import json
        f = tmp_path / "n.json"
        monkeypatch.setattr(cq, "_NOTIFIED_FILE", f)
        cq._save_notified({"1111111|06/25|09:30", "2222222|06/25|10:00"})

        data = json.loads(f.read_text(encoding="utf-8"))
        assert data["charts"] == ["1111111", "2222222"], (
            f"舊版讀不到病歷號 → 會整份重寄：{data}")
        assert data["ids"] == ["1111111|06/25|09:30", "2222222|06/25|10:00"]


def test_the_poll_path_uses_the_migration_aware_comparison():
    """★接線本身也要被測到★

    上面每一支都直接呼叫 `_new_consult_ids`，所以就算 poll 那條路改回
    `_poll_sig - _load_notified()`，它們照樣全綠 —— 而那正是升級會整份重寄的
    樣子。`_do_full_job` 沒辦法在這裡整段跑，所以用 AST 檢查它確實被呼叫。
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(cq._do_full_job)))
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_new_consult_ids" in called, (
        "poll 沒有走升級相容的比對 → 升級當下會對團隊整份重寄")
