# -*- coding: utf-8 -*-
"""[2026-07-30 第二輪外審 P2-07] CI 關卡本身也要被測。

★守衛自己的失效模式是第一級問題★
這一批新關卡（覆蓋率門檻、型別債棘輪、skip 數量守衛、相依弱點掃描）都是「用來擋
問題」的東西 —— 它們如果安靜地失效（讀不到檔就回 0、找不到分層就當通過），會比
沒有它們更糟：紅燈不見了，大家以為一切正常。

所以這裡逐條測「查不到／壞掉的時候會不會誤判成通過」。
"""
import io
import json
import os
import subprocess
import sys

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS)

import check_coverage  # noqa: E402
import check_skips  # noqa: E402


def _run(script: str, *args) -> subprocess.CompletedProcess:
    # ★encoding="utf-8" 而不是 text=True★
    #   關卡腳本自己把 stdout 轉成 UTF-8（_make_stdout_robust）；這裡若用
    #   `text=True`（locale 解碼）就會在 cp936 的機器上讀到亂碼，中文斷言全部失敗
    #   —— 跟這一輪外審抓到的 pyright 解碼問題是同一個根因。
    return subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, script), *args],
        cwd=REPO_ROOT, capture_output=True,
        encoding="utf-8", errors="replace", check=False)


# ─── requirements.txt 必須是純 ASCII ──────────────────────────────────────
def test_requirements_is_ascii_only():
    """★這不是潔癖，是「裝不裝得起來」★

    pip 讀 requirements 檔用的是【系統 locale 的編碼】，不是固定 UTF-8。只要有一個
    非 ASCII 位元組，在 locale 不是 UTF-8 的機器上（cp936 / cp1252 …）
    `pip install -r requirements.txt` 會直接死於 UnicodeDecodeError —— 程式根本
    裝不起來。2026-07-30 在一台 cp936 的機器上實測確認過（乾淨 venv 只裝到 4 個套件）。
    中文說明放 docs/requirements_rationale.md。
    """
    raw = open(os.path.join(REPO_ROOT, "requirements.txt"), "rb").read()
    bad = [(i, b) for i, b in enumerate(raw) if b > 127]
    assert not bad, (
        f"requirements.txt 有 {len(bad)} 個非 ASCII 位元組（前幾個位置：{bad[:5]}）"
        "—— locale 不是 UTF-8 的機器會裝不起來")


def test_the_pillow_floor_is_past_its_own_fix():
    """Pillow 11.x 有 20+ 個已知弱點，修正版是 12.3.0；舊的 `<12` 上限把修正擋在外面。"""
    text = io.open(os.path.join(REPO_ROOT, "requirements.txt"),
                   encoding="utf-8").read()
    assert "Pillow>=12.3.0,<13" in text


# ─── 覆蓋率門檻 ────────────────────────────────────────────────────────────
def _cov_json(shared_pct: float, entry_pct: float, tmp_path):
    """做一份最小的 coverage json（分層由路徑決定）。"""
    def f(cov, tot):
        return {"summary": {"covered_lines": cov, "num_statements": tot}}

    files = {
        "src/cmuh_common/a.py": f(int(shared_pct), 100),
        "src/main.py": f(int(entry_pct), 100),
    }
    total_cov = int(shared_pct) + int(entry_pct)
    data = {"files": files,
            "totals": {"covered_lines": total_cov, "num_statements": 200,
                       "percent_covered": total_cov / 2.0}}
    p = tmp_path / "cov.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return str(p)


def test_coverage_classifies_shared_and_entrypoints():
    assert check_coverage.classify("src/cmuh_common/mail_quota.py") == "shared"
    assert check_coverage.classify("src\\cmuh_common\\roster\\ui\\duty.py") == "shared"
    assert check_coverage.classify("src/main.py") == "entrypoints"
    assert check_coverage.classify("src/consult_query.py") == "entrypoints"


def test_coverage_gate_passes_above_the_floors(tmp_path):
    cp = _run("check_coverage.py", _cov_json(95, 95, tmp_path))
    assert cp.returncode == 0, cp.stdout


def test_coverage_gate_fails_when_the_shared_layer_regresses(tmp_path):
    """★分層的理由★ 共用層掉到 10%，但全域仍有 55%（被 main.py 撐著）。
    只看全域門檻的話這種退步是綠燈。"""
    cp = _run("check_coverage.py", _cov_json(10, 100, tmp_path))
    assert cp.returncode == 1
    assert "shared" in cp.stdout


def test_coverage_gate_fails_when_a_layer_disappears(tmp_path):
    """★分層消失不可當成通過★ —— classify() 跟不上目錄搬動時，
    「這一層沒有任何檔案」會讓門檻無聲失效。"""
    data = {"files": {"src/main.py": {"summary": {"covered_lines": 90,
                                                  "num_statements": 100}}},
            "totals": {"covered_lines": 90, "num_statements": 100,
                       "percent_covered": 90.0}}
    p = tmp_path / "cov.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    cp = _run("check_coverage.py", str(p))
    assert cp.returncode == 1
    assert "找不到任何檔案" in cp.stdout


