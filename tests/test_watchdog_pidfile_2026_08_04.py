# -*- coding: utf-8 -*-
"""[2026-08-04 實機] watchdog 的半死救援在 Windows 11 上完全失效。

實機 log 連續兩小時、每 60 秒印同一組警告，什麼都沒做：

    [watchdog] 打卡: mutex 持有但 log 6758s 沒更新 (>300s) — process 半死，嘗試找 PID 強制 kill
    [watchdog] 無法用 WMIC 找到 中國醫皮膚科打卡程式 的 PID；為避免誤殺其他 Python 程序，本輪不執行 broad fallback kill

舊版靠「列舉 python 行程 → 比對 cmdline 是否含啟動器檔名」找 PID，三個破口在這台
機器上【同時】成立：WMIC 已被 Win11 24H2 移除、PowerShell CIM 對權限較高的行程
回傳空 CommandLine、而實機 cmdline 是 `...\\src\\autoclock.py` 根本不含關鍵字。
改為行程自報 PID 檔（直接事實，不需列舉/不看 cmdline/不受提權影響）。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import pidfile  # noqa: E402
from cmuh_common import watchdog_core as wc  # noqa: E402


def test_write_read_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(pidfile, "get_settings_dir", lambda: str(tmp_path))
    assert pidfile.write_pid_file("autoclock") is True
    assert pidfile.read_raw_pid("autoclock") == os.getpid()
    # 本行程就是 python → 身分驗證應通過（但 read_verified_pid 會排除「自己」）
    assert pidfile.pid_looks_like_python(os.getpid()) is True


def test_missing_or_corrupt_file_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(pidfile, "get_settings_dir", lambda: str(tmp_path))
    assert pidfile.read_raw_pid("nope") is None
    (tmp_path / "bad.pid").write_text("not-a-number", encoding="utf-8")
    assert pidfile.read_raw_pid("bad") is None
    (tmp_path / "neg.pid").write_text("-5", encoding="utf-8")
    assert pidfile.read_raw_pid("neg") is None


def _write_record(tmp_path, name, **over):
    """寫一份【新格式】PID 檔（預設欄位都是合法的，用 over 改單一欄位）。

    ★[2026-08-04 外審 P1-07] 這個 helper 是必要的★ 底下幾支原本寫的是舊格式
    純數字檔。新版把舊格式一律擋掉（無從驗身分），於是那些測試會【因為錯的理由】
    通過 —— 名義上測「行程已死」「不是 python」，實際上只測到「舊格式被擋」。
    """
    import json
    rec = {
        "schema": 1,
        "app_id": name,
        "pid": 4321,
        "create_time": 1_780_000_000.5,
        "executable": r"C:\python\pythonw.exe",
    }
    rec.update(over)
    (tmp_path / f"{name}.pid").write_text(
        json.dumps(rec), encoding="utf-8")
    return rec


def test_dead_pid_is_rejected(tmp_path, monkeypatch):
    """★PID 會被作業系統重用★ 不驗就可能誤殺別人的行程。"""
    monkeypatch.setattr(pidfile, "get_settings_dir", lambda: str(tmp_path))
    _write_record(tmp_path, "gone", pid=999999)
    assert pidfile.read_verified_pid("gone") is None


def test_non_python_pid_is_rejected(tmp_path, monkeypatch):
    """PID 活著但不是 python 行程（PID 被重用）→ 不得採用。"""
    monkeypatch.setattr(pidfile, "get_settings_dir", lambda: str(tmp_path))
    monkeypatch.setattr(pidfile, "pid_looks_like_python", lambda _pid: False)
    _write_record(tmp_path, "reused")
    assert pidfile.read_verified_pid("reused") is None


class TestPidReuseByAnotherPythonProcess:
    """★外審 P1-07 的核心★ 本機六支 CMUH 程式全是 pythonw.exe。

    只比對 process name 的話，stale PID 檔的 PID 被【自家另一支程式】重用時
    驗證照樣通過 —— 而 watchdog 拿到的 PID 會被送去 `taskkill /F /T`。
    這不是「找不到而不動作」，是強殺無關的自家程式。
    """

    def _fake_psutil(self, monkeypatch, *, name="pythonw.exe",
                     create_time=1_780_000_000.5,
                     exe=r"C:\python\pythonw.exe"):
        import types

        class _Proc:
            def __init__(self, pid):
                self.pid = pid

            def name(self):
                return name

            def create_time(self):
                return create_time

            def exe(self):
                return exe

        mod = types.ModuleType("psutil")
        mod.Process = _Proc
        mod.NoSuchProcess = type("NoSuchProcess", (Exception,), {})
        monkeypatch.setitem(sys.modules, "psutil", mod)

    def test_a_reused_pid_is_refused_even_though_it_is_also_pythonw(
            self, tmp_path, monkeypatch):
        """名字一樣、建立時間不同 → 必須拒絕。"""
        monkeypatch.setattr(pidfile, "get_settings_dir", lambda: str(tmp_path))
        _write_record(tmp_path, "autoclock", create_time=1_780_000_000.5)
        # 實際跑在那個 PID 上的是【後來才建立】的另一支 pythonw
        self._fake_psutil(monkeypatch, create_time=1_780_000_900.0)

        assert pidfile.read_verified_pid("autoclock") is None, (
            "★PID 被自家另一支 pythonw 重用卻通過驗證★ watchdog 會強殺它")

    def test_the_real_process_is_still_accepted(self, tmp_path, monkeypatch):
        """★反方向:不可以變成永遠找不到★ 建立時間相符就要採用。

        否則等於把這個模組修回它本來要解決的問題（救援完全失效）。
        """
        monkeypatch.setattr(pidfile, "get_settings_dir", lambda: str(tmp_path))
        _write_record(tmp_path, "autoclock", create_time=1_780_000_000.5)
        self._fake_psutil(monkeypatch, create_time=1_780_000_000.5)

        assert pidfile.read_verified_pid("autoclock") == 4321

    def test_a_tiny_float_difference_is_still_the_same_process(
            self, tmp_path, monkeypatch):
        """浮點表示的最後一位不該決定「要不要強殺一支程式」。"""
        monkeypatch.setattr(pidfile, "get_settings_dir", lambda: str(tmp_path))
        _write_record(tmp_path, "autoclock", create_time=1_780_000_000.5)
        self._fake_psutil(monkeypatch, create_time=1_780_000_000.51)

        assert pidfile.read_verified_pid("autoclock") == 4321

    def test_a_record_without_a_create_time_is_refused(self, tmp_path,
                                                       monkeypatch):
        """沒記建立時間 → 無從驗身分 → 不採用（不可以當成「驗過了」）。"""
        monkeypatch.setattr(pidfile, "get_settings_dir", lambda: str(tmp_path))
        _write_record(tmp_path, "autoclock", create_time=None)
        self._fake_psutil(monkeypatch)

        assert pidfile.read_verified_pid("autoclock") is None

    def test_a_mismatched_executable_is_refused(self, tmp_path, monkeypatch):
        """執行檔路徑不符 → 縱深防禦攔下來。"""
        monkeypatch.setattr(pidfile, "get_settings_dir", lambda: str(tmp_path))
        _write_record(tmp_path, "autoclock")
        self._fake_psutil(monkeypatch, exe=r"D:\other\pythonw.exe")

        assert pidfile.read_verified_pid("autoclock") is None

    def test_an_unreadable_executable_does_not_veto(self, tmp_path,
                                                    monkeypatch):
        """★exe 讀不到不可以否決★（實測 397 個行程中失敗 1 次）

        身分已由 pid + create_time 決定；把 exe 當成必要條件，會在讀不到的機器上
        退回那條壞掉的 cmdline 路徑 —— 等於把這個功能修回它要解決的問題。
        """
        monkeypatch.setattr(pidfile, "get_settings_dir", lambda: str(tmp_path))
        _write_record(tmp_path, "autoclock")

        import types

        class _Proc:
            def __init__(self, pid):
                self.pid = pid

            def name(self):
                return "pythonw.exe"

            def create_time(self):
                return 1_780_000_000.5

            def exe(self):
                raise OSError("AccessDenied")

        mod = types.ModuleType("psutil")
        mod.Process = _Proc
        monkeypatch.setitem(sys.modules, "psutil", mod)

        assert pidfile.read_verified_pid("autoclock") == 4321

    def test_a_record_for_another_program_is_refused(self, tmp_path,
                                                     monkeypatch):
        """app_id 不符 → 不採用（檔名被搬動/複製過）。"""
        monkeypatch.setattr(pidfile, "get_settings_dir", lambda: str(tmp_path))
        _write_record(tmp_path, "autoclock", app_id="consult_query")
        self._fake_psutil(monkeypatch)

        assert pidfile.read_verified_pid("autoclock") is None


def test_a_legacy_plain_number_file_is_refused(tmp_path, monkeypatch):
    """舊格式沒有建立時間 → 無從驗身分 → 退回 cmdline 路徑（不是採用）。

    這是升級後到程式重啟前的短暫狀態；退回去的行為＝這個模組出現以前，
    可能找不到，但不會殺錯。
    """
    monkeypatch.setattr(pidfile, "get_settings_dir", lambda: str(tmp_path))
    (tmp_path / "autoclock.pid").write_text("4321", encoding="utf-8")

    assert pidfile.read_verified_pid("autoclock") is None
    # 但 read_raw_pid 仍要讀得出來，否則 clear_pid_file 認不出自己的舊檔
    assert pidfile.read_raw_pid("autoclock") == 4321


def test_own_pid_is_not_returned(tmp_path, monkeypatch):
    """watchdog 內嵌在同一支程式時，不可回報自己（會自殺）。"""
    monkeypatch.setattr(pidfile, "get_settings_dir", lambda: str(tmp_path))
    pidfile.write_pid_file("self")
    assert pidfile.read_verified_pid("self") is None


def test_clear_only_removes_own_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(pidfile, "get_settings_dir", lambda: str(tmp_path))
    pidfile.write_pid_file("mine")
    pidfile.clear_pid_file("mine")
    assert pidfile.read_raw_pid("mine") is None
    # 別人的 PID 檔不可被我清掉
    (tmp_path / "other.pid").write_text("4321", encoding="utf-8")
    pidfile.clear_pid_file("other")
    assert pidfile.read_raw_pid("other") == 4321


# ─── watchdog 接線 ──────────────────────────────────────────────────────────
def test_lookup_prefers_pid_file(monkeypatch):
    """★核心修正★ 有 PID 檔就直接用，完全不碰 cmdline 比對那條壞掉的路。"""
    called = []
    monkeypatch.setattr(wc, "_wmic_find_pids",
                        lambda kw, **k: called.append(kw) or [])
    import cmuh_common.pidfile as pf
    monkeypatch.setattr(pf, "read_verified_pid", lambda name: 12345)
    got = wc._find_pids_holding_mutex("中國醫皮膚科打卡程式", "mtx",
                                      pid_name="autoclock")
    assert got == [12345]
    assert called == [], "有 PID 檔時不該再走 cmdline 比對"


def test_lookup_falls_back_when_pid_file_unusable(monkeypatch):
    """PID 檔不存在/驗不過 → 退回原本的 cmdline 路徑（不可整條斷掉）。"""
    monkeypatch.setattr(wc, "_wmic_find_pids", lambda kw, **k: [777])
    import cmuh_common.pidfile as pf
    monkeypatch.setattr(pf, "read_verified_pid", lambda name: None)
    assert wc._find_pids_holding_mutex("kw", "mtx", pid_name="autoclock") == [777]
    # 沒給 pid_name（主程式等尚未自報的項目）→ 直接走舊路徑
    assert wc._find_pids_holding_mutex("kw", "mtx") == [777]


def test_pidfile_read_failure_does_not_break_lookup(monkeypatch):
    """讀 PID 檔爆炸也不能讓救援整條掛掉。"""
    monkeypatch.setattr(wc, "_wmic_find_pids", lambda kw, **k: [888])
    import cmuh_common.pidfile as pf

    def _boom(_name):
        raise OSError("disk")
    monkeypatch.setattr(pf, "read_verified_pid", _boom)
    assert wc._find_pids_holding_mutex("kw", "mtx", pid_name="autoclock") == [888]


def test_watched_entries_declare_pid_name():
    """打卡與會診都要宣告 pid_name，否則救援仍走壞掉的舊路。"""
    import inspect
    src = inspect.getsource(wc)
    assert '"pid_name": "autoclock"' in src
    assert '"pid_name": "consult_query"' in src
    # 半死 kill 路徑必須把 pid_name 傳進去
    i = src.index("half_dead_pids = _find_pids_holding_mutex(")
    assert "pid_name=prog.get(" in src[i:i + 200]


def test_both_programs_self_report_at_startup():
    """兩支程式啟動時要自報 PID——沒人寫檔，watchdog 就永遠讀不到。"""
    for path, name in (("src/autoclock.py", "autoclock"),
                       ("src/consult_query.py", "consult_query")):
        full = os.path.join(os.path.dirname(__file__), "..", path)
        text = open(full, encoding="utf-8").read()
        assert f'write_pid_file("{name}")' in text, f"{path} 未自報 PID"


def test_what_we_write_is_actually_verifiable(tmp_path, monkeypatch):
    """★寫進去的東西要驗得過★（突變驗證抓到的缺口）

    原本只斷言 `read_raw_pid` 讀得回 PID —— 那不涉及任何身分欄位，所以把
    `write_pid_file` 改成不記 create_time，測試照樣全綠，而實機上 watchdog
    會從此永遠採用不了 PID 檔（救援靜默失效，回到這個模組要解決的問題）。

    這裡直接驗「寫出來的紀錄餵給驗證器會通過」——寫方與讀方必須對得上。
    `read_verified_pid` 會排除「自己」，所以測的是它底下的身分比對。
    """
    import json
    monkeypatch.setattr(pidfile, "get_settings_dir", lambda: str(tmp_path))
    assert pidfile.write_pid_file("autoclock") is True

    rec = json.loads((tmp_path / "autoclock.pid").read_text(encoding="utf-8"))
    assert isinstance(rec.get("create_time"), (int, float)), (
        f"寫出來的紀錄沒有可用的建立時間：{rec}")
    assert rec["pid"] == os.getpid()
    assert rec["app_id"] == "autoclock"
    assert pidfile._identity_matches(os.getpid(), rec) is True, (
        "★自己寫的紀錄自己驗不過★ 寫方與讀方沒對上")


def test_a_legacy_file_is_never_treated_as_verified(tmp_path, monkeypatch):
    """舊格式即使 PID 活著、也是 pythonw，仍然不可以被採用。

    ★判準必須是「有沒有驗過身分」，不是「找不找得到 PID」★
    """
    import types
    monkeypatch.setattr(pidfile, "get_settings_dir", lambda: str(tmp_path))
    (tmp_path / "autoclock.pid").write_text("4321", encoding="utf-8")

    class _Proc:
        def __init__(self, pid):
            self.pid = pid

        def name(self):
            return "pythonw.exe"

        def create_time(self):
            return 1_780_000_000.5

        def exe(self):
            return r"C:\python\pythonw.exe"

    mod = types.ModuleType("psutil")
    mod.Process = _Proc
    monkeypatch.setitem(sys.modules, "psutil", mod)

    assert pidfile.read_verified_pid("autoclock") is None, (
        "★舊格式沒有建立時間，無從驗身分，不可採用★")


class TestNonFiniteNumbersCannotBypassTheCheck:
    """★NaN 讓「不符就拒絕」的比較永遠不成立★（2026-08-04 外審第 2 輪 P1-04）

    `json.loads` 預設接受非標準的 NaN / Infinity token，而

        abs(got - nan) > tolerance   →   False

    於是建立時間再怎麼不符都攔不下來 —— 整個防重用驗證被一個 token 繞過。
    實測確認過，不是推理。
    """

    def _psutil(self, monkeypatch, create_time):
        import types

        class _Proc:
            def __init__(self, pid):
                self.pid = pid

            def name(self):
                return "pythonw.exe"

            def create_time(self):
                return create_time

            def exe(self):
                return r"C:\python\pythonw.exe"

        mod = types.ModuleType("psutil")
        mod.Process = _Proc
        monkeypatch.setitem(sys.modules, "psutil", mod)

    def test_the_arithmetic_really_does_bypass(self):
        """先證明這個繞過是真的（否則下面的斷言只是在測一個不存在的問題）。"""
        import math
        nan = float("nan")
        assert (abs(1_780_000_000.5 - nan) > 0.05) is False
        assert math.isfinite(nan) is False

    def test_json_nan_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pidfile, "get_settings_dir", lambda: str(tmp_path))
        (tmp_path / "autoclock.pid").write_text(
            '{"schema": 1, "app_id": "autoclock", "pid": 4321,'
            ' "create_time": NaN, "executable": ""}', encoding="utf-8")
        self._psutil(monkeypatch, 1_780_000_900.0)      # 明顯不符的建立時間

        assert pidfile.read_verified_pid("autoclock") is None, (
            "★NaN 繞過了建立時間驗證 → watchdog 會強殺無關行程★")

    def test_infinity_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pidfile, "get_settings_dir", lambda: str(tmp_path))
        (tmp_path / "autoclock.pid").write_text(
            '{"schema": 1, "app_id": "autoclock", "pid": 4321,'
            ' "create_time": Infinity, "executable": ""}', encoding="utf-8")
        self._psutil(monkeypatch, 1_780_000_900.0)
        assert pidfile.read_verified_pid("autoclock") is None

    def test_true_is_not_a_creation_time(self, tmp_path, monkeypatch):
        """`True` 是 int 的子類 → `isinstance(v,(int,float))` 會放行。"""
        monkeypatch.setattr(pidfile, "get_settings_dir", lambda: str(tmp_path))
        _write_record(tmp_path, "autoclock", create_time=True)
        self._psutil(monkeypatch, 1.0)
        assert pidfile.read_verified_pid("autoclock") is None

    def test_a_normal_record_is_still_accepted(self, tmp_path, monkeypatch):
        """★反方向:不可以連正常的也擋掉★"""
        monkeypatch.setattr(pidfile, "get_settings_dir", lambda: str(tmp_path))
        _write_record(tmp_path, "autoclock", create_time=1_780_000_000.5)
        self._psutil(monkeypatch, 1_780_000_000.5)
        assert pidfile.read_verified_pid("autoclock") == 4321

    def test_a_nan_in_any_other_field_also_refuses_the_record(
            self, tmp_path, monkeypatch):
        """★第二道防線要真的承重★（突變驗證抓到）

        `_is_finite_number` 只看 create_time，所以把 `parse_constant` 拿掉時
        行為不變、突變不轉紅 —— 那道防線等於沒被測到。它宣稱的性質是
        「別的欄位也不可能夾帶非有限數」，所以要用【別的欄位】來驗：
        create_time 完全正常，NaN 出現在其他地方，整份紀錄仍須被拒收。
        """
        monkeypatch.setattr(pidfile, "get_settings_dir", lambda: str(tmp_path))
        (tmp_path / "autoclock.pid").write_text(
            '{"schema": 1, "app_id": "autoclock", "pid": 4321,'
            ' "create_time": 1780000000.5, "executable": "", "extra": NaN}',
            encoding="utf-8")
        self._psutil(monkeypatch, 1_780_000_000.5)     # 建立時間完全相符

        assert pidfile.read_verified_pid("autoclock") is None, (
            "★含非標準 JSON token 的 PID 檔仍被採用★")
