# -*- coding: utf-8 -*-
"""啟動前的更新復原 —— ★在 import 任何 cmuh_common 之前跑★
（2026-08-02 外部 code review P1-01）

【為什麼不能沿用 `cmuh_common.updater.recover_incomplete_update`】
那一支是好的，但它跑得【太晚】：

    中國醫皮膚科主程式.pyw  →  runpy.run_path(src/main.py)
                               main.py 開頭就 import 幾十個 cmuh_common 模組
                               …（半套更新的模組已經載進記憶體了）…
    第 285 行  _ensure_deps_runtime()
    之後某處   check_and_update() → recover_incomplete_update()

上一批更新若在 Phase 2 中途斷電，磁碟上是「一部分新、一部分舊」。等到
`check_and_update()` 才復原，那些新舊混合的模組【早就被 import 進來了】——
復原把檔案換回舊版，記憶體裡卻還是混的；而且若混到連 import 都失敗，
根本走不到那一行。

所以復原必須在啟動器裡、`runpy` 之前做完。

【本檔的硬性限制】
* **只用標準庫**。不 import cmuh_common 的任何東西 —— 那正是可能壞掉的東西。
* **不做網路、不做更新**。只負責「把上一批沒走完的更新收乾淨」。
* **失敗不可以自己吞掉**。判不清楚就回報 UNKNOWN，由啟動器決定要不要啟動。

【與 updater 的重複】
journal 檔名與欄位在這裡重寫了一份（不能 import updater）。
`tests/test_review_batch_f_*.py` 有一支守衛比對兩邊必須一致 ——
★這是刻意的重複，不是忘了抽共用★，但它必須被機械性地釘住。

【誠實邊界（沿用 updater 已記錄的那條）】
本檔自己也是被更新的檔案之一。引進這套機制的那一次更新，是由【還沒有這套機制的
舊版】執行的；而且哪天本檔自己被換到一半，就沒有任何東西救得了。要真正消除那個
窗口需要一個不在更新清單內的啟動器（尚未實作）。
"""
from __future__ import annotations

import contextlib
import json
import os
import sys

JOURNAL_FILENAME = ".updater_commit.journal"
FAILED_JOURNAL_SUFFIX = ".failed.json"
LOCK_FILENAME = ".updater_write.lock"
JOURNAL_SCHEMA = 1

# 復原結果
CLEAN = "clean"                      # 沒有交易日誌 —— 上一批正常結束
RECOVERED = "recovered"              # 有日誌，而且全部回滾成功
RETRYABLE_FAILURE = "retryable_failure"    # 有檔沒還原成功，但下次可能會成功
TERMINAL_FAILURE = "terminal_failure"      # 救不回來（備份不見了）
UNKNOWN = "unknown"                  # 連判都判不出來（日誌壞掉／自己出錯）


class RecoveryResult:
    """★不要用一個 bool★ 「沒事」與「查不出來」必須分得開。"""

    def __init__(self, status, *, journal_present=False, restored=(),
                 unresolved=(), errors=()):
        self.status = status
        self.journal_present = journal_present
        self.restored = list(restored)
        self.unresolved = list(unresolved)
        self.errors = list(errors)

    @property
    def safe_to_start(self) -> bool:
        """可不可以載入臨床程式。★只有這兩種狀態才算數★"""
        return self.status in (CLEAN, RECOVERED)

    def describe(self) -> str:
        if self.status == CLEAN:
            return "沒有未完成的更新"
        if self.status == RECOVERED:
            return f"已把上一批沒走完的更新回滾（{len(self.restored)} 個檔案）"
        if self.status == RETRYABLE_FAILURE:
            return (f"上一批更新沒有回滾完成：還有 {len(self.unresolved)} 個檔案"
                    f"沒有還原成功（可能是防毒或權限暫時鎖住）")
        if self.status == TERMINAL_FAILURE:
            if not self.unresolved:
                # 這一輪沒有再跑回滾，是讀到【上一次】留下的失敗標記。
                # ★措辭鐵律★ 不能說「0 個檔案的備份不在了」——那是這次沒去數。
                return "先前有一次更新無法自動修復，狀況尚未解除"
            return (f"上一批更新無法自動修復：{len(self.unresolved)} 個檔案的備份"
                    f"已經不在了")
        return "無法判斷上一批更新是否完成"


