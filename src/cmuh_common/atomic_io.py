# -*- coding: utf-8 -*-
"""原子寫入工具 + corruption-safe JSON 載入。

- atomic_write_json: 先寫 .tmp 再 os.replace，斷電時原檔不變空。
- atomic_write_text: 含 .bak 備份的文字寫入。
- safe_load_json: corrupt JSON → 自動 backup 壞檔到 .corrupt-<ts> + fallback default。
"""
import json
import logging
import os
import shutil
import tempfile
import time


_FILE_OP_RETRY_DELAYS_SEC = (0.05, 0.15, 0.35)


def _file_op_with_retry(label: str, func, *args):
    """Retry transient Windows file locks for small atomic file operations."""
    last_exc = None
    total_attempts = len(_FILE_OP_RETRY_DELAYS_SEC) + 1
    for attempt in range(total_attempts):
        try:
            return func(*args)
        except OSError as e:
            last_exc = e
            if attempt >= len(_FILE_OP_RETRY_DELAYS_SEC):
                break
            delay = _FILE_OP_RETRY_DELAYS_SEC[attempt]
            logging.debug(
                "[atomic_io] %s failed (%s), retry %d/%d in %.2fs",
                label, e, attempt + 2, total_attempts, delay,
            )
            time.sleep(delay)
    raise last_exc


def _replace_with_retry(src: str, dst: str) -> None:
    _file_op_with_retry(f"replace {src} -> {dst}", os.replace, src, dst)


def _copy2_with_retry(src: str, dst: str) -> None:
    import shutil
    _file_op_with_retry(f"copy {src} -> {dst}", shutil.copy2, src, dst)


def _remove_with_retry(path: str) -> None:
    _file_op_with_retry(f"remove {path}", os.remove, path)


def _flush_and_fsync(f) -> None:
    """Flush file content to disk before os.replace."""
    f.flush()
    os.fsync(f.fileno())


def _make_temp_path(target_path: str) -> tuple[int, str]:
    target_dir = os.path.dirname(os.path.abspath(target_path)) or "."
    os.makedirs(target_dir, exist_ok=True)
    base = os.path.basename(target_path)
    return tempfile.mkstemp(prefix=f".{base}.", suffix=".tmp", dir=target_dir)


def _next_corrupt_backup_path(file_path: str, timestamp: str) -> str:
    """Return a non-conflicting corrupt backup path."""
    candidate = f"{file_path}.corrupt-{timestamp}"
    suffix = 1
    while os.path.exists(candidate):
        candidate = f"{file_path}.corrupt-{timestamp}-{suffix}"
        suffix += 1
    return candidate


def atomic_write_json(file_path: str, data, **kwargs) -> None:
    """JSON 原子寫入。kwargs 會傳給 json.dump（如 default=...）。"""
    fd = -1
    tmp_path = ""
    try:
        fd, tmp_path = _make_temp_path(file_path)
        dump_kwargs = {"ensure_ascii": False, "indent": 4}
        dump_kwargs.update(kwargs)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            fd = -1
            json.dump(data, f, **dump_kwargs)
            _flush_and_fsync(f)
        _replace_with_retry(tmp_path, file_path)
    except Exception:
        if fd >= 0:
            try:
                os.close(fd)
            except Exception:
                pass
        if tmp_path and os.path.exists(tmp_path):
            try:
                _remove_with_retry(tmp_path)
            except Exception:
                logging.debug("atomic_write_json: 移除 tmp 失敗", exc_info=True)
        raise


class MultiWriteError(OSError):
    """多檔寫入失敗。written/pending 讓呼叫端能【精確】告訴使用者存了哪些。

    phase：
      "stage"  —— 還在寫暫存檔就失敗 → 一個目標檔都沒動(written 必為空)。
      "commit" —— 暫存檔都寫好了,replace 到一半失敗 → written 是已生效的檔。
    """

    def __init__(self, message: str, *, phase: str, written: list,
                 pending: list, cause: BaseException | None = None):
        super().__init__(message)
        self.phase = phase
        self.written = list(written)
        self.pending = list(pending)
        self.cause = cause


