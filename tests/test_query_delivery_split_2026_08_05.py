# -*- coding: utf-8 -*-
"""查詢與寄送要分開：SMTP 重試不得重跑 HIS（外審第 5 輪 P1-04 / P2-05）。

【上一批只修了一半】
批次 P1-10 讓「寄信失敗不再收掉 HIS session」，log 也寫成「只重試寄信」。
但 retry loop 的下一次 attempt 仍然從頭執行 `run_consult_flow(...)`：

  * 再開一次會診畫面、再擷取一次 roster、再逐位點選病人
  * 再產生一張截圖落地（三次 attempt ＝ 三張病人畫面躺在磁碟上）
  * 再組一封信 —— 而第二次查到的清單可能已經不一樣

所以那句 log 與行為不符：它不是「只重試寄信」，只是「重新查詢時不先登出」。

而且 SMTP 有一種很常見的失敗形狀：**伺服器已經收下，只是回應逾時**。
第二次 attempt 用新的 Message-ID 再寄一次 → 收件人收到兩封內容還不一樣的信。
"""
import ast
import inspect
import os
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import consult_query as cq  # noqa: E402

_TREE = ast.parse(textwrap.dedent(inspect.getsource(cq._do_full_job)))


def _calls_to(name, tree=None):
    return [n for n in ast.walk(tree or _TREE)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == name]


def _guard_of(call_name, var_name):
    """包住這個呼叫、而且條件提到 var_name 的 if 節點。"""
    for node in ast.walk(_TREE):
        if not isinstance(node, ast.If):
            continue
        if not _calls_to(call_name, node):
            continue
        names = {n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)}
        if var_name in names:
            return node
    return None


class TestTheHisQueryHappensOnce:

    def test_the_query_is_guarded_by_a_hoisted_result(self):
        """★核心★ `run_consult_flow` 必須被 `his_result is None` 守著。

        沒有守衛 = 每個 attempt 都重查一次 HIS。
        """
        node = _guard_of("run_consult_flow", "his_result")
        assert node is not None, (
            "★每次 attempt 都會重跑 run_consult_flow★ 寄信失敗會害 HIS 被重複操作")

    def test_the_guard_runs_the_query_only_when_there_is_no_result(self):
        """求值而不是比對形狀（`if X is None` 與 `if X is not None` 的教訓）。"""
        node = _guard_of("run_consult_flow", "his_result")
        for has_result, should_query in ((None, True), ("已經查到了", False)):
            taken = eval(  # noqa: S307 - 受控:只求值本檔案自己的守衛條件
                compile(ast.Expression(body=node.test), "<guard>", "eval"),
                {"__builtins__": {}}, {"his_result": has_result})
            branch = node.body if taken else node.orelse
            ran = bool(_calls_to("run_consult_flow",
                                 ast.Module(body=branch, type_ignores=[])))
            assert ran is should_query, (
                f"his_result={has_result!r} 時 {'應該' if should_query else '不應'}查詢")

    def test_the_result_is_hoisted_out_of_the_attempt_loop(self):
        """`his_result` 要在迴圈【外面】初始化，否則每個 attempt 又是 None。"""
        loop = next(n for n in ast.walk(_TREE)
                    if isinstance(n, ast.For)
                    and getattr(n.target, "id", "") == "attempt")
        inside = {t.id for n in ast.walk(loop) if isinstance(n, ast.Assign)
                  for t in n.targets if isinstance(t, ast.Name)}
        assert "his_result" in inside, "測試失效了(迴圈裡應該有指派)"
        # 迴圈之前必須有一次 `his_result = None`
        before = [n for n in ast.walk(_TREE)
                  if isinstance(n, ast.Assign) and n.lineno < loop.lineno
                  and any(getattr(t, "id", "") == "his_result" for t in n.targets)]
        assert before, "★his_result 在迴圈裡才初始化★ 每個 attempt 都會重查"


class TestTheMailIsBuiltOnce:

    def test_the_artifact_is_guarded(self):
        node = _guard_of("_DeliveryArtifact", "delivery")
        assert node is not None, "★每個 attempt 都重組一封新的信★"

    def test_the_screenshot_is_materialised_inside_that_guard(self):
        """★截圖只落地一次★ 三次 attempt 不可以生出三張病人畫面。"""
        node = _guard_of("_DeliveryArtifact", "delivery")
        assert _calls_to("_materialize_shot", node), (
            "截圖落地不在 delivery 守衛內 → 每個 attempt 都會多一張")
        # 而且整個函式裡只有那一處
        assert len(_calls_to("_materialize_shot")) == 1

    def test_the_send_uses_the_artifact_fields(self):
        """寄送必須用 artifact 的欄位，不可以用當輪重算的區域變數。"""
        for fn in ("send_via_smtp", "send_via_outlook"):
            calls = _calls_to(fn)
            assert calls, f"找不到 {fn}"
            for c in calls:
                srcs = {n.value.id for n in ast.walk(c)
                        if isinstance(n, ast.Attribute)
                        and isinstance(n.value, ast.Name)}
                assert "delivery" in srcs, (
                    f"{fn} 沒有用 delivery 的欄位 → 重試會寄出重算過的內容")

    def test_the_artifact_is_frozen(self):
        """組好之後不可以被改 —— 由型別系統保證，不是靠紀律。"""
        art = cq._DeliveryArtifact(
            recipients=("a@b.c",), subject="s", text_body="t",
            html_body="h", attachment=None, message_id="<x@y>")
        try:
            art.subject = "改掉"
        except Exception:
            return
        raise AssertionError("delivery artifact 可以被改 → 重試可能寄出不同內容")


