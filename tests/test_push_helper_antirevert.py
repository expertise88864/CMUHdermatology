# -*- coding: utf-8 -*-
"""[2026-07-27 事故防護] push_helper 防還原檢查。

OneDrive 兩次（v2026.07.24.4 / v2026.07.27.4）在【pytest 綠燈之後、git add 之前】
把未提交的 src 還原成舊版 → 關卡驗新碼、commit 進舊碼，HEAD 自相矛盾且已推上線。
本檔釘住：指紋快照排除 bump 目標、內容變動必中止、無變動必放行、且真的接在
commit 之前呼叫。
"""
import importlib.util
import os
import sys

import pytest

_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(_ROOT, "src"))


def _load():
    path = os.path.join(_ROOT, "scripts", "push_helper.py")
    spec = importlib.util.spec_from_file_location("push_helper_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_snapshot_covers_sources_and_excludes_bump_target():
    ph = _load()
    snap = ph.snapshot_tracked_sources()
    assert "src/cmuh_common/roster/solve_day.py" in snap, "src 應納入指紋"
    assert "tests/test_push_helper_antirevert.py" in snap, "tests 應納入指紋"
    # version.py 由 bump 在關卡之後合法改寫 → 這一份【工作目錄比對】排除它，
    # 否則每次推送都誤報。★但它不會因此逃過檢查★:[2026-08-02 補審 P1]
    # main() 在 bump/sync_manifest 之後另取 version.py 與 manifest.json 的預期值，
    # 一併納入 index 比對(見 test_version_and_manifest_are_checked_against_index)。
    assert "src/cmuh_common/version.py" not in snap


def test_content_change_aborts_push(capsys):
    """關卡後檔案內容變動 → 中止（fail 走 SystemExit），且點名該檔案。"""
    ph = _load()
    before = ph.snapshot_tracked_sources()
    tampered = dict(before)
    tampered["src/cmuh_common/roster/solve_day.py"] = "0" * 64
    with pytest.raises(SystemExit):
        ph.verify_unchanged_since_tests(tampered)
    out = capsys.readouterr().out
    assert "solve_day.py" in out and "已中止推送" in out


def test_new_or_deleted_file_also_aborts():
    """新增/消失的檔案同樣算變動（還原可能整檔刪除或帶回舊檔）。"""
    ph = _load()
    before = ph.snapshot_tracked_sources()
    missing = {k: v for k, v in before.items()
               if k != "src/cmuh_common/roster/solve_day.py"}
    with pytest.raises(SystemExit):
        ph.verify_unchanged_since_tests(missing)


def test_unchanged_passes():
    ph = _load()
    snap = ph.snapshot_tracked_sources()
    ph.verify_unchanged_since_tests(snap)          # 不得拋出


def test_guard_runs_before_commit_in_main():
    """★[2026-08-02 補審 P1] 順序被改過,而且本測試原本把【有瑕疵的順序】釘死了★

    舊順序是「關卡 → 取指紋 → … → 驗 → commit(內含 git add)」,留下兩個空窗:
      (1) 關卡返回 → 取指紋之間被還原 → 舊版直接成為【合法基準】,檢查必過。
      (2) 驗完 → git add 之間被還原 → 被還原的內容照樣進 commit。
    兩者都正是本防護聲稱要消除的事故類型。新順序:
      取指紋 → 關卡 → 驗(抓關卡期間的還原) → bump/manifest → git add
      → 驗 index(抓 add 之前的任何還原) → commit(提交的就是這個 index)。
    """
    import inspect

    ph = _load()
    src = inspect.getsource(ph.main)
    i_snap = src.index("fingerprint = snapshot_tracked_sources()")
    i_gate = src.index("step_quality_gate()")
    i_verify = src.index("verify_unchanged_since_tests(")
    i_stage = src.index("step5_stage()")
    i_index = src.index("verify_index_matches(")
    i_commit = src.index("step5_commit(")
    assert i_snap < i_gate, "指紋必須在品質關卡【之前】取,否則基準本身可能已被污染"
    assert i_gate < i_verify < i_stage < i_index < i_commit, \
        "驗證 → stage → 驗 index → commit,順序不可調換"


def test_index_check_catches_content_swapped_after_add(tmp_path, monkeypatch):
    """★真正的防線★ 只要 index 內容與測過的不符就中止 —— 不管是什麼時候被換掉的。"""
    ph = _load()
    snap = ph.snapshot_tracked_sources()
    tampered = dict(snap)
    tampered["src/cmuh_common/roster/solve_day.py"] = "0" * 64
    with pytest.raises(SystemExit):
        ph.verify_index_matches(tampered)


def test_index_check_passes_on_clean_index():
    """index 與工作目錄一致時不得誤報(工作區乾淨的正常推送情境)。"""
    ph = _load()
    import subprocess as _sp
    out = _sp.run(["git", "status", "--porcelain"], cwd=ph.REPO_ROOT,
                  capture_output=True, text=True).stdout
    if out.strip():
        pytest.skip("工作區有未提交變更,index 與工作目錄本來就會不同")
    # ★expected 必須涵蓋 index 內的所有檔★ `snapshot_tracked_sources()` 預設
    #   排除 version.py(那是給「關卡前後比對」用的),而 verify_index_matches
    #   比對的是【聯集】—— 少了 version.py 就會被判成「index 多了一個檔」。
    #   正式流程在 main() 裡是 fingerprint + version.py + manifest.json,
    #   不受影響;是這支測試餵錯了 expected(CI 在乾淨工作區上跑才暴露)。
    ph.verify_index_matches(ph.worktree_blob_ids(include_version=True))


def test_index_check_flags_missing_file():
    """該進 index 卻不在(被刪/沒 add)→ 也要中止。"""
    ph = _load()
    with pytest.raises(SystemExit):
        ph.verify_index_matches({"src/根本不存在的檔案.py": "0" * 64})


def test_version_and_manifest_are_checked_against_index():
    """★補審 P1★ version.py 原本被【永久】排除 —— 若 bump 後被 OneDrive 還原,
    commit 進去的是舊 CURRENT_VERSION、manifest 卻記著新版本與新雜湊 →
    所有機器下載後 SHA256 對不上、更新 fail-closed 全面停更。"""
    import inspect

    ph = _load()
    src = inspect.getsource(ph.main)
    i_manifest = src.index("step4_sync_manifest(")
    i_expected = src.index("include_version=True")
    i_index = src.index("verify_index_matches(")
    assert i_manifest < i_expected < i_index, "預期值要在 manifest 同步之後才取"
    assert ph.VERSION_REL == "src/cmuh_common/version.py"
    assert ph.MANIFEST_REL == "manifest.json"


def test_worktree_blob_ids_match_the_index_on_a_clean_tree():
    """★真正的不變量(與平台無關)★ 工作目錄算出來的 blob id 必須等於 index 裡的。

    這是「不可自行 sha256」的根據:讓 git 自己算,filter(CRLF 正規化、binary
    判定)才會與 `git add` 完全一致。
    """
    ph = _load()
    import subprocess as _sp
    if _sp.run(["git", "status", "--porcelain"], cwd=ph.REPO_ROOT,
               capture_output=True, text=True).stdout.strip():
        pytest.skip("工作區有未提交變更,兩者本來就會不同")
    ids = ph.worktree_blob_ids(include_version=True)
    idx = ph.index_blob_ids()
    diff = sorted(k for k in {*ids, *idx} if ids.get(k) != idx.get(k))
    assert diff == [], f"乾淨工作區下不該有差異:{diff}"


def test_crlf_file_would_break_a_naive_sha256():
    """★CRLF 正規化 —— 這是 Windows 專屬的失效情境★

    開發機 core.autocrlf=true 且 .gitattributes 是 `* text=auto eol=lf`,
    而 push_helper 自己 bump 出來的 version.py 在磁碟上就是 CRLF
    (Path.write_text 在 Windows 把 \n 轉成 CRLF)→ 自行 sha256(原始 bytes)
    與 index 裡的 blob 必然對不上 → 【每一次推送都誤報】。

    ★CI 跑在 Linux,checkout 出來是 LF —— 前提不成立時要 skip,不是失敗★
    (我第一版把「磁碟上是 CRLF」寫成無條件斷言,CI 一跑就紅。)
    """
    ph = _load()
    rel = "src/cmuh_common/version.py"
    raw = (ph.REPO_ROOT / rel).read_bytes()
    if b"\r\n" not in raw:
        pytest.skip("本平台 checkout 是 LF(Linux/CI)→ 這個失效情境不會發生")
    lf = raw.replace(b"\r\n", b"\n")
    import hashlib
    assert hashlib.sha256(raw).hexdigest() != hashlib.sha256(lf).hexdigest(), \
        "CRLF 與 LF 的 sha256 不同 —— 這正是自算會對不上 index 的原因"
    ids = ph.worktree_blob_ids(include_version=True)
    idx = ph.index_blob_ids()
    assert ids[rel] == idx[rel], "讓 git 自己算就不受影響"


def test_version_consistency_guard_rejects_mismatch(capsys):
    """★真正會造成危害的不變量★ committed 的 version.py 與 manifest 必須同版本。
    盯著微秒級的還原時窗不可靠,直接驗這條不變量才穩。"""
    ph = _load()
    with pytest.raises(SystemExit):
        ph.verify_staged_version_consistency("9999.99.99.9")
    out = capsys.readouterr().out
    assert "版本不一致" in out and "fail-closed" in out


def test_version_consistency_guard_passes_on_current_head():
    """目前 HEAD 的 version.py 與 manifest 本來就一致 → 不得誤報。"""
    ph = _load()
    import json
    ver = json.loads((ph.REPO_ROOT / "manifest.json").read_text(
        encoding="utf-8"))["app_version"]
    ph.verify_staged_version_consistency(ver)      # 不得拋出


def test_version_consistency_is_checked_before_commit():
    import inspect

    ph = _load()
    src = inspect.getsource(ph.main)
    assert (src.index("verify_staged_version_consistency(")
            < src.index("step5_commit(")), "一致性檢查必須在 commit 之前"


def test_unexpected_new_index_entry_is_flagged():
    """★[2026-08-02 補審第 4 輪]★ 取樣之後、git add -A 之前才出現的新檔,
    若只比對 expected 的鍵就完全看不到 —— 它會被 staged 並 commit 出去,
    而那正是「commit 的內容 == 測過的內容」要保證的事。必須比【聯集】。"""
    ph = _load()
    expected = ph.worktree_blob_ids(include_version=True)
    expected.pop("src/cmuh_common/roster/rules.py", None)   # 模擬「取樣時還沒有這個檔」
    with pytest.raises(SystemExit):
        ph.verify_index_matches(expected)
