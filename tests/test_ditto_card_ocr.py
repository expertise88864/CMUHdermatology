# -*- coding: utf-8 -*-
"""醫師上次 卡號 OCR 純解析邏輯的單元測試。

資料來自真實「醫師上次」截圖的 OCR 輸出(settings 探測,患者卡號 0009/0007/0006/0005/0004,
療程欄 1/2/3)。核心規則:取「最上面 療程=1 那一列」的卡號 → 應為 0009。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.ditto_card_ocr import (  # noqa: E402
    CARD_RE,
    Word,
    card_cells_from_words,
    find_card_column_x,
    find_tiao_column_x,
    pick_card_number,
    tiao_cells_from_words,
)

# ── 真實 OCR 資料 ───────────────────────────────────────────────
# 卡號欄(整張 OCR,含被選取反白的第 1 列)
REAL_CARDS = [
    (39, "0009"), (70, "0009"),
    (101, "0007"), (132, "0007"), (163, "0007"),
    (194, "0007"), (225, "0007"), (256, "0007"),
    (287, "0006"), (318, "0006"), (349, "0006"),
    (380, "0006"), (411, "0006"), (442, "0006"),
    (473, "0005"), (504, "0005"), (535, "0005"),
    (566, "0005"), (597, "0005"), (628, "0005"),
    (659, "0004"), (690, "0004"),
]
# 療欄(裁切+放大 5x 後 OCR;反白的第 1 列讀不到,從第 2 列起)
REAL_TIAOS = [
    (69, "1"), (100, "3"), (131, "2"), (162, "2"), (193, "2"), (224, "2"),
    (255, "1"), (286, "3"), (317, "2"), (348, "2"), (379, "2"), (410, "2"),
    (441, "1"), (472, "3"), (503, "2"), (534, "2"), (565, "2"), (596, "2"),
    (627, "1"), (658, "3"), (689, "2"),
]


def test_real_data_picks_topmost_tiao1_card():
    r = pick_card_number(REAL_CARDS, REAL_TIAOS)
    assert r.card == "0009"
    assert r.confidence == "high"
    assert r.tiao == 1
    assert r.ok is True


def test_card_regex():
    assert CARD_RE.match("0009")
    assert CARD_RE.match("123")
    assert not CARD_RE.match("00009")   # 5 碼
    assert not CARD_RE.match("12")      # 2 碼
    assert not CARD_RE.match("0O09")    # 含字母


def test_no_cards_returns_none():
    r = pick_card_number([], REAL_TIAOS)
    assert r.card is None
    assert r.confidence == "none"


def test_garbage_cards_filtered():
    r = pick_card_number([(39, "abcd"), (70, "1.2"), (101, "00009")],
                         [(70, "1")])
    assert r.card is None
    assert r.confidence == "none"


def test_new_card_single_occurrence_is_low_confidence():
    # 今天就是療程=1、整張只有一列那張卡 → 無法交叉驗證 → 最多 low,不自動填
    cards = [(39, "0012")]
    tiaos = [(39, "1")]
    r = pick_card_number(cards, tiaos)
    assert r.card == "0012"
    assert r.confidence == "low"
    assert r.ok is False


def test_no_tiao_readable_falls_back_but_not_high():
    # 完全讀不到療欄 → 退用最上列卡號,但信心不足(ok=False)
    r = pick_card_number(REAL_CARDS, [])
    assert r.confidence in ("low", "none")
    assert r.ok is False


def test_tiao1_card_must_cross_validate_for_high():
    # 療程=1 在最上,但那個卡號只出現一次 → low(避免單次 OCR 誤判直接填計費欄)
    cards = [(70, "0009"), (101, "0007"), (132, "0007")]
    tiaos = [(70, "1"), (101, "2"), (132, "2")]
    r = pick_card_number(cards, tiaos)
    assert r.card == "0009"
    assert r.confidence == "low"


def test_second_tiao1_group_not_chosen_over_topmost():
    # 有多個 療程=1(每張卡各一個);要選『最上面』那個
    r = pick_card_number(REAL_CARDS, REAL_TIAOS)
    # 最上 療=1 在 y≈69 → 0009,而非下面 0007/0006 的 療=1
    assert r.card == "0009"


def test_missed_top_card_tiao1_never_fills_older_card():
    # Codex 情境:最上面那張卡 0009 的『療程=1』漏讀;下面舊卡 0007 有療=1 且重複。
    # 規則必須拒填:療程=1 那列卡號(0007)≠ 最上列卡號(0009) → 不一致 → 不填。
    cards = [(39, "0009"), (70, "0009"),
             (101, "0007"), (132, "0007"), (163, "0007")]
    tiaos = [(101, "1"), (132, "2"), (163, "2")]  # 只讀到 0007 的療=1
    r = pick_card_number(cards, tiaos)
    assert r.ok is False
    assert r.card != "0007"


def test_high_requires_tiao1_card_equals_topmost():
    # 正常:最上列 0009、其療程=1 也在最上面且 0009 出現 2 次 → high
    cards = [(39, "0009"), (70, "0009"), (101, "0007"), (132, "0007")]
    tiaos = [(70, "1"), (101, "2"), (132, "1")]
    r = pick_card_number(cards, tiaos)
    assert r.card == "0009"
    assert r.confidence == "high"


def test_header_y_guard_blocks_when_top_group_missed():
    # 現在這張卡 0009(應在 y≈39/70)『整組』被 OCR 漏讀;讀到的最上列是 0007@y=101,
    # 離表頭(y=4)太遠 → 幾何把關判定頂部漏讀 → 不填。
    cards = [(101, "0007"), (132, "0007"), (163, "0007")]
    tiaos = [(101, "1"), (132, "2"), (163, "2")]
    r = pick_card_number(cards, tiaos, header_y=4)
    assert r.ok is False
    assert r.card is None


def test_header_y_guard_passes_for_normal_top():
    # 最上列貼近表頭 → 幾何把關通過,維持 high
    r = pick_card_number(REAL_CARDS, REAL_TIAOS, header_y=4)
    assert r.card == "0009"
    assert r.confidence == "high"


def test_header_y_guard_uses_dense_tiao_pitch_for_sparse_cards():
    # Codex round 3:卡號被『稀疏』讀到(101,163,225,假間距 62),但療欄密集(真實
    # 間距 31)。用合併後(含療欄)的真實間距估算 → 最上卡號離表頭過遠 → 仍判頂部漏讀。
    cards = [(101, "0007"), (163, "0007"), (225, "0007")]
    tiaos = [(101, "1"), (131, "2"), (162, "2"),
             (193, "2"), (224, "2"), (255, "2")]
    r = pick_card_number(cards, tiaos, header_y=4)
    assert r.ok is False
    assert r.card is None


def test_header_y_guard_blocks_card_starting_at_row2():
    # Codex round 4:今日(最上、反白)那列被漏讀,讀到的最上卡號落在『第二列』
    # (y≈70,gap≈2.1×pitch)。門檻 1.8×pitch → 擋下,不會把下面更舊的卡誤填。
    cards = [(70, "0007"), (101, "0007"), (132, "0007")]
    tiaos = [(70, "1"), (101, "2"), (132, "2")]
    r = pick_card_number(cards, tiaos, header_y=4)
    assert r.ok is False
    assert r.card is None


# ── 欄位偵測 ───────────────────────────────────────────────────
def _header_words():
    # 仿真實標題列(注意『時段診號』也有『號』,不可拿來當卡號欄錨點)
    return [
        Word("醫", 80, 4, 16, 17), Word("師", 98, 4, 17, 17),
        Word("就", 251, 4, 16, 17), Word("診", 268, 4, 17, 16),
        Word("科", 285, 4, 19, 17), Word("別", 305, 4, 16, 17),
        Word("卡", 582, 4, 18, 17), Word("號", 601, 4, 16, 17),
        Word("療", 679, 4, 17, 17),
        Word("時", 734, 4, 16, 17), Word("段", 751, 4, 18, 17),
        Word("診", 769, 4, 18, 16), Word("號", 788, 4, 16, 17),
    ]


def test_find_card_column_x_ignores_診號():
    cx = find_card_column_x(_header_words())
    assert cx is not None
    # 應落在『卡號』那欄(~591),而不是被『診號』(~796)拉偏
    assert 580 <= cx <= 615


def test_find_tiao_column_x():
    tx = find_tiao_column_x(_header_words())
    assert tx is not None
    assert 670 <= tx <= 700


def test_card_cells_x_filter():
    words = _header_words() + [
        Word("0009", 583, 39, 34, 12),     # 卡號欄
        Word("696.1", 1165, 70, 36, 12),   # 診斷碼,不是卡號
        Word("37", 1837, 68, 17, 13),      # 最右欄數字,不是卡號
    ]
    cx = find_card_column_x(words)
    assert cx is not None
    cells = card_cells_from_words(words, cx)
    assert cells == [(39, "0009")]


def test_tiao_cells_x_filter():
    tx = 679.0
    words = [
        Word("1", 680, 69, 5, 12),       # 療欄
        Word("2", 15, 378, 8, 15),       # 最左序號欄,不是療
        Word("696.1", 1165, 70, 36, 12),
    ]
    cells = tiao_cells_from_words(words, tx)
    assert cells == [(69, "1")]
