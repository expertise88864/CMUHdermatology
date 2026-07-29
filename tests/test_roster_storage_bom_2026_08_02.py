# -*- coding: utf-8 -*-
"""[2026-08-02 補審] storage 的「守門」與「讀取」用不同編碼,BOM 檔會被無聲清空。

`_guard_overwrite` 用 `utf-8-sig` 讀、`_load_json` 與 `assert_readable` 用 `utf-8` 讀。
帶 UTF-8 BOM 的檔案於是同時滿足兩個互相矛盾的判定:

  * `_load_json` → `json.JSONDecodeError`(Python 的訊息本身就寫著
    "Unexpected UTF-8 BOM (decode using utf-8-sig)")→ 被 `except Exception`
    吞掉、當成 **{}**;畫面上看起來這個檔是空的。
  * `_guard_overwrite` → 讀得好好的 → **放行覆寫**,而且不會留 `.corrupt-` 備份。

於是 load→編輯→save 這條再普通不過的路徑,會把整份資料寫成「空白 + 這次的編輯」,
連備份都沒有 —— 正是 `_guard_overwrite` 當初寫下來要防的那件事
(它的 docstring:「否則週色/年度假日表這類無快照的檔會直接無備份消失」)。
週色/年度假日表/門診模板/Clerk 梯次/切片格網都沒有 `_snapshot`,救不回來。

★而且這個教訓 repo 已經學過一次★:`cmuh_common/atomic_io.py` 的 [IF-02] 就是
「用 utf-8-sig 讀,容忍記事本另存 UTF-8 時加的 BOM」,並註明「對無 BOM 的純 utf-8
行為完全一致,向後相容、無副作用」。當時修了 atomic_io 與 storage 的 `_guard_overwrite`,
卻漏掉每一次讀取都會經過的 `_load_json`。

BOM 從哪來:多機 git 衝突是設計內流程(見 settings 的 RF-18 註解),使用者會手動
修 JSON;而這台開發機的 PowerShell `>` / `Out-File` 預設就是寫出 UTF-8 with BOM,
部分編輯器亦然。
"""
import io
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common.roster.storage import RosterStorage  # noqa: E402


def _add_bom(path: str) -> None:
    """把檔案原樣重寫成「UTF-8 with BOM」(內容一個字都沒改)。"""
    raw = io.open(path, encoding="utf-8-sig").read()
    io.open(path, "w", encoding="utf-8-sig").write(raw)


@pytest.fixture
def st(tmp_path):
    return RosterStorage(str(tmp_path))


# ─── 讀:BOM 不等於空檔 ─────────────────────────────────────────────────────
def test_bom_file_is_read_not_treated_as_empty(st):
    st.save_week_colors(2026, {"2026-W31": "pink", "2026-W32": "green"})
    _add_bom(os.path.join(st.base_dir, "week_colors.json"))

    assert st.load_week_colors() == {"2026-W31": "pink", "2026-W32": "green"}


def test_bom_month_file_still_reports_finalized(st):
    """★最兇的一條★ 月檔帶 BOM → `load_month` 讀到 {} → `finalized` 被補成 False。

    `save_month` 的定案守門看的正是 `_load_json(path).get("finalized")`,
    於是【已定案的月份會被靜默覆寫】,而使用者以為定案還鎖著。
    """
    st.save_month("2026-08", {"r_duty": {"2026-08-01": {"person": "A"}}})
    m = st.load_month("2026-08")
    m["finalized"] = True
    st.save_month("2026-08", m, force=True)
    _add_bom(st._month_path("2026-08"))

    assert st.load_month("2026-08")["finalized"] is True, "定案狀態被讀丟了"
    from cmuh_common.roster.storage import FinalizedMonthError
    with pytest.raises(FinalizedMonthError):
        st.save_month("2026-08", {"r_duty": {}})


def test_assert_readable_accepts_a_bom_file(st):
    """`assert_readable` 是 rename_member 的寫入前預檢。程式讀得動的檔不可判定成
    損壞 —— 否則帶 BOM 的月檔會讓整個連動改代號永遠中止。"""
    st.save_config({"r_members": [{"id": "A"}]})
    _add_bom(os.path.join(st.base_dir, "config.json"))

    st.assert_readable("config.json")          # 不該拋


