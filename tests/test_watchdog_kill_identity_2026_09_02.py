# -*- coding: utf-8 -*-
"""[外審第五輪 R5-P3-01] 破壞性動作需要身分證據 —— 後備路徑也一樣。

watchdog 的首選路徑(PID 檔)已經做得很好:app_id / PID / create_time /
executable 驗過,而且 kill 前開 handle 把 PID 釘住、釘住期間再驗一次。
★但 PID 檔不可用時的 cmdline 後備,是拿到裸 PID 就 `taskkill /F /T`★
(連子行程一起殺)。那正是首選路徑花很多力氣解掉的 check-then-act 競態:

    T0 列舉:PID 1234 的 cmdline 命中 keyword
    T1 目標自己結束
    T2 Windows 把 1234 配給另一支程式
    T3 我們強殺 1234 —— 殺到的是 T2 那一支。

而且比對是★沒有邊界的子字串★(`keyword in cmdline`),只證明「命令列某處
出現這串字」,不證明「實際在跑的就是那支程式」。

★修法把兩件事拆開(審查原話:fallback discovery ≠ authorization to kill)★
  * 發現:仍用寬鬆的子字串(不廢掉半死救援 —— 那是外審第 3 回的定案);
  * 動手:開 handle 釘住 → 用精確判準重驗「它此刻還是那支程式嗎」→ 才殺。
    驗不出來一律不殺(下一輪會再來)。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import pidfile as pf  # noqa: E402
from cmuh_common import watchdog_core as wc  # noqa: E402

KW = "中國醫皮膚科主程式"
B = chr(92)          # 反斜線(避免在原始碼裡寫跳脫序列)


def _cmd(*tokens):
    return " ".join(tokens)


# ══ 精確判準:發現用寬鬆的,動手用嚴格的 ═══════════════════════════════════
class TestTheExactIdentityPredicate:
    def test_the_real_launcher_matches(self):
        cl = _cmd('"C:%sPython%spythonw.exe"' % (B, B),
                  '"C:%sapp%ssrc%s%s.pyw"' % (B, B, B, KW), "--background")
        assert wc._cmdline_is_target(cl, KW) is True

    def test_merely_mentioning_the_name_does_not_match(self):
        """★核心★ 下游是 `taskkill /F /T` —— 「命令列提到那個名字」不足以
        授權殺掉一支程式連同它的子行程。"""
        cl = _cmd("python.exe", "修檔工具.py", "%s的備份.txt" % KW)
        assert wc._cmdline_is_target(cl, KW) is False

    def test_a_sibling_program_does_not_match(self):
        """三支自家程式都是 pythonw.exe —— 認錯就是殺錯人。"""
        cl = '"C:%sapp%s中國醫皮膚科打卡程式.pyw"' % (B, B)
        assert wc._cmdline_is_target(cl, KW) is False

    def test_paths_with_spaces_still_match(self):
        """★不可以矯枉過正★:路徑含空白是常態(Program Files),
        引號內不可以被切開,否則救援路徑整條失效。"""
        cl = '"C:%sMy Apps%s%s.pyw"' % (B, B, KW)
        assert wc._cmdline_is_target(cl, KW) is True

    def test_case_and_extension_are_surface_differences(self):
        assert wc._cmdline_is_target("pythonw.exe C:%sapp%s%s" % (B, B, KW), KW)
        assert wc._cmdline_is_target('"%s.PYW"' % KW, KW)

    def test_an_empty_keyword_never_matches(self):
        """★空集合不算通過★:設定漏了 process_match 就變成「什麼都是目標」。

        ★反例要真的靠這條規則分勝負★:第一版我餵
        `("pythonw.exe anything.pyw", "")` —— 但沒有一個 basename 等於空字串,
        所以拿掉守衛也不會命中,突變假綠燈。以路徑分隔字元結尾的引數
        (basename 是空的)才量得到:少了守衛它會match 空 keyword。
        """
        ends_with_sep = '"C:%sapp%s"' % (B, B)
        assert wc._cmdline_is_target(ends_with_sep, "") is False
        # 對照組:同一條命令列配上真的 keyword 仍然不命中(不是永遠 False)
        assert wc._cmdline_is_target('"C:%sapp%s%s.pyw"' % (B, B, KW), KW)


# ══ 審查點名必補的兩條競態測試 ═══════════════════════════════════════════
class TestDiscoveryIsNotAuthorizationToKill:
    @pytest.fixture(autouse=True)
    def _no_real_kill(self, monkeypatch):
        self.killed = []
        monkeypatch.setattr(wc, "kill_pid",
                            lambda pid: self.killed.append(pid) or True)
        # 釘住成功(真的 OpenProcess 對合成 PID 一定失敗)
        monkeypatch.setattr(pf, "_open_process_pin", lambda pid: 1)
        yield

    def test_a_recycled_pid_is_not_killed(self, monkeypatch):
        """★核心競態★ 列舉時是目標,動手前那個 PID 已經被回收給別的程式 →
        ★絕不可以殺★(那會連它的子行程一起強殺)。"""
        monkeypatch.setattr(wc, "_cmdline_tokens_of_pid_now",
                            lambda pid: ["pythonw.exe", "完全不相干的程式.pyw"])
        monkeypatch.setattr(pf, "pid_looks_like_python", lambda pid: True)
        assert wc._kill_unverified_source(1234, KW) is False
        assert self.killed == [], "★PID 被回收後仍然強殺★:" + str(self.killed)

    def test_a_still_valid_target_is_killed(self):
        """★對照組★:重驗過了就要真的殺 —— 不然半死救援整條失效
        (外審第 3 回定案:PID 檔壞掉時不做 recovery 會降低可用性)。"""
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(wc, "_cmdline_tokens_of_pid_now",
                       lambda pid: ["pythonw.exe", "C:%sapp%s%s.pyw" % (B, B, KW)])
            mp.setattr(pf, "pid_looks_like_python", lambda pid: True)
            assert wc._kill_unverified_source(1234, KW) is True
        assert self.killed == [1234]

    def test_an_unpinnable_pid_is_not_killed(self, monkeypatch):
        """★釘不住就不動手★:釘不住代表無法保證「動手時它還是同一個」。

        ★反例要只靠這條規則分勝負★:第一版沒有樁掉 `pid_looks_like_python`,
        於是合成 PID 1234 在【身分那一關】就被擋掉了 —— 把釘住的守衛整個
        拿掉也照樣不殺,突變假綠燈。這裡把其餘關卡全部放行,只留釘住。
        """
        monkeypatch.setattr(pf, "_open_process_pin", lambda pid: 0)
        monkeypatch.setattr(pf, "pid_looks_like_python", lambda pid: True)
        monkeypatch.setattr(wc, "_cmdline_tokens_of_pid_now",
                            lambda pid: ["pythonw.exe", "%s.pyw" % KW])
        assert wc._kill_unverified_source(1234, KW) is False
        assert self.killed == []

    def test_an_unknowable_cmdline_is_not_killed(self, monkeypatch):
        """★查不出來 ≠ 是目標★(也 ≠ 不是目標)—— 不能證明就不動手。"""
        monkeypatch.setattr(wc, "_cmdline_tokens_of_pid_now", lambda pid: None)
        monkeypatch.setattr(pf, "pid_looks_like_python", lambda pid: True)
        assert wc._kill_unverified_source(1234, KW) is False
        assert self.killed == []

    def test_a_non_python_pid_is_not_killed(self, monkeypatch):
        """PID 被配給一支完全無關的程式(連 python 都不是)→ 最明顯的誤殺。"""
        monkeypatch.setattr(pf, "pid_looks_like_python", lambda pid: False)
        monkeypatch.setattr(wc, "_cmdline_tokens_of_pid_now",
                            lambda pid: ["pythonw.exe", "%s.pyw" % KW])
        assert wc._kill_unverified_source(1234, KW) is False
        assert self.killed == []

    def test_without_a_keyword_the_old_behaviour_is_kept_but_audible(
            self, caplog):
        """★唯一還沒有身分可驗的情況★:呼叫端沒給 keyword。
        維持既有行為(不廢掉救援),但必須說出來 —— 這是一次
        「在無法驗證身分的情況下 /F /T」。"""
        with caplog.at_level("WARNING"):
            assert wc._kill_unverified_source(1234, "") is True
        assert self.killed == [1234]
        assert any("沒有可驗證的身分" in r.getMessage()
                   for r in caplog.records), "★靜悄悄地強殺★"


class TestTheRecheckMustNotReadTheOldObservation:
    def test_the_cmdline_lookup_bypasses_the_cache(self, monkeypatch):
        """★重驗要看【此刻】的事實★:吃到「查詢當下那一份」快取的話,
        重驗只是把同一個觀測再讀一次 —— 競態原封不動。"""
        seen = []

        def _fake_read(*, force=False):
            seen.append(force)
            return ""
        monkeypatch.setattr(wc, "_read_wmic_python_process_csv", _fake_read)

        # ★生產的失敗形狀★:admin 行程用 psutil 讀不到 cmdline(AccessDenied)
        #   —— 那正是 WMIC 後備存在的理由。整個模組換掉會弄壞 pytest 收尾,
        #   只讓 `Process` 拋就夠了。
        import psutil
        monkeypatch.setattr(
            psutil, "Process",
            lambda _pid: (_ for _ in ()).throw(psutil.AccessDenied(_pid)))
        wc._cmdline_tokens_of_pid_now(1234)
        assert seen and all(seen), (
            "★重驗吃到快取★ 那份正是「發現」當下的觀測,證明不了此刻的事實")


class TestBothBranchesOfTheKillPathVerify:
    """★接線要用行為驗★:`kill_pids_verified` 有兩條分支(有/沒有 pid_name),
    只看「原始碼裡有沒有出現 `_kill_unverified_source`」的話,改壞其中一條
    照樣綠 —— 我第一版就是這樣,把「後備分支又改回裸殺」的突變放過去了。"""

    @pytest.fixture(autouse=True)
    def _seams(self, monkeypatch):
        self.killed = []
        monkeypatch.setattr(wc, "kill_pid",
                            lambda pid: self.killed.append(pid) or True)
        monkeypatch.setattr(pf, "_open_process_pin", lambda pid: 1)
        monkeypatch.setattr(pf, "pid_looks_like_python", lambda pid: True)
        # 這個 PID 此刻已經是別的程式(= 被回收)
        monkeypatch.setattr(wc, "_cmdline_tokens_of_pid_now",
                            lambda pid: ["pythonw.exe", "別的程式.pyw"])
        monkeypatch.setattr(wc, "_PID_FROM_PIDFILE", {}, raising=False)
        yield

    def test_the_no_pidfile_branch_verifies(self):
        """`pid_name` 是空的那條(呼叫端沒有 PID 檔名)。"""
        assert wc.kill_pids_verified([1234], "", KW) == []
        assert self.killed == [], "★沒有 pid_name 的分支仍然裸殺★"

    def test_the_fallback_branch_verifies(self):
        """有 pid_name、但這個 PID 不是從 PID 檔驗來的(cmdline 後備)。"""
        assert wc.kill_pids_verified([1234], "autoclock", KW) == []
        assert self.killed == [], "★後備分支仍然裸殺★"

    def test_a_verified_target_is_still_killed(self, monkeypatch):
        """★對照組★:重驗過就要真的殺(否則救援整條失效)。"""
        monkeypatch.setattr(wc, "_cmdline_tokens_of_pid_now",
                            lambda pid: ["pythonw.exe", "%s.pyw" % KW])
        assert wc.kill_pids_verified([1234], "autoclock", KW) == [1234]


def test_the_kill_path_is_wired_to_the_identity_check():
    """★接線★:helper 存在但沒人用 = 競態照舊(與既有的 pinned 測試同一個
    理由)。生產的 `kill_pids_verified` 必須把 keyword 一路帶到動手那一刻。"""
    import ast
    import inspect
    import textwrap
    src = textwrap.dedent(inspect.getsource(wc.kill_pids_verified))
    called = {n.func.id for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_kill_unverified_source" in called, (
        "★後備來源仍然直接 kill_pid★")
    assert "process_keyword" in inspect.signature(
        wc.kill_pids_verified).parameters, "★keyword 沒有被帶進來★"
    # 兩個生產呼叫端都要傳 process_match(不傳就退回無身分的舊行為)
    whole = inspect.getsource(wc)
    tree = ast.parse(whole)
    sites = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "kill_pids_verified"]
    assert sites, "★空集合不算通過★"
    for site in sites:
        assert len(site.args) >= 3, (
            "★呼叫端沒有把 process_match 傳進去 → 身分驗證退化成舊行為★:"
            + ast.unparse(site))


class TestArgumentBoundariesSurviveTheLookup:
    """★[外審第 1 輪 P1] 有邊界資訊就不可以丟掉它★

    psutil 給的是切好的 argv。上一版把它 join 成一個字串、再由判準重新切 ——
    ★一個含空白的單一引數會被拆成兩段★,而我們正是拿每一段的 basename 去
    比對身分。於是一支毫不相干的程式(例如編輯器開著
    `C:(路徑)中國醫皮膚科主程式 backup.txt`)會通過驗證,然後被
    `taskkill /F /T` 連子行程一起殺。
    """

    def test_a_single_spaced_argument_is_not_split(self):
        """★核心反例(審查點名要補的那一條)★
        整個路徑是【一個】引數,它的 basename 是「…主程式 backup.txt」,
        不等於 keyword —— 不可以因為中間有空白就被拆成兩半而命中。"""
        argv = ["python.exe", "editor.py",
                "C:%snotes%s%s backup.txt" % (B, B, KW)]
        assert wc._tokens_are_target(argv, KW) is False

    def test_the_real_launcher_still_matches_as_argv(self):
        """★對照組★:真的 launcher 以 argv 形式一樣要命中。"""
        argv = ["C:%sPython%spythonw.exe" % (B, B),
                "C:%sapp%ssrc%s%s.pyw" % (B, B, B, KW), "--background"]
        assert wc._tokens_are_target(argv, KW) is True

    def test_psutil_argv_is_carried_through_without_joining(self, monkeypatch):
        """★接線★:`_cmdline_tokens_of_pid_now` 必須把 psutil 的清單原樣帶走。
        join 過的話,下面這個【單一引數】會在判準裡被拆開而誤判成目標。"""
        argv = ["python.exe", "C:%snotes%s%s backup.txt" % (B, B, KW)]

        class _P:
            def __init__(self, _pid):
                pass

            def cmdline(self):
                return list(argv)
        import psutil
        monkeypatch.setattr(psutil, "Process", _P)
        got = wc._cmdline_tokens_of_pid_now(4321)
        assert got == argv, "★argv 的邊界在取得階段就被破壞了★:" + str(got)
        assert wc._tokens_are_target(got, KW) is False

    def test_an_unrelated_process_is_not_killed_end_to_end(self, monkeypatch):
        """★端對端★:走生產的 `kill_pids_verified` —— 那支無關的程式
        (單一含空白引數剛好提到 keyword)絕不可以被殺。"""
        killed = []
        monkeypatch.setattr(wc, "kill_pid",
                            lambda pid: killed.append(pid) or True)
        monkeypatch.setattr(pf, "_open_process_pin", lambda pid: 1)
        monkeypatch.setattr(pf, "pid_looks_like_python", lambda pid: True)
        monkeypatch.setattr(wc, "_PID_FROM_PIDFILE", {}, raising=False)

        class _P:
            def __init__(self, _pid):
                pass

            def cmdline(self):
                return ["python.exe", "editor.py",
                        "C:%snotes%s%s backup.txt" % (B, B, KW)]
        import psutil
        monkeypatch.setattr(psutil, "Process", _P)
        assert wc.kill_pids_verified([4321], "autoclock", KW) == []
        assert killed == [], "★殺掉了一支毫不相干的程式★:" + str(killed)

    def test_a_raw_wmic_string_is_still_tokenised(self, monkeypatch):
        """WMIC 只給得出原始字串 —— 那一條仍然要自己切(引號內不切)。

        ★反例要有【尾隨引數】才量得到★:沒有的話,整條不切、只看最後一段
        路徑的 basename 剛好還是目標,「不切」這個突變照樣綠(我第一版就是)。
        真正的後果是:launcher 後面帶著 `--background` 時,不切就會拿
        「--background」的 basename 去比 → ★該殺的殺不掉★(救援失效)。
        """
        import psutil
        monkeypatch.setattr(
            psutil, "Process",
            lambda _pid: (_ for _ in ()).throw(psutil.AccessDenied(_pid)))
        # ★第一欄是【機器名】,不是字面的 "Node"★ —— 那是被跳過的表頭
        #   (我第一版把 fixture 寫成 Node,於是那一列被當表頭略過,
        #   測試量到的是「查無此 PID」而不是「切得對不對」)。
        csv_line = ("Node,CommandLine,ProcessId" + chr(10)
                    + 'PC01,C:%sPython%spythonw.exe "C:%sMy Apps%s%s.pyw"'
                      ' --background,4321'
                    % (B, B, B, B, KW))
        monkeypatch.setattr(wc, "_read_wmic_python_process_csv",
                            lambda *, force=False: csv_line)
        got = wc._cmdline_tokens_of_pid_now(4321)
        assert got, "查不到那個 PID(fixture 的第一欄是機器名,不是表頭 Node)"
        # ★直接量「有沒有切成引數」★:拿 `_tokens_are_target` 當判準是不夠的
        #   —— 整條不切時,`splitext` 會把最後一個點之後全部當副檔名吃掉
        #   (`…主程式.pyw" --background` 的 root 仍然等於 keyword),
        #   於是「不切」這個突變照樣綠。要問的就是切了沒有。
        assert "--background" in got, (
            "★WMIC 的原始字串沒有被切成引數★:" + str(got))
        assert any(t.rstrip(chr(34)).endswith("%s.pyw" % KW) for t in got), got
        assert wc._tokens_are_target(got, KW) is True, got
