# -*- coding: utf-8 -*-
"""[2026-07-30 第二輪外審 P1-03] 宣告了保留期卻不主動執行,等於沒有保留期。

在此之前每一種落地檔各自清自己的,而且都有同一個結構性問題:
**清理只發生在「產生那種檔案的事情再度發生」的時候,而且大多只看數量、沒有時效。**

  * `patient_locator.append_index()` 只在【下一次有回讀不符】時才修剪 →
    宣告 `INDEX_RETAIN_DAYS = 30`,實際上某個病人的病歷號可以留一整年。
  * `autoclock.prune_debug_dumps()` 只看「最多 40 個檔」→ 只要總數沒破 40,
    含帳號的完整 screenshot 與 page_source HTML 可以永久留著。
  * `consult_query._prune_old_shots()` 同上(最多 60 張會診截圖)。
  * `settings_defaults.restore_defaults()` 的 `.before-reset-*` 完全沒人清。
  * `paths.sweep_old_restart_err_files()` 有 TTL,但要有人叫它。

修法:集中成一支不依賴任何事件的清掃器,由主程式在【啟動時】與【每日 07:15】
各跑一次 —— 兩者都要有:只有排程,每天重開程式的機器永遠跑不到;只有啟動,
跨夜長駐的機器可以好幾週不清,那正是「宣告 30 天卻留了一年」的成因。
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common.patient_locator import prune_index  # noqa: E402
from cmuh_common.retention import (  # noqa: E402
    RetentionRule, sweep,
)


def _touch(path, *, days_old=0.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    if days_old:
        old = time.time() - days_old * 86400.0
        os.utime(path, (old, old))
    return path


# ─── 純 TTL 的檔案清掃 ─────────────────────────────────────────────────────
def test_expired_files_go_and_fresh_ones_stay(tmp_path):
    d = tmp_path / "debug_dumps"
    old = _touch(d / "clock_20260101_010101.png", days_old=10)
    new = _touch(d / "clock_20260730_010101.png", days_old=0.5)
    rule = RetentionRule("打卡除錯檔", str(d), ("*.png",), 3)

    res = sweep([rule])

    assert not old.exists(), "★過期的含帳號截圖必須被刪掉★"
    assert new.exists(), "期限內的不可動"
    assert res.deleted == {"打卡除錯檔": 1}


def test_all_patterns_of_a_rule_are_covered(tmp_path):
    """★page_source HTML 是最大一塊敏感內容★ 規則漏掉某個副檔名就等於白做。"""
    d = tmp_path / "debug_dumps"
    for name in ("a.png", "a.html", "a.txt"):
        _touch(d / name, days_old=10)
    sweep([RetentionRule("x", str(d), ("*.png", "*.html", "*.txt"), 3)])

    assert list(d.iterdir()) == []


def test_a_missing_directory_is_silently_skipped(tmp_path):
    """有些機器從來沒產生過 debug dump —— 目錄不存在不可炸,也不可記成失敗。"""
    res = sweep([RetentionRule("x", str(tmp_path / "nope"), ("*",), 1)])
    assert res.deleted == {} and res.failed == {}


def test_sweep_never_raises_on_an_undeletable_file(tmp_path, monkeypatch):
    """清理失敗不可影響臨床流程(與 action_ledger / patient_locator 同一原則)。"""
    d = tmp_path / "x"
    _touch(d / "a.png", days_old=10)
    monkeypatch.setattr(os, "remove",
                        lambda *_a: (_ for _ in ()).throw(OSError("鎖住")))

    res = sweep([RetentionRule("x", str(d), ("*.png",), 1)])

    assert res.failed == {"x": 1}
    assert "刪不掉" in res.summary()


def test_the_oldest_sensitive_file_is_reported(tmp_path):
    """健康中心要能顯示「最舊敏感檔日期」—— 不然沒人知道機器上還躺著什麼。"""
    d = tmp_path / "x"
    _touch(d / "old.png", days_old=2.5)
    _touch(d / "new.png", days_old=0.1)
    res = sweep([RetentionRule("打卡除錯檔", str(d), ("*.png",), 30)])

    assert res.oldest is not None
    label, when = res.oldest
    assert label == "打卡除錯檔"
    assert (datetime.now() - when) > timedelta(days=2)


def test_a_non_sensitive_rule_does_not_claim_to_be_the_oldest(tmp_path):
    """★不可矯枉過正★ 設定備份不含個資,不該被算進「最舊敏感檔」而誤導。"""
    d = tmp_path / "x"
    _touch(d / "config.json.before-reset-1", days_old=50)
    res = sweep([RetentionRule("設定備份", str(d), ("*.before-reset-*",),
                               90, sensitive=False)])
    assert res.oldest is None


# ─── 定位索引:逐【列】修剪,檔案要留著 ─────────────────────────────────────
def test_the_locator_index_is_pruned_without_a_new_mismatch(tmp_path):
    """★核心痛點★ 原本修剪只寫在 append_index 裡 —— 沒有新的回讀不符,
    病歷號就一直留著。"""
    p = tmp_path / "patient_locator_index.jsonl"
    now = datetime(2026, 7, 30, 9, 0, 0)
    rows = [
        {"ts": (now - timedelta(days=40)).isoformat(timespec="seconds"),
         "action": "舊", "room": "103", "chart_no": "24994923"},
        {"ts": (now - timedelta(days=5)).isoformat(timespec="seconds"),
         "action": "新", "room": "105", "chart_no": "11111111"},
    ]
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
                 + "\n", encoding="utf-8")

    removed = prune_index(str(p), now=now)

    assert removed == 1
    left = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()
            if x.strip()]
    assert [r["action"] for r in left] == ["新"]
    assert "24994923" not in p.read_text(encoding="utf-8"), \
        "★過期病歷號必須真的從檔案裡消失★"


def test_pruning_a_missing_index_is_a_no_op(tmp_path):
    assert prune_index(str(tmp_path / "nope.jsonl")) == 0


def test_pruning_never_raises_and_keeps_the_file_on_error(tmp_path,
                                                          monkeypatch):
    p = tmp_path / "idx.jsonl"
    p.write_text('{"ts":"2020-01-01T00:00:00","action":"a"}\n', encoding="utf-8")
    monkeypatch.setattr(os, "replace",
                        lambda *_a: (_ for _ in ()).throw(OSError("鎖住")))
    assert prune_index(str(p)) == 0
    assert p.exists(), "失敗時原檔必須留著"


# ─── extra_tasks ──────────────────────────────────────────────────────────
def test_extra_tasks_are_counted_and_isolated():
    def _boom():
        raise OSError("壞了")
    res = sweep([], extra_tasks=[("好的", lambda: 3), ("壞的", _boom)])
    assert res.deleted == {"好的": 3}
    assert res.failed == {"壞的": 1}, "一個任務炸掉不可影響其他任務"


# ─── 接線:啟動與每日都要有 ────────────────────────────────────────────────
def test_main_runs_the_sweep_at_startup_and_daily():
    """★兩個都要★ 只有排程 → 每天重開程式的機器永遠跑不到;只有啟動 →
    跨夜長駐的機器好幾週不清,那正是「宣告 30 天卻留了一年」的成因。"""
    import inspect

    import main
    src = inspect.getsource(main)
    assert "retention-sweep-startup" in src, "缺開機清掃"
    assert "retention-sweep-daily" in src, "缺每日清掃"


def test_the_rules_cover_every_known_landing_place():
    """新增一種落地檔卻忘了進規則表,就是下一個「宣告了卻沒生效」。"""
    import main
    labels = {r.label for r in main._retention_rules()}
    assert {"打卡除錯檔", "會診截圖", "設定備份"} <= labels, labels
    src = __import__("inspect").getsource(main.run_retention_sweep)
    assert "定位索引" in src and "重啟錯誤檔" in src


@pytest.mark.parametrize("days,label", [
    ("DEBUG_DUMP_RETAIN_DAYS", "打卡除錯檔"),
    ("CONSULT_SHOT_RETAIN_DAYS", "會診截圖"),
])
def test_sensitive_retention_is_short(days, label):
    """含【完整畫面】(帳號、病人清單)的東西不可留太久 —— 外審要求 24-72 小時級。"""
    from cmuh_common import retention
    assert getattr(retention, days) <= 7, f"{label} 保留期太長"


# ─── [外審第1輪] 三條 finding 的守衛 ──────────────────────────────────────
def test_an_undeletable_expired_file_is_still_counted_as_oldest(tmp_path,
                                                               monkeypatch):
    """★核心★ 刪不掉的過期檔【還在磁碟上】,不可被排除在「最舊敏感檔」之外 ——
    否則摘要會報一個比實情【新】的日期,把真正的保留期違規藏起來。"""
    d = tmp_path / "x"
    _touch(d / "locked.png", days_old=99)
    _touch(d / "fresh.png", days_old=0.1)
    monkeypatch.setattr(os, "remove",
                        lambda *_a: (_ for _ in ()).throw(OSError("鎖住")))

    res = sweep([RetentionRule("打卡除錯檔", str(d), ("*.png",), 3)])

    assert res.failed == {"打卡除錯檔": 1}
    assert res.oldest is not None
    assert (datetime.now() - res.oldest[1]) > timedelta(days=90),         f"報成 {res.oldest} —— 那是還沒過期的那個,違規被藏起來了"


def test_the_producer_side_pruners_enforce_the_ttl_too():
    """★兩套政策不可並存★ 我第一版加了 sweeper 卻沒動生產端的「只看數量」清理,
    於是宣告的 TTL 在那條路上從來沒被執行過(總數沒破 40/60 就永久留著)。"""
    import inspect

    import autoclock
    import consult_query
    assert "debug_dump_rule" in inspect.getsource(autoclock.prune_debug_dumps)
    assert "consult_shot_rule" in inspect.getsource(
        consult_query._prune_old_shots)


def test_corrupt_backups_are_not_double_governed():
    """★同一種檔只能有一個權威 TTL★ cache_cleanup 已經以 30 天清 `.corrupt-*`;
    我原本又宣告 90 天 → 「有 90 天搶救窗」是謊話(開機 12 秒後就被清掉)。"""
    from cmuh_common.retention import settings_backup_rule
    pats = settings_backup_rule("/x").patterns
    assert not any(".corrupt-" in p for p in pats), pats


def test_the_retention_days_have_a_single_home():
    """天數只能宣告一次 —— 兩處各寫一個數字遲早不一致(這正是上面那條的成因)。"""
    import autoclock
    import consult_query
    import main
    from cmuh_common import retention
    for mod in (main, autoclock, consult_query):
        for name in ("DEBUG_DUMP_RETAIN_DAYS", "CONSULT_SHOT_RETAIN_DAYS"):
            assert not (hasattr(mod, name)
                        and getattr(mod, name) is not getattr(retention, name,
                                                              object())),                 f"{mod.__name__} 自己又宣告了一份 {name}"


# ─── [外審第2輪] append 與 prune 的競態 ────────────────────────────────────
def test_a_concurrent_append_is_not_lost_by_the_sweep(tmp_path):
    """★核心★ 兩者都是 read-modify-replace:背景清掃讀完舊內容之後,若剛好有一筆
    回讀不符寫進來,清掃再把舊快照 replace 回去 —— 那筆【新病人就此消失】,
    而那正是這個功能存在的唯一目的。

    釘住的是【可觀察的契約】:兩者並行跑完之後,過期那筆要不見、同時寫進來的那筆
    要還在。有了 _INDEX_LOCK,append 會被擋到 prune 寫完之後才進行(而不是拿舊快照
    覆蓋);沒有鎖的話,prune 的舊快照會把新那筆蓋掉。
    """
    import threading

    from cmuh_common import patient_locator as pl

    p = str(tmp_path / "idx.jsonl")
    old = (datetime.now() - timedelta(days=99)).isoformat(timespec="seconds")
    pl.append_index(p, ts=old, action="很舊", detail="", locator=None)

    read_done = threading.Event()
    appended = threading.Event()
    real_read = pl._read_rows

    def _slow_read(path):
        rows = real_read(path)
        if not read_done.is_set():
            read_done.set()
            appended.wait(1.0)      # 給 append 一個插進來的機會
        return rows

    pl._read_rows = _slow_read
    try:
        def _appender():
            read_done.wait(3.0)
            pl.append_index(p, ts=datetime.now().isoformat(timespec="seconds"),
                            action="新病人", detail="", locator={"room": "103"})
            appended.set()

        t = threading.Thread(target=_appender)
        t.start()
        pl.prune_index(p)
        t.join(5.0)
    finally:
        pl._read_rows = real_read

    text = (tmp_path / "idx.jsonl").read_text(encoding="utf-8")
    assert "新病人" in text, "★同時寫進來的那一筆被清掃覆蓋掉了★"
    assert "很舊" not in text, "過期那筆仍要被清掉"


def test_the_temp_file_name_is_collision_safe():
    """★同一個行程 ⇒ 同一個 pid★ 原本兩邊都用 `{path}.tmp-{os.getpid()}`,
    彼此會踩同一個暫存檔。"""
    import inspect

    from cmuh_common import patient_locator as pl
    # 只看程式碼:檔頭有一段【說明舊行為】的註解也含這個字串,不可誤判。
    code = "\n".join(ln.split("#")[0] for ln in
                     inspect.getsource(pl).splitlines())
    assert "getpid" not in code, "pid 不足以當唯一性來源(同一行程 ⇒ 同一檔名)"
    assert "mkstemp" in inspect.getsource(pl._atomic_write_rows)


def test_both_index_writers_hold_the_same_lock():
    import inspect

    from cmuh_common import patient_locator as pl
    for fn in (pl.append_index, pl.prune_index):
        assert "_INDEX_LOCK" in inspect.getsource(fn), fn.__name__