def test_coverage_floors_file_matches_the_layers_the_script_produces():
    """門檻檔裡的鍵一定要是 classify() 真的會產生的分層（打錯字＝那條門檻永遠不生效）。"""
    with open(os.path.join(REPO_ROOT, "coverage_floors.json"),
              encoding="utf-8") as fh:
        floors = {k for k in json.load(fh) if not k.startswith("//")}
    assert floors == {"shared", "entrypoints", "total"}


# ─── skip 數量守衛 ─────────────────────────────────────────────────────────
def _junit(tmp_path, n_skipped: int, extra: str = ""):
    cases = "".join(
        f'<testcase classname="t" name="t{i}"><skipped message="why{i}"/></testcase>'
        for i in range(n_skipped))
    xml = (f'<?xml version="1.0"?><testsuites><testsuite name="pytest">'
           f'<testcase classname="t" name="ok"/>{cases}{extra}'
           f'</testsuite></testsuites>')
    p = tmp_path / "junit.xml"
    p.write_text(xml, encoding="utf-8")
    return str(p)


def test_skip_guard_counts_skips_from_junit(tmp_path):
    count, rows = check_skips.parse_junit(_junit(tmp_path, 3))
    assert count == 3
    assert rows[0][1] == "why0"


def _expectations(tmp_path, entries):
    p = tmp_path / "skip_expectations.json"
    p.write_text(json.dumps({"expected": entries}, ensure_ascii=False),
                 encoding="utf-8")
    return str(p)


def _junit_with_reasons(tmp_path, reasons, classname="t"):
    cases = "".join(
        f'<testcase classname="{classname}" name="t{i}">'
        f'<skipped message="{why}"/></testcase>'
        for i, why in enumerate(reasons))
    xml = (f'<?xml version="1.0"?><testsuites><testsuite name="pytest">'
           f'<testcase classname="{classname}" name="ok"/>{cases}'
           f'</testsuite></testsuites>')
    p = tmp_path / "junit.xml"
    p.write_text(xml, encoding="utf-8")
    return str(p)


def _guard(monkeypatch, tmp_path, reasons, entries, classname="t"):
    """就地跑守衛（不開子行程，才能換掉宣告檔路徑）→ (returncode, stdout)。"""
    monkeypatch.setattr(check_skips, "EXPECTATIONS_PATH",
                        _expectations(tmp_path, entries))
    junit = _junit_with_reasons(tmp_path, reasons, classname)
    import io as _io
    import contextlib
    buf = _io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = check_skips.main([junit])
    return rc, buf.getvalue()


_OK_ENTRY = {"id": "dirty", "reason_contains": "工作區有未提交變更",
             "max": 2, "why": "本機開發中才會發生，CI 的 checkout 永遠乾淨"}


def test_an_undeclared_skip_reason_fails_even_though_it_is_only_one(
        monkeypatch, tmp_path):
    """★這次改版的核心★

    舊版是一個數字上限。那個數字漏算了一支平台條件 skip，CI 因此連紅四輪；
    而紅燈時最順手的反應是「把數字調大」—— 一調大，「42 支被靜默關掉」也一起
    放行了。現在判準改成「這個 skip 理由有沒有被宣告過」，數量多寡不是重點。
    """
    rc, out = _guard(monkeypatch, tmp_path, ["某個沒人宣告過的理由"], [_OK_ENTRY])
    assert rc == 1
    assert "未宣告" in out


def test_a_declared_skip_passes_within_its_cap(monkeypatch, tmp_path):
    rc, out = _guard(monkeypatch, tmp_path,
                     ["工作區有未提交變更,index 與工作目錄本來就會不同"] * 2,
                     [_OK_ENTRY])
    assert rc == 0, out


def test_a_declared_skip_fails_when_it_grows_past_its_cap(
        monkeypatch, tmp_path):
    """★這條守的是「多加一個 Tk 測試檔就讓別檔 42 支全變 skip」★
    那次全套仍是綠燈，而且順序一換受害者就換人。理由對得上也不能無限多。"""
    rc, out = _guard(monkeypatch, tmp_path,
                     ["工作區有未提交變更"] * 3, [_OK_ENTRY])
    assert rc == 1
    assert "變多了" in out


def test_the_nodeid_constraint_keeps_a_reason_from_covering_other_files(
        monkeypatch, tmp_path):
    """理由字串相同但出現在別的測試檔 → 不算被宣告過。

    否則一句常見的理由（「未安裝」之類）會變成一張跨檔案的萬用通行證。
    """
    entry = dict(_OK_ENTRY, nodeid_contains="test_push_helper_antirevert")
    rc, _ = _guard(monkeypatch, tmp_path, ["工作區有未提交變更"], [entry],
                   classname="tests.test_push_helper_antirevert")
    assert rc == 0
    rc2, out2 = _guard(monkeypatch, tmp_path, ["工作區有未提交變更"], [entry],
                       classname="tests.test_something_else")
    assert rc2 == 1, out2


