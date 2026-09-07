# -*- coding: utf-8 -*-
"""依賴檢查與安裝（runtime 入口層）。搬自原主程式 line 132-200，泛化以支援多入口。

每個 entry script 啟動最前面呼叫 ensure_dependencies(REQUIRED_LIBS_FOR_THIS_APP)。

【強化版指紋】指紋包含 (pkg:imp 對 + Python 主版本 + interpreter 路徑)。
Python 升級或 .pyw 關聯切換到另一套 Python 後，快取都會自動失效。
"""
import gc
import importlib
import importlib.metadata
import importlib.util
import logging
import os
import re
import sys
import time
from typing import Iterable

from cmuh_common.atomic_io import atomic_write_text
from cmuh_common.deps_manifest import _resolve_pip_spec
from cmuh_common.paths import (
    HANDSHAKE_BOOTSTRAPPING, HANDSHAKE_REPAIR_ONLY, get_app_dir, get_settings_dir, is_frozen,
    parent_supports_repair_only,
    parent_understands_bootstrapping, restart_handshake_active,
    restart_handshake_signal,
)

_REPAIR_LOCK_FILENAME = ".deps_repair.lock"


def _repair_lock_path() -> str:
    return os.path.join(get_settings_dir(), _REPAIR_LOCK_FILENAME)


def _acquire_repair_lock(path: str):
    from cmuh_common.deps_lock import acquire
    return acquire(path)


def _release_repair_lock(fd, path: str) -> None:
    # A pip subprocess may still own its inherited handle. Never unlink/steal.
    os.close(fd)


_PACKAGING_REQUIREMENT = None


def _requirement_class():
    """Return packaging.Requirement, falling back to pip's vendored copy.

    Dependency verification runs before the app's normal dependencies are
    repaired, so a fresh Python installation may not have the standalone
    ``packaging`` distribution yet. pip must exist for the repair path to
    work, and its vendored parser is sufficient to bootstrap that first run.
    """
    global _PACKAGING_REQUIREMENT
    if _PACKAGING_REQUIREMENT is not None:
        return _PACKAGING_REQUIREMENT
    try:
        module = importlib.import_module("packaging.requirements")
    except ImportError:
        module = importlib.import_module("pip._vendor.packaging.requirements")
    _PACKAGING_REQUIREMENT = module.Requirement
    return _PACKAGING_REQUIREMENT


def _resolve_requirement_spec(pkg_name: str) -> str:
    """Resolve a bare runtime package name to its manifest version spec."""
    return _resolve_pip_spec(pkg_name)


def _distribution_satisfies(pkg_name: str) -> bool:
    """Whether the installed distribution satisfies the effective PEP 440 spec."""
    spec = _resolve_requirement_spec(pkg_name)
    try:
        requirement = _requirement_class()(spec)
    except Exception:
        logging.warning("無法解析依賴版本規格: %s", spec, exc_info=True)
        return False
    if requirement.marker is not None and not requirement.marker.evaluate():
        return True
    try:
        installed = importlib.metadata.version(requirement.name)
    except importlib.metadata.PackageNotFoundError:
        return False
    except Exception:
        logging.warning("無法讀取依賴版本: %s", requirement.name, exc_info=True)
        return False
    if not requirement.specifier:
        return True
    try:
        satisfied = requirement.specifier.contains(installed, prereleases=True)
    except Exception:
        # [外審 r11 P2] ★版本字串本身可能不是 PEP 440★(legacy 版號,或被中斷的安裝
        # 留下的殘缺 metadata)—— `contains()` 會拋 InvalidVersion。周圍每一個失敗都
        # 走「當成不滿足 → 交給修復流程」,只有這裡會讓整支程式在修復 UI 之前就掛掉。
        logging.warning("無法比對依賴版本: %s 已安裝=%s 需求=%s",
                        requirement.name, installed, requirement.specifier,
                        exc_info=True)
        return False
    if not satisfied:
        logging.warning(
            "依賴版本不符: %s 已安裝=%s 需求=%s",
            requirement.name, installed, requirement.specifier)
        return False
    return True


def _with_packaging_dependency(required_libs: Iterable[tuple]) -> list[tuple]:
    """Ensure the version parser itself is maintained as a runtime dependency."""
    libs = list(required_libs)
    for pkg, _imp in libs:
        if re.match(r"^packaging(?:\s|[<>=!~;\[]|$)", str(pkg), re.IGNORECASE):
            return libs
    return [("packaging", "packaging"), *libs]


