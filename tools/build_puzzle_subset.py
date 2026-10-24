"""
C.3 -- build a puzzle subset from the Lichess dump and seed it into Supabase.

plan.md C.3: "Do not load the full set -- it will not fit in 500 MB alongside
everything else. Subset it (see §8)."

Offline, run once. The dump is CC0 from database.lichess.org, ~300 MB
compressed and several GB decompressed, so it is streamed and never committed.

    # ~300 MB download
    curl -O https://database.lichess.org/lichess_db_puzzle.csv.zst

    # stratified subset, with a coverage report
    .venv/bin/python tools/build_puzzle_subset.py \\
        --source lichess_db_puzzle.csv.zst --out data/puzzles/subset.csv

    # then load it (needs SUPABASE_URL + SUPABASE_SERVICE_KEY)
    .venv/bin/python tools/build_puzzle_subset.py \\
        --load data/puzzles/subset.csv

Selection is stratified by rating band then by theme, not random: a random
sample mirrors the dump's own distribution, which is heavily middle-weighted,
so a 2200-rated user runs out of puzzles while a 1200 one sees the same
themes forever. Reasoning and the quality floor are documented in
partC.select_subset.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import partC                              # noqa: E402
from partE import load_dotenv             # noqa: E402

CSV_FIELDS = ("id", "fen", "solution", "opponent_replies", "rating",
              "rating_deviation", "popularity", "nb_plays", "themes",
              "game_url", "opening_tags")


def build(source: str, out: str, size: int, min_pop: int,
          min_plays: int, seed: int) -> int:
    if not os.path.exists(source):
        print(f"source not found: {source}")
        print("get it with:  curl -O https://database.lichess.org/lichess_db_puzzle.csv.zst")
        return 2

    print(f"reading {source} ...")
    started = time.perf_counter()
    puzzles = partC.select_subset(
        partC.iter_puzzle_rows(source),
        size=size, min_popularity=min_pop, min_plays=min_plays, seed=seed,
    )
    elapsed = time.perf_counter() - started

    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for p in puzzles:
            writer.writerow({
                "id": p.id, "fen": p.fen,
                # Postgres array literals, so this file can also be used
                # with \\copy directly.
                "solution": "{" + ",".join(p.solution) + "}",
                "opponent_replies": "{" + ",".join(p.opponent_replies) + "}",
                "rating": p.rating, "rating_deviation": p.rating_deviation,
                "popularity": p.popularity, "nb_plays": p.nb_plays,
                "themes": "{" + ",".join(p.themes) + "}",
                "game_url": p.game_url or "",
                "opening_tags": "{" + ",".join(p.opening_tags) + "}",
            })

    report = partC.subset_report(puzzles)
    print(f"\nwrote {out} in {elapsed:.1f}s")
    print(f"  puzzles        {report['count']:,}")
    print(f"  on-disk        {os.path.getsize(out) / 1e6:.1f} MB")
    print(f"  est. in Postgres ~{report['approx_bytes'] / 1e6:.1f} MB of row data")
    print(f"  rating         {report['rating_min']} .. {report['rating_max']} "
          f"(median {report['rating_median']})")
    print(f"  distinct themes {report['distinct_themes']}")
    print("\n  per rating band:")
    for band, n in report["by_band"].items():
        bar = "#" * max(int(40 * n / max(report["by_band"].values())), 1)
        print(f"    {band:>10s} {n:6,d}  {bar}")
    print("\n  most common themes:")
    for theme, n in report["top_themes"].items():
        print(f"    {theme:24s} {n:6,d}")

    # A band with nothing in it means /puzzles/next will widen its window
    # forever for users in that range, so it is worth saying out loud.
    empty = [b for b, n in report["by_band"].items() if n == 0]
    if empty:
        print(f"\n  WARNING: empty rating bands {empty} -- users in that range "
              "will get off-rating puzzles")
    with open(os.path.splitext(out)[0] + "_report.json", "w") as fh:
        json.dump(report, fh, indent=1)
    return 0


def load(path: str, batch: int) -> int:
    """Upsert the subset into Supabase with the service key."""
    import httpx

    load_dotenv()
    url = os.getenv("SUPABASE_URL", "").rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        print("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set (see .env.example)")
        return 2
    if not os.path.exists(path):
        print(f"not found: {path} -- build it first")
        return 2

    def unarray(text: str) -> list[str]:
        text = (text or "").strip().strip("{}")
        return [x for x in text.split(",") if x]

    rows = []
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            rows.append({
                "id": r["id"], "fen": r["fen"],
                "solution": unarray(r["solution"]),
                "opponent_replies": unarray(r["opponent_replies"]),
                "rating": int(r["rating"]),
                "rating_deviation": int(r["rating_deviation"]),
                "popularity": int(r["popularity"]),
                "nb_plays": int(r["nb_plays"]),
                "themes": unarray(r["themes"]),
                "game_url": r["game_url"] or None,
                "opening_tags": unarray(r["opening_tags"]),
            })

    print(f"loading {len(rows):,} puzzles into {url} in batches of {batch} ...")
    headers = {
        "apikey": key, "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        # Idempotent: re-running replaces rather than erroring on the PK, so
        # a part-finished load can simply be run again.
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    sent = 0
    with httpx.Client(base_url=url, headers=headers, timeout=120.0) as client:
        for i in range(0, len(rows), batch):
            chunk = rows[i:i + batch]
            r = client.post("/rest/v1/puzzles", json=chunk)
            if r.status_code >= 400:
                print(f"\n  failed at row {i}: {r.status_code} {r.text[:300]}")
                print("  (is migrations/001_init.sql applied?)")
                return 1
            sent += len(chunk)
            print(f"\r  {sent:,}/{len(rows):,}", end="", flush=True)
    print(f"\ndone: {sent:,} puzzles loaded")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", help="lichess_db_puzzle.csv.zst (or .csv)")
    ap.add_argument("--out", default="data/puzzles/subset.csv")
    ap.add_argument("--size", type=int, default=partC.SUBSET_SIZE,
                    help=f"target puzzle count (default {partC.SUBSET_SIZE:,})")
    ap.add_argument("--min-popularity", type=int, default=partC.MIN_POPULARITY,
                    help="Lichess community score floor; low scores are usually "
                         "dual-solution or dubious puzzles")
    ap.add_argument("--min-plays", type=int, default=partC.MIN_PLAYS)
    ap.add_argument("--seed", type=int, default=20261007,
                    help="deterministic: the same seed rebuilds the same subset")
    ap.add_argument("--load", metavar="CSV",
                    help="upsert an already-built subset into Supabase")
    ap.add_argument("--batch", type=int, default=500)
    args = ap.parse_args()

    if args.load:
        sys.exit(load(args.load, args.batch))
    if not args.source:
        ap.error("pass --source to build, or --load to seed an existing subset")
    sys.exit(build(args.source, args.out, args.size,
                   args.min_popularity, args.min_plays, args.seed))