@contextlib.contextmanager
def _write_lock(app_dir, timeout_sec=10.0):
    """與 `updater._updater_write_lock` 【同一個鎖檔、同一個位元組】。

    開機時 watchdog 幾乎同時拉起五支程式，每一支都會跑到這裡。沒有鎖的話，
    B 會把 A 正在寫的那一批當成「上次崩潰」而回滾掉。
    拿不到鎖 → yield False（不做事），不是「當作沒鎖」。
    """
    if os.name != "nt":
        yield True                    # 非 Windows：部署目標之外，不擋
        return
    try:
        import msvcrt
        import time
    except Exception:
        yield False
        return
    fd = None
    try:
        fd = os.open(os.path.join(app_dir, LOCK_FILENAME),
                     os.O_CREAT | os.O_RDWR)
        # 新建的鎖檔是空的；msvcrt.locking 從目前檔位鎖 nbytes，空檔鎖不住。
        if os.fstat(fd).st_size < 1:
            os.write(fd, b"\0")
        os.lseek(fd, 0, os.SEEK_SET)
    except Exception:
        if fd is not None:
            with contextlib.suppress(Exception):
                os.close(fd)
        yield False
        return
    deadline = time.monotonic() + timeout_sec
    got = False
    try:
        while time.monotonic() < deadline:
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                got = True
                break
            except OSError:
                time.sleep(0.2)
        yield got
    finally:
        if got:
            with contextlib.suppress(Exception):
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        with contextlib.suppress(Exception):
            os.close(fd)


def _inside(app_dir, path) -> bool:
    """★[外審 P1] 日誌裡的絕對路徑不可以照單全收★

    正常情況下日誌是 updater 自己寫的，但它是磁碟上的一個普通 JSON 檔。
    被改過（或被別的東西寫壞）而程式又以管理員身分執行時，
    「照著日誌把 A 換成 B」就等於任意檔案覆寫／刪除。
    所以每一個 target/backup 都要確認它在程式目錄底下。
    """
    try:
        real_root = os.path.realpath(app_dir)
        real_path = os.path.realpath(path)
        return os.path.commonpath([real_root, real_path]) == real_root
    except Exception:
        return False          # 判不出來就當成不在（★查不到 ≠ 沒問題★）


def _rollback_one(app_dir, entry, errors):
    """→ ("restored" | "untouched" | "retryable" | "terminal")。"""
    target = str(entry.get("target") or "")
    if not target or not _inside(app_dir, target):
        errors.append(f"日誌裡的路徑不在程式目錄內，已略過：{os.path.basename(target)}")
        return "terminal"
    existed_before = bool(entry.get("existed_before"))
    staged = str(entry.get("staged") or "")
    backup = target + ".bak"

    # 暫存檔還在 ⇒ 這個檔還沒被 replace 過（os.replace 會把它吃掉）⇒ 沒事
    if staged and _inside(app_dir, staged) and os.path.exists(staged):
        return "untouched"
    if not existed_before and not os.path.exists(target):
        return "untouched"

    try:
        if existed_before:
            if not os.path.exists(backup):
                # 備份不見了：重試一萬次也不會長回來
                errors.append(f"{os.path.basename(target)}：備份不存在，無法還原")
                return "terminal"
            os.replace(backup, target)
        elif os.path.exists(target):
            os.remove(target)
        return "restored"
    except OSError as e:
        errors.append(f"{os.path.basename(target)}：{e}")
        return "retryable"      # 鎖住／權限 —— 下次可能就好了


