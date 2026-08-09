# -*- coding: utf-8 -*-
"""[批次 Y] 排程版 watchdog 的入口必須是固定的 `.pyw`。

★外審 2026-08-09 P1-01★
`安裝開機自動啟動.ps1` 以前把每 2 分鐘的 task 註冊成
`pythonw.exe "<app>/src/watchdog_runner.py" --once` —— **不經過 launcher**。
`current.txt` 切版之後：其他五支跑新版，watchdog 每兩分鐘永遠跑 `<app>/src`
的舊版；`CMUH_APP_DIR` / `CMUH_LAUNCHER` 根本沒被設過。
**最後一道復原防線自己停在舊版，而且沒有任何地方會說出來。**

★改 installer 不夠★ 已部署的電腦不會再跑一次安裝腳本。所以要在執行期偵測
並改寫，**改完回讀確認**，失敗要講出來（不可以假成功）。
"""
import os
import sys

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

from cmuh_common import watchdog_task as wt  # noqa: E402

_LEGACY = 'pythonw.exe "C:\\App\\src\\watchdog_runner.py" --once'
_GOOD = 'pythonw.exe "C:\\App\\中國醫皮膚科守護程式.pyw" --once'


# ── 判準本身 ─────────────────────────────────────────────────────────────
def test_the_legacy_action_is_recognised():
    assert wt.action_is_legacy(_LEGACY) is True


def test_the_launcher_action_is_not_legacy():
    assert wt.action_is_legacy(_GOOD) is False


def test_an_empty_action_is_not_called_legacy():
    """★查不到 ≠ 是舊的★ 空字串不可以觸發改寫（那是「不知道」）。"""
    for v in ("", None, "   "):
        assert wt.action_is_legacy(v) is False


def test_a_launcher_action_that_also_mentions_the_runner_is_fine():
    """launcher 內部本來就會跑 watchdog_runner.py —— 有 launcher 就算對。"""
    mixed = ('pythonw.exe "C:\\App\\中國醫皮膚科守護程式.pyw" --once '
             '# 內部跑 watchdog_runner.py')
    assert wt.action_is_legacy(mixed) is False


def test_the_desired_action_points_at_the_launcher():
    got = wt.desired_action("C:\\App", pythonw="pyw.exe")
    assert wt.LAUNCHER_NAME in got and "--once" in got
    assert wt.action_is_legacy(got) is False


# ── migrate 的每一條出口 ─────────────────────────────────────────────────
@pytest.fixture
def app(tmp_path):
    (tmp_path / wt.LAUNCHER_NAME).write_text("", encoding="utf-8")
    return str(tmp_path)


def _stub(monkeypatch, queries, change_rc=0):
    """queries 依序回傳 (action, reason)；change_rc 是 /Change 的結果。

    ★`_run` 現在回【三值】（含 stderr）★ —— stub 要跟上生產簽章，
    否則測到的是 unpack 錯誤而不是被測的行為。
    """
    seq = list(queries)
    calls = []

    def _q(task_name=wt.TASK_NAME):
        return seq.pop(0) if seq else (None, wt.UNREADABLE)

    def _run(args, timeout=20.0):
        calls.append(args)
        return (change_rc, "", "")

    monkeypatch.setattr(wt, "query_action", _q)
    monkeypatch.setattr(wt, "_run", _run)
    return calls


def _want(app):
    """測試要用【生產算出來的】desired action —— 自己手寫一份會漂。"""
    return wt.desired_action(app, pythonw="pythonw.exe")


def test_an_already_correct_task_is_left_alone(app, monkeypatch):
    calls = _stub(monkeypatch, [(_want(app), "")])
    assert wt.migrate_if_legacy(app, pythonw="pythonw.exe") == wt.OK_ALREADY
    assert calls == [], "已經是對的卻還去改寫"


