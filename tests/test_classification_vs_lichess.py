"""
Part F: does A.3's classification reproduce Lichess's own labels?

plan.md F: "Classification matches Lichess on 3-5 reference games --
validates the ported formula."

Method. Lichess's JSON game export returns, for every ply, its own
evaluation AND its own judgment ("Inaccuracy"/"Mistake"/"Blunder"). This
test feeds partA's formula *Lichess's evals*, not Stockfish's, and compares
the resulting labels to Lichess's judgments.

That isolates the thing under test: running our own Stockfish at some depth
would let eval differences dominate and the test would prove nothing about
the formula.

Two populations are scored separately, because they are judged by different
rules (see partA.classify_mate_transition):

  * ordinary plies        -- Win% drop vs BLUNDER/MISTAKE/INACCURACY_DROP
  * mate-status transitions -- severity from the non-mate side's eval

Games are cached under tests/.cache so reruns do not hammer the Lichess API
(it resets the connection if you do). Use --refresh to refetch.

    .venv/bin/python tests/test_classification_vs_lichess.py
    .venv/bin/python tests/test_classification_vs_lichess.py --refresh --per-user 20
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import partA

UA = "chess-analysis-app/0.1 (UMC301 student project)"
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
CACHE_FILE = os.path.join(CACHE_DIR, "lichess_analysed_games.json")

# Several users so the sample is not one player's style, and large enough
# that mate transitions (~1.5% of plies) are not a handful of anecdotes.
USERS = ["penguingim1", "Zhigalko_Sergei", "nihalsarin2004",
         "Vladimirovich9000", "RebeccaHarris", "EricRosen"]

# plan.md A.3's stated thresholds are kept here as a control, to show the
# correction is load-bearing rather than cosmetic.
PLAN_THRESHOLDS = (30.0, 20.0, 10.0)
LIVE_THRESHOLDS = (partA.BLUNDER_DROP, partA.MISTAKE_DROP, partA.INACCURACY_DROP)

# Pass bars. Not 100%: Lichess declines to label a small number of moves that
# cross a threshold (its advice de-duplication), which we cannot observe.
ORDINARY_BAR = 0.99
MATE_BAR = 0.75


def fetch_games(per_user: int, refresh: bool) -> list[dict]:
    if os.path.exists(CACHE_FILE) and not refresh:
        with open(CACHE_FILE) as fh:
            games = json.load(fh)
        print(f"Using {len(games)} cached games ({CACHE_FILE}); --refresh to refetch")
        return games

    import httpx

    os.makedirs(CACHE_DIR, exist_ok=True)
    games: list[dict] = []
    for user in USERS:
        for attempt in range(3):
            try:
                r = httpx.get(
                    f"https://lichess.org/api/games/user/{user}",
                    params={"max": per_user, "analysed": "true", "evals": "true",
                            "accuracy": "true", "rated": "true"},
                    headers={"User-Agent": UA, "Accept": "application/x-ndjson"},
                    timeout=120.0, follow_redirects=True,
                )
                if r.status_code != 200:
                    print(f"  {user}: HTTP {r.status_code}")
                    break
                got = [json.loads(l) for l in r.text.strip().split("\n") if l.strip()]
                games += [g for g in got if g.get("analysis")]
                print(f"  {user}: {len(got)} games")
                break
            except Exception as exc:
                wait = 5 * (attempt + 1)
                print(f"  {user}: {type(exc).__name__}, retrying in {wait}s")
                time.sleep(wait)
        time.sleep(2)   # be polite; Lichess resets the connection otherwise

    with open(CACHE_FILE, "w") as fh:
        json.dump(games, fh)
    print(f"Cached {len(games)} analysed games to {CACHE_FILE}")
    return games


def win_pct(cp: int) -> float:
    """Uncapped Win% -- the capped partA.win_percent would flatten the tails
    this test needs to distinguish."""
    return 50 + 50 * (2 / (1 + math.exp(-0.00368208 * cp)) - 1)


def label_by_drop(drop: float, thresholds: tuple[float, float, float]) -> str | None:
    blunder, mistake, inaccuracy = thresholds
    if drop >= blunder:
        return "Blunder"
    if drop >= mistake:
        return "Mistake"
    if drop >= inaccuracy:
        return "Inaccuracy"
    return None


def run(per_user: int, refresh: bool) -> bool:
    games = fetch_games(per_user, refresh)
    if len(games) < 3:
        print(f"Only {len(games)} analysed games; need at least 3.")
        return False

    ordinary = {"plan.md 30/20/10": Counter(), "live partA thresholds": Counter()}
    ordinary_n = 0
    mate_conf: Counter = Counter()
    mate_hit = mate_n = 0
    acpl_rows = []

    for g in games:
        analysis = g["analysis"]
        moves = g.get("moves", "").split()
        n_plies = min(len(analysis), len(moves))

        for i in range(1, n_plies):
            before, after = analysis[i - 1], analysis[i]
            if not ({"eval", "mate"} & set(before)):
                continue
            if not ({"eval", "mate"} & set(after)):
                continue

            truth = after.get("judgment", {}).get("name")
            mate_before, mate_after = "mate" in before, "mate" in after
            mover_is_white = (i % 2 == 0)   # analysis[i] describes ply i+1

            if mate_before != mate_after:
                # Mate-status transition: judged on the non-mate eval.
                nonmate = before.get("eval") if not mate_before else after.get("eval")
                if nonmate is None:
                    continue
                mate_n += 1
                mine = partA.classify_mate_transition(nonmate).value.capitalize()
                if mine == truth:
                    mate_hit += 1
                else:
                    mate_conf[f"lichess={truth or 'none'} / ours={mine}"] += 1
                continue

            if mate_before and mate_after:
                continue        # mate throughout; no cp to compare

            wb, wa = win_pct(before["eval"]), win_pct(after["eval"])
            if not mover_is_white:
                wb, wa = 100 - wb, 100 - wa
            drop = max(0.0, wb - wa)
            ordinary_n += 1
            for name, thresholds in (("plan.md 30/20/10", PLAN_THRESHOLDS),
                                     ("live partA thresholds", LIVE_THRESHOLDS)):
                mine = label_by_drop(drop, thresholds)
                if mine == truth:
                    ordinary[name]["match"] += 1
                else:
                    ordinary[name][f"lichess={truth or 'none'} / ours={mine or 'none'}"] += 1

        # ACPL cross-check. Mate scores map to +-CP_CAP; skipping them would
        # drop the decisive end of a game out of the average.
        for colour in ("white", "black"):
            summary = g["players"][colour].get("analysis")
            if not summary or summary.get("acpl") is None:
                continue
            is_white = colour == "white"
            losses = []
            for i in range(1, n_plies):
                if (i % 2 == 0) != is_white:
                    continue
                before, after = analysis[i - 1], analysis[i]
                try:
                    b = _capped_cp(before)
                    a = _capped_cp(after)
                except KeyError:
                    continue
                losses.append(max(0, (b - a) if is_white else (a - b)))
            if losses:
                acpl_rows.append((g["id"], colour, summary["acpl"],
                                  round(sum(losses) / len(losses), 1),
                                  summary.get("accuracy")))

    # ---------------------------------------------------------------
    print(f"\nsample: {len(games)} Lichess-analysed games\n")
    print("=== ordinary plies: Win% drop thresholds ===")
    best_rate = 0.0
    for name in ("plan.md 30/20/10", "live partA thresholds"):
        c = ordinary[name]
        rate = c["match"] / ordinary_n if ordinary_n else 0.0
        label = f"{name} = {LIVE_THRESHOLDS}" if name.startswith("live") else name
        print(f"\n  {label}: {c['match']}/{ordinary_n} = {rate:.2%}")
        for key, count in c.most_common(6):
            if key != "match":
                print(f"      {count:5d}  {key}")
        if name.startswith("live"):
            best_rate = rate

    mate_rate = mate_hit / mate_n if mate_n else 0.0
    print("\n=== mate-status transitions: severity from the non-mate eval ===")
    print(f"  {mate_hit}/{mate_n} = {mate_rate:.1%}")
    for key, count in mate_conf.most_common(6):
        print(f"      {count:5d}  {key}")
    unlabelled = sum(c for k, c in mate_conf.items() if "lichess=none" in k)
    if mate_n:
        print(f"  of {mate_n - mate_hit} misses, {unlabelled} are moves Lichess "
              f"declined to label at all")
        strict = mate_n - unlabelled
        if strict:
            print(f"  agreement where Lichess did give a label: "
                  f"{mate_hit}/{strict} = {mate_hit/strict:.1%}")

    print("\n=== ACPL cross-check (same capped formula) ===")
    deltas = [abs(t - o) for _, _, t, o, _ in acpl_rows]
    if deltas:
        within = sum(1 for d in deltas if d <= 5)
        print(f"  {len(deltas)} player-games | mean |delta| {sum(deltas)/len(deltas):.1f} cp "
              f"| within 5 cp: {within}/{len(deltas)}")
        for gid, colour, theirs, ours, _ in acpl_rows[:6]:
            print(f"    {gid:10s} {colour:6s} lichess {theirs:5} ours {ours:7}")

    print("\n=== Accuracy% (informational, not a pass criterion) ===")
    print("  partA.game_accuracy is a plain mean of per-move accuracies. Lichess")
    print("  blends a volatility-weighted mean with a harmonic mean and does not")
    print("  publish that weighting, so a gap is expected -- decisions.md 2.7.")

    ok_ordinary = best_rate >= ORDINARY_BAR
    ok_mate = mate_rate >= MATE_BAR or mate_n == 0
    print()
    print(f"  [{'PASS' if ok_ordinary else 'FAIL'}] ordinary plies {best_rate:.2%} "
          f"(bar {ORDINARY_BAR:.0%})")
    print(f"  [{'PASS' if ok_mate else 'FAIL'}] mate transitions {mate_rate:.1%} "
          f"(bar {MATE_BAR:.0%})")
    return ok_ordinary and ok_mate


def _capped_cp(entry: dict) -> int:
    cap = partA.CP_CAP
    if "mate" in entry:
        return cap if entry["mate"] > 0 else -cap
    return max(-cap, min(cap, entry["eval"]))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--per-user", type=int, default=20)
    ap.add_argument("--refresh", action="store_true", help="refetch instead of using cache")
    args = ap.parse_args()
    sys.exit(0 if run(args.per_user, args.refresh) else 1)
