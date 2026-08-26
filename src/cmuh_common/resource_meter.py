# -*- coding: utf-8 -*-
"""資源自我量測(穩定性資源優化工單 Phase 0;使用者 2026-08-26 定案:
★做進主程式、跑主程式時自動量測、不另開視窗★)。

背景:會診查詢+打卡改在治療室共用電腦常駐(隨時有人在用),低資源占用是
硬約束,而「先量測再優化」是工單第一原則。量測器由主程式內建自動啟動,
量的是★整個程式家族★:主程式自己、會診查詢、打卡、watchdog,以及它們的
子孫行程樹(systemftp、chromedriver→Chrome 全樹)——加上整機情境列,
分析時才分得出「程式吃掉的」與「機器本來就忙」。

★這個模組是唯讀的★ —— 它絕不可以被拿去「找到行程然後對它做事」。
家族行程的歸屬靠 cmdline+ppid(量測用途足夠),那比 spawn handle 弱:
同名行程可能是別人的(2026-07-27 事故的教訓)。量測拿它只會多算一列,
自動化拿它會關掉別人的程式 —— 所以這裡刻意★只提供讀取,不匯出任何
「找 pid」的 API★給其他用途。

設計原則:
* ★fail-open★:量測自己絕不可以影響本業。任何一段失敗都吞掉(debug log),
  最壞情況是那一輪少幾列。量測工具把主程式弄掛,比不量還糟。
* ★不另開視窗★:行程表用 PowerShell CIM 查,一律 CREATE_NO_WINDOW。
* CPU 記【累積值】不記差分:掉一筆樣本不會讓後面的差分全錯,
  差分由離線報告腳本算(相鄰兩筆相減)。
* 零第三方依賴(ctypes + 內建 PowerShell)。

CSV 欄位(固定;報告腳本靠它,加欄位往後 append、不重排):
  ts,host,version,label,scope,exe,pid,n_procs,cpu_user_s,cpu_kernel_s,
  rss_mb,handles,gdi,user_objs,py_threads,
  sys_idle_s,sys_kernel_s,sys_user_s,mem_load_pct
scope:self(宿主自己)/ proc(家族根行程)/ child(某根的子孫,逐 exe 彙總)
     / system(整機情境)。
"""
from __future__ import annotations

import ctypes
import logging
import os
import re
import subprocess
import threading
from ctypes import wintypes
from datetime import datetime

#: 取樣間隔(秒)。工單定 5 分鐘。
SAMPLE_INTERVAL_SECONDS = 300
#: 檔名按月分檔;啟動時清掉超過這個天數的舊檔(檔名比對,不開檔)。
KEEP_DAYS = 62
#: cmdline 命中這些字串的 python 行程視為家族根(label 取第一個命中)。
#: ★生產的命令列不含英文模組名★(外審 Phase0 R1 P1):launcher 是
#:   `pythonw.exe 中國醫皮膚科打卡程式.pyw`,用 runpy ★行程內★執行
#:   src/autoclock.py —— OS 層 CommandLine 只有中文 launcher 檔名。
#:   英文 token 只涵蓋開發機直跑;兩種形狀都要認得,不然生產全漏量。
TARGET_TOKENS = (("consult_query", "會診查詢"),
                 ("中國醫皮膚科會診查詢程式", "會診查詢"),
                 ("autoclock", "打卡"),
                 ("中國醫皮膚科打卡程式", "打卡"),
                 ("watchdog_runner", "watchdog"),
                 ("中國醫皮膚科守護程式", "watchdog"),
                 ("main.py", "主程式"),
                 ("中國醫皮膚科主程式", "主程式"))
_CREATE_NO_WINDOW = 0x08000000
_PROCESS_QUERY_LIMITED = 0x1000

_CSV_HEADER = ("ts,host,version,label,scope,exe,pid,n_procs,"
               "cpu_user_s,cpu_kernel_s,rss_mb,handles,gdi,user_objs,"
               "py_threads,sys_idle_s,sys_kernel_s,sys_user_s,"
               "mem_load_pct\n")