def test_a_legacy_task_is_migrated_and_verified(app, monkeypatch):
    calls = _stub(monkeypatch, [(_LEGACY, ""), (_want(app), "")])
    assert wt.migrate_if_legacy(app, pythonw="pythonw.exe") == wt.MIGRATED
    assert calls and "/Change" in calls[0]


def test_a_change_that_did_not_take_is_reported_as_failed(app, monkeypatch):
    """★改完一定要回讀★ `/Change` 回 0 不代表真的寫進去了。"""
    _stub(monkeypatch, [(_LEGACY, ""), (_LEGACY, "")])   # 回讀仍是舊的
    assert wt.migrate_if_legacy(app, pythonw="pythonw.exe") == wt.FAILED


def test_a_failing_change_command_is_reported(app, monkeypatch):
    _stub(monkeypatch, [(_LEGACY, "")], change_rc=1)
    assert wt.migrate_if_legacy(app, pythonw="pythonw.exe") == wt.FAILED


def test_an_unreadable_task_is_not_assumed_correct(app, monkeypatch):
    """★核心★ 查不到現況就當成「已經是對的」＝在不知道的情況下宣稱沒問題。"""
    calls = _stub(monkeypatch, [(None, wt.UNREADABLE)])
    assert wt.migrate_if_legacy(app) == wt.UNREADABLE
    assert calls == [], "查不到卻還是去改寫了"


def test_no_task_is_a_distinct_outcome(app, monkeypatch):
    _stub(monkeypatch, [(None, wt.NO_TASK)])
    assert wt.migrate_if_legacy(app) == wt.NO_TASK


def test_a_missing_launcher_blocks_the_rewrite(tmp_path, monkeypatch):
    """★launcher 不在就不可以改★ 改了會變成排程指向不存在的檔，比現況更糟。"""
    calls = _stub(monkeypatch, [(_LEGACY, "")])
    assert wt.migrate_if_legacy(str(tmp_path)) == wt.FAILED
    assert calls == []


def test_the_outcome_set_is_closed():
    got = {wt.OK_ALREADY, wt.MIGRATED, wt.NO_TASK, wt.UNREADABLE, wt.FAILED}
    assert len(got) == 5, "回傳值撞名了 —— 呼叫端會分不出來"


# ── 接線：installer 與 --once ─────────────────────────────────────────────
def test_the_installer_no_longer_registers_the_src_script():
    """★PS1 的排程定義不得再指向 src 底下那支★"""
    with open(os.path.join(REPO_ROOT, "安裝開機自動啟動.ps1"),
              encoding="utf-8") as fh:
        text = fh.read()
    # ★要查【指派】那一行，不是組命令那一行★
    #   `$tr` 用的是 `$scriptFullPath`；真正決定跑哪支檔的是它的指派。
    #   我第一版只看 `$tr`，那是一個永遠不會變的字串 —— 測不到任何東西。
    assigns = [ln.strip() for ln in text.splitlines()
               if ln.strip().startswith("$scriptFullPath =")]
    assert assigns, "找不到 $scriptFullPath 的指派（測試自己失效了）"
    body = "\n".join(assigns)
    assert "ScriptRelPath" not in body, (
        "排程仍用 ScriptRelPath 直接跑 src 底下那支 —— 切版後 watchdog 跑舊版："
        + body)
    assert "$pywPath" in body, "排程沒有改用 .pyw 啟動器：" + body


def test_the_once_path_runs_the_migration():
    """★接上去了才算數★ 不接的話已部署的機器永遠不會被修好。"""
    import ast
    import inspect
    import textwrap
    import importlib
    runner = importlib.import_module("watchdog_runner")
    src = textwrap.dedent(inspect.getsource(runner._run_once_via_core))
    names = {n.func.id for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_migrate_scheduled_task_if_legacy" in names, (
        "--once 沒有檢查排程 —— 已部署的機器不會自己修好")