def atomic_write_json_multi(items, **kwargs) -> None:
    """把多個 JSON 檔【要嘛都生效、要嘛都不生效】地寫下去。

    items: [(file_path, data), ...]，依序 commit。

    【為什麼需要它:2026-08-06 外審 P1-07】
    設定頁的「儲存」要寫 r_doctor_settings / threshold_settings / doctors 三個檔。
    舊做法是連續三次 `atomic_write_json`——單檔各自原子，但【三檔之間不是】：
    第二個檔寫失敗時第一個早就生效了，使用者只看到一個例外，不知道自己的設定
    處於「R 醫師已更新、醫師清單還是舊的」這種半套狀態。

    做法(兩階段)：
      Phase 1 stage  : 全部寫進同目錄的 .tmp + fsync。任一失敗 → 清掉所有 tmp、
                       拋 MultiWriteError(phase="stage")，目標檔【一個都沒動】。
      Phase 2 commit : 依序 os.replace(同磁區的 rename，成功機率極高)。
                       萬一中途失敗 → 拋 MultiWriteError(phase="commit") 並附上
                       「已生效」與「未生效」清單，讓 UI 能講清楚。
    註：Windows 沒有跨檔案的原子 rename，Phase 2 理論上仍可能部分完成；但把所有
    可能失敗的重活(序列化、磁碟寫入、空間不足)都擋在 Phase 1，已經把真實世界的
    半套風險壓到極低，而且失敗時是【可精確描述】的，不再是無聲半套。
    """
    items = [(str(p), d) for p, d in items]
    dump_kwargs = {"ensure_ascii": False, "indent": 4}
    dump_kwargs.update(kwargs)

    staged: list = []          # [(tmp_path, target_path), ...]
    try:
        for target_path, data in items:
            fd, tmp_path = _make_temp_path(target_path)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    json.dump(data, f, **dump_kwargs)
                    _flush_and_fsync(f)
            except Exception:
                try:
                    os.close(fd)
                except Exception:
                    pass
                if os.path.exists(tmp_path):
                    try:
                        _remove_with_retry(tmp_path)
                    except Exception:
                        logging.debug("[atomic_io] 清除 tmp 失敗", exc_info=True)
                raise
            staged.append((tmp_path, target_path))
    except Exception as e:
        for tmp_path, _ in staged:
            try:
                if os.path.exists(tmp_path):
                    _remove_with_retry(tmp_path)
            except Exception:
                logging.debug("[atomic_io] 清除 tmp 失敗", exc_info=True)
        raise MultiWriteError(
            f"寫入暫存檔失敗，未變更任何設定檔：{e}",
            phase="stage", written=[], pending=[p for p, _ in items],
            cause=e) from e

    # ★[2026-08-08 外審] commit 階段也要能回滾★
    #   上一版的 commit 失敗只清掉剩下的 tmp,已經 replace 掉的【留著新值】——
    #   那與本函式 docstring 宣稱的「要嘛都生效、要嘛都不生效」不符。
    #   宣稱與實作不符是這個專案反覆踩到的坑,而這裡的代價是使用者的設定
    #   停在半新半舊,還以為只是「存檔失敗」。
    #   做法:replace 之前先把原檔複製成 `.bak`(同目錄,同一個檔案系統),
    #   任何一個失敗就用 .bak 把已經換掉的那幾個換回去。
    #   ★回滾本身也可能失敗★(磁碟壞、被鎖住)。那時不可以假裝沒事:
    #   仍然拋 MultiWriteError,但 `written` 只列【真的回不去】的那幾個,
    #   讓上層的訊息說的是實話。
    # ★[2026-08-08 外審第 2 回] 備份失敗就不可以開始 commit★
    #   上一版只記一行 warning 然後照樣往下走。之後若真的需要回滾,
    #   `backups.get(target)` 回 None 會被誤讀成「這個檔原本不存在」→
    #   回滾把它【刪掉】—— 使用者的設定檔就這樣沒了,而舊內容也沒備份。
    #   「查不到備份」與「原本沒有這個檔」是兩件事,不可以共用同一個表示法。
    manifest_path = _manifest_path_for([t for _, t in staged])
    # ★[外審] 上一筆交易還沒收乾淨 → 不准開新的★
    #   備份檔名是固定的(`<target>.rollback.bak`)。復原失敗後若讓新交易照跑,
    #   它會用【目前這份半新半舊的內容】覆寫掉那個備份 ——
    #   唯一一份完整的舊設定就此永久消失,而且再也復原不了。
    if os.path.exists(manifest_path) and _manifest_is_recoverable(manifest_path):
        for leftover_tmp, _ in staged:
            try:
                if os.path.exists(leftover_tmp):
                    _remove_with_retry(leftover_tmp)
            except Exception:
                logging.debug("[atomic_io] 清除 tmp 失敗", exc_info=True)
        raise MultiWriteError(
            "上一次設定存檔被中止且尚未復原完成(可能有檔案被鎖住)。"
            "為了不覆蓋掉唯一一份可還原的舊設定,這次【不寫入任何檔案】。"
            "請關閉可能佔用設定檔的程式後重新啟動,程式會自動把設定復原。",
            phase="stage", written=[], pending=[p for p, _ in items])
    backups: dict = {}
    existed: set = set()
    for _tmp, target_path in staged:
        if not os.path.exists(target_path):
            continue                       # 原本就沒有這個檔 → 回滾＝刪掉
        existed.add(target_path)
        bak = target_path + ".rollback.bak"
        try:
            shutil.copy2(target_path, bak)
            backups[target_path] = bak
        except Exception as e:
            _drop_backups(backups)
            for leftover_tmp, _ in staged:
                try:
                    if os.path.exists(leftover_tmp):
                        _remove_with_retry(leftover_tmp)
                except Exception:
                    logging.debug("[atomic_io] 清除 tmp 失敗", exc_info=True)
            raise MultiWriteError(
                f"無法備份 {target_path},為了保證可回滾而放棄本次寫入"
                f"(未變更任何設定檔):{e}",
                phase="stage", written=[], pending=[p for p, _ in items],
                cause=e) from e

    # ★[2026-08-08 外審第 2 回 F7] 跨行程的中止也要救得回來★
    #   Python 的 rollback 只在【例外】時跑。行程被砍、主機斷電的話它不會執行,
    #   磁碟就停在半新半舊,而且沒有任何人知道 —— 下次開機讀到的是一份
    #   「R 醫師已更新、醫師清單還是舊的」的設定。
    #   做法:commit 之前寫一張 manifest(列出這次要換哪幾個檔),全部換完才刪。
    #   開機時看到 manifest 還在 = 上次沒做完 → 用 .bak 還原。
    try:
        _write_manifest(manifest_path, [t for _, t in staged], existed)
    except Exception as e:
        _drop_backups(backups)
        for leftover_tmp, _ in staged:
            try:
                if os.path.exists(leftover_tmp):
                    _remove_with_retry(leftover_tmp)
            except Exception:
                logging.debug("[atomic_io] 清除 tmp 失敗", exc_info=True)
        raise MultiWriteError(
            f"無法寫入交易紀錄,為了保證中止時救得回來而放棄本次寫入"
            f"(未變更任何設定檔):{e}",
            phase="stage", written=[], pending=[p for p, _ in items],
            cause=e) from e
    written: list = []
    for idx, (tmp_path, target_path) in enumerate(staged):
        try:
            _replace_with_retry(tmp_path, target_path)
        except Exception as e:
            for leftover_tmp, _ in staged[idx:]:
                try:
                    if os.path.exists(leftover_tmp):
                        _remove_with_retry(leftover_tmp)
                except Exception:
                    logging.debug("[atomic_io] 清除 tmp 失敗", exc_info=True)
            stuck = _rollback_written(written, backups, existed)
            # ★[第 3 回] 還原失敗時【留著】manifest 與沒還原成功的備份★
            #   上一版不管成敗都清掉 —— 一個短暫的檔案鎖就讓設定停在不一致,
            #   而下次重試需要的舊資料已經被自己刪光了,永遠回不去。
            #   `_rollback_written` 成功還原的那幾個會自己從 backups 移除,
            #   所以這裡剩下的正是「還沒還原成功」的。
            if not stuck:
                _drop_backups(backups)
                _remove_manifest(manifest_path)
            else:
                logging.error(
                    "[atomic_io] 有 %d 個檔還原失敗 → 保留交易紀錄與備份,"
                    "下次啟動會再試一次:%s", len(stuck), stuck)
            if stuck:
                raise MultiWriteError(
                    f"設定只寫入了一部分且回滾失敗（{target_path} 失敗）：{e}",
                    phase="commit", written=stuck,
                    pending=[t for _, t in staged[idx:]], cause=e) from e
            raise MultiWriteError(
                f"設定未變更（{target_path} 失敗,已把先前的檔案還原）：{e}",
                phase="stage", written=[],
                pending=[t for _, t in staged], cause=e) from e
        written.append(target_path)
    # ★全部換完了。丟掉備份【之前】必須先讓「已完成」這件事持久化★
    #   順序反過來(或只是 best-effort 刪 manifest)的話,manifest 一旦刪不掉,
    #   就會留下一張「看起來未完成、卻沒有備份」的紀錄 —— 見
    #   `_mark_manifest_committed` 的說明。
    if _mark_manifest_committed(manifest_path):
        _remove_manifest(manifest_path)      # 刪不掉也無妨:已標記完成
        _drop_backups(backups)
    elif _remove_manifest(manifest_path):
        _drop_backups(backups)               # 紀錄沒了 → 沒有東西會去撤銷
    else:
        # ★[2026-08-08 外審第 4 回] 兩條都失敗 = 這次存檔【不算數】★
        #   上一版只記 error 然後正常返回。於是 `save_all_settings` 照樣套用
        #   新的 live state、跳出「設定已儲存」—— 而開機時的復原會把設定
        #   還原到存檔前。使用者確認過的存檔在重開之後無聲消失。
        #   已經寫出去的東西可以救(備份還在),所以當場回滾,並且【據實回報失敗】。
        #   假的成功比失敗嚴重:失敗使用者會再存一次,假成功他不會。
        stuck = _rollback_written(written, backups, existed)
        if stuck:
            logging.error("[atomic_io] 交易收尾失敗且回滾不完全:%s", stuck)
            raise MultiWriteError(
                "設定寫入後無法完成交易收尾,且回滾未完全成功;"
                "設定目前可能不一致。請關閉可能佔用設定檔的程式後重新啟動,"
                "程式會再試一次復原。",
                phase="commit", written=stuck, pending=[])
        _drop_backups(backups)
        _remove_manifest(manifest_path)
        logging.error("[atomic_io] 清不掉交易紀錄也標記不了完成(檔案被鎖住?)"
                      "→ 已回滾,本次【沒有變更任何設定檔】")
        raise MultiWriteError(
            "無法完成設定存檔的收尾(檔案可能被防毒/備份軟體鎖住),"
            "已還原成原本的設定 —— 這次【一個檔都沒有變更】。"
            "請排除後再按一次儲存。",
            phase="stage", written=[], pending=[p for p, _ in items])