def test_the_guard_fails_when_the_expectations_file_is_missing(
        monkeypatch, tmp_path):
    """★守衛自己失效＝失敗★

    「檔案不見了」跟「沒有任何預期 skip」在磁碟上長得一樣，意義卻相反 ——
    前者代表這道關卡失去了判準，不可以當成後者放行。
    """
    monkeypatch.setattr(check_skips, "EXPECTATIONS_PATH",
                        str(tmp_path / "nope.json"))
    import io as _io
    import contextlib
    buf = _io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = check_skips.main([_junit_with_reasons(tmp_path, [])])
    assert rc == 1
    assert "沒有宣告檔就沒有判準" in buf.getvalue()


def test_an_expectation_without_a_why_is_rejected(monkeypatch, tmp_path):
    """沒寫理由的白名單條目＝小型的「把數字調大」。"""
    rc, out = _guard(monkeypatch, tmp_path, [],
                     [{"id": "x", "reason_contains": "隨便", "max": 1}])
    assert rc == 1
    assert "宣告檔" in out


def test_the_repo_expectations_file_is_usable():
    """repo 裡那份宣告檔本身要能解析，而且每條都有理由。"""
    entries = check_skips.load_expectations()
    assert entries, "至少要有一條（本機髒工作區的兩支）"
    for e in entries:
        assert e["why"].strip()
        assert e["max"] >= 0


def test_it_emits_a_github_annotation_so_the_failure_is_readable(
        monkeypatch, tmp_path):
    """★可觀測性★ job log 要 repo admin 才下載得到，annotation 走 check-runs API
    是公開可讀的。這道關卡紅了四輪都看不出是哪幾支被 skip —— 關卡說不清楚自己
    為什麼紅，跟沒有關卡差不了多少。"""
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    rc, out = _guard(monkeypatch, tmp_path, ["沒宣告過的理由"], [_OK_ENTRY])
    assert rc == 1
    assert "::error title=" in out
    assert "%0A" in out, "多行訊息要轉義，否則 annotation 只會留下第一行"


def test_no_annotation_noise_outside_github_actions(monkeypatch, tmp_path):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    _rc, out = _guard(monkeypatch, tmp_path, ["沒宣告過的理由"], [_OK_ENTRY])
    assert "::error" not in out


def test_skip_guard_fails_when_it_cannot_read_the_report(tmp_path):
    """★守衛自己失效＝失敗，不是通過★"""
    cp = _run("check_skips.py", str(tmp_path / "nope.xml"))
    assert cp.returncode == 1
    assert "讀不到" in cp.stdout


def test_skip_guard_fails_on_a_corrupt_report(tmp_path):
    p = tmp_path / "junit.xml"
    p.write_text("<not xml", encoding="utf-8")
    cp = _run("check_skips.py", str(p))
    assert cp.returncode == 1


# ─── 型別債棘輪 ────────────────────────────────────────────────────────────
def test_the_type_debt_baseline_only_lists_disabled_rules():
    """★基線只能記【目前關掉的】規則★

    如果某條規則已經在 pyrightconfig.json 啟用了，它的錯誤數必然是 0，留在基線裡
    只會讓人以為「還欠著這麼多」。反過來，基線裡漏掉某條被關掉的規則，那條就
    完全沒有棘輪保護。
    """
    with open(os.path.join(REPO_ROOT, "pyrightconfig.json"), encoding="utf-8") as fh:
        cfg = json.load(fh)
    with open(os.path.join(REPO_ROOT, "type_debt_baseline.json"),
              encoding="utf-8") as fh:
        baseline = {k for k in json.load(fh) if not k.startswith("//")}

    # reportMissingModuleSource 不算「型別債」：它是「pywin32 等第三方套件沒有隨附
    # stub」，不是我們寫錯的型別，也不是我們修得掉的東西。
    not_our_debt = {"reportMissingModuleSource"}
    disabled = {k for k, v in cfg.items()
                if k.startswith("report") and v == "none"} - not_our_debt
    assert baseline == disabled, (
        f"基線與 pyrightconfig 的關閉清單不一致：\n"
        f"  只在基線: {sorted(baseline - disabled)}\n"
        f"  只在設定: {sorted(disabled - baseline)}")


def test_the_two_clean_rules_are_actually_enabled():
    """實測為 0 的規則要真的打開 —— 否則「已經沒有債」這件事沒有被鎖住。"""
    with open(os.path.join(REPO_ROOT, "pyrightconfig.json"), encoding="utf-8") as fh:
        cfg = json.load(fh)
    assert cfg.get("reportOptionalIterable") == "error"
    assert cfg.get("reportRedeclaration") == "error"