def test_the_migration_never_blocks_the_watchdog_tick(monkeypatch):
    """★遷移失敗不可以擋住本輪的 watchdog 工作★（它是最後一道防線）"""
    import importlib
    runner = importlib.import_module("watchdog_runner")

    def _boom(*a, **k):
        raise RuntimeError("schtasks 掛了")

    monkeypatch.setattr(wt, "migrate_if_legacy", _boom)
    assert runner._migrate_scheduled_task_if_legacy() == "error"


# ── [外審 P1-03] 查詢層的三個缺陷 ────────────────────────────────────────
_XML = ("""<?xml version="1.0" encoding="UTF-16"?>
<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Actions><Exec>
    <Command>%s</Command>
    <Arguments>%s</Arguments>
  </Exec></Actions>
</Task>""")


def _q_stub(monkeypatch, rc, out="", err=""):
    seen = []
    monkeypatch.setattr(wt, "_run",
                        lambda args, timeout=20.0: seen.append(args) or (rc, out, err))
    return seen


def test_a_stderr_only_failure_is_not_called_no_task(monkeypatch):
    """★核心（外審 P1-03A）★ schtasks 把錯誤寫在 stderr、stdout 為空。

    第一版用「stdout 空不空」分辨 → **查詢失敗被判成「這台沒有 task」**，
    正好推翻這個模組自己宣稱的「查不到 ≠ 沒有 task」。
    """
    _q_stub(monkeypatch, 1, out="", err="ERROR: Access is denied.")
    action, reason = wt.query_action()
    assert (action, reason) == (None, wt.UNREADABLE), (
        "★權限不足被判成『沒有 task』★")


def test_a_genuine_not_found_is_still_no_task(monkeypatch):
    """★反方向★ 真的找不到就要回 NO_TASK（否則每輪都告警）。"""
    _q_stub(monkeypatch, 1, err="ERROR: The system cannot find the file specified.")
    assert wt.query_action()[1] == wt.NO_TASK


def test_a_chinese_not_found_message_is_recognised(monkeypatch):
    _q_stub(monkeypatch, 1, err="錯誤: 系統找不到指定的檔案。")
    assert wt.query_action()[1] == wt.NO_TASK


def test_the_action_comes_from_xml_not_localized_text(monkeypatch):
    """★外審 P1-03B★ `/FO LIST /V` 是給人看的在地化輸出，不是機器契約。"""
    _q_stub(monkeypatch, 0,
            out=_XML % ("pythonw.exe",
                        '"%s" --once' % os.path.join("C:" + os.sep + "App",
                                                     wt.LAUNCHER_NAME)))
    action, reason = wt.query_action()
    assert reason == "" and action
    assert "--once" in action and wt.LAUNCHER_NAME in action


def test_the_query_uses_the_xml_switch(monkeypatch):
    seen = _q_stub(monkeypatch, 0, out=_XML % ("x.exe", "--once"))
    wt.query_action()
    assert seen and "/XML" in seen[0], f"沒有用 /XML：{seen}"
    assert "/FO" not in seen[0], "還在用給人看的 LIST 輸出"


def test_unparseable_xml_is_unreadable(monkeypatch):
    _q_stub(monkeypatch, 0, out="這不是 XML")
    assert wt.query_action()[1] == wt.UNREADABLE


def test_xml_without_a_command_is_unreadable(monkeypatch):
    _q_stub(monkeypatch, 0, out='<?xml version="1.0"?><Task><Actions/></Task>')
    assert wt.query_action()[1] == wt.UNREADABLE


