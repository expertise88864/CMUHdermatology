# -*- coding: utf-8 -*-
"""[外審第二輪 R2-P1-01] ActionLedger 的 durability 順序反了。

`record()` 的 JSONL append ★沒有 fsync★,而其後的 anchor 走 `atomic_write_json`
(內部 `_flush_and_fsync` 後 replace)—— 於是斷電時可能:
    HIS 動作真的發生 → ledger 那一行只在 page cache → anchor 已 durable → 斷電
重開後 `_load_last_state()` ★只讀 JSONL 末筆、完全不看 anchor★,於是:
  * 下一筆動作重用同一個 seq;
  * 它又把 anchor 覆寫成自己的 seq/hash;
  * 於是「曾經有一筆對應到真實 HIS 寫入」的最後證據也沒了 ——
    verify 之後看到的是一條完整、正常的鏈。
偵測窗口只有「當機後、下一筆寫入前」跑過 health check 才成立。
而這本帳正是金絲雀採 notify-only 時的補償控制,不可以有這個洞。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
from cmuh_common.action_ledger import (  # noqa: E402
    ActionLedger, _tail_is_torn, health_snapshot, read_records,
    verify_chain,
)


@pytest.fixture(autouse=True)
def _isolate_counters(monkeypatch):
    import main
    monkeypatch.setattr(main, "_ledger_unanchored", 0, raising=False)
    monkeypatch.setattr(main, "_ledger_anchor_incidents", 0, raising=False)


def _lines(p):
    with open(p, encoding="utf-8") as f:
        return [ln for ln in f.read().splitlines() if ln.strip()]


def _drop_last_line(p):
    """模擬★斷電★:最後一行沒有落盤(anchor 卻已 durable)。"""
    kept = _lines(p)[:-1]
    with open(p, "w", encoding="utf-8", newline="") as f:
        for ln in kept:
            f.write(ln + "\n")


class TestALostTailStaysProvable:
    def test_the_gap_is_detectable_right_after_the_crash(self, tmp_path):
        """前提:當機當下 verify 抓得到(這一條現在就會過,是下一條的對照)。"""
        p = str(tmp_path / "ledger.jsonl")
        led = ActionLedger(p)
        led.record("his_menu", "F9")
        led.record("his_menu", "F10")
        _drop_last_line(p)
        ok, _n, _msg = verify_chain(p)
        assert not ok, "當機當下就該抓得到截尾"

    def test_the_next_action_must_not_erase_the_evidence(self, tmp_path):
        """★核心反例★:當機後【下一筆動作】不可以把證據抹掉。

        現行行為:新的一筆重用 seq=2、把 anchor 也改寫成自己 → verify 全綠,
        那筆真的發生過的 F10 從此無法從資料本身得知。
        """
        p = str(tmp_path / "ledger.jsonl")
        led = ActionLedger(p)
        led.record("his_menu", "F9")
        led.record("his_menu", "F10")
        lost = json.loads(_lines(p)[-1])
        _drop_last_line(p)

        led2 = ActionLedger(p)          # 重開程式
        led2.record("his_menu", "F11")  # 下一次 HIS 動作

        recs = [json.loads(ln) for ln in _lines(p)]
        seqs = [r["seq"] for r in recs]
        assert seqs.count(lost["seq"]) == 0, (
            f"★遺失的 seq={lost['seq']} 被重用了★:{seqs}")
        gap = [r for r in recs if r.get("action") == "durability_gap"]
        assert len(gap) == 1, f"★缺口沒有被記進帳本★:{seqs}"
        d = gap[0]["detail"]
        assert (d["anchor_last_seq"], d["tail_seq"], d["missing"]) == (
            lost["seq"], lost["seq"] - 1, 1), d

    def test_the_append_is_durable_before_the_anchor(self, tmp_path,
                                                     monkeypatch):
        """★順序性質★:ledger 那一行要在 anchor 之前 fsync ——
        否則 anchor 先 durable、ledger 行後 durable 就是上面那個洞的來源。"""
        import cmuh_common.action_ledger as mod
        order = []
        real_fsync = os.fsync

        def _spy_fsync(fd):
            order.append("fsync")
            return real_fsync(fd)
        monkeypatch.setattr(os, "fsync", _spy_fsync)
        real_anchor = mod.ActionLedger._write_anchor

        def _spy_anchor(self, *a, **k):
            order.append("anchor")
            return real_anchor(self, *a, **k)
        monkeypatch.setattr(mod.ActionLedger, "_write_anchor", _spy_anchor)
        ActionLedger(str(tmp_path / "ledger.jsonl")).record("his_menu", "F9")
        assert order and order[0] == "fsync", (
            f"★ledger 行沒有先 fsync 就去寫 anchor★:{order}")


class TestATornTailDoesNotPoisonTheWholeFile:
    def test_a_half_written_last_line_still_finds_the_last_good_record(
            self, tmp_path):
        """★斷電也可能只寫了半行★:`_last_state_of` 對末行 `json.loads` 失敗就
        整個檔案回 None → 續寫起點退到 .1 甚至 genesis,單純的尾端 torn write
        被放大成整條鏈接錯。應該回退到【最後一筆完整的紀錄】。"""
        p = str(tmp_path / "ledger.jsonl")
        led = ActionLedger(p)
        led.record("his_menu", "F9")
        led.record("his_menu", "F10")
        good = json.loads(_lines(p)[-1])
        with open(p, "a", encoding="utf-8", newline="") as f:
            f.write('{"schema_version":2,"seq":3,"ts":"2026-08-2')
        led2 = ActionLedger(p)
        led2.record("his_menu", "F11")
        recs = [json.loads(ln) for ln in _lines(p)]
        # ★殘片的 seq 要被保留★(外審 deep R2-2):殘片代表一次沒寫完的稽核,
        #   它對應的 HIS 動作可能真的發生過 —— 不可以靜靜重用那個號碼。
        #   所以順序是:最後一筆完整紀錄 → durability_gap → 新動作。
        assert [r["action"] for r in recs][-2:] == ["durability_gap", "F11"], (
            [r["action"] for r in recs])
        assert recs[-2]["prev"] == good["hash"], "★沒有接在最後一筆完整紀錄後面★"
        # 殘片自稱 seq=3;那個號碼★空著★(缺口紀錄自己也占一個 seq,所以是 4)。
        assert 3 not in [r["seq"] for r in recs], (
            f"★殘片的 seq 被重用了★:{[r['seq'] for r in recs]}")
        assert verify_chain(p)[0], verify_chain(p)

    def test_the_gap_is_visible_in_the_health_state(self, tmp_path):
        """★出口不可以變成藏起來★:verify 認得缺口紀錄(所以鏈是完整的),
        但健康狀態必須說出「那段期間發生過的動作沒有留下紀錄」——
        否則就是換一種方式把資料遺失蓋掉。"""
        p = str(tmp_path / "ledger.jsonl")
        led = ActionLedger(p)
        led.record("his_menu", "F9")
        led.record("his_menu", "F10")
        _drop_last_line(p)
        ActionLedger(p).record("his_menu", "F11")
        h = health_snapshot(p)
        assert h["level"] == "warn", h
        assert "缺口" in h["summary"], h

    def test_a_forged_gap_record_cannot_excuse_an_arbitrary_jump(self,
                                                                 tmp_path):
        """★出口要對得起自己宣告的數字★:光看 action 名稱就放行,等於任何一筆
        gap 紀錄都能赦免任意跳號 —— 偵測性控制就送掉了。這裡讓缺口紀錄宣稱的
        數字與實際跳號不符,verify 必須照樣判跳號。"""
        p = str(tmp_path / "ledger.jsonl")
        led = ActionLedger(p)
        led.record("his_menu", "F9")
        led.record("his_menu", "F10")
        _drop_last_line(p)
        led2 = ActionLedger(p)
        led2._load_last_state()      # ★先觸發載入★:它是在 record() 裡延遲執行的,
        led2._last_seq += 5          # 不先叫它,下面改的 _last_seq 會被覆蓋掉
        led2.record("his_menu", "F11")
        ok, _n, msg = verify_chain(p)
        assert not ok and "跳號" in msg, (ok, msg)


# ══ 外審 deep 第 1 輪:四條失敗路徑 ═══════════════════════════════════════
class TestTheFailurePathsAreClosed:
    def test_a_failed_gap_write_blocks_the_action_itself(self, tmp_path,
                                                         monkeypatch):
        """★R1-1★ 缺口紀錄寫不進去時,原動作★不准★寫在保留後的 seq 上 ——
        那會是一個沒有紀錄可以解釋的跳號,而且 anchor 若成功還會回報成功。
        缺口要留著下次重試。"""
        import cmuh_common.action_ledger as mod
        p = str(tmp_path / "ledger.jsonl")
        led = ActionLedger(p)
        led.record("his_menu", "F9")
        led.record("his_menu", "F10")
        _drop_last_line(p)
        led2 = ActionLedger(p)
        calls = {"n": 0}
        real = mod.ActionLedger._append_durable

        def _fail_first(self, line):
            calls["n"] += 1
            return False if calls["n"] == 1 else real(self, line)
        monkeypatch.setattr(mod.ActionLedger, "_append_durable", _fail_first)
        r = led2.record("his_menu", "F11")
        assert not r, "★缺口沒落地卻回報成功★"
        assert len(_lines(p)) == 1, f"★原動作被寫進去了★:{_lines(p)}"
        assert led2._gap is not None, "★缺口被丟掉了(下次無從重試)★"
        # 下一次:缺口先寫,原動作接在它後面 → 鏈可驗證
        monkeypatch.setattr(mod.ActionLedger, "_append_durable", real)
        assert led2.record("his_menu", "F11")
        recs = [json.loads(ln) for ln in _lines(p)]
        assert [r2["action"] for r2 in recs] == ["F9", "durability_gap", "F11"]
        assert verify_chain(p)[0], verify_chain(p)

    def test_an_uncertain_fsync_does_not_let_the_next_record_reuse_the_seq(
            self, tmp_path, monkeypatch):
        """★R1-2「不確定」不等於「沒寫成功」★:write+flush 過了而 fsync 拋錯時,
        那一行很可能已經在檔案裡。沿用記憶體狀態續寫 → 下一筆重用同一個
        seq/prev,兩行都在卻接不上,帳本永久驗證失敗。"""
        p = str(tmp_path / "ledger.jsonl")
        led = ActionLedger(p)
        led.record("his_menu", "F9")
        real_fsync = os.fsync
        boom = {"on": True}

        def _fsync(fd):
            real_fsync(fd)                 # ★內容真的落盤了★,只是回報失敗
            if boom["on"]:
                boom["on"] = False
                raise OSError("模擬:fsync 回報錯誤")
        monkeypatch.setattr(os, "fsync", _fsync)
        assert not led.record("his_menu", "F10")   # 不確定 → 回報失敗
        led.record("his_menu", "F11")              # 下一筆
        recs = [json.loads(ln) for ln in _lines(p)]
        seqs = [r["seq"] for r in recs]
        assert len(seqs) == len(set(seqs)), f"★seq 被重用★:{seqs}"
        ok, _n, msg = verify_chain(p)
        assert ok, f"★鏈接不上了★:{msg}"

    def test_a_gap_on_the_very_first_record_is_not_read_as_truncation(
            self, tmp_path):
        """★R1-3★ 第一筆 seq=1 沒落盤、anchor 已是 1 → recovery 產生的首筆是
        prev=genesis 的 gap(seq=2)。判成截頭的話健康狀態永久 error。"""
        from cmuh_common.action_ledger import verify_generations
        p = str(tmp_path / "ledger.jsonl")
        led = ActionLedger(p)
        led.record("his_menu", "F9")
        with open(p, "w", encoding="utf-8"):      # 第一筆整個沒落盤
            pass
        ActionLedger(p).record("his_menu", "F10")
        ok, _n, msg = verify_generations(p)
        assert ok, f"★合法的缺口被判成截頭★:{msg}"
        assert health_snapshot(p)["level"] == "warn", health_snapshot(p)

    def test_a_forged_first_gap_cannot_excuse_a_real_truncation(self,
                                                                tmp_path):
        """★出口的數字要對得起來★:首筆宣稱的缺口與實際 seq 不符 → 照樣判截頭。"""
        from cmuh_common.action_ledger import verify_generations
        p = str(tmp_path / "ledger.jsonl")
        led = ActionLedger(p)
        led.record("his_menu", "F9")
        with open(p, "w", encoding="utf-8"):
            pass
        led2 = ActionLedger(p)
        led2._load_last_state()
        led2._last_seq += 4                       # 宣稱缺 1,實際跳到 seq=6
        led2.record("his_menu", "F10")
        ok, _n, msg = verify_generations(p)
        assert not ok and "截頭" in msg, (ok, msg)

    def test_a_torn_fragment_is_quarantined_so_the_ledger_stays_verifiable(
            self, tmp_path):
        """★R1-4★ 補換行只讓後續紀錄解析得動,殘片仍留在檔案中間 ——
        `_parse_strict` 一遇到它就整份失敗,verify/health 從此永久 error。
        殘片必須★移出帳本★(留檔備查)並截斷,帳本才真的回復為可驗證。"""
        p = str(tmp_path / "ledger.jsonl")
        led = ActionLedger(p)
        led.record("his_menu", "F9")
        with open(p, "a", encoding="utf-8", newline="") as f:
            f.write('{"schema_version":2,"seq":2,"ts":"2026-08-2')
        ActionLedger(p).record("his_menu", "F10")
        ok, _n, msg = verify_chain(p)
        assert ok, f"★殘片仍毒害整個檔案★:{msg}"
        side = [f for f in os.listdir(tmp_path) if ".torn-" in f]
        assert len(side) == 1, f"★殘片沒有留檔備查★:{os.listdir(tmp_path)}"
        # 缺口記完之後側檔要被標成已結案(否則重開會再記一次)
        assert side[0].endswith(".resolved"), side
        with open(os.path.join(tmp_path, side[0]), encoding="utf-8") as f:
            meta = json.load(f)
        assert meta["tail_seq"] == 1
        assert meta["fragment"].startswith('{"schema_version"')
        # ★我原本在這裡斷言 health 是綠的 —— 那是把缺陷釘成正確答案★
        #   (外審 deep R2-2)。殘片代表「一次稽核沒寫完」,而它對應的 HIS 動作
        #   可能真的發生過;補償控制不可以回報「沒有遺失」。
        h = health_snapshot(p)
        assert h["level"] == "warn", h
        recs = [json.loads(ln) for ln in _lines(p)]
        gaps = [r for r in recs if r.get("action") == "durability_gap"]
        assert len(gaps) == 1, f"★殘片沒有換來缺口紀錄★:{[r['seq'] for r in recs]}"
        assert gaps[0]["detail"]["torn"] == 1, gaps[0]["detail"]

    def test_a_complete_final_record_missing_only_its_newline_is_kept(
            self, tmp_path):
        """★R2-1「最後一個位元組不是換行」只是便利的判斷式★:截斷點正好落在
        換行前面時,那是一筆★完整、而且可能已被 anchor 指認★的紀錄 ——
        一律隔離會把它刪掉,而 anchor 還指著它 → 下一筆接不上,反而製造斷鏈。
        """
        p = str(tmp_path / "ledger.jsonl")
        led = ActionLedger(p)
        led.record("his_menu", "F9")
        led.record("his_menu", "F10")
        kept = json.loads(_lines(p)[-1])
        with open(p, "rb+") as f:              # 只砍掉最後那個換行
            f.seek(-1, os.SEEK_END)
            f.truncate()
        ActionLedger(p).record("his_menu", "F11")
        recs = [json.loads(ln) for ln in _lines(p)]
        assert [r["seq"] for r in recs] == [1, 2, 3], [r["seq"] for r in recs]
        assert recs[1]["hash"] == kept["hash"], "★完整的紀錄被隔離掉了★"
        assert not [f for f in os.listdir(tmp_path) if ".torn-" in f]
        ok, _n, msg = verify_chain(p)
        assert ok, msg
        assert health_snapshot(p)["ok"], health_snapshot(p)

    def test_a_fragment_appearing_mid_run_is_not_appended_onto(self, tmp_path):
        """★同一個 instance 已經載入狀態之後才出現的殘片★:此時
        `_load_last_state()` 不會重跑(`_last_hash` 已設),所以寫入路徑自己也要
        擋 —— 否則新紀錄會被接在殘片後面黏成一行。正確行為:本筆不寫、作廢
        記憶體狀態,由下一次寫入重跑 recovery(修復+保留 seq+記缺口)。"""
        p = str(tmp_path / "ledger.jsonl")
        led = ActionLedger(p)
        led.record("his_menu", "F9")          # 狀態已載入
        with open(p, "a", encoding="utf-8", newline="") as f:
            f.write('{"schema_version":2,"seq":2,"ts":"2026-08-2')
        assert not led.record("his_menu", "F10"), "★接在殘片後面寫下去了★"
        assert led.record("his_menu", "F10")   # 下一次:recovery 跑完才寫得進去
        recs = [json.loads(ln) for ln in _lines(p)]
        assert [r["action"] for r in recs] == ["F9", "durability_gap", "F10"]
        assert verify_chain(p)[0], verify_chain(p)
        assert health_snapshot(p)["level"] == "warn", health_snapshot(p)


# ══ 外審 deep 第 3 輪:殘片復原本身要 crash-durable ═══════════════════════
class TestTornRecoveryIsCrashDurable:
    """★遺失的事實不可以只活在記憶體★:「寫側檔 → 截斷主檔 → 記缺口」中間
    斷電的話,重開後主檔已經沒有殘片、anchor 也與末筆相符 —— 舊版靠這一次
    呼叫算出來的旗標,於是缺口就此消失、下一筆靜靜重用 seq、health 全綠。
    側檔在截斷【之前】就 durable,所以它本身就是「尚未記入帳本」的標記。
    """

    def _crashed_after_truncate(self, tmp_path):
        """做出那個窗口的狀態:側檔已寫、主檔已截斷、缺口還沒記。"""
        p = str(tmp_path / "ledger.jsonl")
        led = ActionLedger(p)
        led.record("his_menu", "F9")
        with open(p + ".torn-1", "w", encoding="utf-8", newline="") as f:
            json.dump({"tail_seq": 1,
                       "fragment": '{"schema_version":2,"seq":2,"ts":"2026-'}, f)
        return p

    def test_an_unresolved_side_file_still_produces_the_gap(self, tmp_path):
        """★核心★:重開後主檔完好、anchor 也相符,唯一的線索就是側檔 ——
        缺口仍然必須被記進帳本,health 仍然必須是 warn。"""
        p = self._crashed_after_truncate(tmp_path)
        ActionLedger(p).record("his_menu", "F10")
        recs = [json.loads(ln) for ln in _lines(p)]
        assert [r["action"] for r in recs] == ["F9", "durability_gap", "F10"]
        assert recs[1]["detail"]["torn"] == 1, recs[1]["detail"]
        assert health_snapshot(p)["level"] == "warn", health_snapshot(p)
        assert verify_chain(p)[0], verify_chain(p)

    def test_the_same_loss_is_not_recorded_twice(self, tmp_path):
        """★另一半窗口★:「缺口已 durable、側檔還沒標記結案」中間斷電,
        重開不可以再記一次(把一次損失說成兩次同樣是失真)。"""
        p = self._crashed_after_truncate(tmp_path)
        ActionLedger(p).record("his_menu", "F10")
        os.replace(p + ".torn-1.resolved", p + ".torn-1")   # 模擬:還沒標記就斷電
        ActionLedger(p).record("his_menu", "F11")
        recs = [json.loads(ln) for ln in _lines(p)]
        gaps = [r for r in recs if r["action"] == "durability_gap"]
        assert len(gaps) == 1, f"★同一個損失被記了兩次★:{[r['action'] for r in recs]}"
        assert verify_chain(p)[0], verify_chain(p)
        assert os.path.exists(p + ".torn-1.resolved"), "★沒有補上結案標記★"

    def test_the_marker_is_durable_before_the_truncation_and_is_reused(
            self, tmp_path):
        """★順序 + 冪等,用效果量★(外審 deep R4-2):側檔要在截斷主檔【之前】
        durable —— 讓那個窗口斷電時線索還在;而重開後殘片仍在,復原★不可以★
        再開一個 `.torn-2`,否則一次損失被算成兩筆、白白跳掉兩個 seq。
        """
        import cmuh_common.action_ledger as mod
        p = str(tmp_path / "ledger.jsonl")
        led = ActionLedger(p)
        led.record("his_menu", "F9")
        with open(p, "a", encoding="utf-8", newline="") as f:
            f.write('{"schema_version":2,"seq":2,"ts":"2026-08-2')
        real_open = open

        def _no_truncate(path, mode="r", *a, **k):
            if str(path) == p and "r+" in str(mode):
                raise OSError("模擬:截斷前斷電")
            return real_open(path, mode, *a, **k)
        mod.open = _no_truncate
        try:
            ActionLedger(p).record("his_menu", "F10")
        finally:
            del mod.open
        # ★側檔已經 durable,主檔還沒截斷★ —— 正是那個窗口
        assert os.path.exists(p + ".torn-1"), os.listdir(tmp_path)
        assert _tail_is_torn(p), "★主檔應該仍是殘片狀態★"
        # 重開:殘片還在,但不可以再開一個標記
        ActionLedger(p).record("his_menu", "F10")
        assert not os.path.exists(p + ".torn-2"), (
            f"★同一個殘片開了第二個標記★:{os.listdir(tmp_path)}")
        recs = [json.loads(ln) for ln in _lines(p)]
        gaps = [r for r in recs if r["action"] == "durability_gap"]
        assert len(gaps) == 1, [r["action"] for r in recs]
        assert gaps[0]["detail"]["missing"] == 1, gaps[0]["detail"]
        assert verify_chain(p)[0], verify_chain(p)

    def test_startup_health_is_not_green_with_an_unresolved_marker(
            self, tmp_path):
        """★R4-1★ 缺口紀錄要等到【下一次寫入】才產生,而啟動時的健康檢查就在
        那之前 —— 只數缺口紀錄的話,重開後到下一個動作之間補償控制回報全綠。
        未結案的殘片標記本身就必須讓健康狀態是 warn。"""
        p = self._crashed_after_truncate(tmp_path)
        h = health_snapshot(p)                 # ★還沒有任何後續動作★
        assert h["level"] == "warn", h
        assert "沒寫完" in h["summary"], h
        assert ActionLedger(p).health_check()["level"] == "warn"

    def test_a_gap_rotated_into_an_older_generation_is_still_found(
            self, tmp_path):
        """★R4-3★ 結案改名失敗、缺口紀錄又被輪替搬進 `.2` 之後重開 ——
        只看 base 與 `.1` 會找不到它,同一次損失就被再記一遍。

        (輪替逐次模擬:每搬一代就寫一筆新紀錄,anchor 才會跟著走 ——
         直接把 base 搬走而不留後續世代,是真實輪替不會出現的狀態。
         標記在這期間先藏起來,免得中途就被判定已記過而結案。)
        """
        p = self._crashed_after_truncate(tmp_path)
        ActionLedger(p).record("his_menu", "F10")      # 缺口寫在 base
        os.replace(p + ".torn-1.resolved", p + ".hidden")
        os.replace(p, p + ".1")
        ActionLedger(p).record("his_menu", "F11")      # 缺口在 .1
        os.replace(p + ".1", p + ".2")
        os.replace(p, p + ".1")
        ActionLedger(p).record("his_menu", "F12")      # 缺口在 .2
        assert any(r["action"] == "durability_gap"
                   for r in read_records(p + ".2")), "前提:缺口確實在 .2"
        os.replace(p + ".hidden", p + ".torn-1")       # 模擬:結案改名曾失敗
        ActionLedger(p).record("his_menu", "F13")
        gaps = [r for r in read_records(p) if r["action"] == "durability_gap"]
        assert not gaps, f"★同一個損失在新世代又被記了一次★:{gaps}"
        assert os.path.exists(p + ".torn-1.resolved"), "★沒有補上結案標記★"

    def test_an_unreadable_marker_is_not_counted_as_a_loss(self, tmp_path):
        """★解析不出來的標記不算一筆損失★:標記是用 atomic_write_json 寫的
        (temp+fsync+replace),所以本機制產生的標記只有「完整存在」與「不存在」
        兩種狀態 —— 讀不出內容的檔案不是它寫的。把它當成損失會憑空多報,
        而且★清不掉★(沒有任何動作能讓那個警示消失)= 又一道沒有出口的閘門。
        """
        p = str(tmp_path / "ledger.jsonl")
        led = ActionLedger(p)
        led.record("his_menu", "F9")
        with open(p + ".torn-1", "w", encoding="utf-8") as f:
            f.write('{"foo": 1}')              # 合法 JSON,但不是這個機制的標記
        with open(p + ".torn-2", "w", encoding="utf-8") as f:
            f.write("這根本不是 JSON")
        assert health_snapshot(p)["ok"], health_snapshot(p)
        ActionLedger(p).record("his_menu", "F10")
        recs = [json.loads(ln) for ln in _lines(p)]
        assert [r["action"] for r in recs] == ["F9", "F10"], (
            f"★憑空多報了一筆損失★:{[r['action'] for r in recs]}")

    def test_an_identical_fragment_at_a_different_tail_is_a_new_loss(
            self, tmp_path):
        """★R5-1 身分不可以只看殘片內容★:稽核紀錄彼此有很長的相同前綴,
        斷在同一個位元組位置就會產生【一模一樣的殘片】。舊標記因為結案改名
        失敗而還在時,新的損失會被折進它 —— 而它的缺口早就記過了 →
        新損失連標記帶缺口一起消失、seq 被重用、health 回報全綠。
        """
        p = self._crashed_after_truncate(tmp_path)      # 舊損失:tail_seq=1
        ActionLedger(p).record("his_menu", "F10")       # 缺口已記
        os.replace(p + ".torn-1.resolved", p + ".torn-1")   # 結案改名失敗
        old_frag = json.load(open(p + ".torn-1", encoding="utf-8"))["fragment"]
        with open(p, "a", encoding="utf-8", newline="") as f:
            f.write(old_frag)                            # ★同樣內容、不同 tail★
        ActionLedger(p).record("his_menu", "F11")
        metas = [json.load(open(os.path.join(tmp_path, f), encoding="utf-8"))
                 for f in os.listdir(tmp_path) if ".torn-" in f]
        assert len({m["tail_seq"] for m in metas}) == 2, (
            f"★新的損失被折進舊標記了★:{metas}")
        recs = [json.loads(ln) for ln in _lines(p)]
        gaps = [r for r in recs if r["action"] == "durability_gap"]
        assert len(gaps) == 2, f"★新損失沒有留下缺口★:{[r['action'] for r in recs]}"
        assert verify_chain(p)[0], verify_chain(p)

    def test_health_does_not_count_the_same_incident_twice(self, tmp_path):
        """★R5-2★「缺口已 durable、標記還沒改名結案」那個窗口裡,缺口紀錄與
        待處理標記講的是★同一次★損失 —— 健康摘要不可以說成兩筆。"""
        p = self._crashed_after_truncate(tmp_path)
        ActionLedger(p).record("his_menu", "F10")
        os.replace(p + ".torn-1.resolved", p + ".torn-1")   # 標記還沒結案
        h = health_snapshot(p)                              # ★不先跑任何寫入★
        assert h["level"] == "warn", h
        assert "1 處已記錄" in h["summary"], h
        assert "尚未記入帳本" not in h["summary"], h