# ─── 相依弱點掃描的允許清單 ────────────────────────────────────────────────
def test_the_audit_allowlist_is_valid_and_documents_every_entry():
    """★允許清單本身就是攻擊面★ 每一筆都要有理由字串，不可留空。"""
    with open(os.path.join(REPO_ROOT, "security", "pip_audit_allowlist.json"),
              encoding="utf-8") as fh:
        raw = json.load(fh)
    for section in ("dev_only_packages", "accepted_vulns"):
        for key, why in raw.get(section, {}).items():
            assert isinstance(why, str) and why.strip(), (
                f"{section} 的 {key} 沒有寫理由")


def test_shipped_packages_are_not_in_the_dev_only_allowlist():
    """★判準是「會不會進到診間那台電腦」★
    把出貨用的套件放進「開發工具」清單，等於把它的 CVE 永久靜音。
    """
    with open(os.path.join(REPO_ROOT, "security", "pip_audit_allowlist.json"),
              encoding="utf-8") as fh:
        dev_only = {k.lower() for k in json.load(fh)["dev_only_packages"]}
    shipped = set()
    for line in io.open(os.path.join(REPO_ROOT, "requirements.txt"),
                        encoding="utf-8"):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        name = line.split(">=")[0].split("==")[0].split("<")[0].strip()
        if name:
            shipped.add(name.lower())
    overlap = shipped & dev_only
    assert not overlap, f"這些是出貨相依，不可當成開發工具靜音：{sorted(overlap)}"


# ─── workflow 本身 ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("gate", [
    "scripts/check_coverage.py",
    "scripts/type_debt.py",
    "scripts/check_skips.py",
])
def test_every_gate_is_actually_wired_into_ci(gate):
    """★寫了關卡但沒接上 CI＝沒有關卡★（這個 repo 的老病灶的另一種長相）"""
    text = io.open(os.path.join(REPO_ROOT, ".github", "workflows", "ci.yml"),
                   encoding="utf-8").read()
    assert gate in text, f"{gate} 沒有接進 ci.yml"


def test_the_security_workflow_covers_all_three_scanners():
    text = io.open(os.path.join(REPO_ROOT, ".github", "workflows",
                                "security.yml"), encoding="utf-8").read()
    assert "gitleaks" in text
    assert "scripts/audit_deps.py" in text
    assert "codeql-action" in text
    assert "schedule:" in text, "新 CVE 不會等人來 push 才出現 —— 要有排程"


# ─── ★外審第 1 輪的六個 finding★ ───────────────────────────────────────────
def test_gitleaks_does_not_exclude_whole_directories_of_source():
    r"""★P1：整片路徑略過＝那裡永遠不會告警★

    我第一版寫了 `paths = ['^tests/.*\.py$']`（理由是測試裡有假帳密）。那等於：
    真的 HIS 帳號或 Gmail App Password 只要貼進任何一支測試檔就【永遠不會被告警】，
    連掃完整個 git 歷史也看不到。路徑本身分辨不出真假帳密。

    ★要先剥掉註解才比對★：設定檔裡有一段註解在【解釋為什麼不能這樣寫】，
    直接對整份檔比對會被自己的註解騙過去（同一個坑本專案踩過幾次）。
    """
    lines = io.open(os.path.join(REPO_ROOT, ".gitleaks.toml"),
                    encoding="utf-8").read().splitlines()
    code = " ".join(ln for ln in lines if not ln.lstrip().startswith("#"))
    assert "^tests/" not in code, "不可用路徑整片略過測試目錄"
    assert "^src/" not in code
    # 允許的路徑只能是第三方/非我方程式碼
    for allowed in ("python_embed", ".venv"):
        assert allowed in code


def test_gitleaks_allowlist_targets_the_fake_values_themselves():
    """允許清單要鎖在【假值本身】——值一變就重新告警，這正是我們要的。"""
    text = io.open(os.path.join(REPO_ROOT, ".gitleaks.toml"),
                   encoding="utf-8").read()
    for fake in ("A123456789", "secret", "cmuhdermatology@gmail"):
        assert fake in text, f"測試用的假值 {fake} 應該逐一列在允許清單"


def test_lazy_runtime_deps_are_declared_in_one_shared_manifest():
    """★P2：security job 沒掃到 lazy 相依★

    ortools / openpyxl / python-docx / reportlab 是「用到才裝」，但那仍然是
    【裝到診間電腦上】—— 它們就是 production 相依。舊版把套件名寫死在 ci.yml，
    security workflow 根本沒裝、也就沒掃。
    """
    lazy = io.open(os.path.join(REPO_ROOT, "requirements-lazy.txt"),
                   encoding="utf-8").read()
    for pkg in ("ortools", "openpyxl", "python-docx", "reportlab"):
        assert pkg in lazy, f"{pkg} 應列在 requirements-lazy.txt"
    for wf in ("ci.yml", "security.yml"):
        text = io.open(os.path.join(REPO_ROOT, ".github", "workflows", wf),
                       encoding="utf-8").read()
        assert "requirements-lazy.txt" in text, f"{wf} 沒有共用 lazy manifest"


