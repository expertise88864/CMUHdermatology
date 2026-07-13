# -*- coding: utf-8 -*-
"""UVB 自動調整劑量邏輯。

F2/F3 熱鍵觸發時：
  1. 讀 主窗「處置」TMemo 文字
  2. 找第一行含 UVB 的內容 (最上面)
  3. parse: dose / count / last_date / increase / max
  4. 依「今天 vs last_date」天數差套用劑量調整規則
  5. 覆蓋寫回該行 (count+1, date→today, dose 依規則)

【規則】依「今天 − last_date」天數差：
    0-1 天 → 太密集，跳警告終止 (F2/F3 不繼續跑 51019)
    2-6 天 → dose + increase, cap MAX
    = 7 天 → 保持 dose 不變
    8-14 天 → dose × 0.75, floor 到 10 的倍數 (435→430, 432→430)
    15-21 天 → dose × 0.5, floor 到 10 的倍數
    > 21 天 → 固定 250 (LONG_GAP_DOSE)
    [UC-09 audit 2026-07-12] 補回 15-21 ×0.5 桶(原 docstring 漏列且誤寫「>14 固定 250」);
    以 compute_new_dose 常數為準。

【格式範例】
    UVB 520mj/cm2  (11) on  (2026/05/26)  , increase 30mj/cm2 if no erythema , MAX:800 mj/cm2 , W2, W5M

【容錯】
    - dose / increase / max 可能無單位「mj/cm2」(只看數字)
    - 日期 (YYYY/MM/DD) 月日可有/無零填充
    - 多餘空白忽略
    - MAX 後面可能有 ":" 或沒有
    - W2 / W5M 等後綴一律保留不動
"""
from __future__ import annotations

import calendar
import math
import re
from dataclasses import dataclass
from datetime import date
from typing import Optional


# ─── Constants ──────────────────────────────────────────────────────────
# [2026-06-02] 至少間隔一天；同日或隔天重複照光都警告:
#   days_diff = 0-1                   → 警告終止
#   days_diff = 2-6                   → +increase (cap MAX)
#   days_diff = 7 (剛好)               → 保持 dose
#   days_diff = 8-14 (含14)           → × 0.75, floor 10, 最低 250
#   days_diff = 15-21 (含21)          → × 0.5, floor 10, 最低 250
#   days_diff > 21                    → 固定 250
TOO_CLOSE_DAYS = 2            # days_diff < 此值 → 警告終止 (同日或隔天)
SAME_DOSE_DAYS = 7            # = 此值 → 保持
DECAY_75_UPPER = 14           # 8-14 → ×0.75
DECAY_50_UPPER = 21           # 15-21 → ×0.5
LONG_GAP_DOSE = 250           # > 21 → 250
MIN_DECAY_DOSE = 250          # decay 結果不低於此值 (floor)

DECAY_75_FACTOR = 0.75
DECAY_50_FACTOR = 0.5

# ─── Sanity bounds (拒絕 parse 出來明顯異常的值) ────────────────────────
# [v20.5 2026-05-26] 為了「確保資訊正確」，parse 出來的值若超出合理範圍 →
# 直接 sanity_fail，給 caller 跳警告終止，不嘗試自動計算。
MIN_DOSE = 50              # UVB 劑量正常 200-1500 mj/cm2，給寬點 50 為下限
MAX_DOSE = 1500            # [v20.12] 上限改回 1500，超過跳 Yes/No 確認
MAX_COUNT = 999            # 治療次數不該超過 999 (~5 年週週照)
MAX_GAP_DAYS = 730         # 距上次照光超過 2 年 → 異常 (病歷可能跑掉)
# [v20.14 2026-05-26] 病歷可能有好幾個月前甚至好幾年前的照光紀錄，user 不希望
# 程式直接拿舊紀錄套日期/decay 自動更新。距上次 > 此值 → CONFIRM_NEEDED 跳
# Yes/No 給醫師決定是否要按舊紀錄繼續更新。
STALE_DAYS = 30            # 超過 30 天 → 跳 Yes/No 確認


# ─── 劑量規則外部化(settings/uvb_rules.json,可選覆寫)────────────────────
# [2026-06-29] 把上方天數/衰減/sanity 常數做成可被 settings/uvb_rules.json 覆寫,讓非工程師也能微調
# 劑量曲線而不必改 code 重編譯。【上方常數=唯一真實來源與 fallback】;override 採『逐欄驗證(型別+上下限)
# + 不合理就退回該欄預設』,任何讀檔/解析錯誤都【完全不影響劑量計算】(醫療安全:壞設定絕不算錯劑量)。
# 由 app 啟動時呼叫 load_and_apply_uvb_rules() 一次即生效;測試不呼叫 → 一律用程式內預設(可重現)。
_UVB_RULE_FIELDS = (
    # (json key, 模組常數名, 型別, 下限, 上限) —— 上下限只擋明顯離譜值,欄位間合理性由使用者自負
    ("too_close_days", "TOO_CLOSE_DAYS", int, 1, 7),
    ("same_dose_days", "SAME_DOSE_DAYS", int, 2, 30),
    ("decay_75_upper", "DECAY_75_UPPER", int, 2, 60),
    ("decay_50_upper", "DECAY_50_UPPER", int, 2, 90),
    ("decay_75_factor", "DECAY_75_FACTOR", float, 0.1, 1.0),
    ("decay_50_factor", "DECAY_50_FACTOR", float, 0.1, 1.0),
    ("long_gap_dose", "LONG_GAP_DOSE", int, 50, 1500),
    ("min_decay_dose", "MIN_DECAY_DOSE", int, 50, 1500),
    ("min_dose", "MIN_DOSE", int, 1, 500),
    ("max_dose", "MAX_DOSE", int, 200, 5000),
    ("max_count", "MAX_COUNT", int, 1, 9999),
    ("max_gap_days", "MAX_GAP_DAYS", int, 30, 3650),
    ("stale_days", "STALE_DAYS", int, 7, 365),
)
# 凍結原始預設(import 當下擷取),作為模板與 fallback —— 不受之後 override 影響。
_UVB_RULE_DEFAULTS = {key: globals()[const] for key, const, *_ in _UVB_RULE_FIELDS}
UVB_RULES_SCHEMA_VERSION = 1


def _apply_uvb_rules(rules: dict) -> None:
    """把驗證過的規則 dict 寫回模組常數(compute_new_dose / sanity 於呼叫時讀取 → 立即生效)。"""
    g = globals()
    for key, const, *_ in _UVB_RULE_FIELDS:
        if key in rules:
            g[const] = rules[key]


def _uvb_rules_coherent(r: dict) -> bool:
    """compute_new_dose 的 day-bucket 必須【單調】才有意義:太近 ≤ 保持 ≤ ×0.75 上界 ≤ ×0.5 上界;
    衰減倍率須 0 < ×0.5 ≤ ×0.75 ≤ 1(衰減不可變成增量)。即使每欄都在合理範圍,組合不一致(如
    same_dose_days=30 但 decay_75_upper=14)仍會讓劑量曲線錯亂 → 整份 override 視為不可信、退回預設
    (Codex:壞設定一律 fallback,不可只靠逐欄上下限)。"""
    try:
        return bool(
            r["too_close_days"] <= r["same_dose_days"] <= r["decay_75_upper"]
            <= r["decay_50_upper"]
            and 0 < r["decay_50_factor"] <= r["decay_75_factor"] <= 1.0
        )
    except (KeyError, TypeError):
        return False


def write_uvb_rules_template(path: Optional[str] = None) -> bool:
    """把目前的劑量規則【預設值】寫成 settings/uvb_rules.json 模板(給使用者編輯)。回 True=有寫出。
    best-effort:任何錯誤回 False、不丟例外。"""
    try:
        from cmuh_common.atomic_io import atomic_write_json
        from cmuh_common.paths import get_conf_path
        p = path or get_conf_path("uvb_rules.json")
        payload = {
            "schema_version": UVB_RULES_SCHEMA_VERSION,
            "_說明": "UVB 照光劑量規則;改完存檔、重開程式生效。天數為『今天−上次照光』的間隔;"
                     "decay_*_factor 為衰減倍率;其餘為 sanity 上下限。壞值會自動退回程式內預設。",
            **_UVB_RULE_DEFAULTS,
        }
        atomic_write_json(p, payload)
        return True
    except Exception:
        return False