def _build_fingerprint(required_libs: Iterable[tuple]) -> str:
    py_ver = f"py{sys.version_info[0]}.{sys.version_info[1]}"
    py_exe = os.path.normcase(os.path.abspath(sys.executable))
    # Include resolved manifest specs, not only callers' usually-bare package
    # names. A requirements floor/pin change must invalidate an old cache.
    libs = "|".join(f"{_resolve_requirement_spec(a)}:{b}"
                    for a, b in required_libs)
    return f"{py_ver}|exe:{py_exe}|{libs}"


def _find_missing_libs(required_libs: Iterable[tuple]) -> list[tuple]:
    """回傳無法 import 或實際版本不符的套件。

    ★[外審 r11 P2] 版本檢查與 import 要分成★兩遍★,不可以逐個交錯★:
      * 逐個交錯時,排在前面而版本合格的套件會被 import,而它可能 transitively 載入
        排在後面、版本★不合格★的套件 —— 實例就在 `main.REQUIRED_LIBS`:`pyautogui`
        (337 行)排在 `Pillow`(340 行)之前,而 import PyAutoGUI/PyScreeze 會把 `PIL`
        一起載進來。等輪到檢查 Pillow 時它早就在 `sys.modules` 裡了。
      * 之後 pip 就地升級 Pillow 的檔案,行程卻仍在用記憶體裡的舊 PIL;而安裝後的複驗
        看的是 distribution metadata(新的)→ 通過。「已修好」是假的。
      * 升級之後也就沒有機會「第一次 import」到新程式碼。
    所以第一遍★只看 metadata、一個都不 import★;只要有任何一個不合格就直接回報,
    完全不進 import 那一遍。等它們裝好之後的複驗才會做 import 檢查,那時每個套件在本
    行程都是第一次 import,拿到的就是新版本。

    取捨:「版本不符」與「有 metadata 但檔案損壞」同時發生時要修兩輪。實務上很罕見 ——
    沒裝的套件在第一遍就是 `PackageNotFoundError` → 已經算不合格,不會落到第二遍。
    """
    required_libs = list(required_libs)
    mismatched = [(pkg, imp) for pkg, imp in required_libs
                  if not _distribution_satisfies(pkg)]
    if mismatched:
        return mismatched

    missing_libs = []
    for pkg, imp in required_libs:
        try:
            importlib.import_module(imp)
        except Exception:
            logging.warning("依賴 import 失敗: pip=%s import=%s", pkg, imp,
                            exc_info=True)
            missing_libs.append((pkg, imp))
    return missing_libs


def _all_modules_discoverable(required_libs: Iterable[tuple]) -> bool:
    """Cheap cache guard: check module presence and version without heavy imports."""
    for pkg, imp in required_libs:
        try:
            if importlib.util.find_spec(imp) is None:
                return False
        except Exception:
            return False
        if not _distribution_satisfies(pkg):
            return False
    return True