def test_requirements_lazy_is_ascii_only():
    raw = open(os.path.join(REPO_ROOT, "requirements-lazy.txt"), "rb").read()
    assert not [b for b in raw if b > 127]


def test_the_ortools_pin_matches_the_code():
    """版本釘在兩個地方就一定會不一致 —— 這裡把它們綁在一起。"""
    lazy = io.open(os.path.join(REPO_ROOT, "requirements-lazy.txt"),
                   encoding="utf-8").read()
    src = io.open(os.path.join(REPO_ROOT, "src", "cmuh_common", "roster",
                               "__init__.py"), encoding="utf-8").read()
    import re
    pinned = re.search(r'ORTOOLS_PINNED_VERSION\s*=\s*"([^"]+)"', src).group(1)
    assert f"ortools=={pinned}" in lazy


def test_production_transitives_are_not_labelled_dev_only():
    """★P2：transitive 相依也是 production 相依★

    `python-dotenv`（webdriver-manager 帶的）與 `protobuf`（ortools 帶的）都會裝到
    診間電腦上。放進 dev-only 等於把它們現在與未來的所有弱點永久靜音。
    """
    with open(os.path.join(REPO_ROOT, "security", "pip_audit_allowlist.json"),
              encoding="utf-8") as fh:
        dev_only = {k.lower() for k in json.load(fh)["dev_only_packages"]}
    for pkg in ("python-dotenv", "protobuf", "pyasn1"):
        assert pkg not in dev_only, f"{pkg} 是 production transitive，不可標成 dev-only"


def test_ci_does_not_gate_skips_on_a_magic_number():
    """★[2026-07-31] 舊版在 ci.yml 設 `CMUH_MAX_SKIPPED: "0"`★

    那個 0 是推理出來的、不是量出來的（漏看了一支平台條件 skip），CI 從加上這道
    關卡起連紅四輪。判準現在寫在 skip_expectations.json，ci.yml 不該再有數字。
    """
    text = io.open(os.path.join(REPO_ROOT, ".github", "workflows", "ci.yml"),
                   encoding="utf-8").read()
    body = "\n".join(ln for ln in text.splitlines()
                     if not ln.lstrip().startswith("#"))
    assert "CMUH_MAX_SKIPPED" not in body
    assert os.path.exists(os.path.join(REPO_ROOT, "skip_expectations.json"))


def test_every_ratchet_reports_even_when_an_earlier_one_is_red():
    """★一輪要拿到全部棘輪的結果★

    沒有 always() 時，skip 守衛一紅就整串跳過 —— 覆蓋率門檻與型別債棘輪因此
    【從來沒有在 CI 上執行過】（2026-07-31 查 run 293-296 才發現）。修一道關卡
    就要再等一趟 CI 才知道下一道過不過。
    """
    text = io.open(os.path.join(REPO_ROOT, ".github", "workflows", "ci.yml"),
                   encoding="utf-8").read()
    for step in ("check_skips.py", "check_coverage.py", "type_debt.py"):
        i = text.index(step)
        # 往前找這一步的 `- name:`，確認區塊裡有 always()
        block = text[text.rindex("- name:", 0, i):i]
        assert "if: always()" in block, f"{step} 那一步少了 if: always()"


def test_the_audit_wrapper_refuses_an_incomplete_scan():
    """★P2：掃不完不可當成「沒有弱點」★"""
    import inspect

    sys.path.insert(0, SCRIPTS)
    import audit_deps
    src = inspect.getsource(audit_deps._run_pip_audit)
    assert "--strict" in src, "要用 strict collection，否則收集失敗會被靜默跳過"
    assert "returncode not in (0, 1)" in src, "非 0/1 的離開碼是執行錯誤，必須失敗"
    assert '"dependencies" not in report' in src, "schema 改了不可當成零弱點"
    assert "skipped" in src, "有套件被跳過就是沒掃完"


def _run_ratchet(monkeypatch, baseline: dict, current: dict):
    """就地跑棘輪（把 pyright 換掉 —— 真跑要 10 分鐘）→ (returncode, stdout)。"""
    import contextlib
    import io as _io

    import type_debt
    monkeypatch.setattr(type_debt, "_load_baseline", lambda: baseline)
    monkeypatch.setattr(type_debt, "_rule_diagnostics",
                        lambda rule: current.get(rule, {}))
    buf = _io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = type_debt.main([])
    return rc, buf.getvalue()


_RULE = "reportArgumentType"


def test_a_new_diagnostic_is_still_a_failure(monkeypatch):
    """★棘輪的主要目的★ 新增型別債要紅 —— 這個沒有被放寬。"""
    rc, out = _run_ratchet(monkeypatch, {_RULE: {"a.py|f|msg|code": 1}},
                           {_RULE: {"a.py|f|msg|code": 1, "b.py|g|msg2|code2": 1}})
    assert rc == 1
    assert "新增了型別債" in out