def _parse_journal(payload):
    """→ (entries, 錯誤原因)。★看不懂就說看不懂，不要當成空的★

    ★[2026-08-02 外審第 2 輪 P2] 原本是 `payload.get("files") or []`★
    於是一個 `{}`、一個 schema 對不上的、或 `files` 被寫壞成空陣列的日誌，
    都會安安靜靜地走完「沒有檔要還原」→ 刪掉日誌 → 回 RECOVERED。
    磁碟明明是混版，我們卻回報「已復原」並放行 —— 那是這一整批要消滅的東西，
    自己卻在最外層又做了一次。
    """
    if not isinstance(payload, dict):
        return None, "交易日誌不是物件"
    schema = payload.get("schema")
    if schema != JOURNAL_SCHEMA:
        # ★不認得的版本不要硬解★ 欄位語意可能已經變了，照舊規則動檔案更危險。
        return None, f"交易日誌版本 {schema!r} 不是我認得的 {JOURNAL_SCHEMA}"
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        # updater 只在「這一批真的有檔要寫」時才落地日誌 —— 空的就是壞的。
        return None, "交易日誌沒有任何檔案項目"
    for entry in files:
        if not isinstance(entry, dict) or not isinstance(
                entry.get("target"), str) or not entry.get("target"):
            return None, "交易日誌的項目缺少 target"
        if not isinstance(entry.get("existed_before"), bool):
            return None, "交易日誌的項目缺少 existed_before"
    return files, ""


def recover_before_start(app_dir) -> RecoveryResult:
    """啟動器在 import 主程式【之前】呼叫。絕不拋例外。"""
    journal = os.path.join(app_dir, JOURNAL_FILENAME)
    try:
        if os.path.exists(journal + FAILED_JOURNAL_SUFFIX):
            # ★[2026-08-02 外審第 2 輪 P1] terminal 不可以只擋第一次★
            #   原本改名之後就沒有人再看它，下一次啟動找不到 journal → CLEAN →
            #   磁碟還是混版卻【無聲啟動】。那等於整個機制只生效一次。
            #   這個標記由 `updater` 在「一整輪更新完全成功」之後才清掉
            #   （見 `clear_failed_journal_marker`）—— 那時整棵樹已經被換成
            #   一致的新版，才真的沒事了。
            return RecoveryResult(
                TERMINAL_FAILURE, journal_present=True,
                errors=["上一次更新留下無法修復的紀錄（%s%s）"
                        % (JOURNAL_FILENAME, FAILED_JOURNAL_SUFFIX)])
        if not os.path.exists(journal):
            return RecoveryResult(CLEAN)
    except Exception:
        return RecoveryResult(UNKNOWN, errors=["無法檢查交易日誌"])

    try:
        with _write_lock(app_dir) as acquired:
            if not acquired:
                # ★「有人正在寫」不等於「上次崩潰了」★ 也不等於可以放行 ——
                #   我們就是不知道，所以回 UNKNOWN 讓啟動器去問人。
                return RecoveryResult(UNKNOWN, journal_present=True,
                                      errors=["另一支程式正在寫入更新"])
            if not os.path.exists(journal):
                return RecoveryResult(CLEAN)     # 等鎖的期間對方正常結束了
            with open(journal, "r", encoding="utf-8") as f:
                payload = json.load(f)
            files, why = _parse_journal(payload)
            if files is None:
                # ★保留日誌★ 我們看不懂它，但它是唯一的證據，不可以刪。
                return RecoveryResult(UNKNOWN, journal_present=True,
                                      errors=[why])
            restored, unresolved, terminal, errors = [], [], [], []
            for entry in reversed(files):        # 與寫入相反的順序
                verdict = _rollback_one(app_dir, entry, errors)
                name = os.path.basename(str(entry.get("target") or ""))
                if verdict == "restored":
                    restored.append(name)
                elif verdict == "retryable":
                    unresolved.append(name)
                elif verdict == "terminal":
                    terminal.append(name)

            if unresolved:
                # 留著日誌，下次啟動再試
                return RecoveryResult(RETRYABLE_FAILURE, journal_present=True,
                                      restored=restored, unresolved=unresolved,
                                      errors=errors)
            if terminal:
                # ★保留證據★ 不可以刪掉之後假裝乾淨：磁碟可能仍是混版，
                #   而這個檔是唯一能證明「發生過什麼」的東西。
                _archive_journal(journal, errors)
                return RecoveryResult(TERMINAL_FAILURE, journal_present=True,
                                      restored=restored, unresolved=terminal,
                                      errors=errors)
            _clear_journal(journal, errors)
            return RecoveryResult(RECOVERED, journal_present=True,
                                  restored=restored, errors=errors)
    except Exception as e:      # noqa: BLE001  啟動路徑，絕不可以拋出去
        return RecoveryResult(UNKNOWN, journal_present=True,
                              errors=[f"復原程序本身失敗：{e}"])


