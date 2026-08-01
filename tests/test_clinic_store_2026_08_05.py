# -*- coding: utf-8 -*-
"""[2026-08-05 P2-06 第五刀(a)] 診間燈號歷史／診間設定的存取層。

★這一層原本沒有任何測試★
`clinic_light_history` 的純函式（算平均、加樣本）早就測得很細，但「檔名對不對、
寫回失敗會怎樣、保留幾天」這一半一直卡在 `AutomationApp` 的 method 裡 ——
要有 Tk app 實例才碰得到，實務上等於沒驗。

這裡釘的就是那一半：
  ★`test_a_failed_write_does_not_break_the_clinic_flow`★
      燈號歷史只是統計輔助資料。寫不出去（磁碟滿／防毒鎖檔）時必須吞掉 ——
      看診中的畫面更新不可以因為統計寫不進去就整條掛掉。
  ★`test_the_history_file_is_written_compact`★
      這檔每次門診輪詢、每個診間寫一次（~220KB）。indent 一旦跑回來，
      json.dump 從 ~1ms 變 ~6ms，而且是在 UI 執行緒上。
"""
import json
import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import clinic_store as cs  # noqa: E402


@pytest.fixture
def conf(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "get_conf_path",
                        lambda name: str(tmp_path / name))
    return tmp_path


NOW = datetime(2026, 8, 5, 10, 30, 0)


def test_a_sample_lands_in_the_history_file(conf):
    assert cs.save_light_sample("A1", "王醫師", "上午", "12",
                                now=NOW, history_days=30) is True
    data = json.loads((conf / cs.LIGHT_HISTORY_FILENAME).read_text(
        encoding="utf-8"))
    assert data, "歷史檔要真的寫出東西"


def test_the_history_file_is_written_compact(conf):
    """★這檔是純機器讀寫的大型快取★ 不可以帶縮排（每次輪詢每診間寫一次）。"""
    cs.save_light_sample("A1", "王醫師", "上午", "12", now=NOW, history_days=30)
    raw = (conf / cs.LIGHT_HISTORY_FILENAME).read_text(encoding="utf-8")
    assert "\n" not in raw.strip(), "compact 序列化不該有換行"
    assert ", " not in raw and '": ' not in raw, "不該有縮排/空白分隔"


def test_a_failed_write_does_not_break_the_clinic_flow(conf, monkeypatch,
                                                       caplog):
    """★寫不出去不可以打斷看診★ 只記 debug、回 False，絕不往外拋。"""
    def _boom(*a, **k):
        raise OSError(28, "磁碟空間不足")
    monkeypatch.setattr(cs, "atomic_write_json", _boom)
    assert cs.save_light_sample("A1", "王", "上午", "5",
                                now=NOW, history_days=30) is False


@pytest.mark.parametrize("room,doc,light", [
    ("", "王", "5"), ("A1", "", "5"), ("A1", "王", ""),
    ("A1", "王", None), ("A1", "王", "休"), ("A1", "王", "--"),
])
def test_unusable_samples_never_touch_the_file(conf, monkeypatch,
                                               room, doc, light):
    """★垃圾進不了歷史檔★

    燈號欄可能是「休」「--」或空的（休診、解析失敗）。那些不是數字，
    寫進去會污染之後算出來的平均值 —— 而那個平均值是醫護判斷「還要等多久」的依據。

    （順帶記下量到的事實：燈號**有效**時每次輪詢都會重寫檔案 ——
      `record_light_sample` 是「同日同桶取代」，不是「沒變就跳過」。
      所以 compact 序列化那條不是微優化，是這個寫入頻率下的必要條件。）
    """
    calls = []
    monkeypatch.setattr(cs, "atomic_write_json",
                        lambda *a, **k: calls.append(1))
    assert cs.save_light_sample(room, doc, "上午", light,
                                now=NOW, history_days=30) is False
    assert calls == [], "無效樣本不該碰檔案"