_MANIFEST_NAME = ".multiwrite.manifest.json"


def _manifest_path_for(targets: list) -> str:
    """交易 manifest 放在第一個目標檔的同目錄(設定檔本來就都在同一個資料夾)。"""
    base = os.path.dirname(os.path.abspath(targets[0])) if targets else "."
    return os.path.join(base, _MANIFEST_NAME)


def _write_manifest(path: str, targets: list, existed=None) -> None:
    """把 manifest 原子地寫下去。失敗就【拋例外】。

    ★[2026-08-08 外審第 3 回]★ 上一版是直接覆寫 + 失敗只記 warning 然後照樣
    commit。那等於把「跨行程中止能不能救回」變成擲骰子:manifest 被鎖住、
    磁碟寫失敗、或寫到一半斷電,之後 replace 中途終止就沒有有效的 manifest,
    設定永遠停在半新半舊而且沒有人知道。
    manifest 是整個復原機制的前提 —— 它沒有確定落地,就不該開始換檔案。
    """
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        # ★也要記「交易前這個檔存不存在」★(外審)全新安裝第一次儲存時
        #   三個檔都還不存在,沒有 .bak 可還原 —— 復原若只會「換回備份」,
        #   那第一個【已經建出來】的檔就永遠留著,交易停在部分生效。
        json.dump({"targets": list(targets),
                   "existed": sorted(existed or ())}, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    # 把目錄項本身也刷下去(Windows 上 best-effort:失敗不影響正確性)
    try:
        fd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except Exception:
        logging.debug("[atomic_io] fsync 目錄失敗(略過)", exc_info=True)


def _has_anything_to_undo(targets: list, existed, legacy: bool,
                          committed: bool = False) -> bool:
    """這筆交易還有東西可以撤銷嗎?

    ★兩種「可撤銷」★(外審第 2 回教的:只看有沒有 .bak 是錯的)
      * 交易前【存在】的檔:要有 `.rollback.bak` 才還原得回去;
      * 交易前【不存在】的檔:撤銷就是把它刪掉 —— 它本來就沒有備份,
        全新安裝第一次儲存被中斷正是這一種。
    兩者都沒有 = 上次其實已經完成(備份已清),只是 manifest 沒刪掉。
    """
    if committed:
        return False                      # 這筆交易自己說完成了 —— 不可以撤銷
    for t in targets:
        t = str(t)
        if os.path.exists(t + ".rollback.bak"):
            return True
        if not legacy and t not in existed and os.path.exists(t):
            return True
    return False


def _manifest_is_recoverable(path: str) -> bool:
    """這張 manifest 代表一筆【還救得回來的】未完成交易嗎?

    ★[2026-08-08 外審]★ commit 全部成功之後,備份會被刪掉;若此時
    `_remove_manifest` 剛好因檔案鎖失敗,磁碟上就留下一張【沒有任何備份】的
    manifest。上一版的守衛只看「manifest 在不在」,於是之後每一次存檔都被
    永久拒絕 —— 而那張 manifest 其實什麼也救不回來。
    判準改成看事實:有備份才是「未完成、可還原」;沒有備份就只是殘留,
    清掉它然後照常進行。
    """
    try:
        with open(path, encoding="utf-8") as f:
            _m = json.load(f) or {}
        targets = list(_m.get("targets") or [])
        legacy = "existed" not in _m
        existed = set(_m.get("existed") or ())
        committed = bool(_m.get("committed"))
    except Exception:
        # 讀不到就當它是可還原的(保守):寧可擋一次存檔,也不要蓋掉可能存在的備份。
        logging.warning("[atomic_io] 交易紀錄讀不到 → 保守視為未完成", exc_info=True)
        return True
    if _has_anything_to_undo(targets, existed, legacy, committed):
        return True
    logging.warning("[atomic_io] 發現沒有任何備份的殘留交易紀錄(上次其實已完成)"
                    " → 清除後繼續")
    _remove_manifest(path)
    return False


def _mark_manifest_committed(path: str) -> bool:
    """把交易標成【已完成】。回傳是否確定落地。

    ★[2026-08-08 外審] 為什麼不能只靠「磁碟上還有沒有備份」推斷★
    commit 全部成功之後備份會被刪掉。若此時 manifest 剛好刪不掉,而這筆交易
    裡有「交易前不存在」的檔 —— 那個檔【現在存在】,於是下次開機的復原會
    判定「有東西可撤銷」,把一個剛剛才存好的設定檔刪掉。
    存檔明明回報成功,設定卻在重開之後消失。
    「這筆交易完成了」是一個事實,必須自己寫下來,不能從別的痕跡推。
    """
    tmp = path + ".tmp"
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception:
        data = {}
    data["committed"] = True
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except Exception:
        logging.warning("[atomic_io] 標記交易完成失敗", exc_info=True)
        try:
            if os.path.exists(tmp):
                _remove_with_retry(tmp)
        except Exception:
            logging.debug("[atomic_io] 清除 manifest tmp 失敗", exc_info=True)
        return False


def _remove_manifest(path: str) -> bool:
    try:
        if os.path.exists(path):
            _remove_with_retry(path)
        return True
    except Exception:
        logging.debug("[atomic_io] 清除交易 manifest 失敗", exc_info=True)
        return False


def recover_interrupted_multiwrite(directory: str) -> int:
    """開機時把上次沒做完的多檔交易還原。回傳還原了幾個檔。

    ★manifest 還在 = 上次的 commit 沒跑完★(它在最後一個 replace 成功之後
    才被刪掉)。這時 `.rollback.bak` 就是那幾個檔的舊內容,原樣換回去。
    找不到 .bak 的不動它 —— 沒有備份就沒有「舊內容」可還原,刪掉才是災難。
    """
    path = os.path.join(directory, _MANIFEST_NAME)
    if not os.path.exists(path):
        return 0
    try:
        with open(path, encoding="utf-8") as f:
            _m = json.load(f) or {}
        targets = list(_m.get("targets") or [])
        # ★[外審] 「沒有這個欄位」與「空清單」是兩件事★
        #   舊實作寫的 manifest 只有 targets。把它讀成「全部都不存在」的話,
        #   復原會【刪掉每一個目標】—— 三個設定檔一起消失。
        legacy = "existed" not in _m
        existed = set(_m.get("existed") or ())
        committed = bool(_m.get("committed"))
    except Exception:
        logging.error("[atomic_io] 交易 manifest 讀不到 → 無法自動還原",
                      exc_info=True)
        return 0
    # ★這一段必須在還原迴圈【之前】★(第一版放在後面 —— 而還原是用
    #   `os.replace(bak, target)` 把備份【移走】的,跑完就沒有 .bak 了,
    #   於是剛剛成功的還原會被自己判成「上次已完成」而回報 0。)
    #   沒有任何備份 = 上次其實已經完成,只是 manifest 沒刪掉。
    if not _has_anything_to_undo(targets, existed, legacy, committed):
        logging.info("[atomic_io] 殘留的交易紀錄已經沒有東西可撤銷"
                     "(上次已完成)→ 清除")
        _remove_manifest(path)
        return 0
    n = 0
    stuck = []
    for target in targets:
        bak = str(target) + ".rollback.bak"
        if legacy:
            # 舊格式:不知道交易前存不存在 → 只還原「有備份」的,
            # 其餘一律不動(寧可留下半套,也不可以刪掉可能是使用者唯一的設定)。
            if not os.path.exists(bak):
                logging.warning("[atomic_io] 舊格式交易紀錄且 %s 沒有備份 → 不動它",
                                target)
                continue
            try:
                _replace_with_retry(bak, target)
                n += 1
            except Exception:
                logging.error("[atomic_io] 還原 %s 失敗", target, exc_info=True)
                stuck.append(target)
            continue
        if str(target) not in existed:
            # ★交易前不存在 → 撤銷就是把它刪掉★(不是「跳過」)
            #   跳過的話,全新安裝第一次儲存中途被砍,第一個檔會留下來,
            #   其他檔還不存在 —— 交易永久停在部分生效。
            try:
                if os.path.exists(target):
                    _remove_with_retry(target)
                    n += 1
            except Exception:
                logging.error("[atomic_io] 刪除 %s 失敗", target, exc_info=True)
                stuck.append(target)
            continue
        if not os.path.exists(bak):
            # 原本存在、卻沒有備份 → 沒有舊內容可還原,不可以刪。
            logging.error("[atomic_io] %s 原本存在但找不到備份 → 無法還原", target)
            stuck.append(target)
            continue
        try:
            _replace_with_retry(bak, target)
            n += 1
        except Exception:
            logging.error("[atomic_io] 還原 %s 失敗", target, exc_info=True)
            stuck.append(target)
    if n:
        logging.warning("[atomic_io] 上次設定存檔中途被中止 → 已還原 %d 個檔"
                        "(設定回到存檔前的狀態,請重新存一次)", n)
    # ★[第 3 回] 有還原不了的就【什麼都不要清】★
    #   清掉 manifest 與備份 = 把下次重試需要的舊資料永久刪除,
    #   而設定仍然是半新半舊。留著,下次啟動再試一次。
    if stuck:
        logging.error("[atomic_io] 仍有 %d 個檔還原不了 → 保留交易紀錄與備份,"
                      "下次啟動再試:%s", len(stuck), stuck)
        return n
    _remove_manifest(path)
    for target in targets:
        bak = str(target) + ".rollback.bak"
        try:
            if os.path.exists(bak):
                _remove_with_retry(bak)
        except Exception:
            logging.debug("[atomic_io] 清除備份失敗", exc_info=True)
    return n


def _rollback_written(written: list, backups: dict, existed: set) -> list:
    """把已經 replace 掉的檔案還原。回傳【還原不了】的那幾個。

    ★`existed` 是獨立的事實,不可以從 `backups` 有沒有 key 去推★
    (外審第 11 輪第 2 回)備份失敗時 `backups` 也沒有 key,而那時把檔案
    刪掉就是把使用者的設定毀掉。現在備份失敗根本不會走到 commit,
    這裡再用 `existed` 明確判斷一次。
    """
    stuck = []
    for target_path in written:
        bak = backups.get(target_path)
        try:
            if target_path not in existed:
                # 原本沒有這個檔 → 回滾就是把它刪掉
                if os.path.exists(target_path):
                    _remove_with_retry(target_path)
            elif bak is None:
                # 原本有、卻沒有備份 → 不可以刪,只能承認還原不了
                logging.error("[atomic_io] %s 原本存在但沒有備份 → 無法還原",
                              target_path)
                stuck.append(target_path)
            else:
                _replace_with_retry(bak, target_path)
                backups.pop(target_path, None)
        except Exception:
            logging.error("[atomic_io] 還原 %s 失敗 → 設定仍是半新半舊",
                          target_path, exc_info=True)
            stuck.append(target_path)
    return stuck


def _drop_backups(backups: dict) -> None:
    for bak in list(backups.values()):
        try:
            if os.path.exists(bak):
                _remove_with_retry(bak)
        except Exception:
            logging.debug("[atomic_io] 清除備份失敗", exc_info=True)
    backups.clear()


def safe_load_json_ex(file_path: str, default=None, *,
                      backup_on_corrupt: bool = True):
    """同 safe_load_json，但額外回傳「載入狀態」以便呼叫端決策。回 (value, status)：

      "ok"      正常載入
      "missing" 檔案不存在（回 default）
      "corrupt" JSON/編碼損壞——已 backup 壞檔並回 default（原檔已被 rename 移走）
      "error"   OSError/PermissionError 等暫時性失敗（回 default；**原檔通常仍完好**）

    用途（AB-04）：呼叫端可據 status 決定「是否可用預設值覆寫原檔」——missing/corrupt
    可（原檔已不存在/已移走），但 "error" **不可**（只是暫時被防毒/備份軟體鎖住，覆寫
    會把使用者的好檔毀成預設）。
    """
    if not os.path.exists(file_path):
        return default, "missing"
    try:
        # [IF-02] 用 utf-8-sig 讀:容忍記事本另存 UTF-8 時加的 BOM(否則 json.load 直接 JSONDecodeError
        # → 被當 corrupt)。utf-8-sig 對「無 BOM 的純 utf-8」行為與 utf-8 完全一致,向後相容、無副作用。
        with open(file_path, 'r', encoding='utf-8-sig') as f:
            return json.load(f), "ok"
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logging.warning("[safe_load_json] %s 內容損壞 (%s): %s",
                          file_path, type(e).__name__, e)
        if backup_on_corrupt:
            try:
                ts = time.strftime("%Y%m%d_%H%M%S")
                bak = _next_corrupt_backup_path(file_path, ts)
                _replace_with_retry(file_path, bak)
                logging.warning("[safe_load_json] 已 backup 壞檔到 %s", bak)
            except Exception:
                # [2026-07-25 審查] backup 失敗仍回 "corrupt" 會**違反本函式自己的契約**
                # （見 docstring:"corrupt" 代表「原檔已被 rename 移走」故可安全覆寫）。
                # 別的行程允許讀取但拒絕 rename/delete 時就會走到這裡:呼叫端據 "corrupt"
                # 判定可覆寫 → 直接把使用者的原檔蓋掉且**毫無備份**。原檔既然還在,
                # 語意上就等同 "error"(暫時性失敗、原檔完好、不可覆寫)。
                logging.warning("[safe_load_json] 壞檔 backup 失敗，原檔仍在 → "
                                "回報 error(不可覆寫): %s", file_path,
                                exc_info=True)
                return default, "error"
        return default, "corrupt"
    except (PermissionError, OSError) as e:
        logging.warning("[safe_load_json] %s 讀取失敗 (%s)", file_path, e)
        return default, "error"
    except Exception:
        logging.exception("[safe_load_json] %s 未預期例外", file_path)
        return default, "error"


def safe_load_json(file_path: str, default=None, *,
                    backup_on_corrupt: bool = True):
    """讀 JSON，corrupt 自動 backup 壞檔 + log warning + 回 default。

    使用：
        cfg = safe_load_json('settings.json', default={"enabled": True})

    處理的錯誤：
      - FileNotFoundError → 回 default (不視為錯誤)
      - json.JSONDecodeError → backup 壞檔 → log warning → 回 default
      - UnicodeDecodeError → 同上 (檔案不是 UTF-8，可能被改壞)
      - PermissionError / OSError → log warning → 回 default
      - 其他例外 → log error → 回 default

    backup_on_corrupt=True 時，壞檔會 rename 成 `<file_path>.corrupt-<timestamp>`，
    方便事後 forensic / 手動還原。需要區分失敗原因（暫時鎖住 vs 損壞）請改用
    safe_load_json_ex（契約向後相容，本函式只是丟掉 status）。
    """
    value, _status = safe_load_json_ex(
        file_path, default, backup_on_corrupt=backup_on_corrupt)
    return value


def atomic_write_text(file_path: str, content: str, encoding: str = 'utf-8') -> bool:
    """文字檔原子寫入（含 .bak 備份）。
    搬自原主程式 _safe_write (line 8650-8670)，用於線上更新覆寫程式碼檔。
    """
    backup = file_path + '.bak'
    fd = -1
    tmp = ""
    try:
        target_dir = os.path.dirname(file_path) or '.'
        os.makedirs(target_dir, exist_ok=True)
        if os.path.exists(file_path):
            _copy2_with_retry(file_path, backup)
        fd, tmp = _make_temp_path(file_path)
        with os.fdopen(fd, 'w', encoding=encoding) as f:
            fd = -1
            f.write(content)
            _flush_and_fsync(f)
        _replace_with_retry(tmp, file_path)
        return True
    except Exception as e:
        logging.error("atomic_write_text 失敗 [%s]: %s", file_path, e)
        if fd >= 0:
            try:
                os.close(fd)
            except Exception:
                pass
        if tmp and os.path.exists(tmp):
            try:
                _remove_with_retry(tmp)
            except OSError:
                logging.debug("移除 tmp 失敗", exc_info=True)
        return False