def ensure_dependencies(
    required_libs: list,
    deps_cache_filename: str = '.deps_cache',
    *,
    bootstrap: bool = False,
) -> None:
    """檢查並（必要時）安裝 required_libs。

    required_libs: [(pip_name, import_name), ...]

    【防禦性 2026.05.20】.exe 模式（PyInstaller frozen）下完全跳過：sys.executable
    在 frozen 時是 app exe 本身，呼叫 -m pip install 會無限 spawn 自己 → fork bomb。
    """
    if is_frozen():
        return

    required_libs = _with_packaging_dependency(required_libs)
    fingerprint = _build_fingerprint(required_libs)
    deps_cache_file = os.path.join(get_app_dir(), deps_cache_filename)

    main_script = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else __file__

    # 快速路徑：快取與指紋一致 + 比腳本新 → 跳過
    try:
        if os.path.exists(deps_cache_file):
            with open(deps_cache_file, 'r', encoding='utf-8') as cf:
                cache_lines = cf.read().splitlines()
            if cache_lines and cache_lines[0].strip() == fingerprint:
                cache_mtime = os.path.getmtime(deps_cache_file)
                try:
                    script_mtime = os.path.getmtime(main_script)
                except OSError:
                    script_mtime = 0
                if cache_mtime > script_mtime:
                    if _all_modules_discoverable(required_libs):
                        return
                    logging.info("依賴快取命中但模組已遺失，重新驗證環境")
    except Exception:
        logging.debug("讀依賴快取失敗", exc_info=True)

    # ★[外審 r11 P1] 走到這裡代表快速路徑沒中 → 先跟父行程講一聲★。
    # 接下來的完整檢查會把每個套件真的 import 一遍(selenium/openpyxl 這種重的要數秒),
    # 之後可能還要跑 pip(單次 240 秒、retry 一次)甚至等使用者點確認 —— 整段遠超過重啟
    # 交握「0.6 秒就放 mutex/拔熱鍵、再 3 秒沉默就終止」的預設。不講的話,正在修復依賴的
    # 健康子行程會被父行程殺掉,而且診間在那 3.6 秒之後就失去熱鍵,自動更新每 5 秒再試
    # 一次、每次都殺掉一個裝到一半的 pip。★訊號要在完整檢查之前★,不能等到發現有東西缺
    # 才送(檢查本身就已經超時了)。冷啟動沒有交握 → 這是 no-op。
    # ★只在父行程宣告它認得這個階段時才送★:已經部署在診間的舊版父行程看不懂,
    # 對它送等於送一個「內容無效」的交握檔,毫無好處。它們不會設那個環境變數,
    # 所以升級到這一版時行為與升級前完全一致;等所有機器都是新版就自動生效。
    repair_only = (
        bootstrap
        and parent_supports_repair_only()
        and restart_handshake_active()
    )
    if repair_only:
        restart_handshake_signal(HANDSHAKE_REPAIR_ONLY)
    elif bootstrap and parent_understands_bootstrapping():
        restart_handshake_signal(HANDSHAKE_BOOTSTRAPPING)

    # ★同一台機器只能有一個行程在修★(見 _REPAIR_LOCK_FILENAME 的說明)。
    # 拿不到鎖代表別人正在修而且還在動:
    #   * 我是重啟的交棒候選人 → ★乾淨退出★(exit 0、無 crash 痕跡)。舊父行程會判定
    #     「照設計自行結束」→ 保留服務、不示警;修復由持鎖的那個行程做完。
    #   * 冷啟動(使用者自己開的)→ 等實際持有者結束；不依檔案時間接管活著的 pip。
    _lock_path = _repair_lock_path()
    _lock_fd = _acquire_repair_lock(_lock_path)
    if _lock_fd is None:
        if bootstrap and restart_handshake_active():
            logging.warning("[deps] 另一個行程正在修復依賴 → 本行程(重啟候選人)乾淨退出,"
                            "不重複開 pip;更新下一輪再接手")
            sys.exit(0)
        logging.warning("[deps] 另一個行程正在修復依賴 → 等它完成")
        while _lock_fd is None:
            time.sleep(2.0)
            _lock_fd = _acquire_repair_lock(_lock_path)

    try:
        _ensure_dependencies_locked(required_libs, fingerprint, deps_cache_file, _lock_fd)
    finally:
        _release_repair_lock(_lock_fd, _lock_path)
    if repair_only:
        # Never enter application initialization or contend for its mutex.
        # A later retry starts a fresh interpreter against the repaired cache.
        sys.exit(0)


def _ensure_dependencies_locked(required_libs, fingerprint, deps_cache_file, lock_fd) -> None:
    """完整檢查 + 安裝 + 寫快取。★呼叫端已持有修復鎖★。"""
    # 完整檢查：哪些缺
    missing_libs = _find_missing_libs(required_libs)

    # 缺的話跳 UI
    if missing_libs:
        logging.warning("[deps] 需要修復 %d 個依賴,開始安裝(重啟交握已通知父行程等待)",
                        len(missing_libs))
        from cmuh_common.deps_installer import DependencyInstaller
        app = DependencyInstaller(required_libs, missing_libs, repair_lock_fd=lock_fd)
        is_finished = False
        try:
            app.mainloop()
            is_finished = app.is_finished
        finally:
            try:
                app.destroy()
            except Exception:
                logging.debug("關閉依賴安裝視窗失敗", exc_info=True)
        del app
        gc.collect()  # [核心修正] 清除 Tkinter 變數，避免背景執行緒 Variable.__del__ 崩潰
        if not is_finished:
            sys.exit(1)
        # pip exit code 0 不等於 import 一定成功（例如 transitive dependency
        # 損壞或 .pyw 關聯切到另一套 Python）。成功前再做一次完整驗證。
        missing_after_install = _find_missing_libs(required_libs)
        if missing_after_install:
            logging.error("安裝後仍缺少依賴: %s", missing_after_install)
            sys.exit(1)

    # 寫快取
    try:
        if not atomic_write_text(deps_cache_file, fingerprint + "\n"):
            logging.warning("寫入依賴快取失敗: %s", deps_cache_file)
    except Exception:
        logging.debug("寫依賴快取失敗", exc_info=True)
