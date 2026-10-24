"""
Part C tests: puzzle parsing, validation, rating maths, subset selection.

No network and no database. The Supabase-dependent paths are exercised in
test_accounts.py against a fake client; the RLS policies themselves are
proven by migrations/002_rls_test.sql, which has to run inside Postgres
because that is where the policies live.

    .venv/bin/python tests/test_puzzles.py
"""

from __future__ import annotations

import os
import random
import sys
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import chess
import partC

_results: List[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))


# The documented example from database.lichess.org, which is the one case
# where the format's meaning is written down rather than inferred.
LICHESS_EXAMPLE = {
    "PuzzleId": "00sHx",
    "FEN": "q3k1nr/1pp1nQpp/3p4/1P2p3/4P3/B1PP1b2/B5PP/5K2 b k - 0 17",
    "Moves": "e8d7 a2e6 d7d8 f7f8",
    "Rating": "1760", "RatingDeviation": "80", "Popularity": "83",
    "NbPlays": "72", "Themes": "mate mateIn2 middlegame short",
    "GameUrl": "https://lichess.org/yyznGmXs/black#34", "OpeningTags": "",
}


def synthetic_rows(n: int, seed: int = 3) -> List[Dict[str, str]]:
    """Valid rows with a middle-heavy rating spread, like the real dump."""
    rng = random.Random(seed)
    themes = ["mateIn1", "mateIn2", "fork", "pin", "skewer", "endgame",
              "middlegame", "crushing", "advantage", "backRankMate"]
    rows: List[Dict[str, str]] = []
    i = 0
    while len(rows) < n and i < n * 6:
        i += 1
        board = chess.Board()
        for _ in range(rng.randint(4, 20)):
            moves = list(board.legal_moves)
            if not moves or board.is_game_over():
                break
            board.push(rng.choice(moves))
        if board.is_game_over() or board.legal_moves.count() < 2:
            continue
        fen = board.fen()
        seq: List[str] = []
        probe = board.copy()
        for _ in range(rng.choice([3, 3, 5])):
            moves = list(probe.legal_moves)
            if not moves or probe.is_game_over():
                break
            mv = rng.choice(moves)
            seq.append(mv.uci())
            probe.push(mv)
        if len(seq) < 2:
            continue
        rows.append({
            "PuzzleId": f"syn{len(rows):05d}", "FEN": fen, "Moves": " ".join(seq),
            "Rating": str(max(600, min(2900, int(rng.gauss(1550, 320))))),
            "RatingDeviation": "80",
            "Popularity": str(rng.choice([20, 50, 80, 90, 95])),
            "NbPlays": str(rng.choice([5, 30, 80, 500, 4000])),
            "Themes": " ".join(rng.sample(themes, rng.randint(1, 3))),
            "GameUrl": "", "OpeningTags": "",
        })
    return rows


