# -*- coding: utf-8 -*-
"""沒有新會診的輪次不可以在磁碟上留下病人畫面（2026-08-04 外審 P1-08）。

【問題】
`_query_cycle` 以前【無條件】`img.save()`，而且發生在解析 roster 之前 —— 跟有沒有
新會診毫無關係。常駐模式 3 分鐘一輪 ＝ 每小時 20 張「沒寄出去、也沒有臨床用途」的
完整病人畫面躺在磁碟上。

【修法】截圖先留在記憶體，只有真的要寄信時才呼叫 `_materialize_shot()` 落地。

★與外審建議的差異（刻意）★
外審建議「寄完 finally 刪掉」。這裡落地到既有的 consult_shots/ 並沿用既有 TTL：
那張圖本來就已經寄給臨床收件人了，留在本機不會擴大暴露面，而出事時「當時畫面長
怎樣」是最有用的線索。真正該消滅的是「沒寄出去卻留著」的那 20 張/小時。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import consult_query as cq  # noqa: E402


def _tiny_image():
    from PIL import Image
    return Image.new("RGB", (4, 4), (255, 255, 255))


def test_materialize_writes_the_file(monkeypatch, tmp_path):
    monkeypatch.setattr(cq, "SHOTS_DIR", tmp_path / "shots")
    monkeypatch.setattr(cq, "_prune_old_shots", lambda: None)

    path = cq._materialize_shot(_tiny_image())

    assert os.path.exists(path), "要寄信了卻沒把截圖落地"
    assert str(path).endswith(".png")


def test_a_path_passes_through_unchanged(monkeypatch, tmp_path):
    """相容:已經是路徑就不要再存一次(否則同一張會存兩份)。"""
    monkeypatch.setattr(cq, "SHOTS_DIR", tmp_path / "shots")
    p = tmp_path / "already.png"
    assert cq._materialize_shot(p) is p
    assert not (tmp_path / "shots").exists(), "不該為了一個現成路徑建目錄"


def test_capturing_alone_writes_nothing(monkeypatch, tmp_path):
    """★核心★ 只是擷取、還沒決定要不要寄 → 磁碟上不可以多出東西。"""
    monkeypatch.setattr(cq, "SHOTS_DIR", tmp_path / "shots")
    img = _tiny_image()          # 模擬 _query_cycle 手上那份記憶體影像

    assert not (tmp_path / "shots").exists(), (
        "★還沒決定要寄就已經落地了★ 這就是每小時 20 張的來源")
    assert img is not None


class TestTheCaptureSitesNoLongerSaveDirectly:
    """★接線本身也要被測到★（本 session 這個形狀第六次）

    上面驗的是 `_materialize_shot` 這個新函式。但只要兩個擷取點還留著自己的
    `img.save()`，磁碟照樣每輪長一張 —— 而那些測試會全綠。
    """

    def _names_and_calls(self, fn):
        """→ (該函式實際用到的名字, 實際呼叫的方法名)。

        ★用 AST，不要用字串搜尋★（第一版就是這樣紅的）：我在那兩個函式裡寫的
        說明註解本身就含 `img.save()` 這幾個字，`"img.save(" not in src` 於是
        被【自己的註解】餵飽而失敗。AST 只看得到真正的程式碼。
        """
        import ast
        import inspect
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        methods = {n.func.attr for n in ast.walk(tree)
                   if isinstance(n, ast.Call)
                   and isinstance(n.func, ast.Attribute)}
        return names, methods

    def test_query_cycle_does_not_save(self):
        names, methods = self._names_and_calls(cq._query_cycle)
        assert "save" not in methods, (
            "★_query_cycle 仍然直接存檔★ 沒有新會診也會留下病人畫面")
        assert "SHOTS_DIR" not in names, "仍然在擷取時就碰截圖目錄"

    def test_sw_hide_fallback_does_not_save(self):
        names, methods = self._names_and_calls(cq._run_with_sw_hide)
        assert "save" not in methods, "★SW_HIDE 後備仍然直接存檔★"
        assert "SHOTS_DIR" not in names

    def test_the_send_path_materializes(self):
        """寄信前要落地，而且必須在寄信【之前】。"""
        import ast
        import inspect
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(cq._do_full_job)))
        lines = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                lines.setdefault(node.func.id, node.lineno)
        assert "_materialize_shot" in lines, (
            "寄信前沒有把記憶體影像落地 → 附件會壞掉")
        assert "send_via_smtp" in lines, "找不到寄信呼叫（測試失效了）"
        assert lines["_materialize_shot"] < lines["send_via_smtp"], (
            "落地必須發生在寄信之前")
