# -*- coding: utf-8 -*-
"""[2026-07-25 未審區域 review] UVB/excimer 劑量安全 —— 兩個 P0(皆已實測重現)

共同病灶:**四條劑量路徑的防護不一致**。有日期的 UVB 路徑防得很好,但
「首次治療(無日期)」與「excimer」兩條路徑各自漏掉了姊妹路徑早就有的檢查,
而且它們寫回病歷時【沒有回讀驗證、也不跳確認框】。

P0-1 首次治療缺 max_dose 把關:
    「MAX: 1,500 mj/cm2」的千分位逗號讓 _UVB_MAX_RE 只吃到 1
    → min(500+30, 1) = 1 → 處置欄被寫成「UVB 1 mj/cm2」(500 → 1)。
    「fixed 3 times per week」同理被當成 MAX=3。
P0-2 excimer 把遞減當成遞增:
    「decrease 50 each time」→ 600 變 650(醫囑要求減 50,程式加了 50)。
    UC-03 早就為 UVB 做了方向安全判定,註解寫「excimer 多段路徑…不動」——
    那記錄的是修正範圍,不是安全論證。
"""
import inspect
import os
import re
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import uvb_dose as u  # noqa: E402


def _code_only(src: str) -> str:
    """剝掉註解，只留程式碼——避免源碼守門測試比對到說明文字而誤判。"""
    out = []
    for line in src.splitlines():
        i = line.find("#")
        out.append(line if i < 0 else line[:i])
    return "\n".join(out)

TODAY = date(2026, 7, 25)


def _run(text):
    return u.update_uvb_in_text(text, today=TODAY)


# ── P0-1:首次治療(無日期)的 MAX 把關 ────────────────────────────────────────
@pytest.mark.parametrize("text,why", [
    ("UVB 500 mj/cm2, increase 30 mj/cm2 if no erythema, MAX: 1,500 mj/cm2",
     "千分位逗號 → MAX 被讀成 1"),
    ("UVB 500 mj/cm2, increase 30 mj/cm2 if no erythema, fixed 3 times per week",
     "「fixed N times」被當成 MAX=3"),
    ("UVB 250 mj/cm2, increase 30 each time until 3 weeks later",
     "「until N weeks」被當成 MAX=3"),
])
def test_first_time_rejects_implausible_max(text, why):
    """★病人安全★ 首次治療若算出的 MAX 低於下限 → 一律不寫,交醫師。"""
    r = _run(text)
    assert r.action == u.UvbAction.SANITY_FAIL, f"{why}：{r.action}"
    assert r.new_dose is None
    assert r.sanity_reason, "應說明失敗原因給醫師看"


def test_first_time_rejects_dose_above_max():
    r = _run("UVB 900 mj/cm2, increase 30 mj/cm2, MAX: 800 mj/cm2")
    assert r.action == u.UvbAction.SANITY_FAIL
    assert "超過" in (r.sanity_reason or "")


def test_first_time_normal_case_still_works():
    """正常首次治療不受影響(不可因為加防護就停止服務)。"""
    r = _run("UVB 500 mj/cm2, increase 30 mj/cm2 if no erythema, MAX: 1500 mj/cm2")
    assert r.action == u.UvbAction.UPDATED
    assert r.new_dose == 530


def test_first_time_without_max_is_left_alone():
    """沒有 increase/MAX 的首次治療本來就 silent_skip（既有設計，非本次改動）。
    釘住它，確認新增的守門沒有把它變成別的行為。"""
    r = _run("UVB 500 mj/cm2")
    assert r.action == u.UvbAction.SILENT_SKIP
    assert r.new_dose is None


def test_dated_path_unchanged_by_this_fix():
    """有日期的路徑本來就擋這種 MAX,行為不可被改動。"""
    r = _run("UVB 500 mj/cm2 (10) on (2026/7/20), increase 30, MAX: 1,500 mj/cm2")
    assert r.action == u.UvbAction.SANITY_FAIL


