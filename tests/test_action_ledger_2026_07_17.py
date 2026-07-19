# -*- coding: utf-8 -*-
"""外部動作稽核帳本(ExternalActionGateway 第一片,2026-07-17)。

使用者定案「改版不擋只通知」後,預防性控制沒了 → 補償控制是【偵測性】的動作紀錄:
每次真的動到 HIS 都留一筆(值、當下 HIS 版本、金絲雀裁決、回讀結果),事後查得出。
稽核【絕不可弄壞臨床功能】→ 所有失敗路徑都必須吞掉、回 False,不得拋例外。
"""
import inspect
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import main  # noqa: E402
from cmuh_common import action_ledger as al  # noqa: E402


def _ledger(tmp_path, **kw):
    return al.ActionLedger(str(tmp_path / "action_ledger.jsonl"), **kw)


# ── 基本記錄 ────────────────────────────────────────────────────────────────
def test_record_writes_queryable_fields(tmp_path):
    lg = _ledger(tmp_path)
    assert lg.record(al.SURFACE_HIS_MENU, "F2 醫令代碼輸入", target="menu:219",
                     value="51017", his_version="1150713", canary="ok",
                     outcome=al.OUTCOME_OK, correlation_id="abc") is True
    recs = al.read_records(lg.path)
    assert len(recs) == 1
    r = recs[0]
    assert r["surface"] == "his_menu" and r["action"] == "F2 醫令代碼輸入"
    assert r["target"] == "menu:219" and r["value"] == "51017"
    assert r["his_version"] == "1150713" and r["canary"] == "ok"
    assert r["outcome"] == "ok" and r["correlation_id"] == "abc"
    assert r["schema_version"] == al.SCHEMA_VERSION and r["ts"]


def test_records_append_only_and_chain_links(tmp_path):
    lg = _ledger(tmp_path)
    lg.record(al.SURFACE_HIS_FIELD, "UVB 劑量寫回", value="680")
    lg.record(al.SURFACE_HIS_MENU, "F11 完成不印", target="menu:277")
    recs = al.read_records(lg.path)
    assert len(recs) == 2, "append-only:第二筆不得覆蓋第一筆"
    assert recs[0]["prev"] == al.GENESIS
    assert recs[1]["prev"] == recs[0]["hash"], "chain 應接上前一筆"
    ok, n, msg = al.verify_chain(lg.path)
    assert ok is True and n == 2, msg


def test_outcome_defaults_to_unknown_not_ok(tmp_path):
    # [GPT-5.6 第三輪] 預設 outcome 不可是 ok:呼叫端忘了傳就自動變「成功」紀錄,
    # 是不安全預設,會讓帳本產生假成功。
    lg = _ledger(tmp_path)
    lg.record(al.SURFACE_HIS_FIELD, "療程")
    assert al.read_records(lg.path)[0]["outcome"] == al.OUTCOME_UNKNOWN


def test_submitted_unverified_distinct_from_ok():
    # 「訊息送出成功」≠「HIS 動作成功」:無回讀的路徑最多記 submitted_unverified
    assert al.OUTCOME_SUBMITTED_UNVERIFIED != al.OUTCOME_OK


def test_mismatch_outcome_is_recorded(tmp_path):
    # 回讀對不上是最重要的訊號(改版寫錯病歷時就靠它)
    lg = _ledger(tmp_path)
    lg.record(al.SURFACE_HIS_FIELD, "身份 自費", value="01",
              outcome=al.OUTCOME_MISMATCH, detail="回讀=02")
    r = al.read_records(lg.path)[0]
    assert r["outcome"] == "mismatch" and "02" in r["detail"]


