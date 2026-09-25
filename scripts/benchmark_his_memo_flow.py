"""Compare anonymous F2 fake-HIS stages without touching a real chart.

Run with --baseline /path/to/checkout --candidate /path/to/checkout. The two
workers stay warm while their synthetic F2 runs are interleaved. This is a
developer benchmark, not a hospital-PC latency or p95 measurement.
"""

import argparse
from datetime import date, timedelta
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time


def _worker(root: Path) -> None:
    sys.path.insert(0, str(root / "src"))
    import main  # noqa: PLC0415
    from cmuh_common import uvb_dose  # noqa: PLC0415

    main.check_stop = lambda: None
    main._show_uvb_warning = lambda *_args, **_kwargs: None
    main._record_his_action = lambda *_args, **_kwargs: None
    main._record_uvb_write = lambda *_args, **_kwargs: None
    main._autofill_卡號_from_醫師上次 = lambda **_kwargs: None
    main._script_code_input_adaptive = lambda *_args, **_kwargs: True

    prior = date.today() - timedelta(days=3)
    source = (f"UVB: 800 mj/cm2 (35) on ({prior:%Y/%m/%d}) "
              "add 50 mj/cm2, MAX: 1500 mj/cm2\n"
              "Pathology report: anonymous synthetic line")
    stages = {}

    class FakePort:
        def __init__(self):
            self.text = source
            self.writes = 0

        def find_main_window(self):
            return 10

        def locate_phototherapy(self, _hwnd):
            return 20, "uvb"

        def read_memo(self, _hwnd):
            stage = "readback" if self.writes else "his_read"
            start = time.perf_counter_ns()
            time.sleep(0.002)
            stages[stage] = stages.get(stage, 0) + time.perf_counter_ns() - start
            return self.text

        def write_memo(self, _hwnd, text):
            start = time.perf_counter_ns()
            time.sleep(0.003)
            self.text = text
            self.writes += 1
            stages["write"] = time.perf_counter_ns() - start
            return True

        def update_excimer(self, *_args):
            raise AssertionError("UVB benchmark must not enter Excimer")

    original_calc = uvb_dose.update_uvb_in_text

    def timed_calc(*args, **kwargs):
        start = time.perf_counter_ns()
        result = original_calc(*args, **kwargs)
        stages["calc"] = stages.get("calc", 0) + time.perf_counter_ns() - start
        return result

    uvb_dose.update_uvb_in_text = timed_calc

    for _ in sys.stdin:
        stages.clear()
        port = FakePort()
        main._f23_update_uvb_dose = lambda label="F2", active_port=port: (
            main._update_uvb_dose_core(label, strict=True, memo_port=active_port))
        start = time.perf_counter_ns()
        ok = main.script_F2_adaptive()
        stages["callback"] = time.perf_counter_ns() - start
        payload = {name: round(ns / 1_000_000, 4) for name, ns in stages.items()}
        payload.update(ok=ok, writes=port.writes)
        print(json.dumps(payload), flush=True)


def _open_worker(script: Path, root: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-u", str(script), "--worker", str(root)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"})


def _sample(proc: subprocess.Popen) -> dict:
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write("run\n")
    proc.stdin.flush()
    return json.loads(proc.stdout.readline())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--pairs", type=int, default=20)
    args = parser.parse_args()
    if args.worker:
        _worker(args.worker.resolve())
        return
    if not args.baseline or not args.candidate or args.pairs < 10:
        parser.error("baseline, candidate and at least 10 pairs are required")
    script = Path(__file__).resolve()
    roots = {"baseline": args.baseline.resolve(),
             "candidate": args.candidate.resolve()}
    workers = {name: _open_worker(script, root) for name, root in roots.items()}
    try:
        for worker in workers.values():
            for _ in range(3):
                result = _sample(worker)
                if not result["ok"] or result["writes"] != 1:
                    raise RuntimeError(f"warmup failed: {result}")
        samples = {name: [] for name in workers}
        for index in range(args.pairs):
            names = ("baseline", "candidate") if index % 2 == 0 else (
                "candidate", "baseline")
            for name in names:
                result = _sample(workers[name])
                if not result["ok"] or result["writes"] != 1:
                    raise RuntimeError(f"sample failed: {name}: {result}")
                samples[name].append(result)
        report = {}
        for name, rows in samples.items():
            report[name] = {}
            for stage in ("callback", "his_read", "calc", "write", "readback"):
                values = [row.get(stage, 0) for row in rows]
                report[name][stage] = {
                    "median_ms": round(statistics.median(values), 4),
                    "min_ms": min(values), "max_ms": max(values)}
        print(json.dumps({"pairs": args.pairs,
                          "synthetic_io": {"read_ms": 2, "write_ms": 3},
                          "report": report}, ensure_ascii=False, indent=2))
    finally:
        for worker in workers.values():
            if worker.stdin:
                worker.stdin.close()
            worker.wait(timeout=10)


if __name__ == "__main__":
    main()