# ── P0-2:excimer 的方向安全 ────────────────────────────────────────────────
@pytest.mark.parametrize("phrase", [
    # _UVB_INCREASE_RE 認得的寫法(舊版靠「方向盲有命中、方向安全沒有」判出來)
    "decrease 50 each time",
    "reduce dose by 50 each time",
    "taper 50 each time",
    "-50 each time",
    # ★外審 F1★ _UVB_INCREASE_RE 【完全不命中】的寫法 —— 舊版判不出遞減,
    # increase 當 0 → 回 UPDATED 把【原劑量 600 照寫】並把日期/次數推進成「今天照過
    # 600」。醫囑要求減量(多半因紅斑/灼傷),照原劑量給同樣有害,病歷還被記成已照。
    "每次減 50",
    "decrease 50",
    "每次減少 30",
    "減量到 300",
    "降至 250",
    "調降 50",
    "decrease to 400",
])
def test_excimer_decrease_never_auto_updates(phrase):
    """★病人安全★ 遞減醫囑不得被當成遞增,【也不得以原劑量照寫】。

    「不是 650 就好」是不夠的驗收條件(外審 F1 正是這樣被放過):維持 600 一樣有害。
    唯一可接受的結果是 —— 完全不更新,並讓醫師看到。"""
    text = (f"excimer light 600 mj/cm2 (10) on (2026/7/20), {phrase}, MAX 1000")
    r = _run(text)
    assert r.action != u.UvbAction.UPDATED, (
        f"「{phrase}」被自動更新成 {r.new_dose}（醫療安全紅線：遞減醫囑一律交醫師）")
    assert r.new_dose is None
    # ★外審 F3★ 只是「不更新」還不夠:純 excimer 呼叫端對非 UPDATED 只寫 log 就繼續
    # 設身份,醫師看不到任何提示 → 可能沿用未減量的原劑量。必須回可顯示的原因。
    assert r.action == u.UvbAction.SANITY_FAIL
    assert r.decrease_note, "必須回報是哪一段沒被更新"
    assert r.sanity_reason and r.decrease_note in r.sanity_reason


@pytest.mark.parametrize("tail", [
    # ★外審 R2★ 同一行常有別的醫囑。藥物的 taper 不可壓掉光療【明寫】的 increase,
    # 否則合法醫囑停止服務(誤擋也有代價:醫師得手動)。實測舊版兩者都被擋。
    "; MTX 6# QW taper to 3#",
    ", dupi taper to 4w hold on 0819",
    "; cyclosporine 100mg reduce to 50mg",
])
def test_unrelated_drug_taper_does_not_block_excimer_increase(tail):
    r = _run("excimer light 600 mj/cm2 (10) on (2026/7/20), "
             f"increase 50 each time, MAX 1000{tail}")
    assert r.action == u.UvbAction.UPDATED, f"被「{tail}」誤擋"
    assert r.new_dose == 650
    assert r.decrease_note is None


def test_ceiling_written_before_dose_does_not_truncate_direction_scope():
    """★不可退步★ 實機也有上限寫在最前面的寫法(tests/test_uvb_dose.py 既有案例)。
    子句右界若無條件取第一個上限,會把後面的 add 30 切掉 → 810 不再加量。
    右界只能取【劑量之後】的上限。"""
    r = _run("Excimer light fixed at 1000, 810 (36) on (2026/7/22), add 30 each time")
    assert r.action == u.UvbAction.UPDATED
    assert r.new_dose == 840
    assert r.decrease_note is None


def test_decrease_after_ceiling_still_blocks():
    """★不可因為 R2 的收斂而漏掉 F1★ 遞減寫在 MAX 後面(仍是光療醫囑)必須照擋。"""
    r = _run("excimer light 600 mj/cm2 (10) on (2026/7/20), "
             "MAX 1000, decrease 50 each time")
    assert r.action == u.UvbAction.SANITY_FAIL
    assert r.decrease_note == "decrease 50"


@pytest.mark.parametrize("tail", [
    "MTX 6# QW taper to 3#",
    "dupi taper to 4w hold on 0819",
    "cyclosporine 100mg reduce to 50mg",
])
def test_hold_dose_not_blocked_by_post_ceiling_drug_orders(tail):
    """★外審 R4★ 上限之後最常接【別的藥物】醫囑。那些 taper/reduce 講的是藥,
    不可拿來擋光療 —— 沒有加量醫囑的 hold-dose 本來就該照舊維持劑量並更新日期/次數。
    判方向前先把「數字+藥物單位」抹掉(光療用 mJ 與 each time/每次,不受影響)。"""
    r = _run(f"excimer light 600 mj/cm2 (10) on (2026/7/20), MAX 1000, {tail}")
    assert r.action == u.UvbAction.UPDATED, f"hold 醫囑被「{tail}」誤擋"
    assert r.new_dose == 600
    assert r.decrease_note is None