# ── [外審 P1-03C] 遷移後要精確比對，不是「不含 legacy 就算過」 ───────────
@pytest.mark.parametrize("wrong,why", [
    ('pythonw.exe "{other}" --once', "指到錯的根"),
    ('pythonw.exe "{launcher}"', "缺 --once"),
    ('python.exe "{launcher}" --once', "換成別的執行檔"),
    ('cmd.exe /c something', "完全無關的動作"),
])
def test_a_wrong_but_nonlegacy_action_is_not_accepted(app, monkeypatch,
                                                      wrong, why):
    """★這些都不含 `watchdog_runner.py`，第一版會把它們當成成功★"""
    launcher = os.path.join(app, wt.LAUNCHER_NAME)
    other = os.path.join(str(app) + "_別的根", wt.LAUNCHER_NAME)
    after = wrong.format(launcher=launcher, other=other)
    _stub(monkeypatch, [(_LEGACY, ""), (after, "")])
    assert wt.migrate_if_legacy(app, pythonw="pythonw.exe") == wt.FAILED, why


def test_a_wrong_existing_action_is_rewritten_not_accepted(app, monkeypatch):
    """既不是 legacy 也不是預期值 → 要改寫，不可以當成「已經是對的」。"""
    wrong = 'python.exe "%s"' % os.path.join(app, wt.LAUNCHER_NAME)
    calls = _stub(monkeypatch, [(wrong, ""), (_want(app), "")])
    assert wt.migrate_if_legacy(app, pythonw="pythonw.exe") == wt.MIGRATED
    assert calls and "/Change" in calls[0], "沒有去改寫錯的 action"


@pytest.mark.parametrize("a,b", [
    (r'"x.exe" "C:\App\a.pyw" --once', r'x.exe C:\App\a.pyw --once'),
    (r'"X.EXE" "c:\app\A.PYW" --once', r'x.exe C:\App\a.pyw --once'),
    (r'x.exe "C:\App\.\a.pyw" --once', r'x.exe C:\App\a.pyw --once'),
])
def test_cosmetic_differences_are_not_treated_as_a_mismatch(a, b):
    r"""引號、大小寫、多餘的 `.` 路徑段 —— 這些不是真的不同。

    把它們當成不同,每一輪 `--once` 都會重寫一次排程(而且每次都記 log)。
    ★測試資料要用 raw string★:反斜線被跳脫掉之後,
    `C:\App` 會變成一個控制字元,測到的就不是路徑比對。
    """
    assert wt._same_action(a, b) is True


def test_an_empty_action_never_matches():
    assert wt._same_action("", "") is False
    assert wt._same_action(None, "x.exe a --once") is False


# ── _run 自己的契約（上面的測試都 stub 掉 _run，這一條不 stub）─────────────
class _CP:
    """★假的 CompletedProcess 要跟生產的呼叫形狀一致★

    `_run()` 現在是 `capture_output=True`【不指定 encoding】—— 也就是拿到
    bytes 再自己解碼（外審第 2 輪 #1）。回 str 的假物件測到的是別的東西。
    """

    def __init__(self, rc, out, err):
        self.returncode = rc
        self.stdout = out if isinstance(out, bytes) else out.encode("utf-8")
        self.stderr = err if isinstance(err, bytes) else err.encode("utf-8")


def test_run_returns_stderr_not_just_stdout(monkeypatch):
    """★核心★ 上面每一條 query_action 測試都把 `_run` 換掉了 ——

    所以「_run 到底有沒有把 stderr 帶回來」沒有任何測試會踩到。
    這正是原始缺陷所在的那一行:capture 了卻不回傳。
    這條測試改打在 `subprocess.run` 這一層,讓那一行真的被執行。
    """
    monkeypatch.setattr(wt.subprocess, "run",
                        lambda *a, **k: _CP(1, "", "ERROR: Access is denied."))
    rc, out, err = wt._run(["schtasks", "/Query"])
    assert rc == 1 and out == ""
    assert "denied" in err.lower(), "★stderr 被丟掉了 → 失敗會被判成沒有 task★"


def test_a_permission_failure_end_to_end_is_unreadable(monkeypatch):
    """同一件事走完整條路(不 stub _run):權限不足 ≠ 沒有 task。"""
    monkeypatch.setattr(wt.subprocess, "run",
                        lambda *a, **k: _CP(1, "", "ERROR: Access is denied."))
    assert wt.query_action()[1] == wt.UNREADABLE