_FILE_RE = re.compile(r"^resource_meter_(\d{6})\.csv$")


class _PMC(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t)]


class _MEMSTATEX(ctypes.Structure):
    _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def _ft_sec(ft) -> float:
    return ((ft.dwHighDateTime << 32) | ft.dwLowDateTime) / 1e7


def _sample_handle(handle, *, want_gui: bool) -> "dict | None":
    """讀一個行程 handle 的 CPU/RSS/handle 數(+GUI 物件)。失敗回 None。

    ★handle 一律包成 wintypes.HANDLE★:64 位下把 Python int(尤其
    GetCurrentProcess 的假 handle -1)直接當參數傳,ctypes 預設用 c_int
    會截成 32 位 → ERROR_INVALID_HANDLE。這個 bug 在 fail-open 底下是
    ★完全無聲★的 —— self 列就是這樣整個消失的(pretest 抓到)。
    """
    try:
        if isinstance(handle, int):
            handle = wintypes.HANDLE(handle)
        k32 = ctypes.windll.kernel32
        f = [wintypes.FILETIME() for _ in range(4)]
        if not k32.GetProcessTimes(handle, *(ctypes.byref(x) for x in f)):
            return None
        pmc = _PMC()
        pmc.cb = ctypes.sizeof(pmc)
        # ★讀不到就是【未知】,不是 0★(外審 Phase0 R1 P2):偽裝成 0 的基線
        #   會把「量不到」誤讀成「不吃記憶體」。本平台實測 LIMITED 權限讀得到
        #   (Win10+,err 0),這是防禦老平台/受保護行程的誠實出口。
        rss = (round(pmc.WorkingSetSize / 1048576.0, 1)
               if k32.K32GetProcessMemoryInfo(handle, ctypes.byref(pmc),
                                              pmc.cb) else "")
        n = wintypes.DWORD(0)
        k32.GetProcessHandleCount(handle, ctypes.byref(n))
        gdi = user_objs = 0
        if want_gui:
            u32 = ctypes.windll.user32
            gdi = int(u32.GetGuiResources(handle, 0))
            user_objs = int(u32.GetGuiResources(handle, 1))
        return {"user": round(_ft_sec(f[3]), 1),
                "kernel": round(_ft_sec(f[2]), 1),
                "rss": rss, "handles": int(n.value),
                "gdi": gdi, "user_objs": user_objs}
    except Exception:
        logging.debug("[resource_meter] handle 取樣失敗(略過)", exc_info=True)
        return None


def _default_pid_sampler(pid: int) -> "dict | None":
    """開 PROCESS_QUERY_LIMITED_INFORMATION ★唯讀★取樣;失敗回 None。"""
    try:
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(_PROCESS_QUERY_LIMITED, False, int(pid))
        if not h:
            return None
        try:
            return _sample_handle(h, want_gui=False)
        finally:
            k32.CloseHandle(wintypes.HANDLE(h))
    except Exception:
        logging.debug("[resource_meter] pid 取樣失敗(略過)", exc_info=True)
        return None


def _default_proc_table() -> list:
    """整機行程表 → [{pid, ppid, name, cmd}](PowerShell CIM;失敗回 [])。

    ★CREATE_NO_WINDOW★:量測不可以閃出任何視窗(使用者 2026-08-26 定案)。
    """
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | "
             "Select-Object ProcessId,ParentProcessId,Name,CommandLine | "
             "ConvertTo-Csv -NoTypeInformation"],
            capture_output=True, text=True, timeout=60,
            creationflags=_CREATE_NO_WINDOW)
        import csv as _csv
        import io as _io
        procs = []
        for r in list(_csv.reader(_io.StringIO(out.stdout)))[1:]:
            if len(r) < 4:
                continue
            try:
                procs.append({"pid": int(r[0]), "ppid": int(r[1] or 0),
                              "name": r[2], "cmd": r[3] or ""})
            except ValueError:
                continue
        return procs
    except Exception:
        logging.debug("[resource_meter] 行程表列舉失敗(略過)", exc_info=True)
        return []