# ─── 寫:不可把讀不到的內容當成空白覆蓋 ──────────────────────────────────────
def test_bom_week_colors_are_not_wiped_by_a_merge_save(st):
    """★實際後果★ 週色沒有 `_snapshot`;被清空就永久消失。

    使用者只是想再加一週的顏色,舊的兩週卻整組不見。
    """
    st.save_week_colors(2026, {"2026-W31": "pink", "2026-W32": "green"})
    _add_bom(os.path.join(st.base_dir, "week_colors.json"))

    st.save_week_colors(2026, {"2026-W33": "pink"})       # 只想新增一週

    assert st.load_week_colors() == {"2026-W31": "pink", "2026-W32": "green",
                                     "2026-W33": "pink"}


def test_bom_holiday_table_survives_a_save(st):
    """年度國定假日表同樣沒有快照,而且它的鍵集合【就是】國定假日清單 ——
    被清空等於整年的假日認定消失,點數與週末區塊全部跟著算錯。"""
    from datetime import date
    st.save_holiday_duty({"r": {date(2026, 1, 1): "A"}, "vs": {}})
    _add_bom(os.path.join(st.base_dir, "holiday_duty.json"))

    table = st.load_holiday_duty()
    assert table["r"] == {date(2026, 1, 1): "A"}
    table["r"][date(2026, 2, 28)] = "B"
    st.save_holiday_duty(table)

    assert st.holidays_set() == {date(2026, 1, 1), date(2026, 2, 28)}


def test_the_guard_and_the_loader_agree(st):
    """★不變式★ `_guard_overwrite` 放行 ⇒ `_load_json` 一定讀得到內容。

    兩者只要用不同的解析規則,就會出現「守門說沒事、讀取卻回空」的縫,
    而那正是把好資料寫成空白的路徑。
    """
    import inspect

    from cmuh_common.roster import storage as mod
    src = inspect.getsource(mod)
    assert 'encoding="utf-8"' not in src, (
        "storage 內所有 JSON 讀取都要用 utf-8-sig(對無 BOM 的檔行為完全相同)")


# ─── 不可矯枉過正:真的壞掉的檔仍要照舊處理 ──────────────────────────────────
def test_a_really_corrupt_file_still_reads_empty_and_gets_backed_up(st):
    st.save_week_colors(2026, {"2026-W31": "pink"})
    p = os.path.join(st.base_dir, "week_colors.json")
    io.open(p, "w", encoding="utf-8").write("{ 這不是 JSON")

    assert st.load_week_colors() == {}, "壞檔仍視為空(呼叫端補預設)"
    st.save_week_colors(2026, {"2026-W33": "pink"})
    assert [f for f in os.listdir(st.base_dir) if ".corrupt-" in f], \
        "壞檔覆寫前必須留 .corrupt- 備份"


def test_a_non_object_root_is_still_treated_as_corrupt(st):
    st.save_week_colors(2026, {"2026-W31": "pink"})
    p = os.path.join(st.base_dir, "week_colors.json")
    io.open(p, "w", encoding="utf-8").write("[1, 2, 3]")

    assert st.load_week_colors() == {}
    with pytest.raises(ValueError):
        st.assert_readable("week_colors.json")


def test_plain_utf8_without_bom_is_unaffected(st):
    """utf-8-sig 對「無 BOM 的純 utf-8」行為與 utf-8 完全一致(atomic_io 的 IF-02
    也是這麼寫的)—— 這裡把它釘住,免得日後有人以為改編碼有副作用。"""
    st.save_week_colors(2026, {"2026-W31": "pink"})
    p = os.path.join(st.base_dir, "week_colors.json")
    assert not io.open(p, "rb").read(3).startswith(b"\xef\xbb\xbf"), \
        "我們自己寫出來的檔不帶 BOM"
    assert st.load_week_colors() == {"2026-W31": "pink"}
    assert json.loads(io.open(p, encoding="utf-8").read())["weeks"]