def test_a_real_not_found_end_to_end_is_no_task(monkeypatch):
    monkeypatch.setattr(
        wt.subprocess, "run",
        lambda *a, **k: _CP(1, "", "ERROR: The system cannot find the file "
                                   "specified."))
    assert wt.query_action()[1] == wt.NO_TASK


def test_a_crashed_schtasks_is_unreadable(monkeypatch):
    """執行不起來(找不到 schtasks、逾時)→ 不可以當成「沒有 task」。"""
    def _boom(*a, **k):
        raise OSError("no schtasks here")
    monkeypatch.setattr(wt.subprocess, "run", _boom)
    assert wt._run(["schtasks"]) == (None, "", "")
    assert wt.query_action()[1] == wt.UNREADABLE


# ── [外審第 2 輪 #1] schtasks 的輸出編碼（實機量到的，不是推理） ──────────
def _cp(out=b"", err=b"", rc=0):
    return _CP(rc, out, err)


def test_a_utf16_bom_payload_is_decoded(monkeypatch):
    """有些 Windows 版本／語系會吐 BOM 標示的 UTF-16。"""
    xml = _XML % ("pythonw.exe", '"C:\\App\\守護.pyw" --once')
    monkeypatch.setattr(wt.subprocess, "run",
                        lambda *a, **k: _cp(out=xml.encode("utf-16")))
    action, reason = wt.query_action()
    assert reason == "" and "守護.pyw" in (action or ""), action


def test_an_oem_codepage_payload_is_decoded(monkeypatch):
    """★本機實測就是這一種★ cp936 單位元組，不是 UTF-16。

    2026-08-09 在本機（GetACP=GetOEMCP=936）對真實 schtasks 量到：
    `/XML` 導到 pipe 時吐的是單位元組的 code page 內容，**不是**
    BOM 標示的 UTF-16（雖然 XML 宣告寫著 `encoding="UTF-16"`）。
    用 `encoding="utf-8", errors="replace"` 解，中文整片變成 U+FFFD ——
    而我們的排程名與 launcher **每一個字都是中文**。
    後果不是「偶爾解錯」：action 永遠對不上 `desired_action()`，
    於是每兩分鐘改寫一次排程、回讀又永遠不符，舊排程一次也遷移不成功。
    """
    xml = _XML % ("pythonw.exe", '"C:\\App\\' + wt.LAUNCHER_NAME + '" --once')
    try:
        payload = xml.encode("cp936")
    except UnicodeEncodeError:                     # pragma: no cover
        pytest.skip("這台機器的 cp936 編不了這些字")
    monkeypatch.setattr(wt.subprocess, "run", lambda *a, **k: _cp(out=payload))
    action, _r = wt.query_action()
    assert "\ufffd" not in (action or ""), "★中文被解成替換字元★：%r" % (action,)
    assert wt.LAUNCHER_NAME in (action or ""), action


def test_a_utf8_payload_still_works(monkeypatch):
    xml = _XML % ("pythonw.exe", '"C:\\App\\' + wt.LAUNCHER_NAME + '" --once')
    monkeypatch.setattr(wt.subprocess, "run",
                        lambda *a, **k: _cp(out=xml.encode("utf-8")))
    assert wt.LAUNCHER_NAME in (wt.query_action()[0] or "")


def test_undecodable_bytes_never_raise(monkeypatch):
    """全都解不開也不可以拋 —— 回 UNREADABLE 才是誠實的。"""
    junk = bytes([0x00, 0x01, 0xFF, 0xFE, 0x80, 0x7A])
    monkeypatch.setattr(wt.subprocess, "run", lambda *a, **k: _cp(out=junk))
    assert wt.query_action()[1] == wt.UNREADABLE