def test_a_disappearing_diagnostic_is_a_warning_not_a_failure(monkeypatch):
    """★[2026-07-31] 從紅燈降為警告★

    指紋是 pyright 的診斷，而診斷取決於每個已安裝套件的 stubs。requirements.txt
    刻意用範圍（好讓診間電腦拿得到安全更新），所以 CI 與開發機的版本本來就不一致：
    beautifulsoup4 4.13.5→4.15.0 就讓 main.py 少掉 15 筆診斷，與被推送的改動無關。
    這道關卡因此連紅五輪、後面的步驟從沒跑過 —— 那正是它自己說要避免的
    「永遠紅燈因而被忽略的關卡」。

    代價（誠實記下）：「債被修好、沒人 --update、日後又寫回一模一樣的診斷」
    這條路現在不會紅。
    """
    rc, out = _run_ratchet(monkeypatch,
                           {_RULE: {"a.py|f|msg|code": 1, "b.py|g|msg2|c2": 1}},
                           {_RULE: {"a.py|f|msg|code": 1}})
    assert rc == 0, out
    assert "不見了" in out


def test_added_wins_when_both_happen(monkeypatch):
    """同時有新增與消失時，仍然要紅（不可被「消失」的寬鬆蓋過去）。"""
    rc, _out = _run_ratchet(monkeypatch, {_RULE: {"a.py|f|msg|code": 1}},
                            {_RULE: {"b.py|g|msg2|c2": 1}})
    assert rc == 1


def test_the_ratchet_prints_the_environment_it_measured(monkeypatch):
    """★「本機綠、CI 紅」要能一眼對照★

    這道關卡紅了五輪，我卻只能用推論猜它卡在哪 —— 因為 job log 要 repo admin
    才下載得到，而失敗分支當時根本沒發 annotation。現在每次都把量測環境印出來。
    """
    _rc, out = _run_ratchet(monkeypatch, {_RULE: {}}, {_RULE: {}})
    assert "環境：" in out and "python=" in out
    assert "beautifulsoup4=" in out and "ortools=" in out


def test_the_ratchet_annotates_instead_of_dying_silently(monkeypatch):
    """關卡自己爆掉時也要說得出話（不然「爆炸」跟「判定失敗」長得一樣）。"""
    import type_debt
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(type_debt, "_load_baseline",
                        lambda: (_ for _ in ()).throw(OSError("基線讀不到")))
    import contextlib
    import io as _io
    buf = _io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = type_debt._main_guarded([])
    assert rc == 1
    assert "::error title=" in buf.getvalue()


def test_the_type_debt_ratchet_compares_diagnostics_not_totals():
    """★P3：總數會被「修一個、加一個」抵銷★"""
    import type_debt
    baseline = {"a.py|msg1": 1, "a.py|msg2": 1}
    swapped = {"a.py|msg1": 1, "b.py|msg3": 1}      # 總數一樣，但換了一個
    added, gone = type_debt.diff_counts(baseline, swapped)
    assert added == {"b.py|msg3": 1}
    assert gone == {"a.py|msg2": 1}


def test_the_type_debt_fingerprint_is_independent_of_checkout_location():
    """★指紋不可包含「repo 被 clone 到哪裡」★
    否則 CI 上每一個指紋都算「新的」→ 棘輪紅燈上線 → 被忽略。"""
    import type_debt
    for raw in (r"C:\Users\A\Desktop\repo\src\main.py",
                "/home/runner/work/repo/repo/src/main.py",
                "d:/other/place/src/main.py"):
        fp = type_debt.fingerprint({"file": raw, "message": "boom"})
        assert fp.split("|")[0] == "src/main.py", fp
        assert "boom" in fp


def test_the_type_debt_fingerprint_drops_line_numbers():
    """行號會因為上下增刪幾行整批改變 —— 含行號的棘輪每次都紅，然後被忽略。"""
    import type_debt
    fp = type_debt.fingerprint(
        {"file": "/x/src/a.py", "message": "boom", "range": {"start": {"line": 9}}})
    assert "9" not in fp.split("|")[0]


def test_the_type_debt_baseline_paths_are_all_relative():
    with open(os.path.join(REPO_ROOT, "type_debt_baseline.json"),
              encoding="utf-8") as fh:
        raw = json.load(fh)
    for rule, fps in raw.items():
        if rule.startswith("//"):
            continue
        for fp in fps:
            path = fp.split("|", 1)[0]
            assert path.startswith("src/"), f"{rule}: 指紋路徑不是相對的：{path}"