def _archive_journal(journal, errors) -> None:
    try:
        os.replace(journal, journal + FAILED_JOURNAL_SUFFIX)
    except OSError as e:
        errors.append(f"保留失敗紀錄時出錯：{e}")


def _clear_journal(journal, errors) -> None:
    try:
        os.remove(journal)
    except OSError as e:
        errors.append(f"清除交易日誌失敗：{e}")


RECOVERY_LOG = "update_recovery.log"


def recover_and_report(app_dir, program_name) -> RecoveryResult:
    """所有啟動器都呼叫這一支：跑復原 ＋ 留下紀錄。

    ★不做任何 UI 判斷★ 「要不要因此擋住啟動」是各啟動器自己的政策，
    因為那五支程式的有人/無人看顧狀況不同（見 `confirm_start_despite`）。
    """
    result = recover_before_start(app_dir)
    if result.status == CLEAN:
        return result                 # 絕大多數情況：什麼都不記，別把 log 灌爆
    try:
        import datetime
        with open(os.path.join(app_dir, RECOVERY_LOG), "a",
                  encoding="utf-8") as f:
            f.write("\n===== %s %s 啟動前更新復原 =====\n"
                    % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                       program_name))
            f.write("狀態：%s（%s）\n" % (result.status, result.describe()))
            if result.restored:
                f.write("已還原：%s\n" % "、".join(result.restored))
            if result.unresolved:
                f.write("未還原：%s\n" % "、".join(result.unresolved))
            for e in result.errors:
                f.write("  - %s\n" % e)
    except Exception:      # noqa: BLE001  寫不出 log 不可以擋住啟動判斷
        pass
    return result


def confirm_start_despite(result, program_name) -> bool:
    """★只有【有人看著】的臨床主程式呼叫★ 復原沒做完時要不要照樣啟動。

    政策（使用者 2026-08-02 定案）：**預設不啟動，但留一個明確的覆寫**。
    磁碟上是新舊混合的程式碼，行為無法預期 —— 而這支程式會對 HIS 做寫入
    （打卡、開單、療程）。可是「完全不能啟動」在診間也是一種傷害，所以
    不是硬擋，是問人：預設鈕停在「否」，要按下去才會帶著混版啟動。

    ★問不到人就不啟動★ MessageBox 叫不出來時回 False —— 這時我們既無法
    修好，也無法告知，唯一誠實的作法是不要帶著未知狀態去碰病人資料。
    """
    detail = result.describe()
    if result.unresolved:
        detail += "\n\n受影響的檔案：\n  " + "\n  ".join(result.unresolved[:8])
        if len(result.unresolved) > 8:
            detail += "\n  …等 %d 個" % len(result.unresolved)
    text = (
        "%s 偵測到【上一次自動更新沒有完成】，而且無法自動修復。\n\n"
        "%s\n\n"
        "現在程式資料夾裡可能是新舊版本混在一起，行為無法預期。\n"
        "建議：關掉所有本套程式後重新啟動一次（多數情況會自動修好），"
        "仍然不行請找開發者。\n\n"
        "★是否仍要繼續啟動？★（風險自負；按「否」結束）"
    ) % (program_name, detail)
    try:
        import ctypes
        # MB_YESNO | MB_ICONWARNING | MB_DEFBUTTON2 | MB_TOPMOST
        #   ★DEFBUTTON2★ 預設停在「否」：驚慌之下連按 Enter 不會誤闖進混版。
        flags = 0x04 | 0x30 | 0x100 | 0x40000
        return ctypes.windll.user32.MessageBoxW(
            0, text, "更新未完成", flags) == 6      # IDYES
    except Exception:      # noqa: BLE001
        return False


def main() -> int:
    """讓它也能單獨跑一次（診斷用）：`python src/bootstrap_recovery.py`。"""
    app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    result = recover_before_start(app_dir)
    print(result.status, "|", result.describe())
    for e in result.errors:
        print("  -", e)
    return 0 if result.safe_to_start else 1


if __name__ == "__main__":
    sys.exit(main())