def test_retention_keeps_at_least_sixty_days(conf, monkeypatch):
    """★保留天數要比查詢天數寬★ 只留剛好 history_days 的話，
    邊界那幾天的樣本會在還要用的時候就被修掉。"""
    seen = {}
    monkeypatch.setattr(cs, "record_light_sample",
                        lambda data, **k: (seen.update(k), (data, False))[1])
    cs.save_light_sample("A1", "王", "上午", "5", now=NOW, history_days=30)
    assert seen["retain_days"] == 60
    cs.save_light_sample("A1", "王", "上午", "5", now=NOW, history_days=90)
    assert seen["retain_days"] == 97, "history_days + 7"


def test_hist_avg_says_it_has_nothing_rather_than_guessing(conf):
    """★沒有資料就要說沒有★ 而且那個字元會直接顯示在門診動態表格上。"""
    assert cs.hist_avg_light("", "王", "上午", now=NOW,
                             history_days=30, window_minutes=20) == "—"
    assert cs.hist_avg_light("A1", "", "上午", now=NOW,
                             history_days=30, window_minutes=20) == "—"


def test_hist_avg_reads_back_what_was_saved(conf):
    """存取層要對得起來 —— 寫進去的樣本讀得回來（檔名/鍵值算法一致）。"""
    for minute in (0, 3, 6):
        cs.save_light_sample("A1", "王醫師", "上午", "10",
                             now=datetime(2026, 7, 29, 10, minute),
                             history_days=30)
    got = cs.hist_avg_light("A1", "王醫師", "上午",
                            now=datetime(2026, 8, 5, 10, 2),
                            history_days=30, window_minutes=20)
    assert got != "—", "上週同一天同時段有樣本，不該回『沒有資料』"


# ─── 診間設定 ─────────────────────────────────────────────────────────────
def test_clinic_settings_fall_back_to_defaults(conf):
    got = cs.load_clinic_settings(["A1", "A2"], 2, lambda r: (list(r), False))
    assert got["rooms"] == ["A1", "A2"]
    assert got["time_modes"] == ["auto", "auto"]


def test_normalised_rooms_are_written_back(conf):
    """★正規化後要寫回去★ 否則每次啟動都要重算一次，而且使用者在設定頁看到的
    仍是舊代碼。"""
    (conf / cs.CLINIC_SETTINGS_FILENAME).write_text(
        json.dumps({"rooms": ["舊代碼"], "time_modes": ["auto"]}),
        encoding="utf-8")
    got = cs.load_clinic_settings(["A1"], 1, lambda r: (["新代碼"], True))
    assert got["rooms"] == ["新代碼"]
    on_disk = json.loads((conf / cs.CLINIC_SETTINGS_FILENAME).read_text(
        encoding="utf-8"))
    assert on_disk["rooms"] == ["新代碼"], "改過就要落地"


def test_an_unchanged_settings_file_is_not_rewritten(conf, monkeypatch):
    (conf / cs.CLINIC_SETTINGS_FILENAME).write_text(
        json.dumps({"rooms": ["A1"], "time_modes": ["auto"]}), encoding="utf-8")
    calls = []
    monkeypatch.setattr(cs, "atomic_write_json",
                        lambda *a, **k: calls.append(1))
    cs.load_clinic_settings(["A1"], 1, lambda r: (list(r), False))
    assert calls == []


def test_a_failed_settings_writeback_is_not_fatal(conf, monkeypatch, caplog):
    """寫回失敗只警告 —— 設定本身已經在記憶體裡是對的，不該讓啟動掛掉。"""
    import logging as _lg
    monkeypatch.setattr(cs, "atomic_write_json",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("唯讀")))
    with caplog.at_level(_lg.WARNING):
        got = cs.load_clinic_settings(["A1"], 1, lambda r: (["B1"], True))
    assert got["rooms"] == ["B1"], "回傳值仍要是正規化後的"
    assert any("寫回失敗" in r.getMessage() for r in caplog.records)


def test_the_store_layer_does_not_import_tkinter():
    """存取層不可以相依 UI（否則又變成要開 Tk 才測得到）。"""
    import ast
    path = os.path.join(os.path.dirname(__file__), "..", "src",
                        "cmuh_common", "clinic_store.py")
    tree = ast.parse(open(path, encoding="utf-8").read())
    mods = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            mods.append(node.module or "")
    assert not [m for m in mods if m.split(".")[0] == "tkinter"]