# ── 防竄改 ─────────────────────────────────────────────────────────────────
def test_verify_chain_detects_edited_record(tmp_path):
    lg = _ledger(tmp_path)
    lg.record(al.SURFACE_HIS_MENU, "F2", value="51017")
    lg.record(al.SURFACE_HIS_MENU, "F3", value="51019")
    # 竄改第一筆的值(hash 不動)→ 應驗不過
    lines = open(lg.path, encoding="utf-8").read().splitlines()
    lines[0] = lines[0].replace('"51017"', '"99999"')
    open(lg.path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    ok, _, msg = al.verify_chain(lg.path)
    assert ok is False and "竄改" in msg


def test_verify_chain_detects_deleted_record(tmp_path):
    lg = _ledger(tmp_path)
    for v in ("a", "b", "c"):
        lg.record(al.SURFACE_HIS_MENU, "F2", value=v)
    lines = open(lg.path, encoding="utf-8").read().splitlines()
    del lines[1]                                  # 抽掉中間一筆
    open(lg.path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    ok, _, msg = al.verify_chain(lg.path)
    assert ok is False and "接上" in msg


def test_verify_chain_fails_on_missing_file(tmp_path):
    # [codex P1] 檔案不存在 = 可能整本被刪 → 不可回報「有效的空帳本」
    ok, _, msg = al.verify_chain(str(tmp_path / "nope.jsonl"))
    assert ok is False and "不存在" in msg


def test_verify_chain_ok_on_existing_but_empty(tmp_path):
    p = tmp_path / "action_ledger.jsonl"
    p.write_text("", encoding="utf-8")
    assert al.verify_chain(str(p))[0] is True


def test_verify_chain_fails_on_corrupt_line(tmp_path):
    # [codex P1] 驗證要嚴格解析:壞行本身就是竄改跡象,不可像 read_records 那樣跳過
    lg = _ledger(tmp_path)
    lg.record(al.SURFACE_HIS_MENU, "F2", value="1")
    with open(lg.path, "a", encoding="utf-8") as f:
        f.write("{not json\n")
    ok, _, msg = al.verify_chain(lg.path)
    assert ok is False and "JSON" in msg


def test_verify_chain_detects_seq_gap(tmp_path):
    # seq 跳號 → 就算兇手把 hash 鏈也重算過,少了一筆仍看得出來
    lg = _ledger(tmp_path)
    for v in ("a", "b"):
        lg.record(al.SURFACE_HIS_MENU, "F2", value=v)
    recs = al.read_records(lg.path)
    assert [r["seq"] for r in recs] == [1, 2], "seq 應單調遞增"


def test_chain_hash_is_pure_and_order_independent():
    p = {"a": "1", "b": "2"}
    assert al.chain_hash("x", p) == al.chain_hash("x", {"b": "2", "a": "1"})
    assert al.chain_hash("x", p) != al.chain_hash("y", p)


# ── 續寫既有檔(重啟後 chain 不斷) ──────────────────────────────────────────
def test_reopen_continues_chain(tmp_path):
    lg1 = _ledger(tmp_path)
    lg1.record(al.SURFACE_HIS_MENU, "F2", value="1")
    lg2 = _ledger(tmp_path)                       # 模擬程式重啟
    lg2.record(al.SURFACE_HIS_MENU, "F3", value="2")
    recs = al.read_records(lg1.path)
    assert recs[1]["prev"] == recs[0]["hash"], "重啟後應接上舊檔末筆"
    assert al.verify_chain(lg1.path)[0] is True


# ── 輪替 ───────────────────────────────────────────────────────────────────
def test_rotation_bounds_file_size(tmp_path):
    lg = _ledger(tmp_path, max_bytes=400, keep=2)
    for i in range(40):
        lg.record(al.SURFACE_HIS_FIELD, "x" * 20, value=str(i))
    assert os.path.exists(lg.path + ".1"), "超過上限應輪替"
    assert not os.path.exists(lg.path + ".3"), "只保留 keep 代"
    # 輪替後仍可續寫、且新檔自身 chain 連續
    assert al.verify_chain(lg.path)[0] is True
    # 各代串起來也應是一條完整的鏈(抓輪替交界斷鏈)
    ok, n, msg = al.verify_generations(lg.path, keep=2)
    assert ok is True and n > 0, msg


def test_rotation_interruption_keeps_chain_linked(tmp_path):
    # [codex P2] 輪替把 base 改名成 .1 後、新 base 還沒寫就當機 → 重啟時若讀不到 base
    # 就從 genesis 另起,會斷鏈。應改為回頭接上 .1 的末筆。
    lg1 = _ledger(tmp_path)
    lg1.record(al.SURFACE_HIS_MENU, "F2", value="1")
    os.replace(lg1.path, lg1.path + ".1")          # 模擬「改名後就當機」
    lg2 = _ledger(tmp_path)                        # 重啟
    lg2.record(al.SURFACE_HIS_MENU, "F3", value="2")
    old = al.read_records(lg1.path + ".1")
    new = al.read_records(lg1.path)
    assert new[0]["prev"] == old[-1]["hash"], "應接上 .1 末筆,不可從 genesis 另起"
    assert new[0]["seq"] == old[-1]["seq"] + 1, "seq 應延續"
    assert al.verify_generations(lg1.path)[0] is True


def test_hard_cap_stops_growth_when_rotation_fails(tmp_path, monkeypatch):
    # [codex P2] 輪替失敗(檔案被鎖/權限)時不能無限長大 → 超過硬上限就丟紀錄
    lg = _ledger(tmp_path, max_bytes=300, keep=2, hard_max_bytes=600)
    monkeypatch.setattr(lg, "_rotate_if_needed", lambda: None)   # 輪替永遠失敗
    wrote = sum(1 for i in range(200)
                if lg.record(al.SURFACE_HIS_FIELD, "x" * 20, value=str(i)))
    assert os.path.getsize(lg.path) < 2000, "輪替失敗時仍須被硬上限擋住,不可無限長大"
    assert wrote < 200, "超過硬上限的紀錄應被丟棄(回 False)"


def test_verify_generations_detects_missing_all(tmp_path):
    ok, _, msg = al.verify_generations(str(tmp_path / "gone.jsonl"))
    assert ok is False and "不存在" in msg


# ── 截頭 / 截尾(anchor)──────────────────────────────────────────────────────
def test_anchor_detects_tail_truncation(tmp_path):
    # [codex P1] 鏈自己證不了「後面還有沒有」→ 砍掉最後幾筆,靠 anchor 抓出來
    lg = _ledger(tmp_path)
    for v in ("a", "b", "c"):
        lg.record(al.SURFACE_HIS_MENU, "F2", value=v)
    assert al.verify_generations(lg.path)[0] is True
    lines = open(lg.path, encoding="utf-8").read().splitlines()
    open(lg.path, "w", encoding="utf-8").write("\n".join(lines[:-1]) + "\n")  # 砍尾
    ok, _, msg = al.verify_generations(lg.path)
    assert ok is False and "截尾" in msg


def test_anchor_detects_whole_file_replaced_by_shorter(tmp_path):
    # 整個檔被換成「只剩前面幾筆」的版本(鏈自身仍連續)→ 兩支 verify API 都要抓到。
    # [codex P1] verify_chain 也必須比對 anchor:它是公開 API,不可對截尾回報 True。
    lg = _ledger(tmp_path)
    for v in ("a", "b", "c", "d"):
        lg.record(al.SURFACE_HIS_MENU, "F2", value=v)
    lines = open(lg.path, encoding="utf-8").read().splitlines()
    open(lg.path, "w", encoding="utf-8").write("\n".join(lines[:2]) + "\n")
    assert al.verify_chain(lg.path)[0] is False, "verify_chain 也須靠 anchor 抓截尾"
    assert al.verify_generations(lg.path)[0] is False


def test_head_truncation_detected(tmp_path):
    # [codex P1] 砍掉開頭:首筆自稱 genesis 起點卻 seq!=1 → 抓得到
    lg = _ledger(tmp_path)
    for v in ("a", "b", "c"):
        lg.record(al.SURFACE_HIS_MENU, "F2", value=v)
    recs = al.read_records(lg.path)
    # 偽造:留下第 2、3 筆,並把第 2 筆的 prev 改成 genesis 假裝是起點
    recs[1]["prev"] = al.GENESIS
    payload = {k: v for k, v in recs[1].items() if k != "hash"}
    recs[1]["hash"] = al.chain_hash(al.GENESIS, payload)
    recs[2]["prev"] = recs[1]["hash"]
    payload3 = {k: v for k, v in recs[2].items() if k != "hash"}
    recs[2]["hash"] = al.chain_hash(recs[1]["hash"], payload3)
    with open(lg.path, "w", encoding="utf-8") as f:
        for r in recs[1:]:
            f.write(al._canonical(r) + "\n")
    os.remove(lg.anchor_path)          # 連 anchor 一起湮滅,只靠 seq 規則
    ok, _, msg = al.verify_generations(lg.path)
    assert ok is False and "截頭" in msg


def test_missing_anchor_on_nonempty_ledger_fails(tmp_path):
    # [codex P1] 非空帳本卻沒 anchor → 判失敗。否則「把 anchor 一起刪掉」就能掩飾截尾。
    lg = _ledger(tmp_path)
    lg.record(al.SURFACE_HIS_MENU, "F2", value="a")
    os.remove(lg.anchor_path)
    ok, _, msg = al.verify_generations(lg.path)
    assert ok is False and "anchor" in msg
    # [codex P1 pass4] 公開的 verify_chain 也不可放行(刪 anchor + 截尾 = 矇混)
    ok2, _, msg2 = al.verify_chain(lg.path)
    assert ok2 is False and "anchor" in msg2


def test_verify_chain_rejects_anchor_deleted_then_truncated(tmp_path):
    # [codex P1 pass4] 最現實的掩飾手法:砍掉尾巴 + 把 anchor 一起刪掉
    lg = _ledger(tmp_path)
    for v in ("a", "b", "c"):
        lg.record(al.SURFACE_HIS_MENU, "F2", value=v)
    lines = open(lg.path, encoding="utf-8").read().splitlines()
    open(lg.path, "w", encoding="utf-8").write("\n".join(lines[:-1]) + "\n")
    os.remove(lg.anchor_path)
    assert al.verify_chain(lg.path)[0] is False
    assert al.verify_generations(lg.path)[0] is False


def test_emptying_a_populated_ledger_is_detected(tmp_path):
    # [codex P1 pass5] 把有紀錄的帳本清成 0 bytes 不是「空帳本」,是整本被清空 →
    # anchor 說末筆 seq=N,卻一筆都不剩 → 兩支 verify 都必須抓到。
    lg = _ledger(tmp_path)
    for v in ("a", "b", "c"):
        lg.record(al.SURFACE_HIS_MENU, "F2", value=v)
    open(lg.path, "w", encoding="utf-8").close()      # 清空但保留檔案
    ok, _, msg = al.verify_chain(lg.path)
    assert ok is False and "清空" in msg
    assert al.verify_generations(lg.path)[0] is False


def test_rotation_aborts_when_boundary_cannot_be_persisted(tmp_path, monkeypatch):
    # [codex P2 pass5] anchor 邊界寫不進去就【不准】刪最舊一代 —— 否則檔案少一代、
    # anchor 還是舊邊界 → 之後永遠誤判截頭且救不回來。
    lg = _ledger(tmp_path, max_bytes=300, keep=2)
    for i in range(40):
        lg.record(al.SURFACE_HIS_FIELD, "x" * 20, value=str(i))
    assert os.path.exists(lg.path + ".2"), "先把保留代數塞滿"
    before = open(lg.path + ".2", encoding="utf-8").read()
    monkeypatch.setattr(lg, "_write_anchor", lambda *a, **k: False)   # anchor 寫入失敗
    for i in range(40):
        lg.record(al.SURFACE_HIS_FIELD, "y" * 20, value=str(i))
    assert os.path.exists(lg.path + ".2"), "邊界寫不進去 → 應放棄輪替,不得刪最舊一代"
    assert open(lg.path + ".2", encoding="utf-8").read() == before


def test_rotation_aborts_when_boundary_cannot_be_computed(tmp_path, monkeypatch):
    # [codex P2 pass6] 下一代讀不到/毀損 → 算不出替代邊界 → 也不可刪最舊一代
    # (刪掉是不可逆的;沒有耐久的新邊界就砍,會製造永久誤判截頭的狀態)。
    lg = _ledger(tmp_path, max_bytes=300, keep=2)
    for i in range(40):
        lg.record(al.SURFACE_HIS_FIELD, "x" * 20, value=str(i))
    assert os.path.exists(lg.path + ".2"), "先把保留代數塞滿"
    before = open(lg.path + ".2", encoding="utf-8").read()
    monkeypatch.setattr(al, "_first_seq_of", lambda p: None)   # 邊界算不出來
    for i in range(40):
        lg.record(al.SURFACE_HIS_FIELD, "y" * 20, value=str(i))
    assert os.path.exists(lg.path + ".2"), "算不出邊界 → 應放棄輪替,不得刪最舊一代"
    assert open(lg.path + ".2", encoding="utf-8").read() == before


def test_verify_generations_requires_oldest_seq(tmp_path):
    # [codex P2 pass5] 只留 last_seq/last_hash、把 oldest_seq 拿掉的 anchor 不得跳過截頭檢查
    lg = _ledger(tmp_path)
    lg.record(al.SURFACE_HIS_MENU, "F2", value="a")
    a = al.read_anchor(lg.path)
    a.pop("oldest_seq", None)
    with open(lg.anchor_path, "w", encoding="utf-8") as f:
        json.dump(a, f)
    ok, _, msg = al.verify_generations(lg.path)
    assert ok is False and "oldest_seq" in msg


def test_malformed_anchor_is_rejected(tmp_path):
    # [codex P1 pass4] 光是「檔案存在/dict 非空」不算數;殘缺 anchor 不得放行
    lg = _ledger(tmp_path)
    lg.record(al.SURFACE_HIS_MENU, "F2", value="a")
    for bad in ({}, {"foo": 1}, {"last_seq": 0, "last_hash": ""},
                {"last_seq": 5}, {"last_hash": "x"}):
        with open(lg.anchor_path, "w", encoding="utf-8") as f:
            json.dump(bad, f)
        assert al.verify_chain(lg.path)[0] is False, f"殘缺 anchor {bad} 不得放行"


def test_rotation_crash_before_delete_is_benign_not_false_truncation(tmp_path):
    # [codex P2] 保留代數已滿時,輪替【先寫新邊界 anchor、再刪最舊一代】。若在兩者之間
    # 當機 → 留存的比 anchor 宣稱的【多】,那是良性的,絕不可被誤判成截頭(否則永久卡住)。
    lg = _ledger(tmp_path, max_bytes=300, keep=2)
    for i in range(40):
        lg.record(al.SURFACE_HIS_FIELD, "x" * 20, value=str(i))
    assert al.verify_generations(lg.path, keep=2)[0] is True
    # 手動把 anchor 的邊界往後推(模擬「已寫新邊界但最舊那代還沒被刪掉」)
    a = al.read_anchor(lg.path)
    recs = []
    for seg in (lg.path + ".2", lg.path + ".1", lg.path):
        recs.extend(al.read_records(seg))
    a["oldest_seq"] = int(recs[0]["seq"]) + 3      # 宣稱的邊界比實際留存的還新
    with open(lg.anchor_path, "w", encoding="utf-8") as f:
        json.dump(a, f)
    ok, _, msg = al.verify_generations(lg.path, keep=2)
    assert ok is True, f"留存比宣稱的多 = 良性,不可誤判截頭:{msg}"


def test_prefix_deletion_after_rotation_detected(tmp_path):
    # [codex P1] 輪替後,最舊留存段的首筆 prev 非 genesis → 只靠鏈看不出前面被刪;
    # 靠 anchor 記的 oldest_seq 抓。
    lg = _ledger(tmp_path, max_bytes=400, keep=2)
    for i in range(30):
        lg.record(al.SURFACE_HIS_FIELD, "x" * 20, value=str(i))
    assert al.verify_generations(lg.path, keep=2)[0] is True
    # 從最舊留存段砍掉開頭幾筆
    oldest = lg.path + ".2" if os.path.exists(lg.path + ".2") else lg.path + ".1"
    lines = open(oldest, encoding="utf-8").read().splitlines()
    open(oldest, "w", encoding="utf-8").write("\n".join(lines[2:]) + "\n")
    ok, _, msg = al.verify_generations(lg.path, keep=2)
    assert ok is False and "截頭" in msg


def test_anchor_tracks_last_record(tmp_path):
    lg = _ledger(tmp_path)
    lg.record(al.SURFACE_HIS_MENU, "F2", value="a")
    lg.record(al.SURFACE_HIS_MENU, "F3", value="b")
    a = al.read_anchor(lg.path)
    recs = al.read_records(lg.path)
    assert a["last_seq"] == 2 and a["last_hash"] == recs[-1]["hash"]


# ── 稽核絕不可弄壞臨床功能 ──────────────────────────────────────────────────
def test_record_never_raises_on_unwritable_path(tmp_path):
    # 路徑不存在的目錄 → 寫檔失敗,但只能回 False,絕不可拋
    lg = al.ActionLedger(str(tmp_path / "no_such_dir" / "l.jsonl"))
    assert lg.record(al.SURFACE_HIS_MENU, "F2", value="51017") is False


def test_record_never_raises_on_weird_values(tmp_path):
    lg = _ledger(tmp_path)
    assert lg.record(al.SURFACE_HIS_FIELD, "F2", value=object()) is True
    assert lg.record(al.SURFACE_HIS_FIELD, "F2", value=None) is True
    assert al.read_records(lg.path)[1]["value"] == ""


def test_read_records_skips_corrupt_lines(tmp_path):
    lg = _ledger(tmp_path)
    lg.record(al.SURFACE_HIS_MENU, "F2", value="1")
    with open(lg.path, "a", encoding="utf-8") as f:
        f.write("{not json\n")
    assert len(al.read_records(lg.path)) == 1, "壞行跳過,不拋"


# ── main.py 已把高後果寫入面接上帳本 ─────────────────────────────────────────
def _drain_ledger_queue():
    """把佇列裡的項目交給 writer loop 的邏輯同步跑完(測試用,不起背景緒)。"""
    items = []
    while not main._ledger_queue.empty():
        items.append(main._ledger_queue.get_nowait())
    return items


def test_version_canary_snapshot_taken_at_action_time(monkeypatch):
    # [codex P2] 版本/裁決必須是【動作當下】的快照(找視窗時存的),不可讓背景緒稍後才採樣
    # —— 佇列積壓時 HIS 可能已重啟/升版,會記到錯的版本。
    monkeypatch.setattr(main, "_his_write_baseline_fp",
                        lambda: {"title_version": "1150713"})
    monkeypatch.setattr(main, "_ledger_queue", main.Queue(maxsize=8))
    monkeypatch.setattr(main, "_ledger_shutting_down", False)
    monkeypatch.setattr(main, "_ensure_ledger_writer", lambda: None)
    monkeypatch.setattr(main, "_his_canary_warned", False)
    monkeypatch.setattr(main, "_his_last_sample", ("", ""))
    # 找視窗時採樣 → 快照
    main._sample_his_write_contract("西醫門診醫師作業 V.1150713.02")
    assert main._his_last_sample == ("1150713.02", "ok")

    main._record_his_action(al.SURFACE_HIS_MENU, "F2 醫令代碼", main_hwnd=123,
                            value="51017")
    surface, action, fields, ts = _drain_ledger_queue()[0]
    # 版本已在入列時就定好(不是等背景緒)
    assert fields["his_version"] == "1150713.02" and fields["canary"] == "ok"
    assert surface == al.SURFACE_HIS_MENU and fields["value"] == "51017"
    assert ts, "動作時間須在熱鍵緒當下取,不可用背景緒落檔時間"

    # 即使之後 HIS 升版了,已入列那筆仍保有動作當下的版本
    main._sample_his_write_contract("西醫門診醫師作業 V.1150801.01")
    assert fields["his_version"] == "1150713.02", "已入列的紀錄不得被之後的新版本汙染"


def _start_writer(q):
    """啟動綁定在 q 的寫入緒;回一個會送哨兵並 join 的收工函式(避免測試間漏執行緒)。"""
    t = main.threading.Thread(target=main._ledger_writer_loop, args=(q,), daemon=True)
    t.start()

    def _stop():
        q.put(None)          # 哨兵
        t.join(timeout=2.0)
    return _stop


def test_writer_persists_queued_snapshot_verbatim(monkeypatch):
    got = {}
    monkeypatch.setattr(main, "_action_ledger",
                        lambda: type("L", (), {"record": lambda s, *a, **k:
                                               got.update(a=a, k=k) or True})())
    q = main.Queue(maxsize=8)
    q.put_nowait(
        (al.SURFACE_HIS_MENU, "F2", {"his_version": "1150713.02", "value": "51017"},
         "2026-07-17T10:00:00"))
    stop = _start_writer(q)
    q.join()
    stop()
    assert got["k"]["his_version"] == "1150713.02"
    assert got["k"]["ts"] == "2026-07-17T10:00:00", "須用動作當下的時間,不是落檔時間"


def test_flush_before_exit_drains_queue(monkeypatch):
    # [codex P2] 寫入緒是 daemon,os._exit(0) 會直接砍掉 → 關閉前須有上限排空
    q = main.Queue(maxsize=8)
    monkeypatch.setattr(main, "_ledger_queue", q)
    monkeypatch.setattr(main, "_ledger_writer_started", True)
    # 這個旗標是模組全域,務必讓 monkeypatch 在測試結束後還原,否則後面的測試會被
    # 當成「關閉中」而靜默丟棄紀錄。
    monkeypatch.setattr(main, "_ledger_shutting_down", False)
    monkeypatch.setattr(main, "_action_ledger",
                        lambda: type("L", (), {"record": lambda s, *a, **k: True})())
    for _ in range(3):
        q.put_nowait((al.SURFACE_HIS_MENU, "F2", {}, "ts"))
    stop = _start_writer(q)
    try:
        assert main._flush_ledger_before_exit(timeout=2.0) is True
        assert q.empty()
    finally:
        stop()


def test_flush_before_exit_is_bounded(monkeypatch):
    # 排空絕不可無限拖延關閉:沒人消費時也必須在逾時內放棄
    monkeypatch.setattr(main, "_ledger_queue", main.Queue(maxsize=8))
    monkeypatch.setattr(main, "_ledger_writer_started", True)
    monkeypatch.setattr(main, "_ledger_shutting_down", False)
    main._ledger_queue.put_nowait((al.SURFACE_HIS_MENU, "F2", {}, "ts"))
    t0 = time.time()
    assert main._flush_ledger_before_exit(timeout=0.2) is False
    assert time.time() - t0 < 1.0, "必須在逾時內放棄,不可拖延關閉"


def test_flush_waits_for_inflight_write_not_just_empty_queue(monkeypatch):
    # [codex P2] 最後一筆【被取走】的瞬間 queue 就空了,但 record() 可能還在寫 ——
    # 那時 os._exit 會砍掉寫到一半的動作。排空必須等到真的寫完(task_done)。
    q = main.Queue(maxsize=8)
    monkeypatch.setattr(main, "_ledger_queue", q)
    monkeypatch.setattr(main, "_ledger_writer_started", True)
    monkeypatch.setattr(main, "_ledger_shutting_down", False)
    done = []

    def _slow_record(*a, **k):
        time.sleep(0.4)          # 慢速寫入(佇列此時已空)
        done.append(1)
        return True
    monkeypatch.setattr(main, "_action_ledger",
                        lambda: type("L", (), {"record": lambda s, *a, **k:
                                               _slow_record()})())
    q.put_nowait((al.SURFACE_HIS_MENU, "F2", {}, "ts"))
    stop = _start_writer(q)
    try:
        assert main._flush_ledger_before_exit(timeout=3.0) is True
        assert done == [1], "排空必須等到 record() 真的寫完,不能只看 queue.empty()"
    finally:
        stop()


def test_shutdown_stops_accepting_new_records(monkeypatch):
    # 排空期間不可再收新項目,否則排空永遠追不上、拖延關閉
    q = main.Queue(maxsize=8)
    monkeypatch.setattr(main, "_ledger_queue", q)
    monkeypatch.setattr(main, "_ledger_writer_started", False)
    monkeypatch.setattr(main, "_ledger_shutting_down", False)
    monkeypatch.setattr(main, "_ensure_ledger_writer", lambda: None)
    main._flush_ledger_before_exit(timeout=0.1)
    main._record_his_action(al.SURFACE_HIS_MENU, "F2", main_hwnd=1)
    assert q.empty(), "關閉排空後不得再收新的稽核項目"


def test_exit_path_flushes_ledger_before_os_exit():
    # [codex P2] 關閉/更新重啟前須真的排空(daemon 寫入緒會被 os._exit(0) 直接砍掉)
    whole = open(main.__file__, encoding="utf-8").read()
    idx = whole.index("os._exit(0)")
    before_exit = whole[max(0, idx - 2000):idx]
    assert "_flush_ledger_before_exit" in before_exit, \
        "os._exit(0) 之前應先有上限地排空稽核佇列"


def test_record_his_action_never_blocks_hotkey_thread(monkeypatch):
    # [codex P1] 熱鍵緒【絕不可】等待:取 title 會等 3s、寫檔可能卡住 → 都必須在背景。
    # 這裡讓 title 取樣與落檔都「超級慢」,_record_his_action 仍須立刻返回。
    def _slow_title(h):
        time.sleep(5)
        return "西醫門診醫師作業 V.1150713.02"
    monkeypatch.setattr(main, "_his_title_of", _slow_title)
    monkeypatch.setattr(main, "_action_ledger",
                        lambda: type("L", (), {"record": lambda s, *a, **k:
                                               time.sleep(5) or True})())
    monkeypatch.setattr(main, "_ledger_queue", main.Queue(maxsize=8))
    monkeypatch.setattr(main, "_ledger_shutting_down", False)
    monkeypatch.setattr(main, "_ensure_ledger_writer", lambda: None)
    t0 = time.time()
    main._record_his_action(al.SURFACE_HIS_MENU, "F11 完成不印", main_hwnd=123)
    assert time.time() - t0 < 0.5, "熱鍵緒不得因取 title/寫檔而阻塞"


def test_record_his_action_drops_when_queue_full_without_waiting(monkeypatch):
    # [codex P1] 佇列滿 → 丟棄並回報,絕不等待(寧可少一筆稽核,不可拖住 F 鍵)
    monkeypatch.setattr(main, "_ledger_queue", main.Queue(maxsize=2))
    monkeypatch.setattr(main, "_ledger_shutting_down", False)
    monkeypatch.setattr(main, "_ensure_ledger_writer", lambda: None)
    monkeypatch.setattr(main, "_ledger_dropped", 0)
    t0 = time.time()
    for _ in range(10):
        main._record_his_action(al.SURFACE_HIS_MENU, "F2", main_hwnd=1)
    assert time.time() - t0 < 0.5, "佇列滿也不可等待"
    assert main._ledger_dropped >= 8, "滿了要計數丟棄"


def test_record_his_action_swallows_all_errors(monkeypatch):
    # 稽核【絕不可】弄壞臨床功能:入列路徑爆炸也只能吞掉
    def _boom():
        raise RuntimeError("queue exploded")
    monkeypatch.setattr(main, "_ensure_ledger_writer", _boom)
    main._record_his_action(al.SURFACE_HIS_MENU, "F2", main_hwnd=0)   # 不得拋


def test_ledger_writer_loop_survives_record_exception(monkeypatch):
    # 背景緒也不可因單筆失敗就死掉(死了之後全部稽核靜默消失)
    monkeypatch.setattr(main, "_his_title_of", lambda h: "")
    calls = {"n": 0}

    def _rec(*a, **k):
        calls["n"] += 1
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(main, "_action_ledger",
                        lambda: type("L", (), {"record": lambda s, *a, **k: _rec()})())
    q = main.Queue(maxsize=8)
    for _ in range(3):
        q.put_nowait(("s", "a", {}, "ts"))
    stop = _start_writer(q)
    q.join()
    stop()
    assert calls["n"] == 3, "單筆失敗後仍須繼續處理後續紀錄"


def test_no_readback_paths_record_submitted_unverified_not_ok():
    # [GPT-5.6 第三輪] 醫令代碼與 F11 完成(不印/全部完成)都【無回讀】——「PostMessage
    # 被 Windows 接受」不可記成 ok,最多 submitted_unverified;否則帳本假成功。
    for fn, why in ((main._script_code_input_adaptive, "醫令代碼"),
                    (main._f11_send_finish_no_print, "F11 完成不印"),
                    (main._f11_click_finish_all, "F11 全部完成")):
        src = inspect.getsource(fn)
        assert "_LEDGER_SUBMITTED" in src, f"{why} 無回讀 → 應記 submitted_unverified"
        assert "outcome=_LEDGER_OK" not in src, f"{why} 無回讀 → 不得記 ok"


def test_f11_route_b_honors_click_result_and_audits():
    # [GPT-5.6 第三輪 P1] route B(全部完成)原本忽略 click 結果直接回 True 且零稽核
    src = inspect.getsource(main._f11_click_finish_all)
    assert "click_ok = _post_click_to_control" in src, "須檢查 click 送出結果"
    assert "_record_his_action(" in src, "route B 也是 F11 完成動作,必須記帳"
    assert "if not click_ok:" in src and "return False" in src, \
        "click 送出失敗不得回報成功"


def test_high_consequence_writes_are_wired_to_ledger():
    # 後果最高、且【無回讀】的醫令代碼與完成/同意書選單,必須留下紀錄
    code_src = inspect.getsource(main._script_code_input_adaptive)
    assert "_record_his_action(" in code_src, "醫令代碼寫入應記帳"
    assert "_LEDGER_SKIPPED" in code_src, "焦點不對→未送出,也要記(改版時走這條)"

    for fn, why in ((main._f11_send_finish_no_print, "F11 完成不印"),
                    (main.script_F9_F10_consent_form_adaptive, "F9/F10 同意書"),
                    (main._update_uvb_dose_core, "UVB 劑量"),
                    (main._set_療程_only, "療程"),
                    (main._set_身份_自費, "身份")):
        assert "_record_his_action(" in inspect.getsource(fn), f"{why} 應記帳"


def test_readback_mismatch_paths_record_mismatch():
    # 回讀對不上是改版寫錯病歷的關鍵線索 → 必須以 mismatch 記錄,不能只 log
    for fn, why in ((main._update_uvb_dose_core, "UVB"),
                    (main._set_療程_only, "療程"),
                    (main._set_身份_自費, "身份")):
        assert "_LEDGER_MISMATCH" in inspect.getsource(fn), f"{why} 回讀不符應記 mismatch"


def test_card_number_is_not_written_in_plaintext():
    # 卡號屬識別/計費資料 → 帳本只記回讀結果,不得寫明文(GPT P0#3/#6)
    src = inspect.getsource(main._autofill_卡號_from_醫師上次)
    assert "已遮罩" in src, "卡號應遮罩"
    assert "value=result.card" not in src and "value=str(result.card)" not in src, \
        "卡號不得以明文寫進帳本"


def test_sampled_his_field_text_never_reaches_ledger():
    # [codex P1] 「疑似定位錯欄」與「回讀不符」這兩條分支,採樣到的欄位原文可能正是
    # 姓名/病歷號/卡號 —— 那是【誤抓】才觸發的分支。帳本只能記長度,絕不可記內容。
    for fn, why in ((main._set_療程_only, "療程"), (main._set_身份_自費, "身份")):
        src = inspect.getsource(fn)
        # 找出所有傳給帳本的 detail=,不得內插原始採樣變數
        for leak in ("detail=f\"回讀={", "{_療程_before!r}", "{after!r}", "{before!r}"):
            # 允許出現在 _show_uvb_warning(醫師自己的螢幕),但不得出現在 _record_his_action
            for chunk in src.split("_record_his_action(")[1:]:
                call = chunk.split(")\n")[0]
                assert leak not in call, f"{why}:採樣原文 {leak} 不得進帳本({call[:80]})"
        assert "已遮罩" in src, f"{why} 應以遮罩/長度取代原文"
