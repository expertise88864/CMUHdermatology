# -*- coding: utf-8 -*-
"""[R3-P2-04 R2 P1] 打卡的 check-then-act 要靠跨行程宣告序列化。

「先查刷卡表、沒紀錄才打」中間有 1~5 秒延遲與數次頁面操作,而★重讀刷卡表
擋不住★(讀的是自己那個瀏覽器的 DOM)。repo 內的「清理重複打卡程式.ps1」
就是 session 0 雙開造成重複打卡之後留下的現場工具。

★三個硬要求★:不可以永久卡住(死掉的擁有者要接得走)、接手本身要防競爭、
這一層壞掉不可以讓打卡停擺。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import cmuh_common.cross_process_claim as cpc  # noqa: E402
from cmuh_common.cross_process_claim import exclusive_claim  # noqa: E402


@pytest.fixture
def claims(tmp_path, monkeypatch):
    d = str(tmp_path / "claims")
    monkeypatch.setattr(cpc, "_claims_dir", lambda: d)
    return d


def _write(path, **rec):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(rec, fh)


class TestItSerialises:
    def test_the_first_one_gets_it(self, claims):
        with exclusive_claim("k") as owned:
            assert owned is True

    def test_a_live_owner_blocks_the_second(self, claims, monkeypatch):
        """★核心★:別人正拿著(而且還活著)→ 本次略過。"""
        with exclusive_claim("k") as first:
            assert first
            monkeypatch.setattr(cpc.os, "getpid", lambda: 999999)
            monkeypatch.setattr(cpc, "_owner_gone", lambda _r, _t: False)
            with exclusive_claim("k") as second:
                assert second is False, "★兩個人同時拿到宣告★"

    def test_it_is_released_on_the_way_out(self, claims):
        with exclusive_claim("k") as owned:
            assert owned
        assert not os.path.exists(cpc._claim_path("k")), "離開時沒有放掉"

    def test_it_is_released_even_when_the_body_raises(self, claims):
        """★例外也要放掉★:不然一次失敗就把整個打卡窗鎖死。"""
        with pytest.raises(RuntimeError):
            with exclusive_claim("k") as owned:
                assert owned
                raise RuntimeError("boom")
        assert not os.path.exists(cpc._claim_path("k"))

    def test_different_keys_do_not_block_each_other(self, claims,
                                                    monkeypatch):
        """不同帳號/不同打卡窗本來就該平行跑。"""
        with exclusive_claim("a") as first:
            assert first
            with exclusive_claim("b") as second:
                assert second is True


class TestItCannotWedge:
    """★死掉的擁有者要接得走★ —— 否則這道防線本身會變成「整天不打卡」。"""

    def test_a_dead_owner_is_taken_over(self, claims, monkeypatch):
        _write(cpc._claim_path("k"), pid=424242, create_time=1.0,
               ts=cpc.time.time())
        monkeypatch.setattr(cpc, "_owner_gone", lambda _r, _t: True)
        with exclusive_claim("k") as owned:
            assert owned is True

    def test_a_wedged_owner_expires(self, claims):
        """★還活著但整個卡死★:TTL 是唯一的出口。"""
        _write(cpc._claim_path("k"), pid=os.getpid() + 1, create_time=None,
               ts=cpc.time.time() - 10_000)
        with exclusive_claim("k", ttl_sec=300.0) as owned:
            assert owned is True

    def test_a_fresh_owner_does_not_expire(self, claims):
        """★對照組★:TTL 不可以順手把還在做事的人踢掉。"""
        _write(cpc._claim_path("k"), pid=os.getpid() + 1, create_time=None,
               ts=cpc.time.time())
        with exclusive_claim("k", ttl_sec=300.0) as owned:
            assert owned is False

    def test_it_does_not_delete_someone_elses_claim(self, claims):
        """★只在檔案裡還是自己時才刪★:接手者的宣告不可以被前任的收尾誤刪。"""
        path = cpc._claim_path("k")
        with exclusive_claim("k") as owned:
            assert owned
            _write(path, pid=424242, create_time=1.0, ts=cpc.time.time())
        assert os.path.exists(path), "★把別人的宣告刪掉了★"

    def test_the_lock_file_is_never_removed(self, claims):
        """★互斥的根據不可以暫時消失★(外審 R4 P1):只要鎖檔會被刪掉/搬走,
        就會出現「原路徑不存在、第三個人趁空建立」的窗。"""
        path = cpc._claim_path("k")
        with exclusive_claim("k") as owned:
            assert owned
        assert os.path.exists(cpc._lock_path(path)), "★鎖檔被刪掉了★"
        assert not os.path.exists(path), "宣告本身該被放掉"

    def test_a_lock_timeout_skips_this_round(self, claims, monkeypatch):
        """★取鎖逾時 → 這一輪略過★(不是照打)。打卡每分鐘 re-fire,
        略過一輪不等於漏打;硬打下去才是重複打卡。"""
        monkeypatch.setattr(
            cpc, "_lock_fd",
            lambda _fd: (_ for _ in ()).throw(OSError("busy")))
        with exclusive_claim("k") as owned:
            assert owned is False


class TestItFailsOpen:
    """★這一層壞掉不可以讓打卡停擺★ —— 它是額外的防線,不是前提條件。"""

    def test_a_broken_directory_still_punches(self, monkeypatch):
        monkeypatch.setattr(
            cpc, "_claims_dir",
            lambda: (_ for _ in ()).throw(OSError("no disk")))
        with exclusive_claim("k") as owned:
            assert owned is True

    def test_an_unusable_lock_file_still_punches(self, claims, monkeypatch):
        """★鎖檔本身開不起來(權限/磁碟)也不可以讓打卡停擺★ ——
        少一道防線,但「檔案系統一有問題就整天不打卡」比重複打卡更難發現。
        (與「取鎖逾時」不同:那是有人正卡在幾毫秒的臨界區裡,略過一輪就好;
         這裡是這道防線根本用不了。)"""
        real_open = cpc.os.open

        def _no_lock(path, *a, **k):
            if path.endswith(".lock"):
                raise PermissionError("no")
            return real_open(path, *a, **k)
        monkeypatch.setattr(cpc.os, "open", _no_lock)
        with exclusive_claim("k") as owned:
            assert owned is True

    def test_a_corrupt_claim_file_is_treated_as_held(self, claims):
        """★壞檔是「有人拿著但查不出是誰」★:未知要倒向保守那一邊 ——
        接手的代價是重複打卡,多等一輪只是晚打。"""
        path = cpc._claim_path("k")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{ 不是 JSON")
        with exclusive_claim("k") as owned:
            assert owned is False


class TestTheOwnerLivenessRule:
    def test_a_missing_process_is_gone(self, monkeypatch):
        import psutil
        monkeypatch.setattr(
            cpc.os, "getpid", lambda: 1)
        rec = {"pid": 424242, "create_time": 1.0, "ts": cpc.time.time()}

        class _P:
            def __init__(self, _pid):
                raise psutil.NoSuchProcess(424242)
        monkeypatch.setitem(sys.modules, "psutil",
                            type(sys)("psutil"))
        sys.modules["psutil"].Process = _P
        sys.modules["psutil"].NoSuchProcess = psutil.NoSuchProcess
        assert cpc._owner_gone(rec, 300.0) is True

    def test_a_reused_pid_is_gone(self, monkeypatch):
        """★建立時間不符 = PID 被重用★,那不是原來那個行程。"""
        rec = {"pid": 424242, "create_time": 1.0, "ts": cpc.time.time()}
        monkeypatch.setitem(sys.modules, "psutil", type(sys)("psutil"))
        sys.modules["psutil"].Process = lambda _p: type(
            "X", (), {"create_time": lambda self: 999.0})()
        assert cpc._owner_gone(rec, 300.0) is True

    def test_a_matching_process_is_alive(self, monkeypatch):
        rec = {"pid": 424242, "create_time": 5.0, "ts": cpc.time.time()}
        monkeypatch.setitem(sys.modules, "psutil", type(sys)("psutil"))
        sys.modules["psutil"].Process = lambda _p: type(
            "X", (), {"create_time": lambda self: 5.0})()
        assert cpc._owner_gone(rec, 300.0) is False

    def test_an_old_format_record_counts_as_alive(self, monkeypatch):
        """★行程還在、但紀錄沒有建立時間 → 無從排除 PID 重用 → 當成還在★
        (未知倒向保守)。

        ★PID 必須真的存在★:隨便挑一個不存在的 PID 只會走到「查無此行程 →
        確定不在了」那條路,量不到這一條。"""
        rec = {"pid": 424242, "ts": cpc.time.time()}
        monkeypatch.setitem(sys.modules, "psutil", type(sys)("psutil"))
        sys.modules["psutil"].Process = lambda _p: type(
            "X", (), {"create_time": lambda self: 5.0})()
        assert cpc._owner_gone(rec, 300.0) is False


def test_only_one_of_many_concurrent_takers_wins(claims, monkeypatch):
    """★併發下只能有一個人接手★ —— 這是「原子」真正的意思。

    上面幾條是循序的,量得到「身分再確認」這一半;這一條開多個執行緒同時搶
    同一筆陳舊宣告,量的是★定勝負那一步本身★。用 `os.rename`(同一個來源只有
    一個人搬得走)才過得了;換成「先複製再刪掉」之類的兩步寫法,中間那個
    「檔案不見了」的空窗會讓第二個人也建得起來。
    """
    import threading
    path = cpc._claim_path("k")
    stale = {"pid": 424242, "create_time": 1.0, "ts": 0.0}
    _write(path, **stale)
    monkeypatch.setattr(cpc, "_owner_gone",
                        lambda rec, _t: rec.get("pid") == 424242)
    start = threading.Barrier(8)
    won: list = []
    lock = threading.Lock()

    def _try(i):
        start.wait()
        with exclusive_claim("k") as owned:
            if owned:
                with lock:
                    won.append(i)
                time_sleep(0.02)          # 拿著一下下,讓別人也擠進來

    from time import sleep as time_sleep
    threads = [threading.Thread(target=_try, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert len(won) == 1, f"★同時有 {len(won)} 個人拿到宣告★:{won}"


def test_only_one_of_several_real_processes_wins(tmp_path):
    """★真正的跨行程互斥★ —— 這一條要開子行程,不能用執行緒。

    Windows 的檔案鎖是★以行程為單位★的:同一個行程裡的兩個執行緒各自開 fd
    去鎖同一段,兩邊都會成功(行程不會擋自己)。所以上面那條執行緒測試量的是
    【行程內】那一半(`_local_lock`),★跨行程這一半只有子行程量得到★。
    """
    import subprocess
    import sys as _sys
    claims_dir = str(tmp_path / "claims")
    script = tmp_path / "grab.py"
    script.write_text(
        "import os, sys, time\n"
        f"sys.path.insert(0, {os.path.join(os.path.dirname(__file__), '..', 'src')!r})\n"
        "import cmuh_common.cross_process_claim as cpc\n"
        f"cpc._claims_dir = lambda: {claims_dir!r}\n"
        "os.makedirs(cpc._claims_dir(), exist_ok=True)\n"
        "with cpc.exclusive_claim('k') as owned:\n"
        "    print('WON' if owned else 'LOST', flush=True)\n"
        "    if owned:\n"
        "        time.sleep(1.5)\n",
        encoding="utf-8")
    procs = [subprocess.Popen([_sys.executable, str(script)],
                              stdout=subprocess.PIPE, encoding="utf-8",
                              errors="replace")
             for _ in range(4)]
    outs = []
    for p in procs:
        out, _ = p.communicate(timeout=60)
        outs.append((out or "").strip())
    won = [o for o in outs if o == "WON"]
    assert len(won) == 1, f"★同時有 {len(won)} 個行程拿到宣告★:{outs}"
