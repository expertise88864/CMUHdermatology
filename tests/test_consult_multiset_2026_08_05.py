# -*- coding: utf-8 -*-
"""兩張一模一樣的會診不可被集合吸收（外審第 6 輪 P1-01）＋ 基準唯一入口（P2-01）。

【P1-01 是上一批只修一半的洞】
批次Y 把 radio 去重從「文字」改成「控制項」，第二列已經能活到簽章這一層 ——
但 `_consult_signature_from_roster` 回傳 `set`：

    {"A|8/5|10:30", "A|8/5|10:30"} == {"A|8/5|10:30"}

`_new_consult_ids` 的相減就看不到第二張 → 漏寄，而且無聲。

【為什麼上一輪拒絕、這一輪接受】
上一輪的提案是「把清單位置(occurrence)放進識別」——位置相依，任何一張會診被
回覆離開清單就全體位移 → 整份誤判成新的 → 重寄整份，我拒絕了。
這一輪的設計是**以識別字串為鍵、每個識別自己計數**（第 2 份加 "#2" 序號），
與位置無關：別張會診的來去不影響這一張。檔案格式仍是字串集合，舊基準零遷移。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import consult_query as cq  # noqa: E402

ROW = "莊振銘B7(163)1234567(沈)08/05 (10:30)"


class TestDuplicateRowsSurviveTheSignature:

    def test_two_identical_rows_give_two_ids(self):
        sig = cq._consult_signature_from_roster([ROW, ROW])
        assert len(sig) == 2, f"★第二張被集合吸收★:{sig}"
        base = cq._consult_signature_from_roster([ROW])
        assert len(sig - base) == 1, "1→2 張必須偵測出恰好一張新會診"

    def test_one_copy_is_unchanged(self):
        """★零遷移★ 單張的識別與舊格式完全相同(不帶序號)。"""
        sig = cq._consult_signature_from_roster([ROW])
        assert len(sig) == 1
        assert not any("#" in x for x in sig), "單張不可帶序號(舊基準會全部變新)"

    def test_back_to_one_copy_is_not_new(self):
        """2 張其中一張被回覆 → 剩 1 張不可算新(差集為空、剪枝自然收斂)。"""
        two = cq._consult_signature_from_roster([ROW, ROW])
        one = cq._consult_signature_from_roster([ROW])
        assert one - two == set()

    def test_other_rows_do_not_shift_the_suffix(self):
        """★與位置無關★(上一輪拒絕位置方案的理由,這裡釘住)
        別張會診離開清單,這一張的識別不變。"""
        other = "王小明A3(101)7654321(陳)08/05 (09:00)"
        with_other = cq._consult_signature_from_roster([other, ROW, ROW])
        without = cq._consult_signature_from_roster([ROW, ROW])
        assert without <= with_other, "別張會診的來去改變了這一張的識別"

    def test_chart_extraction_strips_the_suffix(self):
        """舊格式(只有病歷號)降級比對時,序號要剝掉,否則升級輪整份重寄。"""
        assert cq._chart_of_consult_id("1234567|08/05|10:30#2") == "1234567"
        assert cq._chart_of_consult_id("1234567#2") == "1234567"
        assert cq._chart_of_consult_id("1234567|08/05|10:30") == "1234567"

    def test_fallback_chart_rows_also_count_occurrences(self):
        """解析不到結構的列(掃病歷號後備路徑)同樣要能數到第二張。"""
        sig = cq._consult_signature_from_roster(["雜訊 1234567", "雜訊 1234567"])
        assert len(sig) == 2


class TestAllBaselineWritesGoThroughOneGate:
    """★P2-01★ 上一批只守住「寄信成功後」那一個寫入點。

    首次安裝建基準、無新會診剪枝,仍直接呼叫 `_save_notified` —— 一份沒被
    回讀確認的清單照樣能建立/剪出基準。剪枝那條的後果是:還在清單上的會診
    被過期的短清單剪掉 → 之後又變「新」→ 重寄。
    """

    def test_unverified_rows_never_reach_save(self, monkeypatch):
        saved = []
        monkeypatch.setattr(cq, "_save_notified", lambda ids: saved.append(ids))
        rows = cq._RosterTexts(["甲"], baseline_eligible=False)
        assert cq._save_notified_if_eligible(rows, {"x"}, reason="測試") is False
        assert saved == [], "★未確認的清單寫進基準了★"

    def test_verified_rows_do_reach_save(self, monkeypatch):
        saved = []
        monkeypatch.setattr(cq, "_save_notified", lambda ids: saved.append(ids))
        assert cq._save_notified_if_eligible(["甲"], {"x"}, reason="測試") is True
        assert saved == [{"x"}]

    def test_every_save_site_in_the_job_goes_through_the_gate(self):
        """★接線★ `_do_full_job` 裡不可以再有裸的 `_save_notified` 呼叫。

        「至少有一個受保護」的測試在上一輪就綠了,而缺陷在另外兩個寫入點 ——
        判準必須是【逐一盤點】,不是【存在一個】。
        """
        import ast
        import inspect
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(cq._do_full_job)))
        bare = [n.lineno for n in ast.walk(tree)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "_save_notified"]
        guarded = [n.lineno for n in ast.walk(tree)
                   if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                   and n.func.id == "_save_notified_if_eligible"]
        assert bare == [], (
            f"★{len(bare)} 個裸的 _save_notified 呼叫(行 {bare})★ 未確認的清單"
            "仍能改動基準")
        assert len(guarded) >= 3, (
            f"應有三個寫入點(首次安裝/剪枝/寄信後),只找到 {len(guarded)} 個")


class TestPersistedThresholdsAreValidatedAtRuntime:
    """★P2-04★ 設定頁的驗證只擋得住「現在存進去的」。

    舊版曾存下的 0、使用者手改 JSON 的 -1、損壞檔案的異常值,都是從
    `build_doctor_threshold_map` 直接生效 —— 門檻 0 讓提醒恆真。
    """

    def test_a_persisted_zero_does_not_become_a_threshold(self):
        from cmuh_common.threshold_policy import build_doctor_threshold_map
        got = build_doctor_threshold_map("陳駿升", {"chen_tue_night": 0})
        assert (1, "晚上") not in got, "★門檻 0 生效了★ 每一診都會提醒"

    def test_negative_and_absurd_values_are_dropped(self):
        from cmuh_common.threshold_policy import build_doctor_threshold_map
        for bad in (-1, 5, 9999, "0", "-1"):
            got = build_doctor_threshold_map("陳駿升", {"chen_tue_night": bad})
            assert (1, "晚上") not in got, f"{bad!r} 生效了"

    def test_normal_persisted_values_still_work(self):
        from cmuh_common.threshold_policy import build_doctor_threshold_map
        got = build_doctor_threshold_map("陳駿升", {"chen_tue_night": 88})
        assert got[(1, "晚上")] == 88