# ─── schema_version 欄位壞掉不可打斷每一條 load_* ──────────────────────────
def _write_version(st, name, value):
    p = os.path.join(st.base_dir, name)
    data = json.loads(io.open(p, encoding="utf-8-sig").read())
    data["schema_version"] = value
    data.setdefault("未來版本才有的欄位", "不可被本版靜默丟掉")
    io.open(p, "w", encoding="utf-8").write(json.dumps(data, ensure_ascii=False))


def test_unreadable_schema_version_does_not_break_every_load(st):
    """`_check_schema` 的 `int()` 對 "v3" 這種值會拋 ValueError,而它在【每一個】
    `load_*` 的路徑上。UI 端 Tk callback 的例外只會進 log —— 使用者只會看到
    分頁沒重畫、完全不知道為什麼,而且連設定頁都打不開。

    【讀】不明版本 → 放行。(【寫】另有一支測試釘住必須 fail-closed。)
    """
    st.save_config({"r_members": [{"id": "A"}]})
    _write_version(st, "config.json", "v3")

    assert st.load_config()["r_members"] == [{"id": "A"}]


def test_a_genuinely_newer_schema_is_still_refused(st):
    """★不可矯枉過正★ 真的是比較新的版本(數字)仍要擋下,免得舊版程式降級毀損。"""
    from cmuh_common.roster.model import SCHEMA_VERSION
    from cmuh_common.roster.storage import NewerSchemaError
    st.save_config({"r_members": [{"id": "A"}]})
    _write_version(st, "config.json", SCHEMA_VERSION + 1)

    with pytest.raises(NewerSchemaError):
        st.load_config()


def test_unreadable_schema_version_fails_closed_on_every_save(st):
    """★[第1輪外審] 讀寬鬆不可以連寫入一起放寬★

    我第一版把「版本不明」一律放行,等於拿掉了降級保護:較新版本寫的檔會被靜默
    改寫成本版 schema、丟掉本程式不認得的欄位,而 `_guard_overwrite` 只驗 JSON
    結構,擋不住這件事。無快照的那幾個檔(週色/假日表/模板/梯次/切片格網)一旦
    這樣被改寫就沒有回頭路。

    逐一釘住每一條 save_* —— 這不是單一函式的疏漏,是整層的契約。
    """
    from datetime import date
    st.save_config({"r_members": [{"id": "A"}]})
    st.save_ledger({"r": {"A": 1.0}})
    st.save_biopsy({"counts": {"A": 1}})
    st.save_week_colors(2026, {"2026-W31": "pink"})
    st.save_holiday_duty({"r": {date(2026, 1, 1): "A"}, "vs": {}})
    st.save_clinic_template({"template": {"0": {"上午": [{"room": "101"}]}}})
    st.save_clerk_batches([{"id": "b1", "start_monday": "2026-08-03"}])
    st.save_biopsy_grid({"b1": {}})
    st.save_month("2026-08", {"r_duty": {}})

    cases = [
        ("config.json", lambda: st.save_config({"r_members": []})),
        ("ledger.json", lambda: st.save_ledger({"r": {}})),
        ("biopsy.json", lambda: st.save_biopsy({"counts": {}})),
        ("week_colors.json", lambda: st.save_week_colors(2026, {"2026-W33": "pink"})),
        ("holiday_duty.json", lambda: st.save_holiday_duty({"r": {}, "vs": {}})),
        ("clinic_template.json", lambda: st.save_clinic_template({"template": {}})),
        ("clerk_batches.json", lambda: st.save_clerk_batches([])),
        ("biopsy_grid.json", lambda: st.save_biopsy_grid({})),
    ]
    for name, save in cases:
        _write_version(st, name, "v3")
        with pytest.raises(ValueError):
            save()
        after = json.loads(io.open(os.path.join(st.base_dir, name),
                                   encoding="utf-8-sig").read())
        assert after.get("未來版本才有的欄位"), f"{name} 的未知欄位被丟掉了"

    _write_version(st, os.path.join("months", "2026-08.json"), "v3")
    with pytest.raises(ValueError):
        st.save_month("2026-08", {"r_duty": {}})
