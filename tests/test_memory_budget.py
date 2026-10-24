"""
E.3: unit tests for the memory budget's production path.

The cgroup readers are the ones that matter on Render, and they cannot be
exercised on macOS (no cgroup) or without a container. So they are pointed
at synthetic files holding known values, which validates the parsing and the
decision logic that will run in production.

Also pins the behaviour that caused a real false alarm: a non-authoritative
reading must never set the peak used for a tier verdict, nor gate traffic.

    .venv/bin/python tests/test_memory_budget.py
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("STOCKFISH_PATH", "stockfish")

import partE

MB = 1024 * 1024
_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))


class FakeCgroup:
    """Writes cgroup-shaped files and points MemoryBudget at them."""

    def __init__(self, budget_mb: int = 512, headroom_mb: int = 64) -> None:
        self.dir = tempfile.mkdtemp()
        self.budget = budget_mb
        self.headroom = headroom_mb

    def v2(self, current_mb: float, max_mb: float | str, peak_mb: float | None = None,
           inactive_file_mb: float | None = None):
        b = partE.MemoryBudget(self.budget, self.headroom)
        if inactive_file_mb is not None:
            self._write("memory.stat", f"anon 1\ninactive_file {int(inactive_file_mb * MB)}\n")
            b.CG2_STAT = os.path.join(self.dir, "memory.stat")
        else:
            b.CG2_STAT = os.path.join(self.dir, "nonexistent")
        self._write("memory.current", int(current_mb * MB))
        self._write("memory.max", max_mb if isinstance(max_mb, str) else int(max_mb * MB))
        b.CG2_CURRENT = os.path.join(self.dir, "memory.current")
        b.CG2_MAX = os.path.join(self.dir, "memory.max")
        if peak_mb is not None:
            self._write("memory.peak", int(peak_mb * MB))
            b.CG2_PEAK = os.path.join(self.dir, "memory.peak")
        else:
            b.CG2_PEAK = os.path.join(self.dir, "nonexistent")
        # Make sure v1 and the fallbacks cannot be reached.
        b.CG1_USAGE = os.path.join(self.dir, "nonexistent")
        return b

    def _write(self, name: str, value) -> None:
        with open(os.path.join(self.dir, name), "w") as fh:
            fh.write(str(value))


def main() -> int:
    fc = FakeCgroup()

    # ---- cgroup v2 parsing -----------------------------------------
    b = fc.v2(current_mb=300, max_mb=512)
    r = b.read()
    check("cgroup-v2 read", r.source == "cgroup-v2" and r.used_mb == 300.0,
          f"{r.source} {r.used_mb}")
    check("cgroup-v2 is authoritative", r.authoritative)
    check("limit from cgroup", r.limit_mb == 512.0, str(r.limit_mb))
    check("free_mb computed", r.free_mb == 212.0, str(r.free_mb))
    check("fraction computed", r.fraction == round(300 / 512, 4), str(r.fraction))

    # ---- reclaimable page cache is not counted as used --------------
    # A warm container's memory.current sits at its limit because Linux fills
    # free memory with file cache; counting that refused every new job.
    b = fc.v2(current_mb=500, max_mb=512, inactive_file_mb=200)
    r = b.read()
    check("inactive file cache subtracted", r.used_mb == 300.0, str(r.used_mb))
    check("reclaimable cache reported", r.detail.get("reclaimable_cache_mb") == 200.0,
          str(r.detail.get("reclaimable_cache_mb")))
    check("cache-heavy container still admits", b.admit()[0], str(b.admit()))

    # ---- an unlimited cgroup falls back to the configured budget ----
    b = fc.v2(current_mb=100, max_mb="max")
    r = b.read()
    check("'max' limit falls back to the budget", r.limit_mb == 512.0, str(r.limit_mb))

    # ---- the lower of cgroup and budget binds ----------------------
    b = FakeCgroup(budget_mb=512).v2(current_mb=100, max_mb=1024)
    r = b.read()
    check("budget binds when below the cgroup limit", r.limit_mb == 512.0, str(r.limit_mb))
    b = FakeCgroup(budget_mb=2048).v2(current_mb=100, max_mb=512)
    r = b.read()
    check("cgroup binds when below the budget", r.limit_mb == 512.0, str(r.limit_mb))

    # ---- the kernel's own high-water mark is picked up --------------
    b = fc.v2(current_mb=200, max_mb=512, peak_mb=480)
    r = b.read()
    check("kernel peak captured", r.detail.get("kernel_peak_mb") == 480.0,
          str(r.detail.get("kernel_peak_mb")))
    check("kernel peak sets peak_mb", b.peak_mb == 480.0, str(b.peak_mb))

    # ---- admission control -----------------------------------------
    b = fc.v2(current_mb=300, max_mb=512)      # 212 free, needs 64
    ok, why = b.admit()
    check("admits with headroom", ok and why is None, str(why))

    b = fc.v2(current_mb=470, max_mb=512)      # 42 free, needs 64
    ok, why = b.admit()
    check("refuses below headroom", not ok and why is not None, str(why))
    check("refusal counted", b.rejections == 1, str(b.rejections))
    check("refusal explains itself", "headroom" in (why or ""), str(why))

    b = fc.v2(current_mb=511.9, max_mb=512)
    check("refuses when essentially full", not b.admit()[0])

    # ---- verdicts --------------------------------------------------
    cases = [
        (200, "comfortable"),
        (400, "ok"),
        (470, "tight"),
        (520, "over budget"),
    ]
    for used, expect in cases:
        b = fc.v2(current_mb=used, max_mb=512)
        verdict = b.report()["verdict"]
        check(f"verdict at {used}/512 MB starts {expect!r}",
              verdict.startswith(expect), verdict[:60])

    # ---- a partial reading must not drive decisions -----------------
    # This is the regression that matters: summing child RSS once produced a
    # 946 MB "peak" against a 512 MB budget and recommended paying for
    # Render Starter. A non-authoritative reading must be inert.
    b = partE.MemoryBudget(512, 64)
    b.CG2_CURRENT = b.CG1_USAGE = "/nonexistent"
    r = b.read()
    check("falls back to a non-authoritative source off-cgroup",
          not r.authoritative, r.source)
    check("partial reading does not set peak_mb", b.peak_mb == 0.0, str(b.peak_mb))
    check("partial reading does set peak_observed_mb", b.peak_observed_mb > 0.0,
          str(b.peak_observed_mb))
    check("partial reading never refuses traffic", b.admit()[0])
    check("partial reading yields an 'unknown' verdict",
          b.report()["verdict"].startswith("unknown"), b.report()["verdict"][:50])

    # A huge partial reading still must not produce a budget verdict.
    b._record_peak(partE.MemoryReading(9999.0, 512.0, "partial"))
    check("a huge partial reading cannot fake a peak", b.peak_mb == 0.0, str(b.peak_mb))
    check("but it is visible for diagnosis", b.peak_observed_mb == 9999.0,
          str(b.peak_observed_mb))

    # ---- _read_int edge cases --------------------------------------
    check("_read_int on a missing file", partE._read_int("/nonexistent") is None)
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
        fh.write("max")
        max_path = fh.name
    check("_read_int on 'max'", partE._read_int(max_path) is None)
    os.unlink(max_path)

    # ---- the report carries the knobs a profile run needs -----------
    b = fc.v2(current_mb=200, max_mb=512)
    rep = b.report()
    for key in ("stockfish_hash_mb", "max_concurrent_jobs", "maia2_preload",
                "budget_mb", "maia2_loaded"):
        check(f"report records knob {key!r}", key in rep["knobs"])

    width = max(len(n) for n, _, _ in _results)
    print()
    for name, ok, detail in _results:
        line = f"  [{'PASS' if ok else 'FAIL'}] {name:<{width}}"
        if detail and not ok:
            line += f"   <- {detail}"
        print(line)
    passed = sum(1 for _, ok, _ in _results if ok)
    print(f"\n{passed}/{len(_results)} checks passed")
    return 0 if passed == len(_results) else 1


if __name__ == "__main__":
    sys.exit(main())
