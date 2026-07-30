# -*- coding: utf-8 -*-
"""[2026-07-30 實機] 會診連續失敗:登入沒完成,而我們花 120 秒點一個關不掉的通知。

實機告警的診斷輸出:

    期間按了 200 次「確認」(不同通知視窗 1 個,最後一個 hwnd=406676);
    當下看到的視窗:… TFrmLogin(vis=1,en=…)

三件事同時成立:
  1. 列表裡【完全沒有 TFMNewMain】,而 TFrmLogin 還可見 → 登入根本沒完成。
  2. 200 次點擊全打在【同一個 hwnd】(不同視窗 1 個),200 x 0.6s = 整整 120 秒
     的預算 → 那個視窗沒有被關掉。
  3. `click_button` 是純 PostMessage(BM_CLICK)、沒有回讀;而本 repo 自己記過
     「Delphi modal form 只是 Hide 不是 Destroy」——`visible_only=False` 那條
     路徑上,【已經關掉】的通知照樣找得到,於是永遠點下去。

我 2026-07-29 修的是「不該因為有通知就拒絕主畫面」,那條沒錯;但沒處理
「登入沒完成」與「點了沒反應」——於是症狀從『刷滿 log』換成『刷滿點擊』,
最後仍然回報一句無從下手的「等不到主畫面」。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import consult_query as cq  # noqa: E402


class _Win:
    """一組假視窗。cls -> [hwnd];enabled/visible 可個別設定。"""

    def __init__(self):
        self.wins = {}          # hwnd -> (cls, visible, enabled)
        self.clicked = []

    def add(self, hwnd, cls, visible=True, enabled=True):
        self.wins[hwnd] = (cls, visible, enabled)

    def install(self, monkeypatch, *, on_click=None):
        def _find(cls=None, title_prefix=None, pids=None, visible_only=False):
            out = []
            for h, (c, vis, _en) in sorted(self.wins.items()):
                if cls is not None and c != cls:
                    continue
                if visible_only and not vis:
                    continue
                out.append(h)
            return out

        monkeypatch.setattr(cq, "find_windows", _find)
        monkeypatch.setattr(cq, "find_child",
                            lambda h, c, t: h + 1000)

        def _click(btn):
            self.clicked.append(btn)
            if on_click:
                on_click(self, btn)
        monkeypatch.setattr(cq, "click_button", _click)
        monkeypatch.setattr(cq.win32gui, "IsWindowEnabled",
                            lambda h: self.wins.get(h, ("", 1, 1))[2])
        monkeypatch.setattr(cq.win32gui, "IsWindowVisible",
                            lambda h: self.wins.get(h, ("", 1, 1))[1])
        monkeypatch.setattr(cq.win32gui, "GetClassName",
                            lambda h: self.wins.get(h, ("?", 1, 1))[0])
        cq.running.set()


def test_a_stuck_notice_is_not_clicked_forever(monkeypatch):
    """★核心★ 同一個通知按不掉就該停手,不可把 120 秒預算全部耗在它身上。"""
    w = _Win()
    w.add(100, cq.NOTICE_CLASS, visible=True)       # 永遠不會消失
    w.install(monkeypatch)

    with pytest.raises(RuntimeError):
        cq._wait_main_window_after_login(set(), visible_only=False,
                                        timeout_sec=3.0)

    assert len(w.clicked) <= cq._MAX_CLICKS_PER_NOTICE + 1, (
        f"按了 {len(w.clicked)} 次 —— 應在 {cq._MAX_CLICKS_PER_NOTICE} 次後停手")


def test_a_visible_login_window_is_reported_as_such(monkeypatch):
    """★實機真正的狀況★ 登入視窗還在 = 登入沒完成,那是完全不同的一件事;
    原本一律說「等不到住院醫囑主畫面」,會讓人往錯的方向查。"""
    w = _Win()
    w.add(100, cq.NOTICE_CLASS, visible=True)
    w.add(200, cq.LOGIN_CLASS, visible=True)
    w.install(monkeypatch)

    with pytest.raises(RuntimeError) as e:
        cq._wait_main_window_after_login(set(), visible_only=False,
                                         timeout_sec=2.0)

    msg = str(e.value)
    assert "登入沒有完成" in msg, msg
    assert "帳號密碼" in msg, "要說出下一步該查什麼"


def test_a_normal_notice_is_still_dismissed(monkeypatch):
    """★不可矯枉過正★ 真的按得掉的通知仍要按掉,而且主畫面要被接受。"""
    w = _Win()
    w.add(100, cq.NOTICE_CLASS, visible=True)
    w.add(300, cq.MAIN_CLASS, visible=True, enabled=False)

    def _on_click(self, _btn):
        self.wins.pop(100, None)                    # 通知真的關掉了
        self.wins[300] = (cq.MAIN_CLASS, True, True)   # 主畫面解除封鎖

    w.install(monkeypatch, on_click=_on_click)

    got = cq._wait_main_window_after_login(set(), visible_only=False,
                                          timeout_sec=5.0)
    assert got == 300
    assert len(w.clicked) == 1


def test_a_blocked_main_window_is_not_accepted(monkeypatch):
    """主畫面存在但被 modal 擋住(disabled)→ 不可當成可操作。"""
    w = _Win()
    w.add(300, cq.MAIN_CLASS, visible=True, enabled=False)
    w.install(monkeypatch)

    with pytest.raises(RuntimeError):
        cq._wait_main_window_after_login(set(), visible_only=False,
                                         timeout_sec=2.0)


# ─── [外審] 登入沒完成不可被重試 ──────────────────────────────────────────
def test_login_not_completed_is_a_dedicated_exception(monkeypatch):
    """★核心★ 通用 RuntimeError 會被 _do_full_job 的 except Exception 吃掉並重試 ——
    retry_count 預設 3、連續失敗告警門檻又是 3 個任務,使用者收到信之前同一組帳密
    可能已經被送出 9 次。我原本只把訊息改對,卻沒防到自己聲稱在防的風險。"""
    w = _Win()
    w.add(200, cq.LOGIN_CLASS, visible=True)
    w.install(monkeypatch)

    with pytest.raises(cq.LoginNotCompleted) as e:
        cq._wait_main_window_after_login(set(), visible_only=False,
                                         timeout_sec=2.0)
    assert "不再重試" in str(e.value), "訊息要說出它不會重試"


def test_the_retry_loop_skips_backoff_but_still_finalizes():
    """★[外審第2輪] 不可重試,但【仍要走完終局收尾】★

    我第一版另開一個 except 分支直接 return —— 那會跳過
    `_release_trigger_dedup` 與 `_send_failure_notice_async`:email 觸發的醫師
    會被去重卡住(5 分鐘內重發無效)、又收不到失敗通知,只能乾等一個永遠不會來的
    結果。修一個洞不可以開另一個。
    故正解是沿用同一條 except,只把【backoff 重試】那一段略過。
    """
    import inspect
    src = inspect.getsource(cq._do_full_job)
    assert "except LoginNotCompleted" not in src,         "不可另開分支早退(會跳過終局收尾)"
    # [2026-07-30 外審 P2-01] JobSuperseded 也走同一條 fatal 路徑 → 判定式從單一
    # 型別變成 tuple。這裡守的是「fatal 由 isinstance 判定、且 LoginNotCompleted
    # 在其中」，不是那一行的逐字長相。
    assert "fatal = isinstance(e, (" in src
    assert "LoginNotCompleted" in src[src.index("fatal = isinstance(e, ("):][:120]
    assert "if attempt < retry_count and not fatal:" in src,         "只略過 backoff 重試,其餘照原路走"
    # 終局收尾仍在同一個 else 分支裡
    i_else = src.index("if attempt < retry_count and not fatal:")
    tail = src[i_else:]
    for must in ("_note_job_failure", "_release_trigger_dedup",
                 "_send_failure_notice_async", "_cleanup_orphan_systemftp"):
        assert must in tail, f"終局收尾漏了 {must}"


def test_the_give_up_branch_breaks_out_of_the_loop():
    """★[外審第3輪] 收尾完必須結束迴圈★

    原本沒有 break 是因為 else 只在【最後一次】attempt 才進得來;fatal 讓它可能在
    第 1 次就進來 —— 沒 break 就會回頭再送一次帳密,而且把失敗通知再寄一遍。
    (我上一版正是這樣:自以為擋掉了重試,實際上擋掉的只有 backoff。)
    """
    import inspect
    src = inspect.getsource(cq._do_full_job)
    give_up = src[src.index("已重試 %d 次仍失敗"):]
    assert "break" in give_up, "放棄分支結束時必須 break"
    # break 要在收尾之後,不可搶在前面
    assert give_up.index("_send_failure_notice_async") < give_up.index("break")