def family_roots(procs, self_pid: int) -> list:
    """→ [(label, proc)]:cmdline 命中 TARGET_TOKENS 的 python 行程(排除自己)。"""
    roots = []
    for p in procs:
        if p["pid"] == self_pid:
            continue
        low = (p.get("cmd") or "").lower()
        if "python" not in (p.get("name") or "").lower() \
                and not low.endswith((".py", ".pyw")):
            continue
        for token, label in TARGET_TOKENS:
            if token in low:
                roots.append((label, p))
                break
    return roots


def descendant_tree(procs, root_pid: int, claimed: set) -> list:
    """ppid 樹往下收整棵子孫;★不搶已被認領的行程★(別的根、或宿主自己)。"""
    kids: dict = {}
    for p in procs:
        kids.setdefault(p["ppid"], []).append(p)
    out, stack = [], [root_pid]
    while stack:
        for c in kids.get(stack.pop(), ()):
            if c["pid"] in claimed:
                continue
            claimed.add(c["pid"])
            out.append(c)
            stack.append(c["pid"])
    return out


class ResourceMeter:
    """主程式內建的家族資源量測。start() 後每 interval 秒寫一輪 CSV。

    `proc_table` / `pid_sampler` 可注入(測試用);生產一律走預設
    (PowerShell CIM / OpenProcess 唯讀)。
    """

    def __init__(self, out_dir, program: str, version: str,
                 interval_sec: float = SAMPLE_INTERVAL_SECONDS,
                 proc_table=None, pid_sampler=None):
        self._dir = str(out_dir)
        self._program = program
        self._version = version
        self._interval = max(5.0, float(interval_sec))
        self._proc_table = proc_table or _default_proc_table
        self._pid_sampler = pid_sampler or _default_pid_sampler
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = None

    # ── 檔案 ────────────────────────────────────────────────────────────
    def _path(self, now=None) -> str:
        stamp = (now or datetime.now()).strftime("%Y%m")
        return os.path.join(self._dir, f"resource_meter_{stamp}.csv")

    def _write_row(self, row: str) -> None:
        """append 一列(必要時補 header)。★任何失敗吞掉★:量測不可傷本業。"""
        try:
            with self._lock:
                path = self._path()
                need_header = not os.path.exists(path)
                with open(path, "a", encoding="utf-8", newline="") as f:
                    if need_header:
                        f.write(_CSV_HEADER)
                    f.write(row)
        except Exception:
            logging.debug("[resource_meter] 寫入失敗(略過)", exc_info=True)

    def prune_old_files(self, now=None) -> None:
        """啟動時清掉太舊的月檔。★只認自己的檔名格式★,其他檔一概不碰。"""
        try:
            now = now or datetime.now()
            for name in os.listdir(self._dir):
                m = _FILE_RE.match(name)
                if not m:
                    continue
                try:
                    first = datetime.strptime(m.group(1), "%Y%m")
                    # ★年齡以【月底】算★(外審 Phase0 R1 P2):以月初算的話,
                    #   月底寫入的資料只活 ~32 天就被整檔帶走,62 天契約剩一半。
                    #   整個月的最後一筆都超過 KEEP_DAYS 才刪整檔。
                    nxt = (first.replace(year=first.year + 1, month=1)
                           if first.month == 12
                           else first.replace(month=first.month + 1))
                    age = (now - nxt).days + 1        # 月底那天的年齡
                except ValueError:
                    continue
                if age > KEEP_DAYS:
                    os.remove(os.path.join(self._dir, name))
        except Exception:
            logging.debug("[resource_meter] 舊檔清理失敗(略過)", exc_info=True)

    # ── 取樣 ────────────────────────────────────────────────────────────
    def sample_once(self, now=None) -> int:
        """取樣一輪 → 寫入的列數。逐段 fail-open:系統列/自己/家族互不拖累。"""
        rows = 0
        ts = (now or datetime.now()).isoformat(timespec="seconds")
        base = f"{ts},{self._program},{self._version}"
        # ① 整機情境列
        try:
            k32 = ctypes.windll.kernel32
            f = [wintypes.FILETIME() for _ in range(3)]
            k32.GetSystemTimes(*(ctypes.byref(x) for x in f))
            ms = _MEMSTATEX()
            ms.dwLength = ctypes.sizeof(ms)
            k32.GlobalMemoryStatusEx(ctypes.byref(ms))
            self._write_row(f"{base},system,system,,,,,,,,,,,"
                            f"{_ft_sec(f[0]):.1f},{_ft_sec(f[1]):.1f},"
                            f"{_ft_sec(f[2]):.1f},{ms.dwMemoryLoad}\n")
            rows += 1
        except Exception:
            logging.debug("[resource_meter] 系統列失敗(略過)", exc_info=True)
        # ② 宿主自己(pseudo-handle,含 GUI 物件與 Python 執行緒數)
        self_pid = os.getpid()
        try:
            me = ctypes.windll.kernel32.GetCurrentProcess()
            d = _sample_handle(me, want_gui=True)
            if d:
                self._write_row(
                    f"{base},{self._program},self,python.exe,{self_pid},1,"
                    f"{d['user']},{d['kernel']},{d['rss']},{d['handles']},"
                    f"{d['gdi']},{d['user_objs']},"
                    f"{threading.active_count()},,,,\n")
                rows += 1
        except Exception:
            logging.debug("[resource_meter] self 取樣失敗(略過)", exc_info=True)
        # ③ 家族(其他根行程 + 各自的子孫樹;宿主自己的子孫也算一棵)
        try:
            procs = self._proc_table()
            if not procs:
                return rows
            roots = family_roots(procs, self_pid)
            claimed = {self_pid} | {p["pid"] for _, p in roots}
            for label, p in roots:
                d = self._pid_sampler(p["pid"])
                if d:
                    self._write_row(
                        f"{base},{label},proc,{p['name']},{p['pid']},1,"
                        f"{d['user']},{d['kernel']},{d['rss']},"
                        f"{d['handles']},,,,,,,\n")
                    rows += 1
            for label, root_pid in ([(self._program, self_pid)]
                                    + [(lb, p["pid"]) for lb, p in roots]):
                agg: dict = {}
                for c in descendant_tree(procs, root_pid, claimed):
                    dc = self._pid_sampler(c["pid"])
                    if not dc:
                        continue
                    a = agg.setdefault(c["name"], {"n": 0, "user": 0.0,
                                                   "kernel": 0.0, "rss": 0.0,
                                                   "rss_n": 0, "handles": 0})
                    a["n"] += 1
                    for k in ("user", "kernel", "handles"):
                        a[k] += dc[k]
                    if isinstance(dc["rss"], (int, float)):
                        # 未知(空)不參與彙總 —— 加不得也不可當 0
                        a["rss"] += dc["rss"]
                        a["rss_n"] += 1
                for exe, a in sorted(agg.items()):
                    # ★一個都量不到時 rss 是【空】不是 0.0★(外審 Phase0 R2):
                    #   累加器初值漏出去,就是把「全部未知」偽裝成「不吃記憶體」
                    #   —— 與單筆的誠實規則同一條,在彙總層也要成立。
                    _rss = f"{a['rss']:.1f}" if a["rss_n"] else ""
                    self._write_row(
                        f"{base},{label},child,{exe},,{a['n']},"
                        f"{a['user']:.1f},{a['kernel']:.1f},{_rss},"
                        f"{a['handles']},,,,,,,\n")
                    rows += 1
        except Exception:
            logging.debug("[resource_meter] 家族取樣失敗(略過)", exc_info=True)
        return rows

    # ── 常駐 ────────────────────────────────────────────────────────────
    def start(self) -> None:
        """啟動取樣執行緒(daemon;隨主程式結束)。重複呼叫是 no-op。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self.prune_old_files()
        self._thread = threading.Thread(target=self._loop,
                                        name="ResourceMeter", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        # 開機先取一筆(不然要等 5 分鐘才有第一筆,短命行程什麼都留不下)
        self.sample_once()
        while not self._stop.wait(self._interval):
            self.sample_once()