class TestTheMessageIdIsStableAcrossRetries:

    def test_the_artifact_carries_a_message_id(self):
        assert "message_id" in cq._DeliveryArtifact.__dataclass_fields__

    def test_a_new_id_looks_like_a_message_id(self):
        mid = cq._new_message_id()
        assert mid.startswith("<") and mid.endswith(">") and "@" in mid

    def test_smtp_send_passes_it_through(self):
        """★接線★ artifact 帶著 Message-ID 但沒傳下去 = 白帶。"""
        got = {}

        def _fake_send_mail(**kw):
            got.update(kw)
        import cmuh_common.smtp_mail as sm
        real = sm.send_mail
        sm.send_mail = _fake_send_mail
        try:
            cq.send_via_smtp(None, "主旨", "內文", ["a@b.c"],
                             message_id="<fixed@example>")
        finally:
            sm.send_mail = real
        assert got.get("message_id") == "<fixed@example>", (
            "Message-ID 沒有傳到 send_mail → 每次重試都是一封新的信")

    def test_the_builder_honours_it(self):
        """MIME 真的用了指定的 Message-ID（不是自己再產一個）。"""
        from cmuh_common.smtp_mail import _build_message
        msg = _build_message("a@b.c", "寄件人", ["x@y.z"], "s", "b",
                             message_id="<fixed@example>")
        assert msg["Message-ID"] == "<fixed@example>"

    def test_it_still_generates_one_when_not_given(self):
        """★反方向:沒指定時仍要有 Message-ID★（少了它會被當垃圾信）。"""
        from cmuh_common.smtp_mail import _build_message
        msg = _build_message("a@b.c", "寄件人", ["x@y.z"], "s", "b")
        assert msg["Message-ID"] and "@" in msg["Message-ID"]


class TestAnUndeliveredScreenshotIsRemoved:
    """★P2-05／自查 P1-C★ 沒寄出去的病人截圖不留在磁碟上。

    `_materialize_shot` 的既有取捨是「已經寄給臨床收件人的圖留著當線索」——
    那個理由只對【寄成功】成立。寄失敗的那張沒有人看過、也沒有臨床用途。
    """

    def _artifact(self, path):
        return cq._DeliveryArtifact(
            recipients=("a@b.c",), subject="s", text_body="t", html_body="h",
            attachment=path, message_id="<x@y>")

    def test_the_file_is_deleted(self, tmp_path):
        p = Path(tmp_path) / "consult_x.png"
        p.write_bytes(b"fake png")
        cq._discard_undelivered_shot(self._artifact(p))
        assert not p.exists(), "★沒寄出去的病人截圖留在磁碟上★"

    def test_a_missing_file_is_not_an_error(self, tmp_path):
        cq._discard_undelivered_shot(
            self._artifact(Path(tmp_path) / "never_created.png"))

    def test_no_artifact_is_fine(self):
        cq._discard_undelivered_shot(None)

    def test_a_non_path_attachment_is_left_alone(self):
        """還沒落地(仍是記憶體影像)或 None → 沒有檔案要刪。"""
        cq._discard_undelivered_shot(self._artifact(None))
        cq._discard_undelivered_shot(self._artifact("不是 Path"))

    def test_the_giveup_branch_calls_it(self):
        """★接線★ 放棄那一條路徑要真的刪。"""
        assert _calls_to("_discard_undelivered_shot"), (
            "整輪放棄時沒有清掉未送達的截圖")

    def test_it_is_not_called_on_the_success_path(self):
        """★反方向:寄成功的圖要留著★（出事時「當時畫面長怎樣」是最有用的線索）。

        成功路徑以 `return` 結束；刪除只能出現在放棄那一段。
        """
        # ★要取【最內層】那個 try★ 外層還有一個 try 把整個 attempt 迴圈
        #   （連同它的 except）都包起來，用它的 body 去檢查會把 except 裡的
        #   刪除也算進來而誤紅。以 lineno 最大者 = 最晚開始 = 最內層。
        candidates = [n for n in ast.walk(_TREE) if isinstance(n, ast.Try)
                      and any(isinstance(c, ast.Call)
                              and isinstance(c.func, ast.Name)
                              and c.func.id == "send_via_smtp"
                              for c in ast.walk(
                                  ast.Module(body=n.body, type_ignores=[])))]
        assert candidates, "找不到寄信那一段 try"
        inner = max(candidates, key=lambda n: n.lineno)
        body_calls = {n.func.id for n in ast.walk(
            ast.Module(body=inner.body, type_ignores=[]))
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert "_discard_undelivered_shot" not in body_calls, (
            "★寄成功的路徑上也刪圖★ 出事時就沒有線索了")
