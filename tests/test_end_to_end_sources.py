"""
Part F: end-to-end analyse() on each real input source.

plan.md F: "End-to-end on each of the 4 input sources -- Chess.com path is
the fragile one."

Hits live Lichess and Chess.com. Run with the real Maia-2 weights present to
exercise A.5 too; without them A.5 reports unavailable and the rest still
passes.

    .venv/bin/python tests/test_end_to_end_sources.py
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import partA

UA = {"User-Agent": "chess-analysis-app/0.1 (UMC301 student project)"}
DEPTH = 10
MODEL = "models/maia2.onnx"

PASTED_PGN = """[Event "Club game"]
[White "alice"]
[Black "bob"]
[Result "0-1"]
[WhiteElo "1430"]
[BlackElo "1680"]
[TimeControl "600+5"]

1. d4 { [%clk 0:10:00] } Nf6 { [%clk 0:10:02] } 2. c4 { [%clk 0:09:58] } e6 { [%clk 0:09:55] }
3. Nc3 { [%clk 0:09:50] } Bb4 { [%clk 0:09:40] } 4. Qc2 { [%clk 0:09:30] } 0-0 { [%clk 0:09:20] }
5. a3 { [%clk 0:09:00] } Bxc3+ { [%clk 0:08:50] } 6. Qxc3 { [%clk 0:08:55] } b6 { [%clk 0:08:30] }
7. Bg5 { [%clk 0:08:20] } Bb7 { [%clk 0:08:00] } 8. f3 { [%clk 0:07:30] } h6 { [%clk 0:07:40] }
9. Bh4 { [%clk 0:07:20] } d5 { [%clk 0:07:00] } 10. e3 { [%clk 0:06:30] } Nbd7 { [%clk 0:06:40] }
11. cxd5 { [%clk 0:06:00] } exd5 { [%clk 0:06:20] } 12. Bd3 { [%clk 0:05:40] } c5 { [%clk 0:05:50] }
13. dxc5 { [%clk 0:05:10] } bxc5 { [%clk 0:05:30] } 14. Qd2 { [%clk 0:04:40] } Re8 { [%clk 0:05:00] } 0-1
"""


def latest_lichess_id() -> str:
    """A real finished game id, taken from a public user's recent games."""
    import httpx
    import re

    r = httpx.get("https://lichess.org/api/games/user/penguingim1",
                  params={"max": 1, "rated": "true"},
                  headers={**UA, "Accept": "application/x-chess-pgn"}, timeout=60.0)
    r.raise_for_status()
    m = re.search(r"\[Site \"https://lichess\.org/(\w{8})", r.text)
    if not m:
        raise RuntimeError("could not find a Lichess game id")
    return m.group(1)


def latest_chesscom(user: str = "hikaru") -> tuple[str, str, str]:
    """(url, username, 'YYYY/MM') for a real finished Chess.com game."""
    import httpx

    arch = httpx.get(f"https://api.chess.com/pub/player/{user}/games/archives",
                     headers={**UA, "Accept": "application/json"}, timeout=40.0).json()["archives"]
    month_url = arch[-1]
    games = httpx.get(month_url, headers={**UA, "Accept": "application/json"},
                      timeout=90.0).json()["games"]
    finished = [g for g in games if g.get("pgn")]
    g = finished[-1]
    return g["url"], user, "/".join(month_url.split("/")[-2:])


def summarise(name: str, report) -> bool:
    d = report.to_dict()
    ok = True

    if d["kind"] != "game":
        print(f"  FAIL unexpected kind {d['kind']}")
        return False

    print(f"  plies           {d['meta']['plies']}")
    print(f"  elapsed         {d['meta']['elapsed_seconds']}s")
    print(f"  opening         {d['opening']['eco']} {d['opening']['name']}")
    print(f"  left book       move {d['opening']['left_book_move_number']} "
          f"({d['opening']['left_book_move']})")
    print(f"  time analysis   enabled={d['time_control']['time_analysis_enabled']} "
          f"tc={d['time_control']['raw']}")

    for side in ("white", "black"):
        p = d[side]
        r = p["rating_estimate"]
        est = (f"{r['band']} (elo {r['elo']})" if r["available"]
               else f"unavailable: {str(r['reason'])[:48]}")
        print(f"  {side:5s} {str(p['name'])[:16]:16s} acc {str(p['accuracy']):>5s} "
              f"acpl {str(p['acpl']):>5s} B/M/I {p['blunders']}/{p['mistakes']}/{p['inaccuracies']}")
        print(f"        weakest={p['weakest_phase']} rating={est}")

        # invariants
        if p["moves"] == 0:
            print(f"  FAIL {side} has no moves"); ok = False
        if p["accuracy"] is not None and not (0 <= p["accuracy"] <= 100):
            print(f"  FAIL {side} accuracy out of range"); ok = False
        if sum(ph["moves"] for ph in p["phases"]) != p["moves"]:
            print(f"  FAIL {side} phase move counts do not sum"); ok = False
        if r["available"] and r["moves_skipped"] != 0:
            print(f"  WARN {side} skipped {r['moves_skipped']} moves "
                  f"(vocabulary miss -> suspect export)"); ok = False

    for m in d["moves"]:
        if not (0 <= m["complexity"] <= 1):
            print(f"  FAIL complexity out of range at ply {m['ply']}"); ok = False
        if not (0 <= m["accuracy"] <= 100):
            print(f"  FAIL accuracy out of range at ply {m['ply']}"); ok = False

    try:
        json.dumps(d)
    except (TypeError, ValueError) as exc:
        print(f"  FAIL not JSON-serialisable: {exc}"); ok = False

    return ok


def main() -> int:
    has_model = os.path.exists(MODEL)
    print(f"Maia-2 weights: {'present' if has_model else 'ABSENT (A.5 will report unavailable)'}\n")
    results = []

    cases = []
    try:
        gid = latest_lichess_id()
        cases.append((f"1. Lichess URL (live)  {gid}",
                      lambda g=gid: partA.load_lichess(f"https://lichess.org/{g}")))
    except Exception as exc:
        print(f"could not reach Lichess: {exc}")
        results.append(("lichess", False))

    try:
        url, user, month = latest_chesscom()
        cases.append((f"2. Chess.com URL (live) {url.rsplit('/',1)[-1]}",
                      lambda u=url, n=user, m=month: partA.load_chesscom(u, n, month=m)))
    except Exception as exc:
        print(f"could not reach Chess.com: {exc}")
        results.append(("chesscom", False))

    cases.append(("3. Pasted PGN", lambda: partA.load_pgn(PASTED_PGN)))

    for name, loader in cases:
        print(f"=== {name} ===")
        t0 = time.perf_counter()
        try:
            gi = loader()
            print(f"  source_type     {gi.source_type}")
            print(f"  has_clock       {gi.has_clock}")
            report = partA.analyse(gi, stockfish_path="stockfish", depth=DEPTH, multipv=3,
                                   maia2_path=MODEL if has_model else None)
            ok = summarise(name, report)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            print(f"  FAIL {type(exc).__name__}: {exc}")
            ok = False
        print(f"  -> {'PASS' if ok else 'FAIL'} ({time.perf_counter()-t0:.1f}s)\n")
        results.append((name, ok))

    print("=" * 60)
    for name, ok in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    passed = sum(1 for _, ok in results if ok)
    print(f"\n{passed}/{len(results)} sources passed end-to-end")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
