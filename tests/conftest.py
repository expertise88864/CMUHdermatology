# -*- coding: utf-8 -*-
"""Repo 級 pytest 共用夾具（測試基建）。

問題：pytest 下 `sys.argv[0]` 是 pytest，`cmuh_common.paths.get_app_dir()` 會解析到
`site-packages/pytest/`，於是 `get_settings_dir()` → `site-packages/pytest/settings/`。
任何經由它落盤的狀態（`update_policy` 的 `.auto_update_suspended_until`、`watchdog_core`
的 crash-loop 歷史、`consult_query` 的 `consult_notified.json`、`autoclock` 的
`debug_dumps/` …）都會污染那個「所有專案共用」的目錄，並在測試之間互相干擾
（實際發生過：一次 crash-loop 寫了 auto-update suspend 旗標，害 test_updater.py 5 個
測試因「auto-update suspended」失敗）。

解法：
1. import 本檔（pytest 在收集測試檔之前就會載入 conftest）時，先把 `get_app_dir` 換成一個
   『穩定函式 + 可變 holder』：函式物件永不再換，只換 holder 指向的目錄。collection 期各
   入口模組（main / scheduler / consult_query / autoclock）import 時算出的路徑常數、
   `get_settings_dir()` 的 makedirs、以及『模組層就開檔的 log handler』（scheduler /
   coord_detector 在 import 就開 automation_ui.log / coord_detector.log）都落在暫存目錄，
   不會在 site-packages 留檔。
   —— 為何用「事前導向」而非「事後清掉」：Windows 上 log 檔被 handler 開著就刪不掉，
      所以只能在寫入『之前』改路徑；收集期之後再清是清不掉開啟中的 log 的。
   —— 暫存目錄一建立就用 atexit 掛清理：即使只 --collect-only 或收集期就出錯（autouse
      夾具根本不會跑），行程結束時仍會嘗試清掉，不留 cmuh_pytest_* 殘骸。
2. autouse 夾具在「每個測試」把 holder 指到各自的 `tmp_path` 專屬目錄 → 所有透過
   `get_settings_dir()`/`get_conf_path()`/by-name `get_app_dir()` 的檔案 IO 彼此隔離、
   且永不污染共用目錄。因為所有 `from ...paths import get_app_dir` 的 by-name 綁定（不論在
   collection 期或某測試『內』才首次 import）都指向同一個穩定函式，永遠讀到當前(測試)目錄，
   不會卡在某個已刪除的舊 tmp。
   另需修正「import 當下就把值算死」的路徑常數（CONFIG_FILE / LOG_FILE / *_FLAG /
   SHOTS_DIR / _NOTIFIED_FILE / DEBUG_DUMPS_DIR / SETTINGS_DIR / BASE_DIR …）——這些是『值』
   不是函式，holder 幫不到，需逐一改指本測試 tmp。以泛化掃描（非硬列名單）避免漏列。

代價與補償：換掉 `get_app_dir` 會讓 test_paths.py 的 `from ... import get_app_dir` 綁到
穩定函式而非正版，失去對「路徑選擇邏輯」的覆蓋（codex review 指出）。因此另外提供
`real_get_app_dir` 夾具交出『未被導向的正版函式』，讓 test_paths 仍能驗真正的實作。
"""
import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

# 讓 conftest 與所有測試都能 import src/ 下的套件（各測試檔原本各自 insert，這裡集中
# 先做一次；重複 insert 無害）。
_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from cmuh_common import paths  # noqa: E402

# 先留一份『未被導向的正版』get_app_dir，供 real_get_app_dir 夾具交給 test_paths 驗真實
# 邏輯。也趁此記下真實 settings 目錄（＝被導向前的位置）與它「本 session 開始前是否已存在」，
# 供收尾時只清『我們新建的空目錄』用。
_REAL_GET_APP_DIR = paths.get_app_dir
_REAL_SETTINGS_DIR = os.path.join(_REAL_GET_APP_DIR(), "settings")
_REAL_SETTINGS_PREEXISTED = os.path.isdir(_REAL_SETTINGS_DIR)

# ── session 級：collection 期 import 各模組前就把 get_app_dir 換成「穩定函式 + 可變 holder」。
#    函式物件永不再換 → 所有 by-name 綁定（含測試內才首次 import 的）都追蹤同一個 holder。
_SESSION_APP_DIR = tempfile.mkdtemp(prefix="cmuh_pytest_")
_APP_DIR_HOLDER = {"app": _SESSION_APP_DIR}  # 值 = 當前(測試) app 目錄；collection 期＝session


def _stable_get_app_dir():
    return _APP_DIR_HOLDER["app"]


paths.get_app_dir = _stable_get_app_dir