def test_the_decoder_prefers_utf8_over_the_codepage():
    """UTF-8 先試（嚴格）；它過得了就不要再用 code page 解一次。"""
    assert wt._decode_console("中文".encode("utf-8")) == "中文"


def test_the_decoder_handles_empty_output():
    assert wt._decode_console(b"") == ""


def test_the_command_is_not_decoded_as_utf8_by_subprocess():
    """★守衛★ `_run` 一旦又指定 encoding=，上面那幾條就全部測不到東西了。"""
    import ast as _ast
    import inspect
    import textwrap
    src = textwrap.dedent(inspect.getsource(wt._run))
    for call in _ast.walk(_ast.parse(src)):
        if not isinstance(call, _ast.Call):
            continue
        kws = {k.arg for k in call.keywords}
        assert "encoding" not in kws and "text" not in kws, (
            "★subprocess 又自己解碼了 → 中文會變成 U+FFFD★")


# ── [外審第 2 輪 #4] Command 含空白 ────────────────────────────────────────
def test_a_command_with_spaces_still_matches(monkeypatch):
    """★核心★ XML 的 `<Command>` 是結構化欄位，本身【不含引號】。

    直接 `cmd + " " + args` 串起來的話，Python 裝在
    `C:\\Program Files\\...\\pythonw.exe` 的機器上，`_norm_action()` 會把
    執行檔拆成兩個 token → 永遠對不上 `desired_action()` 的 quoted 形式 →
    正確的排程每兩分鐘被重寫一次，而且回讀永遠判失敗。
    """
    exe = r"C:\Program Files\Py\pythonw.exe"
    app = r"C:\App"
    xml = _XML % (exe, '"%s" --once' % os.path.join(app, wt.LAUNCHER_NAME))
    monkeypatch.setattr(wt.subprocess, "run",
                        lambda *a, **k: _cp(out=xml.encode("utf-8")))
    action, _r = wt.query_action()
    want = wt.desired_action(app, pythonw=exe)
    assert wt._same_action(action, want), "%r != %r" % (action, want)


def test_a_command_with_spaces_is_reported_as_already_correct(app, monkeypatch):
    """整條 migrate 路徑：正確的排程不可以每輪都被重寫。"""
    exe = r"C:\Program Files\Py\pythonw.exe"
    xml = _XML % (exe, '"%s" --once' % os.path.join(app, wt.LAUNCHER_NAME))
    calls = []

    def _run(args, timeout=20.0):
        calls.append(args)
        return (0, xml, "")

    monkeypatch.setattr(wt, "_run", _run)
    assert wt.migrate_if_legacy(app, pythonw=exe) == wt.OK_ALREADY
    assert not any("/Change" in c for c in calls), "正確的排程被重寫了"


def test_an_already_quoted_command_is_not_double_quoted():
    """有些 task 的 Command 自己就帶引號 —— 不可以再包一層。"""
    args = r'"C:\App\x.pyw" --once'
    a = wt._join_action(r'"C:\P F\py.exe"', args)
    b = wt._join_action(r'C:\P F\py.exe', args)
    assert a == b, "%r != %r" % (a, b)


def test_an_action_without_arguments_is_still_usable():
    assert wt._join_action(r"C:\a b\x.exe", "") == r'"C:\a b\x.exe"'


def test_an_empty_command_joins_to_nothing():
    assert wt._join_action("", "--once") == ""


def test_the_xml_declaration_is_stripped():
    """`<?xml ... encoding="UTF-16"?>` 對已經解好的 str 沒用，還可能讓 ET 拋。"""
    decl = '<?xml version="1.0" encoding="UTF-16"?>' + chr(10) + "<a/>"
    assert wt._strip_xml_declaration(decl) == "<a/>"
    assert wt._strip_xml_declaration("<a/>") == "<a/>"
    assert wt._strip_xml_declaration("") == ""
