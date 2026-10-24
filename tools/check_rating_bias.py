"""
A.5a -- is the rating estimate biased by colour?

Written because a user reported White scoring better on every Stockfish
metric yet getting the lower Maia-2 estimate. A colour swap in
_estimate_both would look exactly like that, so it was worth measuring
rather than reasoning about.

Result on the cached games (120 rated Lichess games, 238 player-estimates):

    white  n=119  mean error  -993   mean|err| 1008
    black  n=119  mean error -1027   mean|err| 1031
    white-minus-black bias: +34 Elo   -> no colour bias
    better Lichess accuracy also got the higher estimate in 63/79 (80%)

READ THE -993 CAREFULLY. It is not a model defect. 237 of the 240 players
in this cache are rated >= 2000, median 3023 -- they are elite bullet games,
because the cache was built for the classification test, which wanted games
Lichess had already analysed. Maia-2's top bucket IS ">=2000", so a
3023-rated player can score at most 2000 and the error is mechanically
-1023. The number measures the test set, not the model.

The real consequence is a gap, recorded in decisions.md 3a2.5: this cache
CANNOT validate the estimate in the 1100-1999 band where it is actually
used, because only three players in it are in that range. Doing so needs a
sample of sub-2000 rated games, which this tool would then measure
unchanged.

    .venv/bin/python tools/check_rating_bias.py

No engine and no network: the estimate reads only the moves and Maia-2.
Takes about 20 minutes over the full cache.
"""
import json, os, statistics, sys
sys.path.insert(0, "/Users/darkmoonknight/Documents/College/CS/UMC301/Project 2")
os.chdir("/Users/darkmoonknight/Documents/College/CS/UMC301/Project 2")
import chess
import partA

games = json.load(open("tests/.cache/lichess_analysed_games.json"))
session = partA.Maia2Session("models/maia2.onnx")
session.load()

rows = []
for g in games:
    if g.get("variant") != "standard":
        continue
    p = g.get("players", {})
    we = p.get("white", {}).get("rating")
    be = p.get("black", {}).get("rating")
    san = (g.get("moves") or "").split()
    if not we or not be or len(san) < 16:
        continue
    board = chess.Board()
    boards, nodes = [board.copy(stack=False)], []
    ok = True
    for s in san:
        try:
            mv = board.parse_san(s)
        except Exception:
            ok = False
            break
        nodes.append(type("N", (), {"move": mv})())
        board.push(mv)
        boards.append(board.copy(stack=False))
    if not ok or len(nodes) < 16:
        continue
    try:
        rw, rb = partA._estimate_both(
            session, boards, nodes, we, be, partA.OpponentEloMode.HEADER)
    except Exception:
        # A game the model cannot score tells us nothing either way.
        continue
    acc_w = p.get("white", {}).get("analysis", {}).get("accuracy")
    acc_b = p.get("black", {}).get("analysis", {}).get("accuracy")
    if rw.available and rw.elo:
        rows.append(("white", we, rw.elo, rw.elo_method, acc_w))
    if rb.available and rb.elo:
        rows.append(("black", be, rb.elo, rb.elo_method, acc_b))

print(f"{len(rows)} player-estimates from {len(rows)//2} games")
above = sum(1 for _, act, _, _, _ in rows if act >= 2000)
print(f"{above}/{len(rows)} of them are rated >= 2000, which is Maia-2's TOP "
      f"bucket.\nAny error below is bounded by that ceiling, not by the "
      f"model's judgement.\n")

for colour in ("white", "black"):
    sub = [r for r in rows if r[0] == colour]
    if not sub:
        continue
    errs = [est - act for _, act, est, _, _ in sub]
    print(f"{colour:6s} n={len(sub):3d}  "
          f"mean error {statistics.mean(errs):+6.0f}  "
          f"median {statistics.median(errs):+6.0f}  "
          f"mean|err| {statistics.mean(abs(e) for e in errs):5.0f}")

ws = [r for r in rows if r[0] == "white"]
bs = [r for r in rows if r[0] == "black"]
if ws and bs:
    gap = (statistics.mean(e - a for _, a, e, _, _ in ws)
           - statistics.mean(e - a for _, a, e, _, _ in bs))
    print(f"\nwhite-minus-black bias: {gap:+.0f} Elo "
          f"({'colour bias — a bug' if abs(gap) > 80 else 'no meaningful colour bias'})")

# Does a better accuracy reliably mean a higher estimate, within one game?
agree = disagree = 0
for i in range(0, len(rows) - 1, 2):
    w, b = rows[i], rows[i + 1]
    if w[0] != "white" or b[0] != "black":
        continue
    if w[4] is None or b[4] is None:
        continue
    if (w[4] > b[4]) == (w[2] > b[2]):
        agree += 1
    elif w[4] != b[4] and w[2] != b[2]:
        disagree += 1
total = agree + disagree
if total:
    print("\nWithin a game, the player with the better Lichess accuracy also got")
    print(f"the higher Maia-2 estimate in {agree}/{total} games "
          f"({100*agree/total:.0f}%).")
    print("The two disagree often by design: accuracy asks 'how close to the")
    print("engine', the estimate asks 'which rating plays moves like these'.")