def load_and_apply_uvb_rules(path: Optional[str] = None) -> dict:
    """讀 settings/uvb_rules.json 覆寫劑量規則常數(逐欄驗證+不合理退回預設)。回實際生效的規則 dict。

    沒檔 → 寫出預設模板供使用者編輯,本次仍用預設;壞檔/壞值 → 安全退回預設。只給 app 啟動呼叫一次。"""
    import logging
    log = logging.getLogger(__name__)
    effective = dict(_UVB_RULE_DEFAULTS)
    try:
        import json
        import os
        from cmuh_common.paths import get_conf_path
        p = path or get_conf_path("uvb_rules.json")
        if not os.path.exists(p):
            write_uvb_rules_template(p)          # 首次:materialize 模板(本次仍用預設)
        else:
            with open(p, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                for key, _const, typ, lo, hi in _UVB_RULE_FIELDS:
                    if key not in loaded:
                        continue
                    v = loaded[key]
                    # 嚴格型別檢查(【不】用 typ(v) 轉型,否則 true→1 / 7.9→7 / "5"→5 會把壞值轉成合法值
                    # 而真的改到劑量,Codex)。int 欄位須真為 int(排除 bool/float/str);float 欄位容許 int/float。
                    if isinstance(v, bool) or (typ is int and not isinstance(v, int)) \
                            or (typ is float and not isinstance(v, (int, float))):
                        log.warning("uvb_rules.json %s 型別不符(需 %s),沿用預設 %s",
                                    key, typ.__name__, effective[key])
                        continue
                    v = float(v) if typ is float else int(v)
                    if not (lo <= v <= hi):
                        log.warning("uvb_rules.json %s=%s 超出合理範圍 [%s,%s],沿用預設 %s",
                                    key, v, lo, hi, effective[key])
                        continue
                    effective[key] = v
                # 逐欄都合法後,再驗證【整份規則組合一致】;不一致 → 整份退回預設(壞設定不影響劑量)。
                if not _uvb_rules_coherent(effective):
                    log.warning("uvb_rules.json 規則組合不一致(day-bucket 順序或衰減倍率),整份退回預設")
                    effective = dict(_UVB_RULE_DEFAULTS)
    except Exception as e:   # 任何錯誤(IO/JSON/權限…)→ 全退回預設,絕不讓壞設定影響劑量
        log.warning("讀取 uvb_rules.json 失敗,全部沿用程式內預設:%s", e)
        effective = dict(_UVB_RULE_DEFAULTS)
    _apply_uvb_rules(effective)
    return effective


# ─── Action enum ────────────────────────────────────────────────────────
class UvbAction:
    """F2/F3 對應的 UVB 處理動作 (字串 enum)。

    [v20.5 2026-05-26] 為了「確保資訊正確」，每種 uncertain case 都拿到一個
    明確的 action，caller (main.py) 依此決定要警告終止或繼續。
    現在只有 UPDATED 才會繼續走 51019，其他全部都要 stop+prompt。
    """
    NO_UVB_LINE = "no_uvb_line"          # 處置內沒 UVB 行 → 警告 (F2/F3 不該沒 UVB)
    PARSE_FAIL = "parse_fail"            # 有 UVB 但格式怪 → 警告
    TOO_CLOSE = "too_close"              # 同日或隔天重複照光 → 警告
    SANITY_FAIL = "sanity_fail"          # parse 出來的值超出合理範圍 → 警告
    CONFIRM_NEEDED = "confirm_needed"    # [v20.12] dose 超過 MAX_DOSE → Yes/No 確認
    UPDATED = "updated"                  # 正常更新 (唯一繼續走 51019 的 case)
    # [v20.17] 處置有 UVB+dose 但缺 MAX/increase (e.g. "keep UVB 850 mj/cm2") —
    # 不修改處置但繼續執行 51019+療程 (像 F1 lenient mode 的 NO_UVB_LINE 行為)
    SILENT_SKIP = "silent_skip"


# ─── Parse result ───────────────────────────────────────────────────────
@dataclass
class UvbLineInfo:
    """parse 出來的單行 UVB 結構。

    [v20.7] count 變 Optional — 處置可能沒寫 (N) 次數欄位 (醫師選擇不記)。
    沒 count → 不更新 count，dose/date 仍要更新。
    [v20.16] last_date / increase 也變 Optional — 病歷可能是第一次照光，沒
    日期或 increase 字眼。partial parse 可吐沒 last_date 的 info 給 caller 跳
    Yes/No「當作第一次照光」確認。
    """
    full_match: str       # 完整原始行內容 (含 line ending 之前的文字)
    dose: int             # 原劑量
    count: Optional[int]  # 原 (N) 次數，None=處置沒寫
    last_date: Optional[date] = None  # 原日期，None=處置沒寫 (第一次照光)
    increase: Optional[int] = None    # increase 後的數字，None=處置沒寫
    max_dose: int = 0     # MAX 後的數字
    span: tuple = (0, 0)  # 在 source text 中的 (start, end) char offset
    # [v20.15] 原始 date 字串 (e.g. "(115/05/24)" / "(1150524)" / "2026/5/24")
    # — format_uvb_line 寫回時用同樣 format 取代
    date_text: str = ""
    # [v20.15] keyword 文字 ("UVB" 或 "Phototherapy") — format_uvb_line 用
    keyword_text: str = "UVB"
    # [UC-08 2026-07-12] date_text 在 full_match 內的 (start, end) — format_uvb_line
    # 寫回時精確替換 parse 選中的那一個日期。行內同值日期出現兩次(如
    # "(10) 2026/7/8 done, next on (2026/7/8)")時,str.find 會換到第一個而非
    # parse 選中的那個 → 換錯欄位。None=舊 caller 未填,退回 find 行為。
    date_span: Optional[tuple] = None


# [v20.6 2026-05-26] 從「整段 regex」改成「獨立 field 解析」
# 原本 _UVB_LINE_RE 一條 regex 要求 dose/count/on(date)/increase/MAX 順序固定，
# 但實機 data 順序千變萬化:
#   Case 1: "UVB 1000 on (date), (count), increase N"  ← date 先於 count
#   Case 2: "UVB: 1200 mj/cm2已打折(137) on (date)"   ← 數字後夾中文再 (count)
#   Case 3: "UVB: 950 (39) on (date) add 50 each"     ← 用 add 取代 increase
# 一條 regex 撐不下這些變異，改成：
#   1. 找 UVB 起點 → 找 MAX:N 終點 → segment 範圍
#   2. 在 segment 內**各別**找 dose / date / count / increase
#   3. 各 field 順序不限，缺任一 field → parse_fail

# [v20.15 2026-05-26] 也接受 "Phototherapy" 當 keyword (劉香君實機 case)
# [v20.18 2026-05-27] 也接受 "UV" 簡寫當 keyword (陳冠廷實機 case "uv 1150mj")
# 注意 alternation 順序: UVB|UV 必須長的先 — 否則 "UVB" 會被 UV 部分匹配
# [2026-06-01] 分隔符也接受逗號:「phototherapy, 950mj」這類關鍵字與劑量數字間
# 夾逗號的自由寫法(曾大鈞實機 case)。原本只允許冒號/空白,逗號會讓 dose 解析失敗。
# [2026-06-08] 也接受「keyword … to <N> mj」自由寫法(蔡國華實機 case:
#   「keep phototherapy on both lower limbs to 680 mj/cm2」)。關鍵字與劑量間夾一段
#   文字、用「to」帶出劑量。為避免誤抓(如「want to photo 2 times」)，這個分支要求
#   數字後面緊跟「mj」單位(zero-width lookahead)，且關鍵字到 to 之間 ≤40 字、不跨逗號。
# [2026-06-26] 也接受「keyword<描述文字>: <劑量>」自由寫法(簡子泰實機 case:
#   「UVB局部臉和後背: 440 mj/cm2(9)…」)。關鍵字後【沒空格】直接接中文描述(局部臉和後背)、再用冒號
#   帶出劑量 → 原本三個分支都比不到(branch1 要求數字緊跟關鍵字;branch2/3 要求關鍵字後有空白再接
#   dose/to)。新 branch4:關鍵字後 ≤40 字「非數字、非逗號」的描述 + 冒號 + 劑量。因排除數字,描述段
#   不會跨過任何數字(會停在第一個數字),故抓到的是【冒號後的第一個數字=本次劑量】、不會誤抓後面的
#   max(如 fixed at 1000);且描述在冒號【前】,故「UVB: 已打折 1000」(中文在冒號後)不會被這支誤吃。
_UVB_DOSE_RE = re.compile(
    r"(UVB|Phototherapy|UV)(?:\s*[:：,，]?\s*"
    r"|\s+[^\r\n,，]{0,40}?\bdose\s*[:：]?\s*"
    r"|\s+[^\r\n,，]{0,40}?\bto\s+(?=\d+\s*mj)"
    r"|[^\r\n\d,，]{0,40}?[:：]\s*)(\d+)",
    re.IGNORECASE)
# [v20.11] 接受帶 paren 跟不帶 paren 兩種:
#   (2026/05/24) — group 1-3
#    2026/05/24  — group 4-6
# [v20.15] 新增民國年支援:
#   (115/05/24)   — group 7-9   (ROC 3-digit year + slash/dash)
#   (1150524)     — group 10-12 (ROC 7-digit concatenated YYYMMDD)
# year 100-150 視為民國年 (對應 AD 2011-2061)
_UVB_DATE_RE = re.compile(
    r"[\(（]\s*(\d{4})[-/](\d{1,2})[-/](\d{1,2})\s*[\)）]"     # AD paren
    r"|"
    r"\b(\d{4})[-/](\d{1,2})[-/](\d{1,2})\b"                    # AD bare
    r"|"
    r"[\(（]\s*(\d{3})[-/](\d{1,2})[-/](\d{1,2})\s*[\)）]"     # ROC paren slash
    r"|"
    r"[\(（]\s*(\d{3})(\d{2})(\d{2})\s*[\)）]"                  # ROC concat
)
# count: \(\s*\d+\s*\) — 任何 paren 內純數字。
# 為了不抓到日期 (年是 4 位)，caller 會先 mask date span 再 search。
# 大於 MAX_COUNT 的會在 sanity check 時擋下，這裡先放寬接受任意位數。
# [v20.7] 也排除「年」可能性 — 4 位數字當 count 機率極低，先排除避免誤抓
_UVB_COUNT_RE = re.compile(r"[\(（]\s*(\d+)\s*[\)）]")
# increase / increased / add / 每次加 / 增加 (case-insensitive)
# [v20.16] 也接受常見打字錯誤 "incrase" / "incraese" (張耀銘實機 case)
# [v20.17] 接受 "in crease" 中間有空格 (張智宇實機 case)
# [2026-06-02] 兩種寫法都接受：
#   (a) 關鍵字在前、數字在後：increase/add/adding/added/每次加 … N
#       add 改 add(?:ing|ed|s)? 以接受「adding 100」(陳珮淇實機 case，原本
#       "add" 後接 "ing" 就比不到數字 → inc=None → parse_fail)。
#   (b) 數字在前、"each (time)"/"每次" 在後的自由寫法、無 add/increase 關鍵字：
#       「50 each time」「100 mj each」「30 每次」(周宗翰實機 case)。
#       數字落在 group(2)，caller 用 group(1) or group(2) 取值。
_UVB_INCREASE_RE = re.compile(
    r"(?:(?:in\s*cr(?:e?a?|a?e?)se[d]?|add(?:ing|ed|s)?)(?:\s+by)?"
    r"|每次增加|每次加|增加|加)\s*[:：]?\s*(\d+)"
    r"|"
    r"(\d+)\s*(?:mj(?:/cm2)?)?\s*(?:each(?:\s+time)?|每次)",
    re.IGNORECASE)

# [UC-03 2026-07-10] 遞減/維持醫囑不可被當加量。上面 branch(b)「數字 + each time/每次」無方向
# 判斷 → "decrease 50 each time" 被當 +50(遞減變加量、過量寫回);且 first-match-wins 讓「劑量
# 150 each time」蓋掉後面明寫的 "add 20"。改用下面 _find_uvb_increase:branch(a) 明寫 add/increase
# 【優先】;沒有才用 branch(b),且 branch(b) 數字前方緊跟 decrease/reduce/taper/lower/減/降/'-'
# 一律否決。(只給【UVB 主 parse】用;excimer 多段路徑另有自己的 _seg_meta,不動。)
_INCREASE_A_RE = re.compile(          # branch(a):明寫 add/increase 關鍵字 + 數字
    r"(?:(?:in\s*cr(?:e?a?|a?e?)se[d]?|add(?:ing|ed|s)?)(?:\s+by)?"
    r"|每次增加|每次加|增加|加)\s*[:：]?\s*(\d+)",
    re.IGNORECASE)
_INCREASE_B_RE = re.compile(          # branch(b):數字 + each time/每次
    r"(\d+)\s*(?:mj(?:/cm2)?)?\s*(?:each(?:\s+time)?|每次)",
    re.IGNORECASE)
_DECREASE_BEFORE_RE = re.compile(     # 數字前方緊跟這些 = 遞減醫囑,不是加量
    r"(?:de\s*cr(?:e?a?|a?e?)se[d]?|reduc(?:e|ed|es|ing|tion)|taper(?:ed|ing)?"
    r"|lower(?:ed|ing)?|每次減|減(?:少|量|到)?|降(?:低|到)?|調降|-)"
    # [UC-03b audit 2026-07-12] 遞減動詞與數字間可夾中性字(dose/dosage/劑量/the):
    # "decrease dose by 50 each time" 也必須被判為遞減 → 否則 branch(b) 會把 50 當 +50、
    # 該減反增(800→850)。只放行「動詞後緊跟劑量名詞/by」的組合,不影響 "decrease 50"。
    r"\s*(?:(?:the\s+)?dos(?:e|age)|劑量)?\s*(?:by\s+)?[:：,，]?\s*$",
    re.IGNORECASE)


def _find_uvb_increase(segment: str) -> Optional[int]:
    """找 UVB 加量值(方向安全)。明寫 add/increase(branch a)優先;沒有才用『數字 + each time』
    (branch b),且該數字前方緊跟 decrease/reduce/taper/lower/減/降/'-' 一律否決。回加量值或
    None(找不到 → caller 走 maintain/dose>=max/PARSE_FAIL 既有邏輯)。[UC-03]"""
    a = _INCREASE_A_RE.search(segment)
    if a:
        try:
            return int(a.group(1))
        except (TypeError, ValueError):
            return None
    for b in _INCREASE_B_RE.finditer(segment):
        if _DECREASE_BEFORE_RE.search(segment[:b.start(1)]):
            continue   # 遞減醫囑,不是加量 → 略過這個「數字 each time」
        try:
            return int(b.group(1))
        except (TypeError, ValueError):
            return None
    return None
# [v20.8] MAX 接受多種同義表達:
#   MAX:N / MAX N / MAX at N / MAX dose: N / fix N / fixed at N / fixed to N / 固定 N
# \bfix(?:ed)? 確保 word boundary 避免抓到 "prefix"/"fixing" 等
# [v20.15] 新增 "MAX dose" 寫法 (鄧仲強實機 case: "MAX dose: 1200mj/cm2")
# [v20.17] 新增 "MAX UVB / MAX Phototherapy" 寫法 (黃冠輝實機 case:
#   "max UVB 1800 mj/cm2")
# [2026-06-01] 新增 "upper limit" / "上限" 同義(曾大鈞實機 case:
#   "...Add 50mj each time, upper limit: 950mj")。
# [2026-06-09] 分隔符也接受逗號:「fixed at, 1000」「MAX, 800」「固定，1000」這類
#   關鍵字與數字間夾逗號的自由寫法(劉峻榕實機 case)。原本只允許冒號/空白,逗號會讓
#   MAX 抓不到數字 → 整行 parse_fail。與 _UVB_DOSE_RE 已接受逗號的設計一致。
# [UC-04 2026-07-10] till/until 前加 \b —— 否則 "still 900" 內含 till 會被當上限(把 900 當 MAX,
#   讓無 MAX 行被誤判結構完整而自動更新);捕獲數字後加 (?![\d/-]) —— 否則 "treat until 2026/9/1"
#   的年份 2026 會被當 MAX、真 MAX:800 被略過 → 劑量寫回突破醫師上限(830>800 實測)。日期年份
#   後接 / 或 - 會被 lookahead(含 greedy 回溯)整段否決,regex 續掃到後面真正的 MAX。
_UVB_MAX_RE = re.compile(
    r"(?:MAX(?:\s+(?:dose|UVB|Phototherapy))?(?:\s+(?:at|to))?\s*[:：,，]?\s*"
    r"|\bfix(?:ed)?(?:\s+(?:at|to))?\s*[:：,，]?\s*"
    r"|upper\s*limit(?:\s+(?:at|to))?\s*[:：,，]?\s*"
    r"|(?:each\s+time\s+)?\b(?:till|until)\s*[:：,，]?\s*"
    r"|maintain\s+dose\s+at\s*[:：,，]?\s*"
    r"|最大(?:劑量|剂量)?\s*[:：,，]?\s*"
    r"|上限(?:在|為)?\s*[:：,，]?\s*"
    r"|固定(?:在|為)?\s*[:：,，]?\s*)(\d+)(?![\d/-])(?!\.\d)",
    # [UC-04b audit 2026-07-12] 除 / - 外再擋「.」分隔日期:"treat until 2026.9.1" 的 2026
    # 不可當 MAX(否則略過真正 MAX:800 → 寫 830 破上限)。用 (?!\.\d) 只擋「點後接數字」的
    # 日期,不誤殺句尾句點如 "MAX: 800."(點後非數字 → 仍接受 800)。
    re.IGNORECASE,
)

# [2026-06-18] 判斷某劑量數字「前方」是否緊跟 MAX/上限關鍵字 —— 是的話那個數字是
# 上限(ceiling)而非本次要照的劑量,不可拿來當「本次劑量」跳確認(MAX 可超過 1500)。
# 關鍵字集合與 _UVB_MAX_RE 同步,但拿掉結尾的 (\d+)、改成 anchored 在字串尾。
_CEILING_KEYWORD_BEFORE_RE = re.compile(
    r"(?:MAX(?:\s+(?:dose|UVB|Phototherapy))?(?:\s+(?:at|to))?"
    r"|\bfix(?:ed)?(?:\s+(?:at|to))?"
    r"|upper\s*limit(?:\s+(?:at|to))?"
    r"|(?:each\s+time\s+)?\b(?:till|until)"     # [UC-04] \b 擋 "still"
    r"|maintain\s+dose\s+at"
    r"|最大(?:劑量|剂量)?"
    r"|上限(?:在|為)?"
    r"|固定(?:在|為)?)\s*[:：,，]?\s*$",
    re.IGNORECASE,
)

# [v20.12 2026-05-26] 同日期 triplet 偵測 — 用於更新非 UVB 關鍵字 (e.g. excimer
# light) 但日期相同的 (count) ... (date) 三元組。
# 例如:
#   excimer light (25) 1000mJ for nape on (2026/5/25)
#   1500mj/cm2 (44) on (2026/5/25) add 50 each time, fixed at 1500
# 中間限制無括號避免跨越獨立 segment。
_TRIPLET_RE = re.compile(
    r"[\(（]\s*(\d{1,3})\s*[\)）]"                  # (count)
    r"([^()（）\r\n]{0,120}?)"                       # 中間 (same line only)
    r"[\(（]\s*(\d{4})[-/](\d{1,2})[-/](\d{1,2})\s*[\)）]" # (YYYY/MM/DD)
)
# Triplet 周圍需要的 UVB-相關標記 (避免誤動其他內容如「(10) days for ...」)
# 「\d\s*mj」要求數字接 mJ/mj (劑量單位)，比單純 "mj" 更安全
_UVB_MARKER_RE = re.compile(
    r"(?:uvb|excimer|\d\s*mj|phototherapy|photo\s*therapy)",
    re.IGNORECASE,
)
_EXCIMER_MARKER_RE = re.compile(r"\bexcimer(?:\s+light)?\b", re.IGNORECASE)
_EXCIMER_DOSE_RE = re.compile(r"(\d+)\s*(mj(?:/cm2)?)", re.IGNORECASE)

# [UC-08 2026-07-12] triplet 中段((count) 與配對 (日期) 之間)的「裸日期樣 token」
# 偵測 —— 帶括號的日期本就被 _TRIPLET_RE 的中段排除,裸日期(斜線/連字號/點分)不會。
# (count) 與較遠的括號日期之間夾另一個日期時,count 的歸屬不明(更可能屬於較近那個
# 日期)→ Step C 不 bump、uncertain 不納入,留原樣交醫師。典型案例:Step A 更新
# "(10) 2026/7/8 done, next on (2026/7/8)" 後殘留的重複日期,會讓驅動行自己的 count
# 被當續行再 +1(count+2、行內兩處日期都被改)。
_DATE_LIKE_IN_MIDDLE_RE = re.compile(r"\d{2,4}[./-]\d{1,2}[./-]\d{1,2}")


# [2026-06-25] excimer 劑量偵測時,前方緊跟「上限 / 加劑量」關鍵字的數字【不是本次劑量】:
#   - 上限:MAX / fixed at / upper limit / 上限 / 固定 …(= _CEILING_KEYWORD_BEFORE_RE 那組)
#   - 加量:increase / add / 每次加 / 增加 …
# 例:'Excimer fixed at 1000, 810 …' 的 1000 是上限、810 才是劑量 → 必須跳過 1000。
_EXCIMER_NONDOSE_BEFORE_RE = re.compile(
    r"(?:MAX(?:\s+(?:dose|UVB|Phototherapy))?(?:\s+(?:at|to))?"
    r"|\bfix(?:ed)?(?:\s+(?:at|to))?"
    r"|upper\s*limit(?:\s+(?:at|to))?"
    r"|(?:each\s+time\s+)?\b(?:till|until)"     # [UC-04] \b 擋 "still"
    r"|maintain\s+dose\s+at"
    r"|最大(?:劑量|剂量)?|上限(?:在|為)?|固定(?:在|為)?"
    r"|in\s*cr(?:e?a?|a?e?)se[d]?(?:\s+by)?|add(?:ing|ed|s)?(?:\s+by)?"
    r"|每次增加|每次加|增加|加)\s*[:：,，]?\s*$",
    re.IGNORECASE,
)


def _find_excimer_dose(line: str, start: int, date_span=None):
    """找 excimer 劑量:marker 之後第一個「不在括號內、非日期片段、前方非上限/加量關鍵字、
    且 >= MIN_DOSE」的數字。

    [2026-06-25] 不再硬性要求寫 'mj' 單位 —— 同一般 UVB(關鍵字後的數字就是劑量)。
    實機 'Excimer light: 700 m j/cm2'(m 與 j 中間有空格)、'Excimer light 810'(沒寫單位)
    舊 _EXCIMER_DOSE_RE 都吃不到 → F2/F3「完全沒反應」。排除:括號內(次數 (102)/日期)、
    日期片段(像沒加括號的 'on 2026/6/22' 的年月日,Codex 指出否則會把 2026 當劑量)、前方是
    MAX/上限/increase 關鍵字的數字(那是上限/加量)、< MIN_DOSE 的小數字(像 '2 shots'、'add 30')。
    回 (數字起點, 數字終點, 數值) 或 None。"""
    for nm in re.finditer(r"\d+", line[start:]):
        s = start + nm.start()
        e = start + nm.end()
        # 落在主日期 span 內(年/月/日)→ 不是劑量(防 'on 2026/6/22 810' 把 2026 當劑量)
        if date_span and s < date_span[1] and e > date_span[0]:
            continue
        # 與日期分隔符 '/'、'-' 相鄰且另一側也是數字(像 2026/6/22 的年月日片段)→ 不是劑量
        if ((s > 1 and line[s - 1] in "/-" and line[s - 2].isdigit())
                or (e + 1 < len(line) and line[e] in "/-" and line[e + 1].isdigit())):
            continue
        # 在括號內(次數 (102)/日期)→ 不是劑量;容許「( 102 )」括號與數字間有空白(Codex 指出)
        j = s - 1
        while j >= 0 and line[j] in " \t":
            j -= 1
        if j >= 0 and line[j] in "(（":
            continue
        # [codex P1] 只看「本段起點(start) 之後」的緊鄰前綴,避免同行前一段的 add/MAX 關鍵字
        # 誤判掉後一段的劑量(此檢查本就 $ 錨定緊鄰前綴,改用 line[start:s] 語意更精準且不改行為)。
        if _EXCIMER_NONDOSE_BEFORE_RE.search(line[start:s]):   # 前方是上限/加量關鍵字 → 不是劑量
            continue
        val = int(nm.group(0))
        if val >= MIN_DOSE:
            return s, e, val
    return None

# [2026-06-18] F2/F3 照光分流偵測:處置屬於哪種照光,決定要不要 key 51019/療程、
# 身份是否改 01。關鍵在「光療 / Phototherapy 是【泛稱】」—— 中文「光療」「準分子光療」
# 也用來指 excimer(準分子=excimer),不能因為出現「光療」就當成健保 UVB。
# 故分三層,且【excimer 優先於泛稱光療】:
#   1) UVB-specific:UVB / 紫外線 / 獨立 UV —— 一定是健保 UVB → "uvb"(含 excimer+UVB)。
#   2) excimer(自費):excime(含打字漏 r)/ 中文「準分子」—— 無 UVB-specific 時 → "pure_excimer"
#      (即使同時寫了泛稱「光療 / phototherapy」也算純 excimer)。
#   3) 泛稱光療(photo therapy / 光療)且【無 excimer】→ 沿用既有行為當 "uvb"。
_PT_UVB_SPECIFIC_RE = re.compile(r"(?:UVB|紫外線|\bUV\b)", re.IGNORECASE)
_PT_EXCIMER_RE = re.compile(r"(?:excime|準分子)", re.IGNORECASE)
_PT_GENERIC_RE = re.compile(r"(?:photo\s*therapy|光療)", re.IGNORECASE)
# 劑量訊號(數字+mJ):用來分辨「泛稱光療」是治療醫令行(有劑量)還是病史/轉介語境(無劑量)
_PT_DOSE_RE = re.compile(r"\d\s*mj", re.IGNORECASE)
_PT_NUM_RE = re.compile(r"\d+")
_PT_MJ_PREFIX_RE = re.compile(r"\s*mj", re.IGNORECASE)
# [2026-06-29 Codex r3-r6] 判斷 UVB 字眼附近的數字是不是『真照光劑量』要靠【劑量醫令結構】,不能只看
# 數字本身,否則病史的次數('2 years'/'10 times')、年份('course 2019')、病歷號('chart 123456')、
# 體重('BW 100 kg')、BSA、檢驗值都會被當劑量而把純 excimer 卡住;反過來把無單位真劑量誤排成年份又會
# 漏 key 健保。劑量結構 = 數字後緊接:次數括號 '(9)/(20)'、'on (日期)'、increase/add/max/fixed/shots
# 等醫令詞('mj' 單位另判、不限距離)。
_PT_DOSE_CONTEXT_RE = re.compile(
    r"\s*(?:"
    r"[\(（]\s*\d+\s*[\)）]"                       # 次數括號 (9)/(20)(純數字,排除日期 (2026/6/24))
    r"|on\s*[\(（]\s*\d"                           # on (日期)
    r"|(?:increase|inc|add|max|fixed|shots)\b"    # 劑量醫令詞
    r")", re.IGNORECASE)
# 數字若【緊接】在 UVB 字眼後(中間只隔空白/冒號/逗號)→ 可當無單位劑量('紫外線 450'、'UVB, 850'),但要
# 非 4 位年份、後面不接 次數/年數/療程數/藥物單位('UVB 2 years'、'UVB 100 times'、'UVB 100 doses'、
# 'UVB 2019' 排除)。doses? 也排除('100 doses' = 病史療程數,非劑量)。
_PT_UVB_SEP_RE = re.compile(r"[\s:：,，、]*")
_PT_NONDOSE_AFTER_RE = re.compile(
    r"\s*(?:years?|yrs?|times?|sessions?|doses?|months?|weeks?|days?|"
    r"年|個?月|週|次|回|堂|療程|歲|"
    r"(?:mg|mcg|ug|ml|cc|iu|kg)\b|%)", re.IGNORECASE)


def _uvb_window_has_dose(window: str) -> bool:
    """window =『UVB 字眼起(視窗開頭即該字眼)→ 其後第一個 excimer marker 為止』。裡面有沒有一個帶
    【劑量醫令結構】的數字:
      1) 緊接 'mj' 的數字 → 一定算(單位明確,不限距離);
      2) 否則先要 ≥ MIN_DOSE(<50 的多半是次數/年數,如 '2 years'、'x 10 (3)' → 直接略過);
      3) 後面接 次數括號/on(日期)/醫令詞(_PT_DOSE_CONTEXT_RE)→ 結構化劑量,不限距離(體位描述很長時劑量
         會離 UVB 字眼很遠,Codex r7);
      4) 或【緊接 UVB 字眼】(中間只隔 空白/冒號/逗號)且非 4 位年份、後面不接 次數/年數/療程數/藥物單位
         → 無單位劑量('紫外線 450')。
    排除病史的次數/年數/年份/病歷號/體重/BSA/檢驗值(它們不在 UVB 字眼正後方,也沒有劑量結構)。"""
    kw = _PT_UVB_SPECIFIC_RE.match(window)
    kw_end = kw.end() if kw else 0
    for nm in _PT_NUM_RE.finditer(window):
        rest = window[nm.end():]
        if _PT_MJ_PREFIX_RE.match(rest):
            return True
        num = nm.group(0)
        n = int(num)
        if n < MIN_DOSE:
            continue
        if _PT_DOSE_CONTEXT_RE.match(rest):
            return True
        if (_PT_UVB_SEP_RE.fullmatch(window[kw_end:nm.start()])
                and not (len(num) == 4 and 1990 <= n <= 2099)
                and not _PT_NONDOSE_AFTER_RE.match(rest)):
            return True
    return False


def _has_dosed_uvb_specific(t: str) -> bool:
    """是否有【帶劑量的 UVB-specific 醫令】= 本次真健保 UVB(會與別欄位 excimer 形成 ambiguous,且
    update 端據此強制走/不走 excimer)。

    [2026-06-29 Codex r3-r6] 劑量必須歸屬給 UVB 自己:不能讓 excimer 的劑量(r3 跨行/r4 同行)替它背書,
    也不能把病史的次數/年數/年份/病歷號/體重/檢驗值(r5/r6)當劑量。做法:對每個 UVB 字眼,只看『該字眼
    起 → 其後第一個 excimer marker 為止』的視窗,且視窗內要有帶劑量醫令結構的數字(見 _uvb_window_has_dose):
      - 'Previous tx: UVB 2 years ago, now excimer 700 mj' → 視窗 'UVB 2 years ago, now ' 無劑量結構、
        '700' 在 excimer 之後不算 → 讓位給 excimer → pure_excimer(不再被 bare UVB 卡住)。
      - 'excimer 1000mj UVB 500mj'(UVB 在 excimer 之後且自己帶劑量)→ 視窗 'UVB 500mj' → uvb(健保不可漏 key)。
      - 'UVB: 2000 (20) on (date) max 2500'(無單位 4 位數)→ 後接次數括號 → uvb(年份判定不會誤排真劑量)。
    註:極罕見的『UVB + excimer: 450/700』共用劑量合併醫令,UVB 視窗在 excimer 前結束、看不到 450 →
    會判 pure_excimer(可接受邊角;真實病歷一治療一行/一欄,且醫師仍會在 F2 結果上看到)。"""
    for um in _PT_UVB_SPECIFIC_RE.finditer(t):
        nxt_exc = _PT_EXCIMER_RE.search(t, um.end())
        window = t[um.start():nxt_exc.start() if nxt_exc else len(t)]
        if _uvb_window_has_dose(window):
            return True
    return False


def _has_uvb_or_phototherapy_treatment(text: str) -> bool:
    """text 是否有『會讓 excimer 退讓』的 UVB/光療治療訊號(給 update_uvb_in_text 的 excimer gate):
      - UVB-specific(UVB / 紫外線 / 獨立 UV)→ True(健保 UVB,不論有無劑量都保護);
      - UVB / Phototherapy / UV + 數字(劑量)→ True —— 直接用 parser 同一個 _UVB_DOSE_RE 偵測,
        與「真的會被 parser 當成 UVB/光療治療」完全同一把尺(自動涵蓋跨行、無 mj 單位、夾 dose/to
        等自由寫法,不會比 parser 寬鬆而漏判 → Codex 指出逐行 + \\d mj 太窄)。
    只有衛教備註裡的裸 phototherapy 字眼(關鍵字後沒有可配對的劑量數字,如 'avoid phototherapy days')
    → False → 不擋 excimer 劑量更新。"""
    if not text:
        return False
    return bool(_PT_UVB_SPECIFIC_RE.search(text) or _UVB_DOSE_RE.search(text))
# 任一照光關鍵字(粗篩用,給「濾掉太舊的照光段落」逐行判斷哪些行算照光段落)
_PHOTO_ANY_RE = re.compile(
    r"(?:UVB|紫外線|\bUV\b|excime|準分子|photo\s*therapy|光療)", re.IGNORECASE)
# 預設:照光段落日期早於「今天 - 此月數」→ 視為已暫停,分流時忽略該段落(使用者 2026-06-23)
STALE_PHOTO_MONTHS = 2


def _months_before(d: date, months: int) -> date:
    """d 往前推 months 個月(跨年自動處理,日數超過當月則夾到月底)。純函式。"""
    total = d.year * 12 + (d.month - 1) - months
    y, m = divmod(total, 12)
    m += 1
    last = calendar.monthrange(y, m)[1]
    return date(y, m, min(d.day, last))


def _real_dates_in(text: str) -> list:
    """text 內所有【可解析且為合法日曆日】的日期(date 物件)。單一把尺,給 strip 判斷「日期是否全部
    太舊」與 _line_has_undated_active_photo 判斷「段內有無日期」共用 —— 兩邊用同一定義才不會不一致而誤丟。
    日期形狀但解析不出(_resolve_date_match 回 None)或壞月日(date() 丟 ValueError)一律不算數。"""
    out = []
    for dm in _UVB_DATE_RE.finditer(text):
        ymd = _resolve_date_match(dm)
        if ymd:
            try:
                out.append(date(*ymd))
            except ValueError:
                pass
    return out


def strip_stale_phototherapy_segments(text: str, today: date,
                                      months: int = STALE_PHOTO_MONTHS) -> str:
    """逐【行】判斷:一行【有照光關鍵字、有日期、且該行所有日期都早於 (today - months 個月)】→ 視為
    該段照光已暫停,分流偵測時忽略整行(只供分流,【不】改處置原文)。只要該行有任一【近期】日期、或
    【根本沒日期】(像初診/醫師沒寫)→ 保留;非照光行一律保留。

    [為何用整行『所有日期都太舊』而不是更細的段落切割] 真實病歷一個治療寫一行(例:UVB 一行、
    excimer 一行),這條規則對使用者實機(圖二:近期 UVB 行 + 一年多前 excimer 行,各自一行)完全正確,
    且【絕不比現況更不安全】—— 有任一近期日期就整行保留、分類與現行完全相同;只有『整行日期全部太舊』
    才忽略(該行確定是停掉的舊治療)。在同一行硬切治療段落會因日期/關鍵字/劑量位置千變萬化而誤切、
    把還在做的治療誤丟或讓舊治療誤導身份別(Codex 連續四輪指出多種誤切方向),反而不安全,故不採用。"""
    if not text or today is None:
        return text or ""
    cutoff = _months_before(today, months)
    kept = []
    for line in re.split(r"[\r\n]+", text):
        if _PHOTO_ANY_RE.search(line):
            line_dates = _real_dates_in(line)
            # 整行照光、日期全部太舊、【且該行沒有「無日期卻有劑量」的 active 段】→ 已暫停 → 忽略。
            # 最後那道護欄保證【絕不會比現況更不安全】:只要行內還有一段在做(有劑量、沒日期)的醫令,
            # 就整行保留、分類沿用現行,不會把它連同舊段一起丟掉而誤分流(Codex 指出的同行 active+舊 混合)。
            if (line_dates and all(d < cutoff for d in line_dates)
                    and not _line_has_undated_active_photo(line)):
                continue
        kept.append(line)
    return "\n".join(kept)


def _line_has_undated_active_photo(line: str) -> bool:
    """該行是否含『無日期』的照光治療段落 → 視為可能還在做(沒日期就無法判斷新舊),整行保留不丟。
    以照光關鍵字切段(關鍵字到下一個關鍵字前),只要某段【段內無日期】→ True。

    [最保守、與寫法完全無關 —— 可數學證明絕不比現況更不安全] 只要行內有任一段照光沒寫日期就保留整行,
    保證『絕不誤丟沒日期/近期的在做治療』(否則身份別/51019 會分流錯誤)。逐一列舉醫令字眼/數值都必有
    漏網(Codex 連續八輪舉出 510 mj、increase 30、max 800、continue 510、x2 week、BIW、TIW… 各種寫法),
    故不靠內容判斷,只看『有沒有日期』。只有【每一段照光都有日期、且日期全部太舊】(像圖二單一舊 excimer
    一段、日期一年多前)才會整行忽略。代價:複合名稱(如 'UVB phototherapy'、'準分子光療')因關鍵字被切出
    無日期殘段而不被忽略 → 偏安全方向(沿用現行分類、不誤分流),非本次新引入的風險。"""
    marks = [m.start() for m in _PHOTO_ANY_RE.finditer(line)]
    for i, start in enumerate(marks):
        end = marks[i + 1] if i + 1 < len(marks) else len(line)
        seg = line[start:end]
        # 用【可解析且為合法日期】判斷(_real_dates_in,與下方 strip 的 line_dates 完全同一把尺)——
        # 避免「日期形狀但解析不出(補零超界 '0050/06/10')或壞月日('2024/13/40')」的字串被當成『有
        # 日期』,反而把實質無日期的在做治療誤丟(Codex 第 10 輪 #5)。段內無合法日期 → 視為無日期 → 保留。
        if not _real_dates_in(seg):
            return True
    return False


def _line_has_undated_uvb_segment(line: str) -> bool:
    """以照光關鍵字切段、【首段從行首起】(關鍵字前的日期也算進首段)→ 有任一段無合法日期 = 該段照光沒寫
    日期(可能還在做)。與 _line_has_undated_active_photo 差在『首段含關鍵字前文字』→ 正確處理『日期寫在
    關鍵字前』(楊亮筠實機 '(2026/05/15) UVB 500 ...';parse_uvb_line 也特別支援此格式)。否則舊 UVB 會因
    關鍵字後沒日期而被誤判成無日期 active 段而不被忽略(Codex r9)。供 _strip_stale_uvb_when_recent_excimer
    判斷舊 UVB 行是否可忽略;多段時後段日期歸前段、後段判無日期 → 偏保守(整行保留),不會誤丟。"""
    marks = [m.start() for m in _PHOTO_ANY_RE.finditer(line)]
    for i in range(len(marks)):
        seg_start = 0 if i == 0 else marks[i]
        seg_end = marks[i + 1] if i + 1 < len(marks) else len(line)
        if not _real_dates_in(line[seg_start:seg_end]):
            return True
    return False


def _strip_stale_uvb_when_recent_excimer(text: str, today: date) -> str:
    """有【近期(未過 MODIFY_STALE_MONTHS=1 個月)的 excimer】時,把【整段日期都早於 1 個月的純 UVB 行】
    當作已停掉的舊 UVB → 分流時忽略,讓近期 excimer 主導。

    [2026-06-29 Codex r8] detect 的一般 strip 用 STALE_PHOTO_MONTHS(=2 個月),但 update 的 stale-confirm
    用 MODIFY_STALE_MONTHS(=1 個月)。兩者不一致時,1-2 個月前的舊 UVB(像近期改做 excimer、處置欄仍留
    5-8 週前 UVB 紀錄、一治療一行)會:被 detect 判 uvb(2 月內不 strip)→ 主流程偏健保 UVB/ambiguous;
    update 又對它跳『超過 1 個月』確認 → 近期 excimer 本該更新卻被卡住、或醫師按 Yes 誤改舊 UVB。故此處讓
    【近期 excimer 在場】時把『日期全部早於 1 個月』的純 UVB 行一併忽略,使 detect 與 update 對「本次治療
    是那個 excimer」取得一致(且合規:不替已停掉的舊 UVB key 51019)。只在 excimer 近期時生效;UVB-only
    或 excimer 也舊 → 不動(維持舊 UVB 的 stale-confirm 流程);同行 UVB+excimer 合併醫令 → 不動(保守)。"""
    if not text or today is None:
        return text or ""
    cutoff = _months_before(today, MODIFY_STALE_MONTHS)
    lines = re.split(r"[\r\n]+", text)
    has_recent_excimer = any(
        _PT_EXCIMER_RE.search(ln)
        and (not _real_dates_in(ln) or any(d >= cutoff for d in _real_dates_in(ln)))
        for ln in lines)
    if not has_recent_excimer:
        return text
    kept = []
    for line in lines:
        if _PT_UVB_SPECIFIC_RE.search(line) and not _PT_EXCIMER_RE.search(line):
            dts = _real_dates_in(line)
            if (dts and all(d < cutoff for d in dts)
                    and not _line_has_undated_uvb_segment(line)):
                continue   # 純 UVB 行、日期全部早於 1 個月 → 已停掉的舊 UVB → 忽略,讓近期 excimer 主導
        kept.append(line)
    return "\n".join(kept)


def detect_phototherapy_kind(text: str, today: Optional[date] = None,
                             stale_months: int = STALE_PHOTO_MONTHS) -> str:
    """判斷【單一欄位】屬於哪種照光,給 F2/F3 分流用。回傳:

      "uvb"          — 有 UVB-specific 訊號(UVB/紫外線/獨立 UV)→ 確定健保 UVB:
                       正常 key 51019 + 療程,身份不動(含同欄位 excimer+UVB 並存)。
      "pure_excimer" — 有 excimer / 準分子 且【無】UVB-specific 訊號 → 自費:
                       不 key 51019/療程,身份→01(即使同欄位另寫了泛稱「光療」)。
      "uvb_generic"  — 只有【泛稱】光療(photo therapy / 光療)且【無劑量】(像病史/轉介語境,
                       例:轉介單「refer for phototherapy」),無 UVB-specific 也無 excimer。
                       單獨出現時當 uvb(combine 會收斂成 "uvb");但因「光療」是泛稱
                       (準分子光療=excimer 也叫光療),【不可】與別欄位的 excimer 形成歧義 →
                       由 combine 讓 excimer 涵蓋它(見 combine_phototherapy_kinds)。
      "none"         — 都沒有。

    安全考量(billing 敏感):
      - 只要出現 UVB-specific 就回 "uvb",不會把健保 UVB 漏 key;UVB-specific 與別欄位
        excimer 仍會被 combine 判為 ambiguous(真正衝突,交醫師)。
      - 「光療 / phototherapy」是泛稱(中文「準分子光療」也是 excimer)。但只有【無劑量】的泛稱
        (病史/轉介語境)才弱化成 "uvb_generic" 讓 excimer 涵蓋;若泛稱光療【帶劑量】
        (像 "phototherapy 500 mj/cm2" 的治療醫令行,可能是寫得不夠精確的健保 UVB)→ 仍回 "uvb",
        與別欄位 excimer 維持 ambiguous,避免把可能的健保 UVB 靜默分流成自費 excimer。

    [2026-06-23] 傳入 today 時,先濾掉『日期早於 today - stale_months 個月』的照光行
    (見 strip_stale_phototherapy_segments)—— 那段照光極可能已暫停,不該拿來判斷本次身份別。
    """
    t = text or ""
    if today is not None:
        t = strip_stale_phototherapy_segments(t, today, stale_months)
        # [2026-06-29 Codex r8] 近期 excimer 在場時,連 1-2 個月前的舊 UVB 也忽略(與 update 的
        # MODIFY_STALE_MONTHS 一致),避免舊 UVB 把近期 excimer 卡住或誤分流成健保 UVB。
        t = _strip_stale_uvb_when_recent_excimer(t, today)
    # [2026-06-27] UVB-specific 也要【有劑量】才算「本次健保 UVB 醫令」(會與別欄位 excimer 形成歧義);
    # 只有 UVB 字眼【無任何劑量】= 病史/轉介語境(陳韻璇實機:病史 'Previous tx: UVB at singapore',
    # 處置只有 excimer)→ 不算本次 UVB,讓 excimer 涵蓋,不再誤判 ambiguous 卡住 F2。
    # [2026-06-29 Codex r3] 順序很重要:先判「帶劑量 UVB」→ uvb,再判 excimer → pure_excimer,最後才是
    # bare UVB → uvb_generic。原本「UVB-specific 一律先回(uvb/uvb_generic)」會讓『bare UVB + 同欄位
    # excimer』回 uvb_generic;若此欄是唯一照光欄,combine 會收斂成 uvb → 把純 excimer 誤分流成健保 UVB,
    # 且 update 端 _is_pure_excimer 不成立而把 excimer 卡住。改成 excimer 早於 bare-UVB 即可正確 pure_excimer。
    # 劑量歸屬用 _has_dosed_uvb_specific(排除 excimer 行),避免 excimer 劑量替 bare UVB 背書。
    if _has_dosed_uvb_specific(t):
        return "uvb"
    if _PT_EXCIMER_RE.search(t):
        return "pure_excimer"
    if _PT_UVB_SPECIFIC_RE.search(t):
        # 只有 bare UVB(無劑量、無 excimer)= 病史/轉介語境 → uvb_generic;單獨一欄時 combine 收斂成 uvb。
        return "uvb_generic"
    if _PT_GENERIC_RE.search(t):
        # 帶劑量 → 像治療醫令行,當實質 UVB("uvb");無劑量 → 病史/轉介語境("uvb_generic")。
        # (此分支必【無】excimer —— 上面已先回 pure_excimer,故 _PT_DOSE_RE 全域搜不會抓到 excimer 劑量)
        return "uvb" if _PT_DOSE_RE.search(t) else "uvb_generic"
    return "none"


def combine_phototherapy_kinds(kinds) -> str:
    """把多個欄位(memo)各自的 detect_phototherapy_kind 結果彙整成單一結論:

      "ambiguous"    — 同時出現【UVB-specific 的 "uvb"】與 pure_excimer(分屬不同欄位)→
                       真正無法判斷是健保 UVB 還是自費 Excimer,caller 應警告中止、交醫師手動。
      "pure_excimer" — 有 pure_excimer 且【無】UVB-specific(即使另有泛稱光療 "uvb_generic")。
                       泛稱光療被 excimer 涵蓋(準分子光療=excimer),不形成歧義。
      "uvb"          — 有 UVB-specific 或只有泛稱光療(無 excimer)。
      "none"         — 都沒有(忽略 "none")。

    用於主程式逐 memo 分類後跨欄位彙整。關鍵:只有【UVB-specific】才會與別欄位 excimer 形成
    歧義;病史/轉介單裡的【泛稱】「光療 / phototherapy」不再與處置的 excimer 互打而誤判 ambiguous
    (使用者實機案例:處置 excimer + 病史 'refer for phototherapy' 被卡住無法觸發 F2)。"""
    s = set(kinds or ())
    has_uvb_specific = "uvb" in s
    has_exc = "pure_excimer" in s
    has_generic = "uvb_generic" in s
    if has_uvb_specific and has_exc:
        return "ambiguous"
    if has_exc:
        return "pure_excimer"          # excimer 涵蓋泛稱光療,不歧義
    if has_uvb_specific or has_generic:
        return "uvb"
    return "none"


def _date_text(dt: date, sep: str = "/") -> str:
    """格式化日期並保留原本分隔符，sep 非 '-' 時預設用 '/'。"""
    sep = "-" if sep == "-" else "/"
    return f"{dt.year}{sep}{dt.month:02d}{sep}{dt.day:02d}"


def _has_maintain_dose(text: str) -> bool:
    return bool(re.search(r"\bmaintain(?:\s+the)?\s+dose\b(?!\s+at\s*\d)", text,
                          re.IGNORECASE))


def _replace_uvb_dose(src: str, old_dose: int, new_dose: int) -> str:
    """Replace a structured UVB dose while preserving its original prefix."""
    match = _UVB_DOSE_RE.search(src)
    if match is None:
        return src
    try:
        if int(match.group(2)) != old_dose:
            return src
    except (TypeError, ValueError):
        return src
    return src[:match.start(2)] + str(new_dose) + src[match.end(2):]


def _resolve_date_match(date_m) -> Optional[tuple]:
    """[v20.15] _UVB_DATE_RE 有 4 個 alternative，回 (year_ad, month, day) 或 None。

    Group layout:
        1-3:  AD paren  (\\d{4}/\\d{1,2}/\\d{1,2})
        4-6:  AD bare    \\d{4}/\\d{1,2}/\\d{1,2}
        7-9:  ROC paren slash  (\\d{3}/\\d{1,2}/\\d{1,2})    民國年 + 1911
        10-12: ROC concat       (\\d{3}\\d{2}\\d{2})         民國年 + 1911
    """
    g = date_m.groups()
    is_ad = False
    # AD slashed (paren or bare)
    if g[0]:    # AD paren
        y, m, d = int(g[0]), int(g[1]), int(g[2])
        is_ad = True
    elif g[3]:  # AD bare
        y, m, d = int(g[3]), int(g[4]), int(g[5])
        is_ad = True
    elif g[6]:  # ROC paren slash (3-digit)
        roc_y = int(g[6])
        if not (60 <= roc_y <= 200):  # 民國 60-200 = AD 1971-2111
            return None
        y, m, d = roc_y + 1911, int(g[7]), int(g[8])
    elif g[9]:  # ROC concat 7-digit
        roc_y = int(g[9])
        if not (60 <= roc_y <= 200):
            return None
        y, m, d = roc_y + 1911, int(g[10]), int(g[11])
    else:
        return None
    # [2026-06-23] 4 位數「西元年」< 1911 不是真實病歷的西元年:幾乎都是補零的民國年
    # (例 '0115' = 民國 115 = 2026)。在民國合理範圍就換算成西元;否則視為無法解析(回 None,
    # 呼叫端當『無日期』處理)→ 絕不把當前治療的日期誤判成上古而誤丟(Codex 第 9 輪指出)。
    if is_ad and y < 1911:
        if 60 <= y <= 200:
            y += 1911
        else:
            return None
    return y, m, d


# [UC-02 2026-07-10] 「日期形狀殘跡」偵測 —— 比 _UVB_DATE_RE 寬,用來抓「看起來像日期但沒被
# 解析成功」的殘跡(裸民國 115/7/9、點分隔 2026.7.9、括號 7-8 位純數字/病歷號連寫日期)。有這種
# 殘跡就【不是】真正無日期的 first-time,而是『日期存在但解析不出』→ 走 first-time 會繞過
# TOO_CLOSE/decay/stale 全部間隔防線(昨天照過也照樣 +increase)。
# [codex P2] 限縮到「合理日期範圍」避免誤判量測值:年只收西元 1900-2099 或民國 100-199(_UVB_DATE_RE
#  /_resolve_date_match 實際支援的範圍)、月 1-12、日 1-31。這樣血壓 'BP 120/80/70'(月 80 超範圍)、
#  檢驗 'lab 500/10/20'(年 500 非合理年)都不會被當日期而誤 PARSE_FAIL。
_UNPARSED_DATE_SHAPE_RE = re.compile(
    r"\b(?:(?:19|20)\d{2}|1\d{2})"           # 年:西元 1900-2099 或民國 100-199
    r"[./-](?:0?[1-9]|1[0-2])"                # 月 1-12
    r"[./-](?:0?[1-9]|[12]\d|3[01])\b"        # 日 1-31
    r"|[(（]\s*\d{7,8}\s*[)）]")                # 括號 7-8 位連寫(日期/病歷號)


def _has_unparsed_date_shape(segment: str) -> bool:
    """segment 內是否含『看起來像日期但沒被 _UVB_DATE_RE 解析成功』的殘跡。[UC-02]"""
    return bool(_UNPARSED_DATE_SHAPE_RE.search(segment))


def _first_resolvable_date(segment: str, dose_off: int):
    """[UC-02] 回 (date_m, last_date):用 finditer 逐一試,回第一個能解析成合法日期者;優先『劑量
    之後』的日期(典型 'dose (count) on (date)')。原本只試第一個【形狀】match、resolve 失敗就整段
    放棄 → 7-8 位病歷號 (1234567)/壞日期會讓真正合法的 (2026/7/9) 被跳過、整段誤當『無日期』走
    first-time。找不到任何可解析日期回 (None, None)。

    [codex P1] 劑量之後若【出現過】日期形狀但都不可解析(如 'on (2026/2/30)' 二月卅日)→ 不可退回用
    劑量【之前】的日期(那可能是 'since 起始日' 等不相關日期,會把治療時序算錯);安全 fail 交醫師。
    只有劑量之後【完全沒有】任何日期形狀時,才用劑量之前的(罕見 '(date) UVB ...' 寫法)。"""
    post_dose_shape_seen = False
    best_before = None
    for m in _UVB_DATE_RE.finditer(segment):
        after_dose = m.start() >= dose_off
        if after_dose:
            post_dose_shape_seen = True
        ymd = _resolve_date_match(m)
        d = None
        if ymd is not None:
            try:
                d = date(*ymd)
            except (ValueError, TypeError):
                d = None
        if d is None:
            continue
        if after_dose:
            return m, d            # 劑量之後、可解析 → 直接用(最典型)
        if best_before is None:
            best_before = (m, d)   # 劑量之前第一個可解析的,僅在「劑量之後完全無日期形狀」時才用
    if post_dose_shape_seen:
        return None, None          # 劑量之後有(被 _UVB_DATE_RE 認出的)日期形狀但都壞 → 安全 fail
    # [codex P1-2] 劑量之後即使 _UVB_DATE_RE 沒認出(不支援格式:點分隔 2026.7.9、裸民國),仍可能
    #  有日期 → 用更寬的形狀偵測;有 → 不可退回劑量【之前】的日期(那可能是 since 起始日,會把治療
    #  時序算錯)。安全 fail 交醫師。
    if _UNPARSED_DATE_SHAPE_RE.search(segment, dose_off):
        return None, None
    return best_before if best_before else (None, None)


def parse_uvb_line(text: str) -> Optional[UvbLineInfo]:
    """從 text 中找第一個 UVB 行，回 UvbLineInfo 或 None。

    [v20.6] 獨立 field 解析 — 順序不限、容忍中文夾雜、increase/add 等同義。

    解析步驟：
      1. UVB 起點 (或 Phototherapy): `(UVB|Phototherapy)\\s*:?\\s*(\\d+)`
      2. MAX 終點 (or fixed/固定): UVB 之後第一個 MAX/fix 找到 max_dose
      3. segment = text[line_start:max_end]   ← [v20.15] 擴到行首吸收日期可能
         在 UVB 前的 case (e.g. "(2026/05/24) UVB 850 ...")
      4. 在 segment 內各別找:
         - date: AD or 民國年 (paren / slashed / concat)
         - count: 不重疊 date 的 `[\\(（]\\s*\\d+\\s*[\\)）]`
         - increase: `(increase[d]?|add|每次加|增加|加)\\s*\\d+`
      5. 任一 field 缺 → 回 None (parse_fail)

    [v20.15] 新增:
      - keyword 接受 "Phototherapy" (劉香君實機 case)
      - 民國年 date format (115/05/24, 1150524 — 詹晟凱/陳文海實機 case)
      - segment 擴到行首吸收 date 在 UVB 前的 case (楊亮筠實機 case)
      - "MAX dose: N" 寫法 (鄧仲強實機 case)
    """
    lower = text.lower()
    # [v20.18] 也接受 "uv" 簡寫 (但需 word boundary 避免誤抓 uveitis/UVA 等)
    if ("uvb" not in lower and "phototherapy" not in lower
            and not re.search(r"\buv\b", lower)):
        return None

    # 1. UVB / Phototherapy dose
    dose_m = _UVB_DOSE_RE.search(text)
    if not dose_m:
        return None
    try:
        dose = int(dose_m.group(2))
    except ValueError:
        return None
    # [2026-06-26 Codex] 劑量數字若【緊跟】上限/固定關鍵字(max / fixed / upper limit / 上限 / 固定…,
    # 即 _CEILING_KEYWORD_BEFORE_RE)→ 那是上限不是本次劑量(新 branch4 '描述+冒號' 會把 'UVB max: 1000'
    # 的 1000 誤抓成劑量)。寧可回 None(parse_fail 讓醫師手動),也絕不可拿上限當本次劑量去加減/維持。
    if _CEILING_KEYWORD_BEFORE_RE.search(text[:dose_m.start(2)]):
        return None
    keyword_text = dose_m.group(1)  # "UVB" or "Phototherapy"
    dose_start = dose_m.start()

    # 2. MAX (從 UVB 之後找) [UC-01 2026-07-10] 限制在 dose 所在行 —— 否則 UVB 行沒寫 MAX 時
    #    會 borrow 下一行別的治療(excimer 等)的 MAX,把 dose/date/count 縫合成假醫令、寫回錯
    #    劑量(實測 "UVB 850 keep\nexcimer... MAX 700" → 850 被改成 700);混合病歷是常態,且
    #    round-trip verify 用同一 parser 重 parse 同樣縫合＝共享盲點照樣過,故必須在 parse 阻斷。
    #    找不到同行 MAX → 維持既有 return None(PARSE_FAIL/SILENT_SKIP)交醫師。
    _line_end = text.find("\n", dose_start)
    if _line_end == -1:
        _line_end = len(text)
    max_m = _UVB_MAX_RE.search(text, dose_start, _line_end)
    if not max_m:
        return None
    try:
        max_dose = int(max_m.group(1))
    except ValueError:
        return None
    max_end = max_m.end()

    # [v20.15] segment 擴到 line_start (UVB 所在行的開頭) — 日期可能寫在
    # UVB 之前 (e.g. "(2026/05/24) UVB 850 ...")
    line_start = text.rfind("\n", 0, dose_start) + 1  # 0 if not found
    segment = text[line_start:max_end]
    dose_off = dose_start - line_start  # 劑量在 segment 內的位置

    # 3. Date — [2026-06-19] 優先找「劑量之後」的日期(典型寫法 "dose (count) on (date)");
    #    找不到才回頭找劑量之前的(罕見「(date) UVB ...」寫法,v20.15 楊亮筠 case)。否則
    #    像「UVB since (108/5/31), new UVB: 1300mj (142) on (2026/06/16)」會誤抓 since 起始
    #    日(108/5/31=民國108=2019)→ days_diff 爆大誤判 SANITY_FAIL(陳松栢實機 case)。
    # [UC-02] 用 finditer 逐一試到能解析(原本只試第一個形狀 match、resolve 失敗就整段放棄 →
    #  7-8 位病歷號/壞日期會讓真正合法的日期被跳過、誤當『無日期』first-time 繞過間隔防線)。
    date_m, last_date = _first_resolvable_date(segment, dose_off)
    if date_m is None:
        return None
    date_text = date_m.group(0)

    # 4. Count (segment 內第一個數字 paren，排除 date 範圍)
    # [v20.7] count 變 Optional — 沒 (N) 處置仍可更新 dose/date
    # [UC-05] count 先在【劑量之後】找 —— 否則 v20.15 segment 擴到行首後,行首清單編號「(1)」會
    #  被當次數(編號被 +1、真次數不動);劑量之前的括號數字只在劑量之後找不到時才接受。
    seg_masked = (
        segment[:date_m.start()]
        + " " * (date_m.end() - date_m.start())
        + segment[date_m.end():]
    )
    count_m = (_UVB_COUNT_RE.search(seg_masked, dose_off)
               or _UVB_COUNT_RE.search(seg_masked))
    count: Optional[int] = None
    if count_m:
        try:
            count = int(count_m.group(1))
        except ValueError:
            count = None

    # 5. Increase / add  [UC-03] 方向安全:add/increase 優先;"N each time" 前是 decrease/
    #    reduce/taper/減/降/- 一律否決(遞減醫囑不是加量,回 None → 走下方 maintain/PARSE_FAIL)。
    increase = _find_uvb_increase(segment)
    if increase is None:
        if _has_maintain_dose(segment):
            increase = 0
        elif dose >= max_dose:
            # [2026-06-08] 劑量已達/超過 MAX 且沒寫 increase → 本就無法再加量(會被
            # cap 在 MAX)，視為 increase=0「保持」。涵蓋「keep phototherapy … to 680
            # mj/cm2 … MAX 680 due to mild pain」這類固定劑量寫法(蔡國華實機 case)。
            # 僅在 dose>=max 這種「無歧義」情況放寬；dose<max 仍維持 return None 的安全
            # 預設(避免醫師漏寫 increase 時程式擅自猜測)。
            increase = 0
        else:
            return None

    return UvbLineInfo(
        full_match=segment,
        dose=dose,
        count=count,
        last_date=last_date,
        increase=increase,
        max_dose=max_dose,
        span=(line_start, max_end),
        date_text=date_text,
        keyword_text=keyword_text,
        date_span=date_m.span(),   # [UC-08] date_m 是對 segment 匹配,span 直接可用
    )


def parse_uvb_partial(text: str) -> Optional[UvbLineInfo]:
    """[v20.16] 寬鬆 parse — 只需要 dose + max，date/count/increase 可缺。

    使用情境：strict parse_uvb_line 失敗時，caller 用這個試試是不是「第一次
    照光」case (有 UVB+劑量+MAX 但沒 (N)/date/increase)。
    回 UvbLineInfo 時 last_date/increase 可能是 None。
    """
    lower = text.lower()
    # [v20.18] 也接受 "uv" 簡寫 (但需 word boundary 避免誤抓 uveitis/UVA 等)
    if ("uvb" not in lower and "phototherapy" not in lower
            and not re.search(r"\buv\b", lower)):
        return None

    dose_m = _UVB_DOSE_RE.search(text)
    if not dose_m:
        return None
    try:
        dose = int(dose_m.group(2))
    except ValueError:
        return None
    # [2026-06-26 Codex] 同 parse_uvb_line:劑量緊跟上限/固定關鍵字 → 是上限不是劑量 → 不解析。
    if _CEILING_KEYWORD_BEFORE_RE.search(text[:dose_m.start(2)]):
        return None
    keyword_text = dose_m.group(1)
    dose_start = dose_m.start()

    # [UC-01] 同 parse_uvb_line:MAX 限 dose 所在行,不跨行縫合(此為 strict 失敗的 fallback,
    #  不修同樣會從這條路徑把下一行別的治療的 MAX 縫進來)。
    _line_end = text.find("\n", dose_start)
    if _line_end == -1:
        _line_end = len(text)
    max_m = _UVB_MAX_RE.search(text, dose_start, _line_end)
    if not max_m:
        return None
    try:
        max_dose = int(max_m.group(1))
    except ValueError:
        return None
    max_end = max_m.end()

    line_start = text.rfind("\n", 0, dose_start) + 1
    segment = text[line_start:max_end]
    dose_off = dose_start - line_start

    # Optional date [UC-02] 同 parse_uvb_line 用 finditer 試到能解析(避免壞日期/病歷號讓可解析的
    #  日期被跳過 → 誤當無日期 first-time)。
    date_m, resolved_date = _first_resolvable_date(segment, dose_off)
    last_date: Optional[date] = resolved_date
    date_text = date_m.group(0) if date_m else ""

    # Optional count (mask date span if any) [UC-05] count 先在劑量之後找,避免行首編號被當次數。
    if date_m:
        seg_masked = (segment[:date_m.start()]
                      + " " * (date_m.end() - date_m.start())
                      + segment[date_m.end():])
    else:
        seg_masked = segment
    count_m = (_UVB_COUNT_RE.search(seg_masked, dose_off)
               or _UVB_COUNT_RE.search(seg_masked))
    count: Optional[int] = None
    if count_m:
        try:
            count = int(count_m.group(1))
        except ValueError:
            count = None

    # Optional increase  [UC-03] 方向安全 helper(遞減不當加量、add 優先於「劑量 each time」)
    increase: Optional[int] = _find_uvb_increase(segment)

    return UvbLineInfo(
        full_match=segment,
        dose=dose,
        count=count,
        last_date=last_date,
        increase=increase,
        max_dose=max_dose,
        span=(line_start, max_end),
        date_text=date_text,
        keyword_text=keyword_text,
        date_span=date_m.span() if date_m else None,   # [UC-08] 同 parse_uvb_line
    )


# ─── 劑量計算 ────────────────────────────────────────────────────────────
def _floor_to_10(value: float) -> int:
    """下十推到 10 倍數: 432→430, 435→430, 437.5→430"""
    return int(math.floor(value / 10) * 10)


def compute_new_dose(*, dose: int, increase: int, max_dose: int,
                     days_diff: int) -> Optional[int]:
    """[v20.10] 依天數差算新劑量。

    days_diff < 2 (即同日或隔天) → 回 None (caller 跳警告)
    其他天數差一定有 int 回值。所有 decay 結果不低於 MIN_DECAY_DOSE (250)。
    """
    if days_diff < TOO_CLOSE_DAYS:                  # 同日或隔天 → 太密集
        return None
    if days_diff < SAME_DOSE_DAYS:                  # 2-6 天 → +increase, cap MAX
        return min(dose + increase, max_dose)
    if days_diff == SAME_DOSE_DAYS:                 # 7 天剛好 → 保持
        return dose
    # [safety] decay/long-gap 結果一律夾在 [.., dose, max_dose]：MIN_DECAY_DOSE(250)
    # 下限在 dose 或 max_dose 低於 250 時，會讓「衰退」反而回傳高於當前劑量/超過
    # 上限的值（照光過量風險）。min(.., dose, max_dose) 確保衰退永遠不增量、不超
    # 上限。正常情況(max_dose=800、dose≥250)此夾值不改變結果。
    if days_diff <= DECAY_75_UPPER:                 # 8-14 天 → ×0.75
        decayed = _floor_to_10(dose * DECAY_75_FACTOR)
        return min(max(decayed, MIN_DECAY_DOSE), dose, max_dose)
    if days_diff <= DECAY_50_UPPER:                 # 15-21 天 → ×0.5
        decayed = _floor_to_10(dose * DECAY_50_FACTOR)
        return min(max(decayed, MIN_DECAY_DOSE), dose, max_dose)
    return min(LONG_GAP_DOSE, dose, max_dose)       # > 21 天 → 250(夾上限/當前)


# ─── 寫回行內容 ──────────────────────────────────────────────────────────
def _today_in_format(today: date, sample_text: str) -> str:
    """[v20.15] 把 today 格式化成跟 sample_text 一樣的 format。

    sample_text 是 _UVB_DATE_RE 的原 match (e.g. "(115/05/24)" / "(1150524)" /
    "(2026/05/24)" / "2026/5/24")。

    支援格式:
      - AD slashed: 2026/5/24 → 2026/05/26
      - AD paren: (2026/5/24) → (2026/05/26)
      - ROC slashed: (115/05/24) → (115/05/26)
      - ROC concat 7-digit: (1150524) → (1150526)
      - 全形 paren: （115/05/24）→（115/05/26）
    """
    stripped = sample_text.strip()
    has_paren_full = stripped.startswith("（")
    has_paren_half = stripped.startswith("(")
    inner = stripped.strip("()（）").strip()
    sep = "-" if "-" in inner else "/"

    if "-" not in inner and "/" not in inner:
        # ROC concat 7-digit YYYMMDD
        roc_y = today.year - 1911
        body = f"{roc_y:03d}{today.month:02d}{today.day:02d}"
    else:
        year_part = inner.split(sep)[0]
        if len(year_part) == 3:
            # ROC slashed (3-digit year)
            roc_y = today.year - 1911
            body = f"{roc_y:03d}{sep}{today.month:02d}{sep}{today.day:02d}"
        else:
            # AD slashed (4-digit year)
            body = f"{today.year}{sep}{today.month:02d}{sep}{today.day:02d}"

    if has_paren_full:
        return f"（{body}）"
    if has_paren_half:
        return f"({body})"
    return body


def format_uvb_line(original: UvbLineInfo, *, new_dose: int,
                    new_count: Optional[int], today: date) -> str:
    """產生新的 UVB 行內容，shape 維持跟 original 一樣。

    替換 dose / count / date 三個值，其餘 (mj/cm2 / on / increase X / MAX:Y /
    W2 / W5M 等後綴) 全部保留。

    [v20.7] new_count=None → 不替換 count (處置原本就沒寫 (N))。
    [v20.15] 支援 Phototherapy keyword + ROC 民國日期格式 (slashed/concat)
    + date 可能在 UVB 之前 (full_match 已擴到 line_start)。
    """
    # 從 original.full_match 抓出原本的 3 個欄位字串位置 → 用 str.replace 替換
    # 不用 regex 替換是因為要保持其他空白格式
    src = original.full_match

    # 1. 替換日期(最先換)— [v20.15] 用 original.date_text 對應同 format 寫回
    #    (支援 AD slashed / ROC slashed / ROC concat)。
    # [UC-08 2026-07-12] 優先用 parse 時記下的 date_span 精確替換:
    #  - 行內同值日期出現兩次(如 "(10) 2026/7/8 done, next on (2026/7/8)")時,
    #    str.find 會換到第一個而非 parse 選中的那個 → 換錯欄位、殘留的舊日期再被
    #    Step C 湊成幽靈 triplet(count+2、整行日期被改)。
    #  - span 是相對原始 full_match 的 offset,dose/count 替換會改變字串長度 →
    #    日期必須【最先】換(dose/count 用 regex 對齊 pattern,不受先後影響;新日期
    #    字串必含分隔符或為 7 位數,不可能匹配 1-999 的 count pattern)。
    #  - span 內容與 date_text 不符(防衛:舊 caller 手工構造 UvbLineInfo)→ 退回
    #    原 find 行為。
    _date_done = False
    if original.date_text:
        new_date_text = _today_in_format(today, original.date_text)
        ds = original.date_span
        if (ds and 0 <= ds[0] < ds[1] <= len(src)
                and src[ds[0]:ds[1]] == original.date_text):
            src = src[:ds[0]] + new_date_text + src[ds[1]:]
        else:
            idx = src.find(original.date_text)
            if idx >= 0:
                src = (src[:idx] + new_date_text
                       + src[idx + len(original.date_text):])
            else:
                src = src.replace(original.date_text, new_date_text, 1)
        _date_done = True

    # 2. 替換 dose：找原 dose 數字第一次出現 (在 UVB 之後)
    #    使用 regex 因為要對齊「UVB 520」這個 pattern，不能誤改 "(11)" 的 11
    #    [v20.2] 允許「UVB:」冒號 — 跟 parse regex 一致
    #    [v20.18] 接受 "UV" 簡寫 — 跟 _UVB_DOSE_RE 一致
    src = _replace_uvb_dose(src, original.dose, new_dose)

    # 3. 替換 count: (N) → (N+1) — 僅當原本有 count 且傳入 new_count
    # [UC-05/codex P2] \u53EA\u66FF\u63DB\u3010\u95DC\u9375\u5B57\u4E4B\u5F8C\u3011\u7684 (N):\u771F\u6B21\u6578\u5728\u5291\u91CF\u4E4B\u5F8C\u3001\u884C\u9996\u6E05\u55AE\u7DE8\u865F\u300C(1)\u300D\u5728\u95DC\u9375\u5B57
    #  \u4E4B\u524D\u3002count \u503C\u525B\u597D\u7B49\u65BC\u884C\u9996\u7DE8\u865F\u6642(\u5982 '(1) UVB 500 (1)'),\u4E0D\u9650\u4F4D\u7F6E\u6703\u628A\u884C\u9996 (1) \u8AA4\u6539\u6210 (2)\u3001
    #  \u771F\u6B21\u6578\u4E0D\u52D5\u3002\u7528\u95DC\u9375\u5B57\u4F4D\u7F6E\u5207\u958B,\u53EA\u5728 tail \u66FF\u63DB\u7B2C\u4E00\u500B\u3002
    if original.count is not None and new_count is not None:
        _kw = original.keyword_text or "UVB"
        _kpos = src.lower().find(_kw.lower())
        _from = _kpos if _kpos >= 0 else 0
        src = src[:_from] + re.sub(
            r"([\(\uFF08]\s*)" + str(original.count) + r"(\s*[\)\uFF09])",
            lambda mo: f"{mo.group(1)}{new_count}{mo.group(2)}",
            src[_from:],
            count=1,
        )

    # 日期已於步驟 1 換完 → 完成
    if _date_done:
        return src

    # Fallback (老舊 caller 沒填 date_text) — 用零填充 AD format
    old_y = original.last_date.year
    old_m = original.last_date.month
    old_d = original.last_date.day
    with_paren_re = (
        rf"([\(（]\s*){old_y}([/-])0?{old_m}([/-])0?{old_d}"
        rf"(\s*[\)）])"
    )
    if re.search(with_paren_re, src):
        src = re.sub(
            with_paren_re,
            lambda mo: (f"{mo.group(1)}"
                        f"{_date_text(today, mo.group(2))}"
                        f"{mo.group(4)}"),
            src,
            count=1,
        )
    else:
        bare_re = rf"\b{old_y}([/-])0?{old_m}([/-])0?{old_d}\b"
        src = re.sub(
            bare_re,
            lambda mo: _date_text(today, mo.group(1)),
            src,
            count=1,
        )

    return src


# ─── 主入口 ──────────────────────────────────────────────────────────────
@dataclass
class UvbUpdateResult:
    """處理結果，給 caller 決定後續流程。"""
    action: str                          # UvbAction.*
    new_text: Optional[str] = None       # action=UPDATED 時的整段處置新 text
    new_dose: Optional[int] = None       # 新劑量
    new_count: Optional[int] = None      # 新次數
    last_date: Optional[date] = None     # 原日期 (給警告 dialog 顯示)
    days_diff: Optional[int] = None      # 天數差
    parsed: Optional[UvbLineInfo] = None # 原 parse 結果 (debug 用)
    sanity_reason: Optional[str] = None  # action=SANITY_FAIL 時的失敗原因 (給警告顯示)
    confirm_reason: Optional[str] = None # [v20.12] action=CONFIRM_NEEDED 時的提問內容
    uvb_line_count: int = 0              # 處置內有幾行 UVB (≥2 給 info log)
    additional_lines_updated: int = 0    # [v20.11] 同日期額外更新的 UVB 行數
    additional_triplets_updated: int = 0 # [v20.12] 同日期非 UVB 關鍵字 triplet 額外更新數
    # [v20.13] 偵測到的「不確定 / 需要醫師確認」其他 triplet (e.g. line 1 是
    # excimer 但日期跟第一行 UVB 不同)。caller 應該跳 Yes/No 詢問是否套用。
    uncertain_other_triplets: Optional[list] = None


# [codex P1] 續行延續的【否決】關鍵字:明確提到「過去/之前/上次/病史/N 天前」的行,即使結構
# 完整(dose+add+MAX)也【絕不】當成當次醫令延續去改 —— 避免誤改病史/舊療程紀錄(醫療文書安全)。
# 注意只比對 'last time/visit/...' 不比對 'each time'(避免誤傷 'add N each time' 的正常醫令)。
_EXCIMER_HISTORY_HINT_RE = re.compile(
    r"\b(?:previous(?:ly)?|prior|history|hx"
    r"|last\s+(?:time|visit|dose|session|course)"
    r"|\d+\s*(?:days?|weeks?|months?|years?)\s+ago|ago)\b"
    r"|前次|上次|上回|之前|過去|先前|曾經|舊(?:的)?療程|病史",
    re.IGNORECASE,
)


def _looks_like_excimer_continuation(line: str) -> bool:
    """[2026-07-09 多段修正] 判斷「沒有 excimer 關鍵字、卻是前一個 excimer 醫令延續」的續行。

    實機(圖一/二):醫師把同一次 excimer 處置的不同部位分行寫,續行只寫劑量不重複 excimer 字眼
    (例 '左右頭皮各一發: 600mj/cm2, add 30 each time, MAX: 1000')→ 舊版 `search` 找不到 marker
    整行跳過、漏改。保守把關:必須【同時】具備(1)劑量數字(>=MIN_DOSE、非括號內次數、非上限/
    加量關鍵字後的數字)、(2)加量關鍵字(add/increase/每次加)、(3)上限關鍵字(MAX/fixed/上限)——
    三者齊全才算一段合法照光劑量醫令。衛教/病史/一般醫囑不會三者齊全 → 不會被誤當劑量更改。
    另外[codex P1]即使三者齊全,只要行內出現「過去/之前/上次/病史」等字樣(病史/舊療程),也一律
    否決,絕不改動歷史醫療文字(醫療安全紅線:不確定就不動)。此判斷只在 flexible_dose(純自費
    excimer)路徑、且緊接 excimer 段時才生效。"""
    if _EXCIMER_HISTORY_HINT_RE.search(line):   # 病史/過去療程行 → 絕不當延續改動
        return False
    if _find_excimer_dose(line, 0) is None:
        return False
    if _UVB_MAX_RE.search(line) is None:
        return False
    if _UVB_INCREASE_RE.search(line) is None:
        return False
    return True


def _compute_excimer_segment_edit(
        line: str, seg_start: int, dose_bound: int, tail_start: int, today: date,
        allow_undated: bool, skip_stale: bool):
    """對 line 的一個 excimer 劑量段算更新(flexible_dose 路徑用)。

    劑量/次數限制在 [seg_start, dose_bound) 內(同行多 marker 時不可抓到下一段的劑量/次數)。
    日期/上限/加量:【先在本段 [seg_start, dose_bound) 內找(段自己的)】,段內沒有才退回句尾共用
    區 [tail_start, EOL)(像 '..., excimer 440 for X, on (日期) add 10 fixed 700' 的日期/上限/加量
    是兩段共用寫在句尾) —— 這樣既能吃共用句尾,又不會讓前一段誤偷後一段【自己】的日期/上限
    (codex P1:否則無日期的第一段會借到第二段的日期、套錯 staleness/覆寫日期)。
    回 (edits, first_update, too_close, recognized):edits 為 [((start,end), 取代字串)],沒更新
    則為 [];recognized=本段確實是「有劑量＋上限」的照光劑量醫令(即使因太近/太舊/無效值而沒改),
    供上層判定要不要啟用續行延續 —— 純提及 excimer 而無劑量(如 'discuss excimer')recognized=False。
    邏輯與原本單段完全一致(只是把範圍參數化),含所有既有防呆與 compute_new_dose。"""
    def _seg_meta(rx):
        """段內優先;段內沒有才退回句尾共用區(僅當句尾確實在本段之後,避免自借)。

        [設計決策 — 句尾共用是【刻意】保留的]實機 figure 3:
          'excimer light 580mJ (151) for 嘴周圍, excimer light 440mJ (147) for 右下耳,
           on (日期) add 10mJ each time fixed at 700mJ'
        ——整句只寫一個日期/加量/上限,是【兩段共用】(同一次照光的不同部位),使用者明確要求
        兩段都要遞增。第一段(580)本來就靠這個句尾共用才會更新;若禁止前段借用句尾(如某次
        Codex 建議),figure 3 的 580 反而會停止更新=退步。此借用不會繞過任何安全防線:借到
        的日期一樣要過 too_close/stale(>1月轉確認窗)/>2年硬停/次數範圍/MIN_DOSE(見下方防呆),
        '不確定就不動' 仍成立。故【保留句尾共用】,不採「禁止借用最後一段 metadata」的建議。"""
        m = rx.search(line, seg_start, dose_bound)
        if m is None and tail_start > dose_bound:
            m = rx.search(line, tail_start)
        return m

    def _seg_maintain():
        """[codex P1] maintain-dose(維持不加量)也要限本段(＋句尾共用區),否則某一段寫了
        maintain 會把【其他段】的 add 也壓掉(該段本應遞增卻被誤留原劑量)。"""
        if _has_maintain_dose(line[seg_start:dose_bound]):
            return True
        return tail_start > dose_bound and _has_maintain_dose(line[tail_start:])

    date_m = _seg_meta(_UVB_DATE_RE)
    max_m = _seg_meta(_UVB_MAX_RE)
    inc_m = _seg_meta(_UVB_INCREASE_RE)
    dose_info = _find_excimer_dose(
        line, seg_start, date_span=(date_m.span() if date_m else None))
    # 劑量必須落在本段範圍內 —— 否則本段其實沒劑量(下一個 marker 段才有)→ 不處理。
    if dose_info is not None and dose_info[0] >= dose_bound:
        dose_info = None
    # recognized:本段有劑量＋上限結構 → 是一段真的照光劑量醫令(啟用續行延續的依據)。
    recognized = dose_info is not None and max_m is not None
    if max_m is None or dose_info is None:
        return [], None, None, recognized
    dose_start, dose_end, dose = dose_info

    # 沒日期(初診/醫師沒寫日期)→ first-time:只加劑量(cap MAX),不補日期/次數(2026-06-25 user)。
    if date_m is None:
        if not allow_undated or inc_m is None or dose_start > max_m.start():
            return [], None, None, recognized
        try:
            max_dose = int(max_m.group(1))
            increase = int(inc_m.group(1) or inc_m.group(2))
        except (TypeError, ValueError):
            return [], None, None, recognized
        if (dose < MIN_DOSE or max_dose < MIN_DOSE
                or not (0 < increase <= 200) or dose >= max_dose):
            return [], None, None, recognized
        new_dose = dose if _seg_maintain() else min(dose + increase, max_dose)
        return ([((dose_start, dose_end), str(new_dose))],
                {"dose": new_dose, "count": None, "last_date": None, "days_diff": None},
                None, recognized)

    if dose_start > date_m.start():
        return [], None, None, recognized
    ymd = _resolve_date_match(date_m)
    if ymd is None:
        return [], None, None, recognized
    try:
        last_date = date(*ymd)
        max_dose = int(max_m.group(1))
        increase = int(inc_m.group(1) or inc_m.group(2)) if inc_m else 0
    except (TypeError, ValueError):
        return [], None, None, recognized

    masked = (line[:date_m.start()]
              + " " * (date_m.end() - date_m.start())
              + line[date_m.end():])
    # 次數(N)限制在本段起點~min(下一段起點, 日期起點) —— 不可抓到下一段的次數或日期後的數字。
    count_upper = min(dose_bound, date_m.start())
    count_m = _UVB_COUNT_RE.search(masked, seg_start, count_upper)
    count = int(count_m.group(1)) if count_m else None
    days_diff = (today - last_date).days

    # 先擋真正無效/太久遠/舊段(靜默略過,跟原本一樣)
    if (dose < MIN_DOSE or max_dose < MIN_DOSE
            or increase < 0 or increase > 200
            or (count is not None and not (1 <= count <= MAX_COUNT))
            # [2026-06-24] >2 年(MAX_GAP_DAYS)一律不動 —— 即使 skip_stale(Yes),
            # 太久遠的紀錄可能跑錯病人,與 UVB 路徑的硬停一致,不可照舊更新。
            or days_diff > MAX_GAP_DAYS
            # 1 個月門檻才受 skip_stale 控制:正常路徑略過舊段;Yes 後照舊紀錄更新。
            or (not skip_stale
                and last_date < _months_before(today, MODIFY_STALE_MONTHS))):
        return [], None, None, recognized
    # [2026-06-25 user] 日期距今 < 2 天(當天又按一次)→ 不加劑量,但記下 days_diff 讓上層跳
    # 「距上次太近」、不設身份。負值(未來日期)維持原本靜默略過(too_close 回 None)。
    if days_diff < TOO_CLOSE_DAYS:
        return [], None, (days_diff if 0 <= days_diff else None), recognized

    new_dose = compute_new_dose(
        dose=dose, increase=increase, max_dose=max_dose, days_diff=days_diff)
    if new_dose is None:
        return [], None, None, recognized
    if _seg_maintain():
        new_dose = dose
    new_count = count + 1 if count is not None else None

    edits = [
        ((dose_start, dose_end), str(new_dose)),
        (date_m.span(), _today_in_format(today, date_m.group(0))),
    ]
    if count_m is not None and new_count is not None:
        edits.append((count_m.span(1), str(new_count)))
    return (edits,
            {"dose": new_dose, "count": new_count,
             "last_date": last_date, "days_diff": days_diff},
            None, recognized)


def _update_excimer_lines(text: str, today: date,
                          allow_undated: bool = False,
                          skip_stale: bool = False,
                          flexible_dose: bool = False
                          ) -> tuple[str, int, Optional[dict], Optional[int]]:
    """Update structured excimer lines independently from UVB lines.

    回傳 (新文字, 更新段數, 第一筆更新摘要, too_close_days)。too_close_days 非 None 代表
    有「有效但日期距今 < 2 天」的 excimer 段 —— 上層應跳「距上次太近」提示、不加劑量、不設身份。

    flexible_dose=True(只給【純自費 Excimer】F2/F3 路徑用):劑量用「第一個不在括號內且
    >= MIN_DOSE 的數字」抓(同一般 UVB,容忍 '700 m j/cm2' 空格、'810' 無單位),【並支援多段】:
    同一行多個 excimer marker、以及沒重複 excimer 字眼但結構完整的續行,每一段各自更新。
    UVB 路徑的 Step D 用 False(維持嚴格 mj 比對＋單段)—— 不改健保 UVB visit 順手動 excimer 的
    既有保守行為。
    allow_undated=True(只給【純自費 Excimer】路徑用):連沒有日期的 excimer 劑量段也加劑量。
    [2026-06-25 user] 沒日期就【只改劑量、不補日期/次數】(維持原本有什麼改什麼,同一般 UVB
    的 first-time)。UVB 路徑的 Step D 用 False —— UVB visit 不應順手改沒日期的 excimer 行。
    [2026-06-24] skip_stale=True(使用者在確認窗按 Yes 後)→ 不因『日期早於 1 個月』而略過該段,
    照舊紀錄繼續更新;預設 False 時,日期早於今天往前 1 個日曆月的 excimer 段一律略過(忽略舊段)。
    """
    lines = text.splitlines(keepends=True)
    updated = 0
    first_update: Optional[dict] = None
    too_close_days: Optional[int] = None

    if flexible_dose:
        # 【純自費 Excimer F2/F3 多段】[2026-07-09 楊智翔實機]原本每個處置欄只改「第一個 excimer
        # 關鍵字後的第一個劑量」→ 同一行第二段(圖三 440)、沒重複關鍵字的續行(圖一 middle neck、
        # 圖二 頭皮)漏改。改為:逐行取【所有】excimer marker 各自成段(劑量限本段、日期/上限/加量
        # 可共用句尾);無 marker 但結構完整(dose+increase+MAX)且緊接 excimer 段的續行也視為延續。
        last_seg_was_excimer = False
        for index, line in enumerate(lines):
            markers = list(_EXCIMER_MARKER_RE.finditer(line))
            if markers:
                segments = [
                    (m.end(),
                     markers[mi + 1].start() if mi + 1 < len(markers) else len(line))
                    for mi, m in enumerate(markers)
                ]
                # 句尾共用區起點 = 最後一個 marker 之後(前面各段若自己沒日期/上限/加量才退回這裡借)。
                tail_start = markers[-1].end()
            elif last_seg_was_excimer and _looks_like_excimer_continuation(line):
                segments = [(0, len(line))]
                tail_start = 0   # 續行單段:整行即本段,不需要句尾借用
            else:
                last_seg_was_excimer = False  # 非 excimer 行 → 中斷延續,不跨越誤改
                continue

            line_edits: dict = {}   # span → 取代字串;同 span(共用日期)自動去重
            line_recognized = False
            for seg_start, dose_bound in segments:
                edits, seg_first, seg_too_close, recognized = \
                    _compute_excimer_segment_edit(
                        line, seg_start, dose_bound, tail_start,
                        today, allow_undated, skip_stale)
                if recognized:
                    line_recognized = True
                if seg_too_close is not None and too_close_days is None:
                    too_close_days = seg_too_close
                if not edits:
                    continue
                for span, replacement in edits:
                    line_edits[span] = replacement
                updated += 1
                if first_update is None and seg_first is not None:
                    first_update = seg_first
            # [codex P1] 只有本行確實是「有劑量＋上限」的照光劑量醫令才啟用/維持續行延續 ——
            # 純提及 excimer(如 'discuss excimer treatment',無劑量)不可讓下一個不相關的
            # dose+increase+MAX 行被當成延續而誤改。
            last_seg_was_excimer = line_recognized
            if line_edits:
                new_line = line
                for (start, end), replacement in sorted(line_edits.items(), reverse=True):
                    new_line = new_line[:start] + replacement + new_line[end:]
                lines[index] = new_line

        return "".join(lines), updated, first_update, too_close_days

    # 【健保 UVB visit Step D】flexible_dose=False —— 維持既有保守單段行為:嚴格 _EXCIMER_DOSE_RE
    # (要求 mj 單位)、每行只認第一個 marker 後的第一個劑量,不擴充多段(不動 UVB visit 順手動
    # excimer 的既有行為)。此路徑目前只被 UVB Step D 以預設參數呼叫。
    for index, line in enumerate(lines):
        marker = _EXCIMER_MARKER_RE.search(line)
        if marker is None:
            continue
        date_m = _UVB_DATE_RE.search(line, marker.end())
        max_m = _UVB_MAX_RE.search(line, marker.end())
        _dm = _EXCIMER_DOSE_RE.search(line, marker.end())
        dose_info = ((_dm.start(1), _dm.end(1), int(_dm.group(1)))
                     if _dm else None)
        inc_m = _UVB_INCREASE_RE.search(line, marker.end())
        if max_m is None or dose_info is None:
            continue
        dose_start, dose_end, dose = dose_info

        # 沒日期(像初診/醫師沒寫日期,例:"excimer 510 mj/cm2 increase 30 ... max 800")→
        # 同一般 UVB 的 first-time:只加劑量(cap MAX),【不補日期、不補次數】(2026-06-25 user)。
        if date_m is None:
            if (not allow_undated or inc_m is None
                    or dose_start > max_m.start()):
                continue
            try:
                max_dose = int(max_m.group(1))
                increase = int(inc_m.group(1) or inc_m.group(2))
            except (TypeError, ValueError):
                continue
            if (dose < MIN_DOSE or max_dose < MIN_DOSE
                    or not (0 < increase <= 200) or dose >= max_dose):
                continue
            new_dose = dose if _has_maintain_dose(line) else min(dose + increase, max_dose)
            lines[index] = line[:dose_start] + str(new_dose) + line[dose_end:]
            updated += 1
            if first_update is None:
                first_update = {"dose": new_dose, "count": None,
                                "last_date": None, "days_diff": None}
            continue

        if dose_start > date_m.start():
            continue

        ymd = _resolve_date_match(date_m)
        if ymd is None:
            continue
        try:
            last_date = date(*ymd)
            max_dose = int(max_m.group(1))
            increase = int(inc_m.group(1) or inc_m.group(2)) if inc_m else 0
        except (TypeError, ValueError):
            continue

        masked = (line[:date_m.start()]
                  + " " * (date_m.end() - date_m.start())
                  + line[date_m.end():])
        count_m = _UVB_COUNT_RE.search(masked, marker.end(), date_m.start())
        count = int(count_m.group(1)) if count_m else None
        days_diff = (today - last_date).days

        # 先擋真正無效/太久遠/舊段(這些靜默略過,跟原本一樣)
        if (dose < MIN_DOSE or max_dose < MIN_DOSE
                or increase < 0 or increase > 200
                or (count is not None and not (1 <= count <= MAX_COUNT))
                # [2026-06-24] >2 年(MAX_GAP_DAYS)一律不動 —— 即使 skip_stale(Yes),
                # 太久遠的紀錄可能跑錯病人,與 UVB 路徑的硬停一致,不可照舊更新。
                or days_diff > MAX_GAP_DAYS
                # 1 個月門檻才受 skip_stale 控制:正常路徑略過舊段;Yes 後照舊紀錄更新。
                or (not skip_stale
                    and last_date < _months_before(today, MODIFY_STALE_MONTHS))):
            continue
        # [2026-06-25 user] 日期距今 < 2 天(像當天又按一次)→ 不加劑量,但記下 days_diff 讓上層
        # 跳「距上次太近」提示、不設身份(同一般 UVB 的 TOO_CLOSE)。負值(未來日期)維持原本靜默略過。
        if days_diff < TOO_CLOSE_DAYS:
            if 0 <= days_diff and too_close_days is None:
                too_close_days = days_diff
            continue

        new_dose = compute_new_dose(
            dose=dose, increase=increase, max_dose=max_dose,
            days_diff=days_diff)
        if new_dose is None:
            continue
        if _has_maintain_dose(line):
            new_dose = dose
        new_count = count + 1 if count is not None else None

        edits = [
            ((dose_start, dose_end), str(new_dose)),
            (date_m.span(), _today_in_format(today, date_m.group(0))),
        ]
        if count_m is not None and new_count is not None:
            edits.append((count_m.span(1), str(new_count)))
        new_line = line
        for (start, end), replacement in sorted(edits, reverse=True):
            new_line = new_line[:start] + replacement + new_line[end:]

        lines[index] = new_line
        updated += 1
        if first_update is None:
            first_update = {
                "dose": new_dose,
                "count": new_count,
                "last_date": last_date,
                "days_diff": days_diff,
            }

    return "".join(lines), updated, first_update, too_close_days


def _count_uvb_lines(text: str) -> int:
    """數處置 text 內有幾行 UVB (粗略 — 每行算一次)。"""
    return sum(1 for ln in text.splitlines() if "uvb" in ln.lower())


def _detect_uncertain_triplets(text: str, today: date,
                                max_days_ago: int = 365,
                                driver_max_dose: int = 0) -> list:
    """[v20.13] 偵測 text 中「看起來像 UVB/excimer 但日期不同於今天」的 triplet。

    使用情境：update_uvb_in_text 第一行 UVB 已更新，同日期 triplet 也更新後，
    剩下的 (count) ... (date) 若有 UVB-marker 又日期合理 (近 1 年內)，
    視為「不確定該不該更新」，caller 應該跳 Yes/No 詢問醫師。

    [UC-07 2026-07-12] driver_max_dose(選填)=驅動行 MAX。capped(緊鄰該 triplet
    前方的劑量 >= 適用 MAX)且已進 decay 區間(>SAME_DOSE_DAYS)的段一律排除 ——
    不問醫師、留原樣(Yes 套用是 kept-dose bump,會把未衰退劑量標成今天),
    與 Step C 的不-bump 守衛成對,見迴圈內註解。

    Returns list of dicts:
        [{'line': str, 'count': int, 'date': date, 'days_ago': int,
          'span': (start, end), 'original_seg': str, 'replacement': str}, ...]
        replacement 內含 count+1, date→today (供 caller Yes 時 apply)。
    """
    out = []
    for m in _TRIPLET_RE.finditer(text):
        try:
            seg_date = date(int(m.group(3)), int(m.group(4)),
                            int(m.group(5)))
        except (ValueError, TypeError):
            continue
        # 今天的 date (已經被 step A/B/C 更新) 跳過
        if seg_date == today:
            continue
        # 未來日期跳過 (異常)
        if seg_date > today:
            continue
        # 太舊跳過 (> 1 年 — 歷史紀錄)
        days_ago = (today - seg_date).days
        if days_ago > max_days_ago:
            continue
        # [2026-06-24] 早於 1 個日曆月的舊段 → 視為「已暫停、忽略不修改」,不再當成
        # 「不確定、要問醫師」(否則被驅動行重選略過的舊行又會跳 Yes/No,違背忽略舊行的意圖)。
        if seg_date < _months_before(today, MODIFY_STALE_MONTHS):
            continue
        # marker 必須同行 (不跨 newline) — 避免誤抓不相關內容
        line_start = text.rfind("\n", 0, m.start()) + 1
        line_end = text.find("\n", m.end())
        if line_end == -1:
            line_end = len(text)
        line_text = text[line_start:line_end]
        if not _UVB_MARKER_RE.search(line_text):
            continue
        try:
            old_count = int(m.group(1))
        except ValueError:
            continue
        if not (1 <= old_count <= MAX_COUNT):
            continue
        # [UC-08 2026-07-12] 與 Step C 同款守衛:(count) 與配對 (日期) 之間夾另一個
        # 裸日期 → 配對歸屬不明,不納入 uncertain(否則 Step C 跳過的幽靈 triplet 會
        # 變成 Yes/No 問醫師,按 Yes 又 kept-dose 再 bump 一次)。
        if _DATE_LIKE_IN_MIDDLE_RE.search(m.group(2)):
            continue
        # [UC-07 2026-07-12] capped(緊鄰劑量 >= 適用 MAX)且已進 decay 區間(>7 天)的段:
        # 「Yes 套用」只 bump count/date、【不重算劑量】(kept-dose)→ 會把未衰退劑量標成
        # 今天(獨立算應 ×0.75/×0.5)。Step C 已同步不 bump 這類段;此處一併排除、留原樣
        # 交醫師手動,免得 Step C 跳過後又經 uncertain-Yes 寫回(codex 指出的交互)。
        # (codex P1:不可 parse 整行拿 primary dose 判 capped —— 同一行 primary 500<800
        #  + 續行 800 capped 會誤放行;須逐 triplet 取「緊鄰該 triplet 前方的劑量」,
        #  比照 Step C continuation_m/guard_dose_m 的作法、用同款寬單位 regex。)
        # 適用 MAX=該行自己的 MAX;行內無 MAX(parse 失敗)時退驅動行 MAX —— Step C 正是
        # 拿驅動行 MAX 判 capped 而跳過的,不退則同款段仍會從這裡漏回寫。其餘 uncertain
        # 行為(dose<其行 MAX 的第二療程行、無緊鄰劑量的 excimer 行)不變。
        if days_ago > SAME_DOSE_DAYS:
            dose_prefix = text[max(line_start, m.start() - 32):m.start()]
            trip_dose_m = re.search(
                r"(\d+)\s*mj(?:/cm2?)?\s*$", dose_prefix, re.IGNORECASE)
            if trip_dose_m is not None:
                _lp = parse_uvb_line(line_text) or parse_uvb_partial(line_text)
                _applicable_max = ((_lp.max_dose if _lp is not None else 0)
                                   or driver_max_dose)
                if _applicable_max and int(trip_dose_m.group(1)) >= _applicable_max:
                    continue
        # [2026-06-19] 只有當這個 (count) 緊鄰前方【就是】一個日期(中間只隔標點/空白,
        # 典型主行更新後格式 "on(date), (count)")才跳過 —— 代表 count 已有自己的日期
        # partner,不該再跟後方 120 字內別欄位的日期(例如 "acitretin w7-9 on (date)")
        # 湊成「不確定 triplet」誤跳 Yes/No(林章熙實機 case)。
        # 若日期與 count 之間夾了別的字(如 "(date), excimer (count)"),仍是合法的第二
        # 療程 triplet,要保留(Codex 審查:不可一律用 24 字內有日期就跳)。
        pre = text[max(line_start, m.start() - 24):m.start()]
        _pre_dates = list(_UVB_DATE_RE.finditer(pre))
        if _pre_dates and re.fullmatch(
                r"[\s,，、.。:：)）]*", pre[_pre_dates[-1].end():]):
            continue
        # 構造 "Yes 時" 套用的新 segment: count+1, date→today
        rep = m.group(0)
        rep = re.sub(
            r"([\(\uFF08]\s*)" + str(old_count) + r"(\s*[\)\uFF09])",
            lambda mo: f"{mo.group(1)}{old_count + 1}{mo.group(2)}",
            rep,
            count=1,
        )
        rep = re.sub(
            rf"([\(\uFF08]\s*){seg_date.year}([/-])0?{seg_date.month}([/-])0?{seg_date.day}(\s*[\)\uFF09])",
            lambda mo: f"{mo.group(1)}{_date_text(today, mo.group(2))}{mo.group(4)}",
            rep,
            count=1,
        )
        out.append({
            'line': line_text.strip(),
            'count': old_count,
            'date': seg_date,
            'days_ago': days_ago,
            'span': m.span(),
            'original_seg': m.group(0),
            'replacement': rep,
        })
    return out


def apply_uncertain_updates(text: str, triplets: list) -> str:
    """[v20.13] 將 _detect_uncertain_triplets 偵測到的 triplet 套用 (count+1,
    date→today)，回新 text。

    end-to-start 套用避免 offset 失效。
    """
    if not triplets:
        return text
    out = text
    # span 從後往前套
    for t in sorted(triplets, key=lambda x: x['span'][0], reverse=True):
        s, e = t['span']
        out = out[:s] + t['replacement'] + out[e:]
    return out


def _first_time_update(parsed: UvbLineInfo, today: date,
                        uvb_lines: int) -> UvbUpdateResult:
    """[v20.16] 處置 UVB 行沒日期 → 當作第一次照光記錄:
      - [v20.17] dose 套用 +increase 公式 (treat as 2-6 days, min cap MAX)
      - 只更新原句已經存在的欄位
      - 原本沒有 count 或 date 時，不自行補寫

    v20.17 起此 path 是 silent — 不再需要 Yes/No 確認。
    """
    # [UC-06] first-time 原本跳過所有 sanity → 一次加 500(add 500)、或 (20260708) 被當次數 +1 都
    #  照寫。補上與 dated 路徑相同的結構把關:count 需 1..MAX_COUNT、increase 需 0..200、dose 需
    #  >= MIN_DOSE;超出 → 不猜、回 PARSE_FAIL 交醫師。(刻意【不】套用全域 1500 上限確認 —— 醫師
    #  親手寫的 dose+max 仍視為可信,維持既有 silent first-time 設計。)
    if parsed.count is not None and not (1 <= parsed.count <= MAX_COUNT):
        return UvbUpdateResult(action=UvbAction.PARSE_FAIL, uvb_line_count=uvb_lines)
    if parsed.increase is not None and not (0 <= parsed.increase <= 200):
        return UvbUpdateResult(action=UvbAction.PARSE_FAIL, uvb_line_count=uvb_lines)
    if parsed.dose < MIN_DOSE:
        return UvbUpdateResult(action=UvbAction.PARSE_FAIL, uvb_line_count=uvb_lines)
    # 若有 increase → 套用 +increase 公式 (尊重原 MAX); 否則保持原 dose
    if parsed.increase is not None and parsed.max_dose:
        new_dose = min(parsed.dose + parsed.increase, parsed.max_dose)
    else:
        new_dose = parsed.dose
    new_count = parsed.count + 1 if parsed.count is not None else None
    src = parsed.full_match

    # 1. 替換 dose: UVB:OLD → UVB:NEW
    # [v20.18] 接受 "UV" 簡寫 keyword
    src = _replace_uvb_dose(src, parsed.dose, new_dose)

    # 2. 原句有 count 才更新 count；沒有 date 就保持沒有 date。
    # [UC-05/codex P2] 同 format_uvb_line:只替換關鍵字之後的 (N),避免行首清單編號被誤改。
    if parsed.count is not None and new_count is not None:
        _kw = parsed.keyword_text or "UVB"
        _kpos = src.lower().find(_kw.lower())
        _from = _kpos if _kpos >= 0 else 0
        src = src[:_from] + re.sub(
            r"([\(（]\s*)" + str(parsed.count) + r"(\s*[\)）])",
            lambda mo: f"{mo.group(1)}{new_count}{mo.group(2)}",
            src[_from:], count=1,
        )

    return UvbUpdateResult(
        action=UvbAction.UPDATED,
        new_text=src,  # caller 會把 segment 接回完整 text
        new_dose=new_dose,
        new_count=new_count,
        last_date=None,
        days_diff=None,
        parsed=parsed,
        uvb_line_count=uvb_lines,
    )


# [2026-06-24] 「改劑量」的 staleness 門檻:照光行日期早於『今天往前 1 個日曆月』視為舊。
# 與身份別分流的 STALE_PHOTO_MONTHS(=2,billing 用)是不同概念、各自獨立。
MODIFY_STALE_MONTHS = 1


def _first_fresh_uvb(text: str, cutoff: date) -> Optional[UvbLineInfo]:
    """回 text 中第一個『日期 >= cutoff(近期)』的 UVB 行(span 轉成全文座標);沒有則 None。

    用於多行:若排在最上面的 UVB 行是舊的(早於 1 個月),改用下面第一條近期 UVB 行來
    驅動更新——舊行因排在驅動行之前、不會被 Step A/B 動到(等於忽略不修改)。

    [2026-06-24] 逐【行】嘗試 parse(而非用 parse_uvb_line 對整段 search 再前進)——
    舊行與近期行之間若夾了壞格式 / 有 dose 卻無 MAX/日期 的 UVB 殘句,parse_uvb_line 會回
    None;逐行掃才不會在那裡提早停、漏掉下面真正的近期行(codex 指出)。"""
    pos = 0
    for line in text.splitlines(keepends=True):
        p = parse_uvb_line(line)
        if (p is not None and p.last_date is not None
                and p.last_date >= cutoff):
            p.span = (p.span[0] + pos, p.span[1] + pos)   # 該行 span → 全文座標
            return p
        pos += len(line)
    return None


def _stale_confirm_result(text: str, today: date) -> "UvbUpdateResult":
    """所有照光行都早於 1 個月 → 回 CONFIRM_NEEDED(單行舊 / 多行全舊 / 單行舊 excimer)。
    confirm_reason 含「距今 N 天」字樣,讓 caller 判定為 stale 確認窗。"""
    # [2026-06-27] 只取「真的早於 1 個月」的日期當『上次照光日期』:否則若文字裡另有近期日期(像近期
    # excimer / 近期藥物日期),max(全部) 會挑到那個近期日期 → 顯示「距今 2 天 (超過 1 個月)」自相矛盾
    # (林怡君實機)。改只看 stale 的日期 → 顯示的日期與「超過 1 個月」一致。
    cutoff = _months_before(today, MODIFY_STALE_MONTHS)
    dts = [d for d in _real_dates_in(text) if d <= today and d < cutoff]
    last = max(dts) if dts else None
    days = (today - last).days if last is not None else None
    if last is not None:
        reason = (f"上次照光日期 {last.strftime('%Y/%m/%d')} 距今 {days} 天 "
                  f"(超過 1 個月) — 病歷可能是舊紀錄，請確認是否真要按舊紀錄繼續更新")
    else:
        reason = "照光紀錄距今已超過 1 個月 — 請確認是否真要按舊紀錄繼續更新"
    return UvbUpdateResult(
        action=UvbAction.CONFIRM_NEEDED, confirm_reason=reason,
        last_date=last, days_diff=days, uvb_line_count=_count_uvb_lines(text))


def _all_excimer_stale(text: str, today: date) -> bool:
    """純 excimer 是否該跳『按舊紀錄確認』:【每一行】excimer 都『早於 1 個月』(全舊),
    【且】至少有一行在 2 年內(Yes 後 _update_excimer_lines 才真的能更新)。

    [2026-06-24] 兩道條件:
      - 「全部 excimer 行都舊」而非「任一行舊」—— 否則「舊 excimer + 一行太近的 excimer」
        也會跳確認,Yes 後反而更新到舊行(太近的那行才是當前治療)。有近期日期、或有【無日期】
        (可能在做)的 excimer 行 → 非全舊、不跳確認。
      - 「至少一行在 2 年內」—— 否則全部 >2 年時跳了確認、Yes 卻因 MAX_GAP_DAYS 不更新,白跳窗。"""
    has_excimer = False
    has_updatable = False
    cutoff = _months_before(today, MODIFY_STALE_MONTHS)
    for line in re.split(r"[\r\n]+", text):
        if _EXCIMER_MARKER_RE.search(line):
            dts = _real_dates_in(line)
            if not dts:
                return False          # 無日期 excimer 行(可能在做)→ 非全舊
            if not all(d < cutoff for d in dts):
                return False          # 有近期日期 → 非全舊
            has_excimer = True
            if any(0 <= (today - d).days <= MAX_GAP_DAYS for d in dts):
                has_updatable = True  # 1 個月 ~ 2 年 → Yes 後可更新
    return has_excimer and has_updatable


def uvb_written_back_ok(text: str, expected_dose, expected_count,
                        today: Optional[date] = None) -> bool:
    """寫回後驗證(給 caller 用):掃描 text 內【所有】UVB 行,有一條 dose==expected_dose、
    count==expected_count 且【日期==today(剛寫回的那條)】即視為寫回成功。

    [2026-06-24] 三個重點:
      1. 不能只看第一行 —— driver 重選(舊行在上、改下面近期行)後,被更新的驅動行未必是
         第一條 UVB;只看第一行會把已正確寫回的 stale-above-fresh 誤判失敗。
      2. 必須一併比對【日期==today】—— 否則巧合 dose/count 相同的舊行(如未動的 530 (6) on
         3/1)會讓『寫回其實失敗』被誤判成功(codex 指出)。
      3. 逐【行】嘗試 parse —— 中間夾壞格式 / 有 dose 卻無 MAX/日期 的 UVB 殘句時,整段
         search 會在那裡失敗或把殘句 dose 跟後面行的 MAX/日期湊在一起,導致驗不到真正更新
         的那一行而誤判失敗(codex 指出)。
    無日期(first-time)case:沒有日期可比,改用 parse_uvb_partial 且僅在『確實無日期』時以
    dose+count 認可(有日期卻沒寫成 today → 視為失敗,不走此後備)。"""
    if today is None:
        today = date.today()
    for line in text.splitlines(keepends=True):
        p = parse_uvb_line(line)
        if (p is not None and p.dose == expected_dose
                and p.count == expected_count and p.last_date == today):
            return True
    pp = parse_uvb_partial(text)
    return bool(pp is not None and pp.last_date is None
                and pp.dose == expected_dose and pp.count == expected_count)


def update_uvb_in_text(text: str, today: Optional[date] = None,
                       skip_dose_sanity: bool = False,
                       skip_stale_check: bool = False) -> UvbUpdateResult:
    """主入口：給整段「處置」text，回更新後 text + 動作類型。

    today=None 用今天日期；測試時傳 fixed date 方便 reproducible。

    [v20.5 2026-05-26] 加 sanity check —「確保資訊正確，不確定就停下來」：
      - parse 後驗證 dose/count/max/days_diff 都在合理範圍
      - 寫回後 round-trip verify (重新 parse 新 text → 預期值是否一致)
      - 任一不符 → 回 SANITY_FAIL 給 caller 警告

    [v20.12 2026-05-26 → 2026-06-18 調整] dose 上限 1500。MAX(最高劑量)欄位本身
    可超過 1500、不跳確認;只有「本次實際要照的劑量」(decay/maintain 計算後的 new_dose,
    含同日多行/續行) 超過 1500 才回 CONFIRM_NEEDED。原劑量 >1500 但久未照光 decay 後
    ≤1500 則不跳。caller 跳 Yes/No dialog，按 Yes 後以 skip_dose_sanity=True 重 call
    跳過上限檢查繼續執行。
    新增同日期 triplet 偵測 — 處置內非 UVB-關鍵字 (如 excimer light) 但 (count)
    on (date) 日期跟第一行 UVB 相同的，count+1, date→today 一併更新。

    [v20.14 2026-05-26] 病歷可能有舊紀錄 (好幾個月/年前)，user 不希望直接
    用舊紀錄套日期/decay 更新。distance > STALE_DAYS (30) 不在 skip 模式 →
    回 CONFIRM_NEEDED 給 caller 跳 Yes/No: Yes 重 call 帶 skip_stale_check=True
    才繼續更新；No 直接終止不修改處置。

    [v20.16→v20.17 2026-05-26] 處置 UVB 行可能沒日期 (第一次照光紀錄)，strict
    parse_uvb_line 會 fail。strict fail 後試 parse_uvb_partial，若 last_date is None
    (無日期) → 直接走 silent first-time (_first_time_update)：跳過 days_diff/decay，
    只更新原句已有欄位、不跳對話框。
    (註：v20.16 曾用 CONFIRM_NEEDED + treat_as_first_time 參數重 call，v20.17 改為
    silent first-time 後該參數已無作用，[stability r4] 已移除以免誤導未來 caller。)
    """
    if today is None:
        today = date.today()

    uvb_lines = _count_uvb_lines(text)

    parsed = parse_uvb_line(text)
    # [2026-06-27 林怡君] 本次治療其實是近期 excimer(同欄位另有舊 UVB 病歷紀錄)時,detect_phototherapy_kind
    # (會 strip 掉早於 2 個月的舊照光段)會判為 pure_excimer;但 parse_uvb_line 不 strip → 會抓到那條舊 UVB
    # → 落到 UVB 路徑把舊 UVB(2024/11/12)當本次而誤跳「距今超過 1 個月」確認。故 detect 判 pure_excimer
    # 時,即使 parse 到舊 UVB 也把 parsed 歸 None、強制走 excimer 分支(更新近期 excimer、保留舊 UVB 行)。
    # 與主程式身份別分流一致、billing 安全:近期真有 UVB 時 detect 會是 uvb/ambiguous,不會誤判 pure_excimer。
    _is_pure_excimer = detect_phototherapy_kind(text, today) == "pure_excimer"
    if _is_pure_excimer:
        parsed = None
    if parsed is None:
        # excimer / excimer light 本身也是照光，不要求同時出現 UVB。
        # [2026-06-25] 只有「會讓 excimer 退讓的真 UVB/光療治療訊號」才不進 excimer 分支:
        #   - UVB-specific(UVB / 紫外線 / 獨立 UV)—— 不論有無劑量都保護(健保 UVB);
        #   - 泛稱 Phototherapy 且【同行有劑量】—— 像真正的光療醫令行(可能是健保 UVB,保守不當 excimer)。
        # 但【衛教備註裡的裸 phototherapy 字眼(無劑量)】不算 → 照常更新 excimer。
        # 修正:舊版只要文字任一處出現 Phototherapy 字眼(連 'avoid phototherapy days' 這種備註)就誤擋
        # excimer 劑量更新(楊智翔實機:有 excimer 卻沒辦法修改)。紫外線仍保護 → 不會誤入 allow_undated。
        if _is_pure_excimer or not _has_uvb_or_phototherapy_treatment(text):
            (excimer_text, excimer_count, excimer_first,
             excimer_too_close) = _update_excimer_lines(
                text, today, allow_undated=True, skip_stale=skip_stale_check,
                flexible_dose=True)
            # [2026-06-25 user] 只要有「有效但日期距今 < 2 天」的 excimer 行 → 一律回 TOO_CLOSE,
            # 跳「太近」提示、不加劑量、不設身份(同一般 UVB)。此判斷【優先於 UPDATED】:即使另一行
            # 可更新,只要存在當天行就保守不自動動作、交醫師手動(Codex 指出:否則會更新到別行又設
            # 身份 01,違背「當天不設身份」)。丟掉已在記憶體改過的 excimer_text(不寫回)。
            if excimer_too_close is not None:
                return UvbUpdateResult(action=UvbAction.TOO_CLOSE,
                                       days_diff=excimer_too_close,
                                       uvb_line_count=uvb_lines)
            if excimer_count and excimer_first:
                return UvbUpdateResult(
                    action=UvbAction.UPDATED,
                    new_text=excimer_text,
                    new_dose=excimer_first["dose"],
                    new_count=excimer_first["count"],
                    last_date=excimer_first["last_date"],
                    days_diff=excimer_first["days_diff"],
                    uvb_line_count=excimer_count,
                    additional_triplets_updated=max(0, excimer_count - 1),
                )
            # [2026-06-24] 純 excimer 但每一行日期都早於 1 個月(被略過、沒更新到)→
            # 跳 Yes/No 確認(單行舊 excimer / 多行 excimer 全舊);Yes 後 caller 帶
            # skip_stale_check=True 重 call → 上面 skip_stale=True 會照舊紀錄更新。
            if not skip_stale_check and _all_excimer_stale(text, today):
                return _stale_confirm_result(text, today)
        # Strict parse 失敗 — 試 partial parse 看是不是「沒日期」case
        partial = parse_uvb_partial(text)
        if partial is None:
            # 連結構化 dose+max 都找不到。再細分:
            # - 有「UVB:」或「UVB：」結構 (with garbage 中文 etc.) → PARSE_FAIL
            #   (e.g. 廖三發「UVB:已打折 1000」中文夾在冒號後)
            # - [v20.17] 有 UVB/Phototherapy + 數字 但缺 MAX (e.g. 圖三梁雯琳
            #   `keep UVB 850 mj/cm2`) → SILENT_SKIP (不修改處置但繼續執行
            #   51019+療程)
            # - 連 UVB/Phototherapy + 數字 結構都沒有 → NO_UVB_LINE
            #   (例如「keep phototherapy on both lower limbs to 680」這種
            #   一般描述語句，不是結構化處置)
            # [v20.18] 加 UV 同樣判斷 (但 UV: 較罕見)
            if re.search(r"(?:UVB|Phototherapy|UV)\s*[:：]",
                         text, re.IGNORECASE):
                return UvbUpdateResult(action=UvbAction.PARSE_FAIL,
                                       uvb_line_count=uvb_lines)
            if _UVB_DOSE_RE.search(text):
                # 有 UVB+數字但結構不完整 (缺 MAX) → silent skip
                return UvbUpdateResult(action=UvbAction.SILENT_SKIP,
                                       uvb_line_count=uvb_lines)
            return UvbUpdateResult(action=UvbAction.NO_UVB_LINE,
                                   uvb_line_count=uvb_lines)
        # Partial 抓到 dose+max
        if partial.last_date is None:
            # [UC-02] 安全網:segment 內若有『日期形狀殘跡』(未被解析成功的日期,如裸民國 115/7/9、
            #  點分隔 2026.7.9、括號 7-8 位純數字/病歷號),代表這【不是】真正無日期的 first-time,
            #  而是『日期存在但解析不出』→ 走 first-time 會繞過 TOO_CLOSE/decay/stale 全部間隔防線
            #  (昨天照過也照樣 +increase)。一律 PARSE_FAIL 交醫師,不猜。
            if _has_unparsed_date_shape(partial.full_match):
                return UvbUpdateResult(action=UvbAction.PARSE_FAIL,
                                       uvb_line_count=uvb_lines)
            # [v20.17] 真的沒 date → silent first-time 更新，不跳對話框
            # (user request: "不用跳出是否新增日期 直接修改劑量")
            # 註：first-time 刻意只受 phrase 內「本地 max」約束(_first_time_update
            # 已 min(dose+increase, 本地max) 夾住)，不套用全域 MAX_DOSE 上限確認
            # ——醫師當下親手寫的 dose+max 視為可信(見 test_silent_first_time_*)。
            result = _first_time_update(partial, today, uvb_lines)
            # [UC-06] first-time 的 sanity 沒過會回 PARSE_FAIL(無 new_text)→ 不可 splice,直接回。
            if result.action != UvbAction.UPDATED or result.new_text is None:
                return result
            # 接回原文
            full_new = (text[:partial.span[0]]
                        + result.new_text
                        + text[partial.span[1]:])
            result.new_text = full_new
            return result
        # partial 有 date 但少 increase 之類 — fall through to PARSE_FAIL
        # (theoretically 應該被 strict parse_uvb_line 抓到才對)
        return UvbUpdateResult(action=UvbAction.PARSE_FAIL,
                               uvb_line_count=uvb_lines)

    # [2026-06-18] CONFIRM_NEEDED 上限確認改為「只看本次要照的劑量」:
    # 不在此用「原劑量 parsed.dose」早退 —— 原劑量 >1500 但久未照光經 decay 後本次
    # 可能 ≤1500(例:1700 隔 15 天 → 850),早退會誤跳確認。也不在此用 MAX(最高劑量),
    # MAX 可超過 1500。一律往下走 decay/maintain 計算,由「本次計算劑量 new_dose」
    # (compute 之後)+ 同日多行/續行 max_applied_dose(函式尾端)統一判定是否 >1500。

    # [2026-06-24] staleness 分流(改劑量,門檻 = 1 個日曆月):
    #   - 第一行 UVB 太舊(早於 today-1月)但下面有近期 UVB 行 → 改用近期行驅動,舊行排在
    #     前面、不會被 Step A/B 動到(等於忽略不修改),不跳窗。
    #   - 沒有任何近期 UVB 行(單行舊 / 多行全舊)→ 跳 Yes/No 確認;Yes(skip_stale_check)後
    #     維持舊 parsed、照舊紀錄繼續更新。
    # 注意:driver 重選【不受】skip_stale_check 影響(dose-confirm 的 Yes 重 call 也要略過舊行);
    # 只有「全舊 → 確認」這一步才受 skip_stale_check 控制。
    _cutoff = _months_before(today, MODIFY_STALE_MONTHS)
    if parsed.last_date is not None and parsed.last_date < _cutoff:
        _fresh = _first_fresh_uvb(text, _cutoff)
        if _fresh is not None:
            parsed = _fresh
        elif (not skip_stale_check
              and (today - parsed.last_date).days <= MAX_GAP_DAYS):
            # 1 個月 ~ 2 年的舊紀錄 → 跳 Yes/No 確認。
            # 超過 2 年(MAX_GAP_DAYS)不在此攔截 → 落到下方 sanity 檢查回 SANITY_FAIL
            # (病歷可能跑錯病人,維持硬停);skip_stale_check(Yes)時亦不攔,照舊更新。
            return _stale_confirm_result(text, today)

    # ─── Sanity checks on parsed values ─────────────────────────────────
    # 下限永遠檢查；上限只在非 skip 模式才當作異常
    if parsed.dose < MIN_DOSE:
        return UvbUpdateResult(
            action=UvbAction.SANITY_FAIL,
            sanity_reason=(f"原劑量 {parsed.dose} 低於下限 {MIN_DOSE}"),
            parsed=parsed, uvb_line_count=uvb_lines,
        )
    if parsed.max_dose < MIN_DOSE:
        return UvbUpdateResult(
            action=UvbAction.SANITY_FAIL,
            sanity_reason=(f"MAX {parsed.max_dose} 低於下限 {MIN_DOSE}"),
            parsed=parsed, uvb_line_count=uvb_lines,
        )
    # count sanity (count 可能 None — 處置沒寫，跳過 sanity)
    if parsed.count is not None and (
            parsed.count <= 0 or parsed.count > MAX_COUNT):
        return UvbUpdateResult(
            action=UvbAction.SANITY_FAIL,
            sanity_reason=f"次數 ({parsed.count}) 異常 [1-{MAX_COUNT}]",
            parsed=parsed, uvb_line_count=uvb_lines,
        )
    # increase=0(保持/不加量)合法的兩種情況：(a)明寫 maintain dose；(b)劑量已達/超過
    # MAX→本就無法再加量(會被 cap)。[2026-06-08] 補上 (b)，與 parse_uvb_line 同步，
    # 涵蓋「keep phototherapy … to 680 … MAX 680」固定劑量寫法(蔡國華實機 case)。
    if ((parsed.increase <= 0
            and not _has_maintain_dose(parsed.full_match)
            and parsed.dose < parsed.max_dose)
            or parsed.increase > 200):
        return UvbUpdateResult(
            action=UvbAction.SANITY_FAIL,
            sanity_reason=f"increase ({parsed.increase}) 異常 [1-200]",
            parsed=parsed, uvb_line_count=uvb_lines,
        )

    days_diff = (today - parsed.last_date).days

    # 日期在未來 → 病歷有問題
    if days_diff < 0:
        return UvbUpdateResult(
            action=UvbAction.SANITY_FAIL,
            sanity_reason=(f"上次照光日期 ({parsed.last_date}) 在未來，"
                          f"病歷可能有誤"),
            parsed=parsed, uvb_line_count=uvb_lines,
        )
    # 距上次超過 2 年 → 異常 (病歷可能跑錯病人)
    if days_diff > MAX_GAP_DAYS:
        return UvbUpdateResult(
            action=UvbAction.SANITY_FAIL,
            sanity_reason=(f"距上次照光 {days_diff} 天 (>{MAX_GAP_DAYS}天)，"
                          f"異常請確認"),
            last_date=parsed.last_date, days_diff=days_diff,
            parsed=parsed, uvb_line_count=uvb_lines,
        )

    # [2026-06-24] 「久未照光 → 確認」已改由前面的 staleness 分流(driver 重選 +
    # _stale_confirm_result,門檻 1 個日曆月)處理,此處不再重複 STALE_DAYS(30天)檢查。

    if days_diff < TOO_CLOSE_DAYS:
        return UvbUpdateResult(
            action=UvbAction.TOO_CLOSE,
            last_date=parsed.last_date,
            days_diff=days_diff,
            parsed=parsed, uvb_line_count=uvb_lines,
        )

    # [UC-11 audit 2026-07-12] 病歷矛盾:原劑量已 > 本行 MAX(如 900 但 MAX:800)。原本 2-6 天
    # 會靜默 min() 壓回 800、7 天卻保持 900 → 行為不一致且都在靜默處理。改為交醫師確認(dose==MAX
    # 的合法固定劑量不受影響,用嚴格 >);按 Yes 後 skip_dose_sanity 放行。
    if not skip_dose_sanity and parsed.max_dose and parsed.dose > parsed.max_dose:
        return UvbUpdateResult(
            action=UvbAction.CONFIRM_NEEDED,
            confirm_reason=(
                f"原劑量 {parsed.dose} mj/cm2 已超過本行 MAX {parsed.max_dose} mj/cm2,"
                "病歷可能有誤,請確認是否仍要更新"),
            last_date=parsed.last_date,
            days_diff=days_diff,
            parsed=parsed, uvb_line_count=uvb_lines,
        )

    new_dose = compute_new_dose(
        dose=parsed.dose, increase=parsed.increase,
        max_dose=parsed.max_dose, days_diff=days_diff,
    )
    assert new_dose is not None  # 已通過 too-close 檢查

    # [v20.15] 處置含 "maintain" 字眼 → 醫師意圖維持原劑量，覆蓋 compute 結果
    # 只動 count + date，dose 保持 parsed.dose 不增不減
    if _has_maintain_dose(parsed.full_match):
        new_dose = parsed.dose

    # 新 dose sanity 再檢一次 (理論上 compute_new_dose 不會吐出怪值，這層保險)
    if new_dose < MIN_DOSE:
        return UvbUpdateResult(
            action=UvbAction.SANITY_FAIL,
            sanity_reason=f"計算出新劑量 {new_dose} 低於下限",
            parsed=parsed, days_diff=days_diff, uvb_line_count=uvb_lines,
        )
    # [2026-06-18] 本次計算劑量 >1500 → 不再硬擋,改回 CONFIRM_NEEDED 讓 caller 跳
    # Yes/No,按 Yes 後以 skip_dose_sanity=True 重 call 繼續套用(MAX 可超過 1500)。
    if not skip_dose_sanity and new_dose > MAX_DOSE:
        return UvbUpdateResult(
            action=UvbAction.CONFIRM_NEEDED,
            confirm_reason=(
                f"本次計算劑量 {new_dose} mj/cm2 超過建議上限 {MAX_DOSE} mj/cm2"),
            parsed=parsed, days_diff=days_diff, uvb_line_count=uvb_lines,
        )

    # [v20.7] count optional — 處置沒寫 (N) 就不更新 count，dose/date 仍更新
    new_count: Optional[int] = None
    if parsed.count is not None:
        new_count = parsed.count + 1
    new_line = format_uvb_line(parsed, new_dose=new_dose, new_count=new_count,
                               today=today)

    # ─── Step A: 套用第一行 UVB segment 替換 ─────────────────────────────
    # 後續 step B/C 都在這個 working text 上操作，避免 offset 失效。
    working = text[:parsed.span[0]] + new_line + text[parsed.span[1]:]
    first_end = parsed.span[0] + len(new_line)

    # ─── Step B: v20.11 同日期 UVB-關鍵字多行更新 ───────────────────────
    # 處置可能有多行 UVB (e.g. 不同部位獨立記)，全部 last_date 跟第一行一致的
    # 都套用 format_uvb_line (dose+count+date 全部更新)，不同日期就停。
    # [2026-06-18] 追蹤「本次實際要照的最高劑量」(主行 + 同日多行 + 續行 triplet)。
    # 主行 new_dose 已於上面 >1500 檢查;additional/續行則累積到這裡,函式尾端統一確認。
    max_applied_dose = new_dose
    uvb_additional = 0
    cursor = first_end
    while True:
        rest = working[cursor:]
        next_uvb = parse_uvb_line(rest)
        if next_uvb is None:
            break
        if next_uvb.last_date != parsed.last_date:
            # 不同日期 (通常是更早的歷史紀錄) → 不動，停止
            break
        # 各別 sanity (additional segment 也要過 dose 上下限)
        if (next_uvb.dose < MIN_DOSE or next_uvb.max_dose < MIN_DOSE):
            break
        # [2026-06-18] additional 行的 MAX 與本次劑量都可超過 1500;是否 >1500 由
        # 函式尾端 max_applied_dose 統一確認(不在此 break,以免漏更新同日其他行)。
        # [review C 2026-06-12] increase=0 的豁免條件與第一行檢查同步：明寫 maintain、
        # 或「劑量已達/超過 MAX 的固定劑量行」(dose>=max 本就無法再加量)都合法。
        # 原本漏了 dose>=max 豁免 → 多行處置中第 2 行以後的固定劑量行會中斷迴圈
        # 不更新(第一行更新了、後行日期/次數停舊值，病歷不一致)。
        if ((next_uvb.increase <= 0
             and not _has_maintain_dose(next_uvb.full_match)
             and next_uvb.dose < next_uvb.max_dose)
                or next_uvb.increase > 200):
            break
        # [UC-11 audit 2026-07-12] 同日附加行同樣把關(與主行一致):原劑量 > 該行 MAX → 交
        # 醫師確認,不靜默 min() 壓回。否則「主行合法、第二行 900>MAX800」會被靜默壓 800。
        if (not skip_dose_sanity and next_uvb.max_dose
                and next_uvb.dose > next_uvb.max_dose):
            return UvbUpdateResult(
                action=UvbAction.CONFIRM_NEEDED,
                confirm_reason=(
                    f"同日另一行原劑量 {next_uvb.dose} mj/cm2 已超過該行 MAX "
                    f"{next_uvb.max_dose} mj/cm2,病歷可能有誤,請確認是否仍要更新"),
                last_date=parsed.last_date,
                days_diff=days_diff,
                parsed=parsed, uvb_line_count=uvb_lines,
            )
        # 同日期 — 用該行自己的 dose/increase/MAX 算
        next_new_dose = compute_new_dose(
            dose=next_uvb.dose, increase=next_uvb.increase,
            max_dose=next_uvb.max_dose, days_diff=days_diff,
        )
        if next_new_dose is None or next_new_dose < MIN_DOSE:
            break
        # [2026-06-18] 與主行一致(見上方 v20.15 maintain 覆蓋):該行寫 maintain dose
        # → 維持原劑量不加量。否則固定劑量行會被誤加,且本次劑量 ≤1500 卻誤算成 >1500
        # 而跳確認。next_uvb.dose 已於上方通過 MIN 下限檢查。
        if _has_maintain_dose(next_uvb.full_match):
            next_new_dose = next_uvb.dose
        max_applied_dose = max(max_applied_dose, next_new_dose)
        next_new_count = (next_uvb.count + 1
                          if next_uvb.count is not None else None)
        next_new_line = format_uvb_line(
            next_uvb, new_dose=next_new_dose, new_count=next_new_count,
            today=today)
        abs_start = cursor + next_uvb.span[0]
        abs_end = cursor + next_uvb.span[1]
        working = working[:abs_start] + next_new_line + working[abs_end:]
        uvb_additional += 1
        cursor = abs_start + len(next_new_line)

    # ─── Step C: 同行照光 continuation triplet 更新 ─────────────────────
    # 找剩下沒被 step A/B 更新的同行 (count) ... (date) 三元組:
    #   - 同行繼續的 UVB segment (e.g. `/ new for ... 1500mj/cm2 (44) on (date)`)
    #   - excimer / excimer light 由 Step D 依自己的欄位獨立更新
    # Step A 已經把第一行 triplet 的 date 改成 today，所以這裡掃描 working text
    # 時，第一行的 triplet 不會匹配 parsed.last_date，自動跳過。
    # 只更新 count + date，不動 segment 內 dose (continuation 通常 fixed at MAX
    # 不會變；[UC-07] 跨日期且進入 decay 區間者不 bump、留原樣交醫師,見迴圈內守衛)。
    triplet_edits = []
    for m in _TRIPLET_RE.finditer(working):
        try:
            seg_date = date(int(m.group(3)), int(m.group(4)),
                            int(m.group(5)))
        except (ValueError, TypeError):
            continue
        try:
            old_count = int(m.group(1))
        except ValueError:
            continue
        if not (1 <= old_count <= MAX_COUNT):
            continue
        # 要求 same line 內有 UVB-相關標記 (uvb/excimer/數字mj/phototherapy)
        # 不跨換行 — 避免誤動其他「(N) ... (date)」內容 (e.g. 上一行才是 UVB
        # 行，下一行的「(5) days post op」不該被算 UVB 同類項)
        line_start = working.rfind("\n", 0, m.start()) + 1   # 0 if not found
        line_end = working.find("\n", m.end())
        if line_end == -1:
            line_end = len(working)
        line_text = working[line_start:line_end]
        excimer_marker = _EXCIMER_MARKER_RE.search(line_text)
        if (excimer_marker is not None
                and line_start + excimer_marker.start() < m.start()):
            continue
        if not _UVB_MARKER_RE.search(line_text):
            continue
        # Same-line continuation segments are phototherapy too. They may carry
        # their own date, so update them independently when a dose precedes
        # the triplet. Other different-date triplets remain uncertain.
        # [UC-08 2026-07-12] (count) 與配對 (日期) 之間夾另一個裸日期 → 配對歸屬不明
        # (count 更可能屬於較近那個日期;典型=Step A 更新後殘留的重複日期,會把驅動行
        # 自己的 count 再 +1 成 count+2)→ 不 bump,留原樣交醫師。
        if _DATE_LIKE_IN_MIDDLE_RE.search(m.group(2)):
            continue
        dose_prefix = working[max(line_start, m.start() - 32):m.start()]
        continuation_m = re.search(
            r"(\d+)\s*mj(?:/cm2)?\s*$", dose_prefix, re.IGNORECASE)
        if seg_date != parsed.last_date:
            # Cross-date continuation updates are safe without recalculating
            # dose only when that segment is already capped at this line's MAX.
            if (continuation_m is None
                    or int(continuation_m.group(1)) != parsed.max_dose):
                continue
        seg_days_diff = (today - seg_date).days
        if seg_days_diff < TOO_CLOSE_DAYS or seg_days_diff > MAX_GAP_DAYS:
            continue
        # [UC-07 2026-07-12] 跨日期續行且已進入 decay 區間(>SAME_DOSE_DAYS):其劑量==本行
        # MAX 只是「已達上限」,不代表今天仍可照原劑量(當獨立劑量算應衰退,例:14 天前
        # 800→應 ×0.75=600)。醫療保守=不靜默 bump 成今天,留原樣交醫師;且下方
        # _detect_uncertain_triplets 以同款 capped 判定排除、不進 Yes/No(否則醫師按 Yes
        # 仍 kept-dose 寫回未衰退值 —— codex 指出的交互)。同日期續行(共用驅動行日期)與
        # ≤SAME_DOSE_DAYS(不需衰退,keep/加量 cap MAX 結果同 kept-dose)不受影響。
        if seg_date != parsed.last_date and seg_days_diff > SAME_DOSE_DAYS:
            continue
        # [2026-06-24] 略過「與驅動行【不同日期】且早於 1 個月」的舊段 triplet —— 那是另一筆
        # 要忽略的舊紀錄,避免其劑量剛好等於驅動行 MAX 時被當「安全續行」誤改 count/date。
        # 但驅動行【自己同日期】的續行 triplet 不在此略過(seg_date==parsed.last_date):
        # 否則「全舊 → 確認 Yes」後主行更新了、同筆續行卻沒跟著更新(codex 指出)。
        if (seg_date != parsed.last_date
                and seg_date < _months_before(today, MODIFY_STALE_MONTHS)):
            continue
        # 構造該 triplet 替換內容: count→count+1, date→today
        seg_text = m.group(0)
        seg_text = re.sub(
            r"([\(\uFF08]\s*)" + str(old_count) + r"(\s*[\)\uFF09])",
            lambda mo: f"{mo.group(1)}{old_count + 1}{mo.group(2)}",
            seg_text,
            count=1,
        )
        seg_text = re.sub(
            rf"([\(\uFF08]\s*){seg_date.year}([/-])0?{seg_date.month}([/-])0?{seg_date.day}(\s*[\)\uFF09])",
            lambda mo: f"{mo.group(1)}{_date_text(today, mo.group(2))}{mo.group(4)}",
            seg_text,
            count=1,
        )
        # [2026-06-18] 續行 triplet 保留原劑量、只 bump count/date;若該劑量 >1500
        # 也算「本次要照的劑量」→ 納入尾端統一確認。
        # 單位接受 mj / mj/cm / mj/cm2(與全域 dose parser 一致);continuation_m 的
        # 單位匹配較窄(只 mj、mj/cm2),這裡另用較寬的 regex 抓緊鄰 (count) 前的劑量,
        # 以免 mj/cm 等變體漏掉。仍只取 (count) 緊鄰前方的劑量 → 抓的是本次「劑量」
        # 而非句尾的 MAX(fixed at / upper limit),才不會把 MAX>1500 又當成要確認。
        guard_dose_m = re.search(
            r"(\d+)\s*mj(?:/cm2?)?\s*$", dose_prefix, re.IGNORECASE)
        # 但若該數字前方緊跟 MAX/上限關鍵字(upper limit:/fixed at/MAX…),那是上限不是
        # 本次劑量 → 不納入(否則 MAX>1500 又會害跳確認)。
        if guard_dose_m is not None and not _CEILING_KEYWORD_BEFORE_RE.search(
                dose_prefix[:guard_dose_m.start()]):
            max_applied_dose = max(max_applied_dose,
                                   int(guard_dose_m.group(1)))
        triplet_edits.append((m.span(), seg_text))

    triplet_count = 0
    for span, replacement in reversed(triplet_edits):
        working = working[:span[0]] + replacement + working[span[1]:]
        triplet_count += 1

    # ─── Step D: excimer / excimer light 各自依自己的欄位更新 ───────────
    working, excimer_count, _, _ = _update_excimer_lines(working, today)
    triplet_count += excimer_count

    new_text = working

    # [2026-06-18] 統一上限確認:同日多行 / 續行 triplet 裡任一「本次要照的劑量」
    # >1500 → 跳 Yes/No 確認(MAX 最高劑量本身可超過 1500、不在此擋;只看實際要照
    # 的劑量)。主行已於上方檢查,此處補抓 additional/續行。caller 按 Yes 帶
    # skip_dose_sanity=True 重 call → 略過本檢查、全部套用。
    if not skip_dose_sanity and max_applied_dose > MAX_DOSE:
        return UvbUpdateResult(
            action=UvbAction.CONFIRM_NEEDED,
            confirm_reason=(
                f"本次要照的劑量 {max_applied_dose} mj/cm2 "
                f"超過建議上限 {MAX_DOSE} mj/cm2"),
            parsed=parsed, days_diff=days_diff, uvb_line_count=uvb_lines,
        )

    # ─── Round-trip verify: 重新 parse 驅動行的新內容確認結果一致 ─────────
    # 防 format_uvb_line 因為奇怪格式沒替換成功，dose/count/date 跟預期不符。
    # [2026-06-24] 直接 parse Step A 產生的 new_line(驅動行新內容),【不靠位置】——
    # driver 重選時第一行是舊行;且驅動行前方若有 fresh excimer 被 Step D 改成不同長度,
    # 用原始 span 切片會偏位 → 改驗 new_line 本身,與位置/前方編輯無關(new_line 在 Step A
    # 後不會再被 Step B/C/D 動到:Step C 對 date==today 的驅動行會跳過)。
    verify = parse_uvb_line(new_line)
    if verify is None:
        return UvbUpdateResult(
            action=UvbAction.SANITY_FAIL,
            sanity_reason="寫回後重新 parse 失敗 (格式可能損毀)",
            parsed=parsed, days_diff=days_diff, uvb_line_count=uvb_lines,
        )
    # dose / date 一定要對；count 若有更新也要對 (none → none, 有 → 數字符)
    dose_ok = verify.dose == new_dose
    date_ok = verify.last_date == today
    count_ok = verify.count == new_count  # 若兩邊都 None 也算 ok
    if not (dose_ok and date_ok and count_ok):
        return UvbUpdateResult(
            action=UvbAction.SANITY_FAIL,
            sanity_reason=(f"寫回後 round-trip verify 失敗: "
                          f"預期 dose={new_dose}/count={new_count}/date={today}, "
                          f"實際 dose={verify.dose}/count={verify.count}/"
                          f"date={verify.last_date}"),
            parsed=parsed, days_diff=days_diff, uvb_line_count=uvb_lines,
        )

    # ─── v20.13 偵測「不確定其他 triplet」(日期不同) ─────────────────────
    # 例：line 1 是 excimer (37) (2026/5/22), line 2 是 UVB ... (2026/5/24)
    # Step A 處理 line 2 (UVB)，Step C 因日期不同沒動 line 1。
    # 但醫師可能希望 line 1 也一起更新 — 跳 Yes/No 給醫師決定。
    uncertain_others = _detect_uncertain_triplets(
        new_text, today, driver_max_dose=parsed.max_dose)

    return UvbUpdateResult(
        action=UvbAction.UPDATED,
        new_text=new_text,
        new_dose=new_dose,
        new_count=new_count,
        last_date=parsed.last_date,
        days_diff=days_diff,
        parsed=parsed,
        uvb_line_count=uvb_lines,
        additional_lines_updated=uvb_additional,
        additional_triplets_updated=triplet_count,
        uncertain_other_triplets=uncertain_others if uncertain_others else None,
    )
