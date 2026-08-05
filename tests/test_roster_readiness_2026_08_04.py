# -*- coding: utf-8 -*-
"""病人清單要等它不再變動，不能只睡固定秒數（2026-08-04 外審 P1-03）。

【問題】
`_query_cycle` 只 `time.sleep(1.8)` 就當清單載入完了。Delphi 視窗是先建立、資料
再逐步填進去的，那一秒八【不保證】看到的是最終狀態：

  * 還沒載入 → 空清單被當成「成功且真的沒有病人」→ 基準被剪成空
                → 下一輪所有既有會診都變「新」→ ★對團隊重寄整份清單★
  * 載入到一半 → partial roster 被存成基準 → 還沒出現的病人此後不算新 → ★漏寄★

固定睡多久都治不了（慢的機器仍會失手）。要看的是【內容有沒有還在變】。

【修法】連續讀到相同才算穩定；逾時仍在變 → `roster_texts` 回 None，走既有的
「判斷不了 → fail-open 照寄、但不更新基準」通道。

★[2026-08-05 外審第 4 輪 P1-07/08/09]★ 本檔在這一輪擴充了三件事：
  * 有內容的清單也要有最短觀察窗（原本只有空清單有）—— 見
    `TestAPartialRosterIsNotAcceptedTooFast`
  * 清單文字與 radio 控制項必須來自【同一次】列舉 —— 見 `TestOneSnapshot`
  * 截圖要在清單穩定【之後】才拍 —— 見 `TestTheScreenshotMatchesTheList`
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import consult_query as cq  # noqa: E402


def _tree(texts):
    """把清單文字造成一棵控制項樹（radio 文字要能被 _PATIENT_LABEL_RE 認得）。"""
    return [(1000 + i, cq._PATIENT_RADIO_CLASS, t, (0, i * 20, 200, i * 20 + 18))
            for i, t in enumerate(texts)]


def _snap(texts):
    """生產形狀的一次讀取結果：(children, radios, texts)。"""
    children = _tree(texts)
    return children, cq._find_patient_radios(children), list(texts)


def _clock(step=0.25):
    """可控 monotonic：每讀一次前進 step 秒。"""
    t = {"v": 0.0}

    def _now():
        t["v"] += step
        return t["v"]
    return _now


def _scripted(reads):
    """把一連串「每次讀到什麼」腳本化。用完後一直回最後一筆。

    ★回傳的是生產的形狀★（三元組），不是只有文字 —— `_await_stable_roster`
    現在拿的是一整份快照（外審第 4 輪 P1-08）。
    """
    seq = list(reads)

    def _read(_hwnd):
        cur = seq.pop(0) if len(seq) > 1 else seq[0]
        return _snap(cur)
    return _read


class TestLoadingIsNotMistakenForEmpty:

    def test_a_roster_that_appears_late_is_waited_for(self):
        """★審查點名的情境★ 視窗先出現、2 秒後才有 radio。"""
        pt = ["甲C16(1)1111111"]
        snap = cq._await_stable_roster(
            1, read=_scripted([[], [], pt, pt, pt]),
            sleep=lambda _s: None, now=_clock())

        assert snap.stable is True
        assert snap.texts == pt, f"沒等到載入完成：{snap.texts}"

    def test_a_roster_that_grows_is_waited_for(self):
        """先出現 2 位、稍後增至 4 位 —— 不可以拿 2 位那份去更新基準。"""
        two = ["甲1111111", "乙2222222"]
        four = two + ["丙3333333", "丁4444444"]
        snap = cq._await_stable_roster(
            1, read=_scripted([two, four, four, four]),
            sleep=lambda _s: None, now=_clock())

        assert snap.stable is True and snap.texts == four, (
            f"拿到 partial roster：{snap.texts}")

    def test_a_genuinely_empty_roster_is_accepted(self):
        """★反方向:真的沒有病人也要判得出來★

        否則沒有病人的時段永遠「判斷不了」→ 每輪都 fail-open 寄信，變成天天洗信箱。
        """
        snap = cq._await_stable_roster(
            1, read=_scripted([[], [], [], []]), sleep=lambda _s: None,
            now=_clock())

        assert snap.stable is True and snap.texts == [], (
            "空清單也要能被判定成穩定，否則沒病人時每輪都會寄信")

    def test_an_endlessly_changing_roster_is_reported_unstable(self,
                                                               monkeypatch):
        """一直在變 → 逾時 → 回報判斷不了（而不是把當下那份當真）。"""
        n = {"i": 0}

        def _read(_hwnd):
            n["i"] += 1
            return _snap([f"病人{n['i']}C16(1)111111{n['i']}"])

        ticks = {"t": 0.0}

        def _mono():
            ticks["t"] += 1.0
            return ticks["t"]
        monkeypatch.setattr(cq.time, "monotonic", _mono)

        snap = cq._await_stable_roster(1, read=_read, sleep=lambda _s: None)

        assert snap.stable is False, "一直在變卻回報穩定"


class TestTheUnstableRosterGoesDownTheFailOpenChannel:
    """不穩定要走既有的 `roster_texts is None` 通道。

    那條路的語意已經是「無法判斷有沒有新會診 → fail-open 照常寄信、且【不更新
    基準】」——正是我們要的處置，呼叫端不需要新增任何處理。
    """

    def test_none_means_do_not_touch_the_baseline(self):
        """釘住那個通道的語意（它是本修法的整個依據）。"""
        assert cq._consult_signature_from_roster(None) == set()

    def test_extract_returns_none_when_unstable(self, monkeypatch):
        """接線:不穩定時 `_extract_consult_text` 的第三個回傳必須是 None。"""
        monkeypatch.setattr(
            cq, "_await_stable_roster",
            lambda *a, **k: cq._RosterSnapshot(["甲1111111"], False, [], []))
        monkeypatch.setattr(cq, "_find_text_panes", lambda _c: [])

        _t, _h, roster_texts = cq._extract_consult_text(1, {})

        assert roster_texts is None, (
            "★不穩定卻回報成有效清單★ 基準會被一份還在變的資料更新")

    def test_extract_returns_the_list_when_stable(self, monkeypatch):
        """★反方向:穩定時要正常回報★ 否則基準永遠不更新、每輪都重寄。"""
        monkeypatch.setattr(
            cq, "_await_stable_roster",
            lambda *a, **k: cq._RosterSnapshot(["甲1111111"], True, [], []))
        monkeypatch.setattr(cq, "_find_text_panes", lambda _c: [])

        _t, _h, roster_texts = cq._extract_consult_text(1, {})

        assert roster_texts == ["甲1111111"]


def test_the_extraction_actually_waits():
    """★接線本身也要被測到★（本 session 這個形狀第七次）

    上面幾支直接呼叫 `_await_stable_roster`。若 `_extract_consult_text` 仍然
    單次讀取，它們照樣全綠 —— 而那正是 bug 還在的樣子。

    ★[2026-08-05] 現在允許呼叫端把已經等好的快照傳進來★（截圖必須排在穩定
    之後、擷取之前）。所以判準是「要嘛自己等、要嘛收下別人等好的」，
    不可以兩者皆非。
    """
    import ast
    import inspect
    import textwrap

    src = textwrap.dedent(inspect.getsource(cq._extract_consult_text))
    tree = ast.parse(src)
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_await_stable_roster" in called, (
        "擷取仍然單次讀清單 → 載入中的空/半份清單會被當成有效資料")
    assert "settled" in {a.arg for a in ast.walk(tree)
                         if isinstance(a, ast.arg)}, (
        "沒有收下呼叫端等好的快照 → 截圖與清單會是不同時刻的")


class TestAnEmptyRosterNeedsALongerLook:
    """★[外審第 3 輪 P1-01]★「沒有變」不等於「載入完成」。

    【空】剛好也是「還沒開始載入」的樣子。連續三次讀到空只要 0.5 秒就達成 ——
    若 HIS 在 1.5 秒才開始填，那 0.5 秒的「穩定空清單」會被當成「今天真的沒有
    會診」→ 基準被剪成空 → 下一輪整份重寄。

    外審也指出我原本那支「2 秒後才有 radio」的測試沒有真的模擬時間（sleep 被
    换成 no-op、也沒有可控時鐘），所以測不到這件事。這裡用可控時鐘補上。
    """

    def test_empty_then_patients_at_1_5s_is_not_called_empty(self):
        """★這就是外審點名的情境★ 前 1.5 秒空，之後才出現病人。"""
        empties = [[] for _ in range(6)]          # 0.25s × 6 = 1.5 秒
        pt = ["甲C16(1)1111111"]
        snap = cq._await_stable_roster(
            1, read=_scripted(empties + [pt, pt, pt]),
            sleep=lambda _s: None, now=_clock())

        assert snap.stable is True
        assert snap.texts == pt, (
            f"★0.5 秒就把還沒載入的空清單當成『真的沒有會診』★：{snap.texts}")

    def test_a_truly_empty_roster_is_accepted_after_the_longer_look(self):
        """★反方向:真的沒病人仍要判得出來★ 觀察夠久之後要回 stable。

        否則沒有病人的時段永遠「判斷不了」→ 每輪 fail-open 寄信＝天天洗信箱。
        """
        snap = cq._await_stable_roster(
            1, read=_scripted([[]]), sleep=lambda _s: None, now=_clock())

        assert snap.stable is True and snap.texts == [], (
            "觀察夠久之後空清單仍要能被接受")


class TestAPartialRosterIsNotAcceptedTooFast:
    """★[2026-08-05 外審第 4 輪 P1-07]★ 有內容的清單也需要最短觀察窗。

    ★這個檔案原本有一支測試把這個缺陷釘成了通過條件★
    `test_a_nonempty_roster_does_not_wait_the_extra_window`，理由寫著
    「有內容就不必等，資料都出現了，不可能是『還沒載入』」。

    那句話只對【完全沒載入】成立，對【載到一半】完全不成立 —— Delphi 是逐列填
    的，4 位只填出 2 位、然後停頓超過 0.5 秒（慢機器／後端慢很常見），就會拿
    2 位那份當成最終清單存進基準 → 另外 2 位此後永遠不算「新」→ ★漏寄★。
    而「漏寄」正是本檔開頭列出的失敗模式之一。
    """

    def test_a_stalled_partial_roster_is_not_taken_as_final(self):
        """2 位卡住 0.5 秒（3 次相同讀取）→ 之後才補齊 4 位。不可以只拿 2 位。"""
        two = ["甲1111111", "乙2222222"]
        four = two + ["丙3333333", "丁4444444"]
        # 2 位連續讀到 4 次（＝超過「連續三次相同」那道關），才長出 4 位
        snap = cq._await_stable_roster(
            1, read=_scripted([two, two, two, two, four, four, four, four]),
            sleep=lambda _s: None, now=_clock())

        assert snap.texts == four, (
            f"★把載到一半的清單當成最終清單★ 少掉的兩位此後永遠不算新：{snap.texts}")

    def test_a_nonempty_roster_still_settles_well_inside_the_timeout(self):
        """★反方向:不可以變成每輪都等到逾時★

        觀察窗只是「最短」，穩定之後就該回來。這裡確認有內容的清單不會被拖到
        逾時（那會讓每一輪都吃滿 6 秒、而且回報 unstable ＝ 每輪 fail-open 寄信）。
        """
        pt = ["甲1111111"]
        reads = {"n": 0}

        def _read(_h):
            reads["n"] += 1
            return _snap(pt)

        snap = cq._await_stable_roster(
            1, read=_read, sleep=lambda _s: None, now=_clock())

        assert snap.stable is True and snap.texts == pt
        assert reads["n"] <= 10, f"有內容卻讀了 {reads['n']} 次"

    def test_the_nonempty_window_is_shorter_than_the_empty_one(self):
        """空清單的風險更高（會把基準剪成空 → 整份重寄），觀察窗要更長。"""
        assert 0 < cq._ROSTER_MIN_OBSERVE < cq._ROSTER_EMPTY_MIN_OBSERVE
        assert cq._ROSTER_MIN_OBSERVE > _clock_step_of_three_reads(), (
            "最短觀察窗若小於『連續三次相同』本來就會花掉的時間，等於沒有加")


def _clock_step_of_three_reads():
    return cq._ROSTER_SETTLE_INTERVAL * (cq._ROSTER_SETTLE_READS - 1)


class TestOneSnapshot:
    """★[2026-08-05 外審第 4 輪 P1-08]★ 清單文字與 radio 必須同源。

    以前是兩次列舉：穩定判定讀一次、擷取前再讀一次。中間長出一位病人時
    信裡列 N 位、逐病人內文卻是 N±1 位，基準也只存到其中一份。

    ★修法是「結構上不可能」而不是「事後再比對」★：一次列舉同時產出
    children / radios / texts，三者必然是同一時刻的。
    """

    def test_one_enumeration_produces_all_three(self, monkeypatch):
        calls = {"n": 0}
        texts = ["甲C16(1)1111111", "乙C16(2)2222222"]

        def _enum(_h):
            calls["n"] += 1
            return _tree(texts)
        monkeypatch.setattr(cq, "enum_children", _enum)
        monkeypatch.setattr(cq, "_is_visible_below", lambda *a: True)

        children, radios, got = cq._read_roster_snapshot(1)

        assert calls["n"] == 1, f"讀一份快照列舉了 {calls['n']} 次"
        assert got == texts
        assert len(radios) == len(texts)
        assert children == _tree(texts)

    def test_extract_uses_the_snapshot_radios_not_a_fresh_read(self,
                                                               monkeypatch):
        """★核心★ 擷取用的 radio 必須是快照裡那一份，不可以自己再列舉一次。

        再列舉一次就等於把 P1-08 放回來：那一次讀到的可能已經多／少一位。
        """
        settled_texts = ["甲C16(1)1111111", "乙C16(2)2222222",
                         "丙C16(3)3333333", "丁C16(4)4444444"]
        snap_children = _tree(settled_texts)
        snap_radios = cq._find_patient_radios(snap_children)

        def _boom(_h):
            raise AssertionError("★擷取又自己列舉了一次★ 清單與內文會對不起來")
        monkeypatch.setattr(cq, "enum_children", _boom)
        monkeypatch.setattr(
            cq, "_await_stable_roster",
            lambda *a, **k: cq._RosterSnapshot(
                settled_texts, True, snap_children, snap_radios))

        seen = {}

        def _panes(children):
            seen["n"] = len(children)
            return []
        monkeypatch.setattr(cq, "_find_text_panes", _panes)

        _t, _h, roster = cq._extract_consult_text(1, {})

        assert roster == settled_texts
        assert seen["n"] == len(snap_children), (
            "文字面板是從另一棵樹找的 → 又不同源了")

    def test_unpacking_it_as_a_pair_fails_loudly(self):
        """★舊寫法必須當場爆掉，不可以靜默拿到一半★

        `texts, stable = _await_stable_roster(...)` 若還能跑，children/radios
        就會被留在別的時間點 —— 那正是 P1-08。
        """
        snap = cq._RosterSnapshot(["甲"], True, [], [])
        try:
            _a, _b = snap
        except ValueError:
            return
        raise AssertionError("兩元素解包居然成功了 → 舊呼叫端會靜默拿到半份資料")


class TestTheScreenshotMatchesTheList:
    """★[2026-08-05 外審第 4 輪 P1-09]★ 截圖要拍在清單穩定【之後】。

    原本是固定 `time.sleep(1.8)` 之後截圖，而清單是在那之【後】才等到穩定的：
    信裡的清單是 Tn 的、附圖是 T0+1.8s 的 —— 醫師收到兩份互相矛盾的證據。
    """

    def test_capture_happens_after_settling(self):
        order = []

        def _settle(_h):
            order.append("settle")
            return cq._RosterSnapshot(["甲1111111"], True, [], [])

        def _capture(_h):
            order.append("capture")
            return "IMG"

        img, snap = cq._capture_with_settled_roster(
            1, capture=_capture, settle=_settle,
            read=lambda _h: ([], [], ["甲1111111"]))

        assert order == ["settle", "capture"], order
        assert img == "IMG" and snap.stable is True

    def test_a_list_that_changes_after_the_shot_is_not_trusted(self):
        """截圖之後清單又變了 → 這份快照不代表圖上那一刻 → 走 fail-open。"""
        _img, snap = cq._capture_with_settled_roster(
            1, capture=lambda _h: "IMG",
            settle=lambda _h: cq._RosterSnapshot(["甲1111111"], True, [], []),
            read=lambda _h: ([], [], ["甲1111111", "乙2222222"]))

        assert snap.stable is False, (
            "★截圖後清單變了卻照樣更新基準★ 圖與清單對不起來，還會漏寄")
        assert snap.texts == ["甲1111111"], "顯示用的內容不該被改掉"

    def test_a_readback_failure_does_not_invalidate_the_snapshot(self):
        """回讀失敗 ≠ 清單變了。不可以因為讀不到就把好好的一輪判成 unstable
        （那會變成每輪 fail-open 寄信）。"""
        def _boom(_h):
            raise OSError("列舉失敗")

        _img, snap = cq._capture_with_settled_roster(
            1, capture=lambda _h: "IMG",
            settle=lambda _h: cq._RosterSnapshot(["甲1111111"], True, [], []),
            read=_boom)

        assert snap.stable is True

    def test_an_already_unstable_snapshot_is_left_alone(self):
        """本來就不穩定 → 不必也不該再回讀一次（省一次列舉）。"""
        read_calls = {"n": 0}

        def _read(_h):
            read_calls["n"] += 1
            return ([], [], [])

        _img, snap = cq._capture_with_settled_roster(
            1, capture=lambda _h: "IMG",
            settle=lambda _h: cq._RosterSnapshot(["甲"], False, [], []),
            read=_read)

        assert snap.stable is False and read_calls["n"] == 0


def test_both_capture_sites_settle_before_shooting():
    """★接線★ 兩條路徑（隱藏桌面、SW_HIDE 後備）都要走同一個順序。

    只修其中一條，另一條照樣拍到半份清單 —— 而上面那些測試會全綠。
    同時確認固定的 `time.sleep(1.8)` 已經被拿掉（它就是舊順序的殘骸）。

    ★判準用 AST，不是找字串★（這個形狀在本 session 是第四次）
    第一版寫成 `assert "time.sleep(1.8)" not in src` —— 而我在同一段程式碼的
    【註解裡】解釋了「固定的 time.sleep(1.8) 已被取代」。測試被自己的說明文字餵飽，
    當場誤紅。要看的是真的有沒有那個【呼叫】。
    """
    import ast
    import inspect
    import textwrap

    checked = 0
    # 兩條：常駐 session 的每輪查詢、以及建不出隱藏桌面時的 SW_HIDE 後備模式
    for fn in (cq._query_cycle, cq._run_with_sw_hide):
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
        names = {n.func.id for n in calls if isinstance(n.func, ast.Name)}
        if not (names & {"capture_window_image", "_capture_with_settled_roster"}):
            continue
        checked += 1
        assert "_capture_with_settled_roster" in names, (
            f"{fn.__name__} 仍然先截圖再等清單")
        assert "capture_window_image" not in names, (
            f"{fn.__name__} 還有一個沒有等清單的截圖")
        # 真的有沒有 `time.sleep(1.8)` 這個【呼叫】（不是註解裡提到它）
        for n in calls:
            if (isinstance(n.func, ast.Attribute) and n.func.attr == "sleep"
                    and n.args and isinstance(n.args[0], ast.Constant)
                    and n.args[0].value == 1.8):
                raise AssertionError(
                    f"{fn.__name__} 還留著固定 1.8 秒的等待（舊順序的殘骸）")
    assert checked == 2, f"只檢查到 {checked} 條截圖路徑（應為 2 條）"