# ─── ★外審第 2 輪★ 型別債指紋的兩個洞 ─────────────────────────────────────
def test_pyright_output_is_decoded_as_utf8_not_locale():
    """★P2：locale 解碼會讓基線在別台機器上整批失配★

    `subprocess.run(text=True)` 是拿【系統 locale】解碼；pyright 吐 UTF-8，而它的
    診斷訊息用 U+00A0 做縮排 —— 在 cp936 的機器上那個位元組被解成「聽」。
    第一版基線 273 個指紋裡有 83 個中鏢；CI runner 的代碼頁不同 → 每個指紋都算
    「新的」→ 棘輪紅燈上線 → 被忽略。
    """
    import inspect

    import type_debt
    # ★要先剝掉註解★：那段註解本身在解釋「為什麼不能用 text=True」，
    # 直接對原始碼比對會被自己的註解騙過去。
    raw = inspect.getsource(type_debt._rule_diagnostics)
    src = "\n".join(ln.split("#", 1)[0] for ln in raw.splitlines())
    assert 'encoding="utf-8"' in src
    assert "text=True" not in src, "text=True 是拿 locale 解碼"


def test_the_baseline_has_no_locale_mojibake():
    with open(os.path.join(REPO_ROOT, "type_debt_baseline.json"),
              encoding="utf-8") as fh:
        raw = json.load(fh)
    hits = [fp for rule, fps in raw.items() if not rule.startswith("//")
            for fp in fps if "\u807d" in fp]
    assert not hits, f"基線含 cp936 mojibake（{len(hits)} 筆）：{hits[:2]}"


def test_the_fingerprint_includes_the_source_line(tmp_path):
    """★P3：同檔同訊息換位置也要擋得住★

    只用 `檔案|訊息` 的話，「修掉一個、在同一個檔案別的地方又寫出一個完全相同的錯」
    計數不變 → 棘輪看不到。
    """
    import type_debt
    src = tmp_path / "src"
    src.mkdir()
    f = src / "a.py"
    f.write_text("alpha = 1\nbeta = 2\n", encoding="utf-8")
    d1 = {"file": str(f), "message": "boom",
          "range": {"start": {"line": 0}}}
    d2 = {"file": str(f), "message": "boom",
          "range": {"start": {"line": 1}}}
    fp1, fp2 = type_debt.fingerprint(d1), type_debt.fingerprint(d2)
    assert fp1 != fp2, "同檔同訊息但不同程式碼行必須是不同指紋"
    assert fp1.endswith("|alpha = 1")
    assert fp2.endswith("|beta = 2")


def test_the_fingerprint_survives_pure_line_movement(tmp_path):
    """★但單純上下增刪行不可讓指紋變動★ —— 否則棘輪每次都紅，然後被忽略。"""
    import type_debt
    src = tmp_path / "src"
    src.mkdir()
    f = src / "a.py"
    f.write_text("alpha = 1\n", encoding="utf-8")
    before = type_debt.fingerprint(
        {"file": str(f), "message": "boom", "range": {"start": {"line": 0}}})
    f.write_text("# 上面插了兩行\n# 又一行\nalpha = 1\n", encoding="utf-8")
    after = type_debt.fingerprint(
        {"file": str(f), "message": "boom", "range": {"start": {"line": 2}}})
    assert before == after


def test_the_fingerprint_tolerates_an_unreadable_source_file():
    """讀不到原始碼不可讓整支腳本爆掉（回空字串，指紋仍可用）。"""
    import type_debt
    fp = type_debt.fingerprint(
        {"file": "/no/such/src/a.py", "message": "boom",
         "range": {"start": {"line": 3}}})
    assert fp == "src/a.py||boom|", fp


@pytest.mark.parametrize("script", [
    "type_debt.py", "check_coverage.py", "check_skips.py", "audit_deps.py",
])
def test_every_gate_script_survives_a_narrow_console(script):
    """★關卡不可死在「印不出自己的輸出」★

    這些關卡會原樣印出外部工具的訊息（pyright 用 U+2022 項目符號、pytest 的 skip
    理由…）。控制台是 cp936/cp1252 時 `print` 會拋 UnicodeEncodeError ——
    關卡於是因為【印不出來】而失敗，而不是因為它要擋的事情。實測踩過一次。
    """
    text = io.open(os.path.join(SCRIPTS, script), encoding="utf-8").read()
    assert "_make_stdout_robust()" in text, f"{script} 沒有讓輸出容錯"


def test_the_fingerprint_distinguishes_identical_lines_in_different_functions(
        tmp_path):
    """★[外審第 3 輪] 同檔、同訊息、同一行字面 —— 但在不同函式★

    基線裡真的有幾個計數 2-5 的重複指紋（同一行程式碼在同檔出現多次）。
    只用 `檔案|訊息|那一行` 的話，「修掉一個、在同檔別處又寫一個一模一樣的」
    計數不變 → 棘輪看不到。加上所在函式就分得出來。
    """
    import type_debt
    type_debt._SCOPE_CACHE.clear()
    src = tmp_path / "src"
    src.mkdir()
    f = src / "a.py"
    f.write_text(
        "def alpha():\n"
        "    cells = row.find_all('td')\n"
        "\n"
        "def beta():\n"
        "    cells = row.find_all('td')\n",
        encoding="utf-8")
    d1 = {"file": str(f), "message": "boom", "range": {"start": {"line": 1}}}
    d2 = {"file": str(f), "message": "boom", "range": {"start": {"line": 4}}}
    fp1, fp2 = type_debt.fingerprint(d1), type_debt.fingerprint(d2)
    assert fp1 != fp2, "同一行字面但在不同函式，必須是不同指紋"
    assert "|alpha|" in fp1 and "|beta|" in fp2