def test_post_ceiling_photo_decrease_survives_drug_filter():
    """藥物過濾不可把【光療自己】的遞減也濾掉(它寫 mJ / each time / 每次)。"""
    for phrase in ("decrease 50 each time", "每次減 50", "decrease 50 mj each time"):
        r = _run("excimer light 600 mj/cm2 (10) on (2026/7/20), "
                 f"MAX 1000, {phrase}")
        assert r.action == u.UvbAction.SANITY_FAIL, phrase
        assert r.decrease_note


@pytest.mark.parametrize("phrase", ["increase 50 each time", "add 50 each time"])
def test_increase_after_ceiling_still_applies(phrase):
    """★外審 R3★ 上限之後的區域必須【兩個方向對稱】處理。只擋遞減卻不吃加量,
    會讓「MAX 1000, increase 50 each time」變成維持 600 照寫 + 次數推進成「今天照過」
    —— 與 F1 完全同一類的失效(舊版本來吃得到,不可退步)。"""
    r = _run(f"excimer light 600 mj/cm2 (10) on (2026/7/20), MAX 1000, {phrase}")
    assert r.action == u.UvbAction.UPDATED
    assert r.new_dose == 650


def test_ceiling_tail_with_both_directions_takes_safe_side():
    """上限之後同時出現遞減與加量(矛盾醫囑)→ 走安全側,不動。"""
    r = _run("excimer light 600 mj/cm2 (10) on (2026/7/20), MAX 1000, "
             "decrease 50 each time, increase 20 each time")
    assert r.action == u.UvbAction.SANITY_FAIL
    assert r.decrease_note == "decrease 50"


def test_excimer_decrease_reason_quotes_source_only():
    """★外審 F4★ 給醫師的訊息只能引用原文,不得推斷成因。"""
    r = _run("excimer light 600 mj/cm2 (10) on (2026/7/20), "
             "decrease 50 each time, MAX 1000")
    assert r.decrease_note == "decrease 50"
    assert "可能" not in (r.sanity_reason or ""), "不得寫程式並不確知的推測成因"


def test_strict_path_decrease_segment_never_borrows_next_segment_increase():
    """★外審 F2 病人安全★ UVB visit 的 Step D(嚴格路徑)只處理第一個 marker 段,
    但方向判定原本搜到行尾 → 第一段寫 decrease 50 卻借到第二段的 increase 20,
    實測把 600 寫成 620(遞減醫囑直接變成加量)。"""
    r = _run("UVB 300 mj/cm2 (5) on (2026/7/20), increase 20 each time, MAX 900; "
             "excimer light 600 mj/cm2 (10) on (2026/7/20), "
             "decrease 50 each time, MAX 1000, "
             "excimer light 400 mj/cm2 (8) on (2026/7/20), "
             "increase 20 each time, MAX 700")
    assert r.action == u.UvbAction.UPDATED, "UVB 主行本來就該更新(那是另一行)"
    assert "excimer light 620" not in r.new_text, "遞減段被借走隔壁的 increase"
    assert "excimer light 600 mj/cm2 (10) on (2026/7/20)" in r.new_text, \
        "遞減段必須原封不動(劑量、次數、日期都不可推進)"
    assert r.new_dose == 320, "UVB 主行照常 +20"
    # 醫師看到 UVB 更新成功時,必須同時知道有一段【沒有】被自動處理。
    assert r.decrease_note == "decrease 50"


def test_excimer_increase_still_works():
    """正向加量不可受影響。"""
    r = _run("excimer light 600 mj/cm2 (10) on (2026/7/20), "
             "increase 50 each time, MAX 1000")
    assert r.action == u.UvbAction.UPDATED
    assert r.new_dose == 650


def test_excimer_no_directive_holds_dose():
    r = _run("excimer light 600 mj/cm2 (10) on (2026/7/20), MAX 1000")
    assert r.action == u.UvbAction.UPDATED
    assert r.new_dose == 600


