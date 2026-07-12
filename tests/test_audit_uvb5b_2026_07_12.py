# -*- coding: utf-8 -*-
"""UVB批次5 其餘 P3(UD-07/09/10/11/14)回歸測試(2026-07-12)。

main.py 依賴 Selenium/Tk,headless 無法 import → 沿用【原始碼/AST 檢查】模式。
UD-07 確認迴圈(單 flag+防無限跳窗);UD-09 F1 失敗文案附「醫令已下」;
UD-10 uncertain 套用失敗不靜默;UD-11 W7 (B) 追加額外行提醒;UD-14 F1 分流矛盾警示。
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "src" / "main.py"


def _func_source(name: str) -> str:
    source = MAIN.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"main.py 找不到函式：{name}")


# ── UD-07:CONFIRM_NEEDED 改迴圈、每次只帶已確認那種的 skip flag ────────────────
def test_ud07_confirm_is_loop_with_per_kind_flag():
    src = _func_source("_update_uvb_dose_core")
    assert "while result.action == UvbAction.CONFIRM_NEEDED:" in src, \
        "UD-07: CONFIRM_NEEDED 應為迴圈(另一種 confirm 要再跳窗)"
    assert "_skip_flags[skip_flag] = True" in src and \
        "update_uvb_in_text(text, **_skip_flags)" in src, \
        "UD-07: 重 call 應只帶已確認那種的 skip flag"
    # 不得回退成「一次帶兩個 flag」的舊寫法
    assert "skip_dose_sanity=True, skip_stale_check=True" not in src, \
        "UD-07: 不可同時帶兩個 skip flag(另一種確認會被靜默跳過)"


def test_ud07_repeated_same_kind_aborts():
    src = _func_source("_update_uvb_dose_core")
    assert "_confirmed_kinds" in src and "if kind in _confirmed_kinds:" in src, \
        "UD-07: 同種 confirm 重複出現須有防無限跳窗守衛"
    seg = src[src.index("if kind in _confirmed_kinds:"):]
    seg = seg[:seg.index("logging.info")]
    assert "return False if strict else True" in seg, \
        "UD-07: 重複同種 confirm 應保守中止"


# ── UD-09:F1 失敗文案附「51019+療程1 已下」提醒 ─────────────────────────────
def test_ud09_core_threads_codes_already_placed():
    src = _func_source("_update_uvb_dose_core")
    assert "codes_already_placed" in src.split("\n")[0] + src.split("\n")[1], \
        "UD-09: core 簽名應有 codes_already_placed 參數"
    assert "_placed_note" in src, "UD-09: 失敗文案應定義 _placed_note"
    n = src.count("{_placed_note}")
    assert n >= 6, f"UD-09: PARSE/SANITY/TOO_CLOSE/寫回/read-back/verify 文案應附註(現 {n} 處)"


def test_ud09_f1_passes_codes_already_placed():
    src = _func_source("_f1_update_uvb_dose_if_present")
    assert "codes_already_placed=" in src and "51019" in src, \
        "UD-09: F1(先醫令後 UVB)應傳 codes_already_placed"


# ── UD-10:uncertain 套用失敗不得靜默 fallback ───────────────────────────────
def test_ud10_uncertain_apply_failure_warns():
    src = _func_source("_update_uvb_dose_core")
    idx = src.index("apply_uncertain_updates 失敗")
    window = src[idx:idx + 900]
    assert "_show_uvb_warning" in window, \
        "UD-10: apply_uncertain_updates 失敗應跳警告(醫師剛按「是」)"
    assert "沒有】更新" in window, "UD-10: 警告應明講額外行未更新"


# ── UD-11:W7「(B)改回原值」對一併更新的其他行不完整 ─────────────────────────
def test_ud11_record_uvb_write_tracks_extra_lines():
    src = _func_source("_record_uvb_write")
    assert "extra_lines" in src, "UD-11: rec 應記錄一併更新的額外行數"
    core = _func_source("_update_uvb_dose_core")
    idx = core.index("_record_uvb_write(")
    call = core[idx:idx + 600]
    assert "extra_lines=" in call and "additional_lines_updated" in call \
        and "_uncertain_applied" in call, \
        "UD-11: 寫回記錄應含 Step B 附加行+同日 triplet+已套用 uncertain 行數"


def test_ud11_w7_option_b_mentions_extra_lines():
    src = _func_source("_show_light_code_incomplete_warning")
    assert 'rec.get("extra_lines")' in src, "UD-11: W7 應讀取 extra_lines"
    assert "一併改回" in src, "UD-11: (B) 文案應提醒額外行也要還原"


# ── UD-14:F1 route=normal 已下 51019、core 二次分流=純 excimer → 矛盾警示 ────
def test_ud14_f1_route_core_divergence_warns():
    src = _func_source("_f1_update_uvb_dose_if_present")
    assert "res == _F23_PURE_EXCIMER" in src, \
        "UD-14: F1 應檢查 core 是否二次分流成純自費 Excimer"
    idx = src.index("res == _F23_PURE_EXCIMER")
    window = src[idx:]
    assert "_show_uvb_warning" in window and "人工核對" in window, \
        "UD-14: 分流矛盾應跳人工核對警告"
    assert "51019" in window, "UD-14: 警告應點名 51019 已下"


def test_ud14_f1_also_warns_on_aborted_pure_excimer():
    # [codex P2] 更新中止(TOO_CLOSE)時分流一樣是純 excimer、矛盾一樣存在 → 也要警告
    src = _func_source("_f1_update_uvb_dose_if_present")
    assert "res == _F23_PURE_EXCIMER_ABORTED" in src, \
        "UD-14: F1 對「純 excimer 但更新中止」也應跳矛盾警告"


def test_ud14_too_close_returns_aborted_sentinel_not_false():
    src = _func_source("_f23_pure_excimer_update")
    idx = src.index("UvbAction.TOO_CLOSE")
    end = src.index("elif", idx)          # TOO_CLOSE 分支到下一個 elif 為止
    window = src[idx:end]
    assert "return _F23_PURE_EXCIMER_ABORTED" in window, \
        "UD-14: TOO_CLOSE 應回 aborted 哨符(保留分流結論),不可回裸 False"
    assert "return False" not in window, \
        "UD-14: TOO_CLOSE 分支不可回裸 False(會丟失分流結論)"


def test_ud14_aborted_sentinel_is_falsy_fail_closed():
    # 哨符 falsy 是 F2/F3 零改動照樣中止(不 key 51019/不設身份)的前提 —— 用 AST 取
    # class+賦值原始碼實際執行驗證(main.py 無法 headless import)。
    source = MAIN.read_text(encoding="utf-8")
    tree = ast.parse(source)
    cls_src = assign_src = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "_PureExcimerAbortedType":
            cls_src = ast.get_source_segment(source, node)
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "_F23_PURE_EXCIMER_ABORTED"
                for t in node.targets):
            assign_src = ast.get_source_segment(source, node)
    assert cls_src and assign_src, "找不到 aborted 哨符定義"
    ns: dict = {}
    exec(cls_src + "\n" + assign_src, ns)   # noqa: S102 — 受控:僅執行本 repo 原始碼片段
    sentinel = ns["_F23_PURE_EXCIMER_ABORTED"]
    assert not sentinel, "UD-14: aborted 哨符必須 falsy(未知呼叫端 fail-closed 當中止)"
    assert sentinel == "pure_excimer_aborted" and sentinel != "pure_excimer", \
        "UD-14: aborted 哨符須可與正常純 excimer 哨符區分"