def main() -> int:
    # ---------------- parsing ----------------
    print("C.3 parsing the Lichess format")
    p = partC.Puzzle.from_row(LICHESS_EXAMPLE)
    # The documented contract: FEN is before the opponent's move, Moves[0]
    # is that move, Moves[1] begins the solution.
    check("setup move is applied, not treated as the solution",
          p.fen != LICHESS_EXAMPLE["FEN"], p.fen)
    check("the player's position is after the setup move",
          p.fen.startswith("q5nr/1ppknQpp"), p.fen)
    check("solution is the odd-indexed moves", p.solution == ["a2e6", "f7f8"],
          str(p.solution))
    check("replies are the even-indexed moves", p.opponent_replies == ["d7d8"],
          str(p.opponent_replies))
    check("side to move is derived from the player's position",
          p.side_to_move == "white", p.side_to_move)
    check("moves_required counts only the player's moves", p.moves_required == 2)
    check("metadata parsed", p.rating == 1760 and "mateIn2" in p.themes)

    check("a row with fewer than two moves is rejected",
          _raises(partC.PuzzleError, partC.Puzzle.from_row,
                  {**LICHESS_EXAMPLE, "Moves": "e8d7"}))
    check("an illegal setup move is rejected",
          _raises(partC.PuzzleError, partC.Puzzle.from_row,
                  {**LICHESS_EXAMPLE, "Moves": "a1a2 a2e6"}))
    check("a missing column is rejected",
          _raises(partC.PuzzleError, partC.Puzzle.from_row, {"FEN": "x"}))
    check("bad numbers fall back to defaults, not a crash",
          partC.Puzzle.from_row({**LICHESS_EXAMPLE, "Rating": "n/a"}).rating == 1500)

    # ---------------- the solution is not leaked ----------------
    print("C.3 the solution stays server-side")
    check("to_dict withholds the solution", "solution" not in p.to_dict())
    check("to_dict withholds the replies too",
          "opponent_replies" not in p.to_dict())
    check("include_solution is explicit", "solution" in p.to_dict(include_solution=True))
    check("the client still gets what it needs to render",
          {"fen", "side_to_move", "rating", "themes", "moves_required"}
          <= set(p.to_dict()))

    # ---------------- validation ----------------
    print("C.3 validation")
    check("the correct line solves", partC.check_attempt(p, ["a2e6", "f7f8"]).solved)
    check("SAN is accepted as well as UCI",
          partC.check_attempt(p, ["Be6+", "Qf8#"]).solved)
    r = partC.check_attempt(p, ["a2e6"])
    check("an incomplete line does not solve", not r.solved and r.moves_correct == 1,
          str(r.moves_correct))
    check("incomplete still says what was expected", r.expected_instead == "Qf8#",
          str(r.expected_instead))
    r = partC.check_attempt(p, ["f7g8"])
    check("a legal but wrong move does not solve", not r.solved)
    check("a wrong move reports the expected move", r.expected_instead == "Be6+",
          str(r.expected_instead))
    check("a wrong move reports what was played", r.first_wrong_move is not None)
    r = partC.check_attempt(p, ["e2e4"])
    check("an illegal move does not solve", not r.solved)
    check("an illegal move still reports the expected move",
          r.expected_instead == "Be6+", str(r.expected_instead))
    r = partC.check_attempt(p, ["not-a-move"])
    check("an unreadable move is a failure, not an exception",
          not r.solved and "Could not read" in (r.reason or ""), str(r.reason))
    check("no moves at all does not solve", not partC.check_attempt(p, []).solved)
    check("extra moves beyond the line still solve",
          partC.check_attempt(p, ["a2e6", "f7f8", "a1a2"]).solved)

    # Each accepted move should carry the resulting position, for a review UI.
    r = partC.check_attempt(p, ["a2e6", "f7f8"])
    check("accepted moves carry the resulting FEN",
          all(c.fen_after for c in r.checks), str([c.fen_after for c in r.checks]))
    check("the scripted reply is reported",
          r.checks[0].opponent_reply == "d7d8", str(r.checks[0].opponent_reply))

    # An alternative mate must count.
    mate = partC.Puzzle.from_row({
        "PuzzleId": "m", "FEN": "6k1/5ppp/8/8/7p/8/8/RR4K1 b - - 0 1",
        "Moves": "h4h3 a1a8", "Rating": "900", "RatingDeviation": "80",
        "Popularity": "95", "NbPlays": "500", "Themes": "mateIn1",
        "GameUrl": "", "OpeningTags": "",
    })
    board = chess.Board(mate.fen)
    mates = []
    for mv in board.legal_moves:
        board.push(mv)
        if board.is_checkmate():
            mates.append(mv.uci())
        board.pop()
    check("the position really has two mates in one", len(mates) == 2, str(mates))
    other = next(m for m in mates if m not in mate.solution)
    alt = partC.check_attempt(mate, [other])
    check("a different mate in one is accepted", alt.solved, other)
    check("and is flagged as an alternative",
          alt.checks[0].accepted_alternative, str(alt.checks[0]))
    check("the expected mate is not flagged as an alternative",
          not partC.check_attempt(mate, mate.solution).checks[0].accepted_alternative)
    non_mate = next(m.uci() for m in chess.Board(mate.fen).legal_moves
                    if m.uci() not in mates)
    check("a non-mating move is still wrong",
          not partC.check_attempt(mate, [non_mate]).solved)

    # ---------------- rating ----------------
    print("C.3 Elo-style rating")
    check("an even match expects 0.5", abs(partC.expected_score(1500, 1500) - 0.5) < 1e-9)
    check("a harder puzzle is less expected",
          partC.expected_score(1200, 1800) < partC.expected_score(1200, 1400))
    check("k falls as history grows",
          partC.k_factor(0) > partC.k_factor(20) > partC.k_factor(100),
          f"{partC.k_factor(0)}/{partC.k_factor(20)}/{partC.k_factor(100)}")

    up = partC.update_rating(1200, 1200, True, attempts=0)
    down = partC.update_rating(1200, 1200, False, attempts=0)
    check("solving raises the rating", up.delta > 0, str(up.delta))
    check("failing lowers it", down.delta < 0, str(down.delta))
    check("an even match is symmetric", up.delta == -down.delta,
          f"{up.delta} vs {down.delta}")
    hard = partC.update_rating(1200, 1800, True, attempts=0)
    easy = partC.update_rating(1200, 900, True, attempts=0)
    check("solving a harder puzzle gains more", hard.delta > easy.delta,
          f"{hard.delta} vs {easy.delta}")
    cheap = partC.update_rating(1200, 1800, False, attempts=0)
    costly = partC.update_rating(1200, 900, False, attempts=0)
    check("failing an easy puzzle costs more", costly.delta < cheap.delta,
          f"{costly.delta} vs {cheap.delta}")
    check("the rating has a floor",
          partC.update_rating(partC.MIN_RATING, 3000, False, attempts=100).after
          >= partC.MIN_RATING)
    check("the rating has a ceiling",
          partC.update_rating(partC.MAX_RATING, 400, True, attempts=100).after
          <= partC.MAX_RATING)
    check("the change explains itself",
          {"before", "after", "delta", "expected", "k"} <= set(up.to_dict()))

    # A run of correct answers must converge upward, not oscillate.
    rating, attempts = partC.DEFAULT_RATING, 0
    for _ in range(25):
        rating = partC.update_rating(rating, 1600, True, attempts=attempts).after
        attempts += 1
    check("25 solves against 1600 converge upward", 1500 < rating < 2100, str(rating))

    # ---------------- serving window ----------------
    print("C.3 rating window")
    w0 = partC.rating_window(1500, 0)
    w3 = partC.rating_window(1500, 3)
    check("the window widens on retry", (w3[1] - w3[0]) > (w0[1] - w0[0]),
          f"{w0} -> {w3}")
    check("the window aims slightly above the player", (w0[0] + w0[1]) // 2 > 1500,
          str(w0))
    check("the window stays inside the rating bounds",
          partC.rating_window(partC.MIN_RATING, 9)[0] >= partC.MIN_RATING
          and partC.rating_window(partC.MAX_RATING, 9)[1] <= partC.MAX_RATING)

    # ---------------- subset selection (§8.4) ----------------
    print("C.3 subset selection")
    rows = synthetic_rows(1200)
    subset = partC.select_subset(rows, size=300, seed=11)
    check("the subset respects the size cap", len(subset) <= 300, str(len(subset)))
    check("the subset is not empty", len(subset) > 0)
    rep = partC.subset_report(subset)

    # The point of stratifying: flatten a middle-heavy source.
    source_bands: Dict[str, int] = {}
    for row in rows:
        key = str(partC.band_for(int(row["Rating"]))[0])
        source_bands[key] = source_bands.get(key, 0) + 1
    busiest = max(source_bands.values())
    quietest = min(source_bands.values())
    subset_counts = [n for n in rep["by_band"].values() if n]
    check("the source is middle-heavy, as the real dump is",
          busiest / max(quietest, 1) > 3, f"{quietest}..{busiest}")
    check("the subset is flatter than the source",
          (max(subset_counts) / max(min(subset_counts), 1)) < (busiest / max(quietest, 1)),
          f"subset {min(subset_counts)}..{max(subset_counts)} vs "
          f"source {quietest}..{busiest}")
    check("the quality floor is applied",
          all(p.popularity >= partC.MIN_POPULARITY for p in subset))
    check("the play-count floor is applied",
          all(p.nb_plays >= partC.MIN_PLAYS for p in subset))
    check("ids are unique", len({p.id for p in subset}) == len(subset))
    check("the same seed gives the same subset",
          [p.id for p in partC.select_subset(rows, size=300, seed=11)]
          == [p.id for p in subset])
    check("a different seed gives a different subset",
          [p.id for p in partC.select_subset(rows, size=300, seed=12)]
          != [p.id for p in subset])
    check("the report covers bands and themes",
          rep["distinct_themes"] > 1 and len(rep["by_band"]) > 1, str(rep["by_band"]))

    # Every selected puzzle must be solvable from its stored position --
    # the check that would have caught an off-by-one in the setup move.
    unsolvable = [p.id for p in subset if not partC.check_attempt(p, p.solution).solved]
    check("every selected puzzle's solution validates from its stored FEN",
          not unsolvable, f"{len(unsolvable)} failed")

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


def _raises(exc: type, fn: Any, *args: Any) -> bool:
    try:
        fn(*args)
    except exc:
        return True
    except Exception:
        return False
    return False


if __name__ == "__main__":
    sys.exit(main())
