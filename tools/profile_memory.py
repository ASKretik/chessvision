"""
E.3 / Part F: profile memory under concurrent load.

plan.md E.3: "Profile under load before declaring this done (F)" and
"Memory profile under concurrent load -- E.3 is the most likely production
failure."

Drives N concurrent full-game analyses through the real app (in-process, so
no server needed) while sampling /ops/memory, then reports the high-water
mark against the budget and a verdict on plan.md §8.7 -- Render free at
512 MB, or Starter at ~$7/month.

    .venv/bin/python tools/profile_memory.py
    .venv/bin/python tools/profile_memory.py --jobs 4 --depth 14 --budget 512

Read the verdict, not just the peak: locally the figure is USS, while on
Render the binding number is the cgroup's, which also counts page cache.
To profile the real thing, deploy and hit /ops/memory after driving load.
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# A real game with clocks, long enough that a pass is not trivial.
PGN = """[Event "profile"]
[White "alice"]
[Black "bob"]
[Result "1-0"]
[WhiteElo "1520"]
[BlackElo "1495"]
[TimeControl "300+3"]

1. e4 { [%clk 0:05:00] } c5 { [%clk 0:04:58] } 2. Nf3 { [%clk 0:04:55] } d6 { [%clk 0:04:50] }
3. d4 { [%clk 0:04:45] } cxd4 { [%clk 0:04:40] } 4. Nxd4 { [%clk 0:04:35] } Nf6 { [%clk 0:04:30] }
5. Nc3 { [%clk 0:04:25] } a6 { [%clk 0:04:20] } 6. Be3 { [%clk 0:04:10] } e6 { [%clk 0:04:05] }
7. f3 { [%clk 0:04:00] } b5 { [%clk 0:03:50] } 8. Qd2 { [%clk 0:03:40] } Nbd7 { [%clk 0:03:30] }
9. g4 { [%clk 0:03:20] } h6 { [%clk 0:03:10] } 10. O-O-O { [%clk 0:03:00] } Ne5 { [%clk 0:02:50] }
11. h4 { [%clk 0:02:40] } Qc7 { [%clk 0:02:30] } 12. Kb1 { [%clk 0:02:20] } Bd7 { [%clk 0:02:10] }
13. Bg2 { [%clk 0:02:00] } Rc8 { [%clk 0:01:50] } 14. g5 { [%clk 0:01:40] } hxg5 { [%clk 0:01:30] }
15. hxg5 { [%clk 0:01:20] } Nh5 { [%clk 0:01:10] } 16. f4 { [%clk 0:01:00] } Nc4 { [%clk 0:00:50] }
17. Qe2 { [%clk 0:00:45] } Nxe3 { [%clk 0:00:40] } 18. Qxe3 { [%clk 0:00:35] } Be7 { [%clk 0:00:30] }
19. f5 { [%clk 0:00:28] } exf5 { [%clk 0:00:25] } 20. Nd5 { [%clk 0:00:22] } Qc5 { [%clk 0:00:18] } 1-0
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--jobs", type=int, default=None,
                    help="concurrent analyses (default: MAX_CONCURRENT_JOBS + 1, "
                         "so the semaphore is actually exercised)")
    ap.add_argument("--depth", type=int, default=12)
    ap.add_argument("--budget", type=int, default=None, help="override MEMORY_BUDGET_MB")
    ap.add_argument("--interval", type=float, default=0.25, help="sampling interval (s)")
    args = ap.parse_args()

    os.environ.setdefault("STOCKFISH_PATH", "stockfish")
    if args.budget:
        os.environ["MEMORY_BUDGET_MB"] = str(args.budget)
    # Rate limiting would reject the burst before memory ever became the
    # constraint, which is not what we are measuring.
    os.environ["RATE_LIMIT_PER_HOUR"] = "10000"

    from fastapi.testclient import TestClient
    import partE

    n_jobs = args.jobs if args.jobs else partE.settings.max_concurrent_jobs + 1

    samples: list[tuple[float, float]] = []
    stop = threading.Event()

    with TestClient(partE.app) as client:
        baseline = client.get("/ops/memory").json()
        print("configuration")
        for k, v in baseline["knobs"].items():
            print(f"  {k:22s} {v}")
        print(f"\nbaseline        {baseline['used_mb']:.1f} MB of "
              f"{baseline['limit_mb']:.0f} MB   source={baseline['source']}")
        print(f"driving {n_jobs} concurrent analyses at depth {args.depth} "
              f"(semaphore allows {partE.settings.max_concurrent_jobs})\n")

        def sampler() -> None:
            t0 = time.perf_counter()
            while not stop.is_set():
                try:
                    m = client.get("/ops/memory").json()
                    samples.append((time.perf_counter() - t0, m["used_mb"]))
                except Exception:
                    pass
                stop.wait(args.interval)

        sampler_thread = threading.Thread(target=sampler, daemon=True)
        sampler_thread.start()

        started = time.perf_counter()
        job_ids, rejected = [], 0
        for _ in range(n_jobs):
            r = client.post("/analyse", json={"pgn": PGN, "depth": args.depth})
            if r.status_code == 202:
                job_ids.append(r.json()["job_id"])
            else:
                rejected += 1
                print(f"  submit rejected: {r.status_code} "
                      f"{r.json().get('detail','')[:90]}")

        done, failed = 0, 0
        deadline = time.perf_counter() + 900
        pending = set(job_ids)
        while pending and time.perf_counter() < deadline:
            for jid in list(pending):
                j = client.get(f"/jobs/{jid}").json()
                if j["state"] == "done":
                    done += 1
                    pending.discard(jid)
                elif j["state"] == "failed":
                    failed += 1
                    print(f"  job failed: {str(j.get('error'))[:100]}")
                    pending.discard(jid)
            time.sleep(0.2)
        elapsed = time.perf_counter() - started

        stop.set()
        sampler_thread.join(timeout=5)
        final = client.get("/ops/memory").json()

    used = [u for _, u in samples] or [baseline["used_mb"]]
    authoritative = final.get("authoritative", False)
    print("\nresults")
    print(f"  jobs            {done} done, {failed} failed, {rejected} rejected at submit")
    print(f"  wall clock      {elapsed:.1f}s")
    print(f"  samples         {len(samples)} at {args.interval}s")
    print(f"  baseline        {baseline['used_mb']:.1f} MB")
    print(f"  mean            {statistics.mean(used):.1f} MB")
    print(f"  median          {statistics.median(used):.1f} MB")
    print(f"  peak sampled    {max(used):.1f} MB")
    if authoritative:
        print(f"  peak recorded   {final['peak_mb']:.1f} MB   "
              f"(includes spikes between samples)")
    else:
        print(f"  peak observed   {final['peak_observed_mb']:.1f} MB   "
              f"(source={final['peak_observed_source']}, NOT authoritative)")
    print(f"  growth          {final['used_mb'] - baseline['used_mb']:+.1f} MB "
          f"after all jobs finished")
    print(f"  limit           {final['limit_mb']:.0f} MB")
    if authoritative:
        headroom = final["limit_mb"] - final["peak_mb"]
        print(f"  headroom        {headroom:+.1f} MB at peak "
              f"({100 * final['peak_mb'] / final['limit_mb']:.0f}% used)")
    print(f"  memory source   {final['source']}"
          f"{'' if authoritative else '  (partial -- see below)'}")
    print(f"\nverdict: {final['verdict']}")

    # Growth after every job has finished is the leak signal, and it IS
    # meaningful even from a partial source: the same measurement before and
    # after, so a per-job allocation that is never released still shows up.
    leaked = final["used_mb"] - baseline["used_mb"]
    print(f"\nsteady state: {leaked:+.1f} MB retained after all jobs finished")
    if leaked >= 50:
        print("  WARNING: suspect a per-job allocation that is not released.")
    else:
        print("  flat -- the shared ONNX session and opening book are being reused.")

    if not authoritative:
        print("\nCANNOT CONCLUDE on plan.md §8.7 from this run.")
        print("  Child-process private memory is unreadable on this platform, so the")
        print("  container total is unknown. Stockfish reports a large RSS that is")
        print("  mostly its shared NNUE mapping; summing it overstates usage badly.")
        print("  Deploy to Render and read /ops/memory -- the cgroup counts each page")
        print("  once and is what the OOM killer compares against the limit.")

    concurrency_ok = failed == 0 and rejected == 0
    budget_ok = (final["peak_mb"] < final["limit_mb"]) if authoritative else None
    ok = concurrency_ok and leaked < 50 and (budget_ok is not False)
    print(f"\n  [{'PASS' if concurrency_ok else 'FAIL'}] {n_jobs} concurrent jobs completed")
    print(f"  [{'PASS' if leaked < 50 else 'FAIL'}] no per-job retention")
    print(f"  [{'PASS' if budget_ok else 'SKIP' if budget_ok is None else 'FAIL'}] "
          f"within budget at peak"
          f"{'  (needs an authoritative source)' if budget_ok is None else ''}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