def _cleanup_session_artifacts():
    """清掉暫存 app 目錄；再順手移除真實 settings/ 下『本 session 才新建、且仍為空』的殘留
    （某些背景 daemon 誤觸 launcher 而以 sys.argv[0]=pytest 起的臨時行程可能建）。
    開啟中的檔案（模組層 log handler）刪不掉時 ignore_errors 略過，殘留只在 %TEMP%。"""
    shutil.rmtree(_SESSION_APP_DIR, ignore_errors=True)
    if not _REAL_SETTINGS_PREEXISTED:
        try:
            if os.path.isdir(_REAL_SETTINGS_DIR) and not os.listdir(_REAL_SETTINGS_DIR):
                os.rmdir(_REAL_SETTINGS_DIR)
        except OSError:
            pass


# 一建立就掛清理 → 即使只收集/收集期出錯、autouse 夾具沒跑，行程結束仍會清。
atexit.register(_cleanup_session_artifacts)

_SRC_NORM = os.path.normcase(_SRC)
_SESSION_APP_NORM = os.path.normcase(_SESSION_APP_DIR)

# 每個 per-test app 目錄用這個獨特名字（tmp_path/_cmuh_app）。除了辨識度，也讓 _rebased
# 能認出「上一個測試」的 per-test 根：模組若在某測試『內』才首次 import，其路徑常數會直接
# 算在該測試的 tmp 下（掃描已跑過、來不及導）；下一個測試靠這個標記把它從舊 tmp 前綴改到
# 新 tmp，維持逐測試隔離（codex review 指出的邊界）。
_MARK = "_cmuh_app"


@pytest.fixture
def real_get_app_dir():
    """交出『未被 conftest 導向的正版』get_app_dir，讓 test_paths 能驗真實路徑選擇邏輯。"""
    return _REAL_GET_APP_DIR


def _rebased(value, app_dir: str):
    """若 value 是「某個 app 根」底下的路徑（session 根，或前一個測試的 tmp/_cmuh_app 根），
    回傳把該前綴換成本測試 app_dir 後的新值（型別沿用 str / Path）；否則回 None（不動）。"""
    if isinstance(value, str):
        s = value
    elif isinstance(value, Path):
        s = str(value)
    else:
        return None
    sn = os.path.normcase(s)
    # 1) collection 期（session 根）算出的常數——最常見路徑。
    if sn == _SESSION_APP_NORM:
        return type(value)(app_dir)
    if sn.startswith(_SESSION_APP_NORM + os.sep):
        return type(value)(os.path.join(app_dir, s[len(_SESSION_APP_DIR) + 1:]))
    # 2) 前一個測試的 per-test 根（…/_cmuh_app）底下算出的常數（模組在測試內首次 import）。
    tail_norm = os.path.normcase(os.sep + _MARK)
    if sn.endswith(tail_norm):                       # value 剛好是某個 app 根（BASE_DIR）
        return type(value)(app_dir)
    key_norm = os.path.normcase(os.sep + _MARK + os.sep)
    i = sn.find(key_norm)                            # value 落在某個 app 根底下
    if i >= 0:
        return type(value)(os.path.join(app_dir, s[i + len(key_norm):]))
    return None


def _redirect_cached_consts(monkeypatch, app_dir: str) -> None:
    """把我方 src/ 模組裡、import 期算死在（某個）app 根底下的『路徑常數』改指本測試 tmp。
    （get_app_dir 這個『函式』本身用穩定 holder 處理，見檔頭 §1/§2；此處只處理『值』常數。）
    泛化掃描（非硬列名單）→ CONFIG_FILE / LOG_FILE / *_FLAG / SHOTS_DIR / _NOTIFIED_FILE /
    DEBUG_DUMPS_DIR / SETTINGS_DIR / BASE_DIR … 全被涵蓋，避免漏列導致跨測試共用狀態。"""
    for mod in list(sys.modules.values()):
        f = getattr(mod, "__file__", None)
        # abspath 正規化：模組可能經由帶 ".." 的 sys.path 進入（各測試檔用
        # os.path.join(dirname, "..", "src") 匯入）→ __file__ 含 ".."，未正規化會漏掉。
        if not f or not os.path.normcase(os.path.abspath(f)).startswith(_SRC_NORM):
            continue
        for attr, val in list(vars(mod).items()):
            new = _rebased(val, app_dir)
            if new is not None:
                monkeypatch.setattr(mod, attr, new, raising=False)


@pytest.fixture(autouse=True)
def _isolate_settings_dir(tmp_path, monkeypatch):
    """每個測試把 get_settings_dir() 導向本測試專屬的 tmp 目錄，互相隔離、零污染。"""
    app_dir = tmp_path / _MARK
    (app_dir / "settings").mkdir(parents=True, exist_ok=True)

    # 只換 holder 指向 → 所有綁定穩定函式的 by-name get_app_dir 立刻拿到本測試目錄。
    monkeypatch.setitem(_APP_DIR_HOLDER, "app", str(app_dir))
    _redirect_cached_consts(monkeypatch, str(app_dir))
    yield
