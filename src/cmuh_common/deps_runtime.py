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
from typing import Iterable

from cmuh_common.atomic_io import atomic_write_text
from cmuh_common.deps_manifest import _resolve_pip_spec
from cmuh_common.paths import get_app_dir, is_frozen


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
    if requirement.specifier and not requirement.specifier.contains(
            installed, prereleases=True):
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
    """回傳無法 import 或實際版本不符的套件。"""
    missing_libs = []
    for pkg, imp in required_libs:
        try:
            importlib.import_module(imp)
        except Exception:
            logging.warning("依賴 import 失敗: pip=%s import=%s", pkg, imp,
                            exc_info=True)
            missing_libs.append((pkg, imp))
            continue
        if not _distribution_satisfies(pkg):
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

    # 完整檢查：哪些缺
    missing_libs = _find_missing_libs(required_libs)

    # 缺的話跳 UI
    if missing_libs:
        from cmuh_common.deps_installer import DependencyInstaller
        app = DependencyInstaller(required_libs, missing_libs)
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