def test_excimer_shared_tail_metadata_still_updates_both_segments():
    """★不可退步★ figure 3:整句只寫一組日期/加量/上限,兩段共用(同次照光不同部位)。
    方向判定必須用與 _seg_meta 相同的搜尋範圍(段內優先、沒有才退回句尾),
    否則前段會因為加量字樣在句尾而被誤判成「有遞減醫囑」→ 停止更新。"""
    r = _run("excimer light 580mJ (151) for 嘴周圍, excimer light 440mJ (147) "
             "for 右下耳 (圓形), on (2026/7/22) add 10mJ each time fixed at 700mJ")
    assert r.action == u.UvbAction.UPDATED
    assert "590mJ (152) for 嘴周圍" in r.new_text
    assert "450mJ (148) for 右下耳" in r.new_text


def test_uvb_and_excimer_agree_on_direction():
    """同一句寫法,UVB 與 excimer 的方向判定結論必須一致(不可一個擋一個放行)。"""
    tail = " 600 mj/cm2 (10) on (2026/7/20), decrease 50 each time, MAX 1000"
    uvb = _run("UVB" + tail)
    exc = _run("excimer light" + tail)
    assert uvb.new_dose != 650 and exc.new_dose != 650
    assert uvb.action != u.UvbAction.UPDATED
    assert exc.action != u.UvbAction.UPDATED


@pytest.mark.parametrize("phrase,expect", [
    ("add 30 each time", 630),
    ("increase 30 each time", 630),
    ("每次加 30", 630),
    ("每次增加 30", 630),
    # 'lower' 是遞減動詞,但這裡它後面接的是 limbs 不是數字 → 不可誤判成遞減而停止服務。
    ("add 30 each time, keep on both lower limbs to 680", 630),
])
def test_excimer_increase_paths_not_over_blocked(phrase, expect):
    """★不可退步★ 誤擋也有代價(醫師得手動)。正向加量與含遞減字眼的描述句必須照常更新。"""
    r = _run(f"excimer light 600 mj/cm2 (10) on (2026/7/20), {phrase}, MAX 1000")
    assert r.action == u.UvbAction.UPDATED
    assert r.new_dose == expect
    assert r.decrease_note is None


# ── 呼叫端:醫師必須看得到(外審 F3 / R5)────────────────────────────────────
def _main_src(fn_name):
    import main
    return _code_only(inspect.getsource(getattr(main, fn_name)))


def test_every_excimer_writeback_path_surfaces_skipped_segment():
    """★外審 R5★ 純 excimer 有【兩條】寫回路徑(直接更新、stale/超限確認後重算)。
    第二條原本是另一份重複的處理碼,漏接了 F3 的兩個提示 —— 醫師寫回成功後完全
    不知道有一段沒被自動處理。兩條都必須呼叫同一個 helper。"""
    src = _main_src("_f23_pure_excimer_update")
    writes = [m.start() for m in re.finditer(r"_write_tmemo_text\(memo_hwnd", src)]
    assert len(writes) == 2, f"寫回路徑數量變了({len(writes)}),請同步檢查提示是否都接上"
    assert src.count("_warn_excimer_segment_skipped(") == 2, \
        "每一條寫回成功路徑都要提示「有段落未自動更新」"
    # 沒有更新任何一段的收尾(含確認後重算的 else)也不可只寫 log。
    assert src.count("_warn_excimer_not_updated(") == 2, \
        "「沒更新任何一段」的兩個收尾都要走 helper(SANITY_FAIL 要跳警告窗)"


def test_uvb_path_surfaces_skipped_excimer_segment_after_verify():
    """UVB 路徑(Step D)也會跳過 excimer 段。提示必須在【回讀驗證通過之後】——
    驗證前就宣稱「UVB 劑量已更新」是陳述程式尚未確認的事。"""
    src = _main_src("_update_uvb_dose_core")
    assert "decrease_note" in src, "UVB 路徑必須處理 decrease_note"
    i_verify = src.index("uvb_written_back_ok")
    i_note = src.index("decrease_note")
    assert i_note > i_verify, "提示必須排在回讀驗證之後"


def test_warn_helper_quotes_note_without_inferring_cause():
    """★外審 F4★ 提示只能引用原文,不得推斷成因。"""
    import main
    src = _code_only(inspect.getsource(main._warn_excimer_segment_skipped))
    assert "{note}" in src, "必須引用原文片段"
    assert "可能" not in src, "不得寫程式並不確知的推測成因"