def test_the_scope_uses_the_innermost_enclosing_definition(tmp_path):
    """巢狀函式要取【最內層】—— 取外層會把不同的內層函式混成同一個範圍。"""
    import type_debt
    type_debt._SCOPE_CACHE.clear()
    src = tmp_path / "src"
    src.mkdir()
    f = src / "b.py"
    f.write_text(
        "class C:\n"
        "    def outer(self):\n"
        "        def inner():\n"
        "            x = 1\n",
        encoding="utf-8")
    fp = type_debt.fingerprint(
        {"file": str(f), "message": "m", "range": {"start": {"line": 3}}})
    assert "|C.outer.inner|" in fp, fp


def test_the_scope_is_empty_at_module_level_and_on_unparsable_files(tmp_path):
    """解析不了的檔案不可讓整支腳本爆掉（回空範圍，指紋仍可用）。"""
    import type_debt
    type_debt._SCOPE_CACHE.clear()
    src = tmp_path / "src"
    src.mkdir()
    mod = src / "c.py"
    mod.write_text("X = 1\n", encoding="utf-8")
    assert "||" in type_debt.fingerprint(
        {"file": str(mod), "message": "m", "range": {"start": {"line": 0}}})

    broken = src / "d.py"
    broken.write_text("def (((\n", encoding="utf-8")
    fp = type_debt.fingerprint(
        {"file": str(broken), "message": "m", "range": {"start": {"line": 0}}})
    assert fp.startswith("src/d.py||m|")


def test_the_baseline_fingerprints_all_have_the_scope_field():
    with open(os.path.join(REPO_ROOT, "type_debt_baseline.json"),
              encoding="utf-8") as fh:
        raw = json.load(fh)
    for rule, fps in raw.items():
        if rule.startswith("//"):
            continue
        for fp in fps:
            assert fp.count("|") >= 3, f"{rule}: 指紋沒有範圍欄位：{fp[:80]}"


# ─── ★[2026-08-01] --update 只能往上轉★ ──────────────────────────────────
def _floors_file(tmp_path, values):
    p = tmp_path / "coverage_floors.json"
    p.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
    return p


def _run_update(monkeypatch, tmp_path, floors, shared_pct, extra=()):
    """用真的 main() 跑 --update → 回更新後的門檻 dict 與 stdout。"""
    import contextlib
    import io as _io

    floors_p = _floors_file(tmp_path, floors)
    monkeypatch.setattr(check_coverage, "FLOORS_FILE", str(floors_p))
    cov = _cov_json(shared_pct, 50.0, tmp_path)
    buf = _io.StringIO()
    with contextlib.redirect_stdout(buf):
        check_coverage.main([cov, "--update", *extra])
    return json.loads(floors_p.read_text(encoding="utf-8")), buf.getvalue()


def test_update_raises_the_floor_when_coverage_improved(monkeypatch, tmp_path):
    got, _out = _run_update(monkeypatch, tmp_path,
                            {"shared": 70.0, "entrypoints": 0.0, "total": 0.0},
                            shared_pct=80.0)
    assert got["shared"] == 79.0, "漲上去要把欄杆跟著提上來"


def test_update_refuses_to_lower_the_floor(monkeypatch, tmp_path):
    """★會往下轉的棘輪不是棘輪★

    `--update` 原本無條件寫入「目前值 − 1」。覆蓋率只要小幅回落（正好在那 1 個
    百分點的波動範圍內），順手跑一次 --update 就把欄杆【調低】了 —— 而這支腳本
    自己的說明寫的是「覆蓋率提升之後請跑 --update 把欄杆跟著提上來」。
    """
    got, out = _run_update(monkeypatch, tmp_path,
                           {"shared": 85.0, "entrypoints": 0.0, "total": 0.0},
                           shared_pct=80)            # 只會算出 79.0
    assert got["shared"] == 85.0, "不可以被調低"
    assert "沒有【被調低" in out or "沒有】被調低" in out or "調低" in out


def test_allow_lower_makes_it_an_explicit_visible_action(monkeypatch,
                                                         tmp_path):
    """真的要降是可以的 —— 但要特別寫出 --allow-lower。"""
    got, _out = _run_update(monkeypatch, tmp_path,
                            {"shared": 85.0, "entrypoints": 0.0, "total": 0.0},
                            shared_pct=80, extra=("--allow-lower",))
    assert got["shared"] == 79.0
