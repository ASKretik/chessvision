#!/usr/bin/env python3
"""
B.1 -- dump every motif the fact layer emits, and measure the filters.

Written for the question "how do you know the motif fixes actually worked",
which cannot be answered by a unit test: the detectors were never *wrong*
about geometry, they were wrong about relevance, and relevance needs a
sample.

Two things it produces:

  1. A CSV of every motif with the FEN it came from, for hand-checking.
     Sample 40 rows, mark each real/relevant, and you have a precision
     number per detector -- a measured result rather than "I improved the
     prompt".

  2. A before/after count, by re-running the same games with the new gates
     disabled. That isolates what the gates removed from what the engine
     happened to produce.

Needs no engine and no network: it replays cached Lichess games through
Part B's fact layer only.

    .venv/bin/python tools/audit_motifs.py --games 60 --csv motifs.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import Counter
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import chess                                   # noqa: E402
import partB                                   # noqa: E402

CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "tests", ".cache", "lichess_analysed_games.json")


def replay(games: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    """Every pin/skewer the fact layer emits, with where it came from."""
    rows: List[Dict[str, Any]] = []
    for g in games[:limit]:
        if g.get("variant") != "standard":
            continue
        board = chess.Board()
        for san in (g.get("moves") or "").split():
            try:
                move = board.parse_san(san)
            except ValueError:
                break
            before = board.copy(stack=False)
            mover = before.turn
            after = before.copy(stack=False)
            after.push(move)

            was = {partB._geometry_key(m)
                   for m in partB.find_pins_and_skewers(before, mover)}
            for m in partB.find_pins_and_skewers(after, mover):
                if partB._geometry_key(m) in was:
                    continue
                rows.append({
                    "game": g.get("id", ""),
                    "san": san,
                    "motif": m["motif"],
                    "benefits": m.get("benefits", ""),
                    "roles": " | ".join(
                        f"{k}={v}" for k, v in m.items()
                        if k not in ("motif", "benefits", "absolute")),
                    "absolute": m.get("absolute", ""),
                    "fen_after": after.fen(),
                })
            board.push(move)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--games", type=int, default=60)
    ap.add_argument("--csv", default="")
    args = ap.parse_args()

    if not os.path.isfile(CACHE):
        print(f"No cache at {CACHE}. Run the classification test once to build it.")
        return 1
    games = json.load(open(CACHE))

    gated = replay(games, args.games)

    # The same games with the relevance gates removed, which is what the
    # detector used to emit. The diff is the measurement.
    material, credible = partB._motif_is_material, partB._skewer_threat_is_credible
    partB._motif_is_material = lambda *a, **k: True
    partB._skewer_threat_is_credible = lambda *a, **k: True
    try:
        ungated = replay(games, args.games)
    finally:
        partB._motif_is_material, partB._skewer_threat_is_credible = material, credible

    def tally(rows):
        c = Counter(r["motif"] for r in rows)
        c["absolute pins"] = sum(1 for r in rows if r["absolute"] is True)
        return c

    before, after = tally(ungated), tally(gated)
    print(f"{args.games} games replayed through the B.1 fact layer\n")
    print(f"{'motif':16s} {'ungated':>9s} {'gated':>9s} {'removed':>9s}")
    for key in ("pin", "skewer", "absolute pins"):
        b, a = before.get(key, 0), after.get(key, 0)
        pct = f"{100 * (b - a) / b:.0f}%" if b else "-"
        print(f"  {key:14s} {b:9d} {a:9d} {b - a:6d} {pct:>4s}")
    print(f"\n  {'total':14s} {len(ungated):9d} {len(gated):9d} "
          f"{len(ungated) - len(gated):6d}")

    if args.csv:
        with open(args.csv, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(gated[0].keys()) if gated
                               else ["game", "san", "motif"])
            w.writeheader()
            w.writerows(gated)
        print(f"\nwrote {len(gated)} rows to {args.csv}")
        print("Sample 40 and mark each real/relevant for a precision number.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
