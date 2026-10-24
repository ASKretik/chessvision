"""
Regression suite for Part A + Part E.1.

Covers every sub-part with no network calls: A.1 adapters (PGN/FEN), the A.3
formulas against published Lichess values, A.2's single-pass engine
behaviour, A.4 switching off cleanly without clocks, A.5 in both the
weights-present and weights-absent branches, A.6 opening/phase, and the
E.1/E.2 HTTP surface.

Live-service checks live in test_end_to_end_sources.py, the Lichess label
comparison in test_classification_vs_lichess.py, and Part B's real API calls
in test_commentary_live.py.

Every /analyse here passes `commentary: false`. Without it, a populated .env
makes this suite call the LLM provider for real -- which spent quota, made the
run depend on the network, and timed out the job-completion check once Part B
was wired in.

    .venv/bin/python tests/test_analysis_core.py
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("STOCKFISH_PATH", "stockfish")

import chess
import partA

MODEL = "models/maia2.onnx"

PGN = ('[Event "t"]\n[White "alice"]\n[Black "bob"]\n[Result "1-0"]\n[WhiteElo "1520"]\n'
       '[BlackElo "1495"]\n[TimeControl "300+3"]\n\n'
       '1. e4 { [%clk 0:05:00] } e5 { [%clk 0:04:58] } 2. Nf3 { [%clk 0:05:01] } Nc6 { [%clk 0:04:55] } '
       '3. Bc4 { [%clk 0:05:00] } Nf6 { [%clk 0:04:40] } 4. Ng5 { [%clk 0:04:35] } d5 { [%clk 0:04:20] } '
       '5. exd5 { [%clk 0:04:30] } Nxd5 { [%clk 0:04:00] } 6. Nxf7 { [%clk 0:04:10] } Kxf7 { [%clk 0:03:55] } '
       '7. Qf3+ { [%clk 0:04:05] } Ke6 { [%clk 0:03:10] } 8. Nc3 { [%clk 0:03:50] } Nd4 { [%clk 0:02:40] } 1-0\n')

_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))


def main() -> int:
    print("A.1 adapters")
    gi = partA.load_pgn(PGN)
    check("pgn parsed, clocks detected", gi.has_clock and gi.source_type == "pgn")
    check("fen adapter has a distinct shape",
          partA.load_fen(chess.STARTING_FEN).is_position_only)

    print("A.3 formulas (published Lichess values)")
    check("win%(0) == 50", abs(partA.win_percent(0) - 50) < 1e-9)
    check("win% symmetric about 0",
          abs(partA.win_percent(250) + partA.win_percent(-250) - 100) < 1e-9)
    check("win% capped at +-1000cp", partA.win_percent(1000) == partA.win_percent(9000))
    check("accuracy(no drop) ~ 100", partA.accuracy_percent(50, 50) > 99.9)
    check("accuracy monotonic in drop",
          partA.accuracy_percent(50, 40) > partA.accuracy_percent(50, 20))
    # Thresholds are 15/10/5, corrected from plan.md's 30/20/10; see
    # test_classification_vs_lichess.py for the evidence.
    check("thresholds are the Lichess-matching set",
          (partA.BLUNDER_DROP, partA.MISTAKE_DROP, partA.INACCURACY_DROP) == (15.0, 10.0, 5.0),
          f"{partA.BLUNDER_DROP}/{partA.MISTAKE_DROP}/{partA.INACCURACY_DROP}")
    check("blunder at 15 drop", partA.classify(15, played_best=False) == partA.MoveLabel.BLUNDER)
    check("mistake at 10 drop", partA.classify(10, played_best=False) == partA.MoveLabel.MISTAKE)
    check("inaccuracy at 5 drop", partA.classify(5, played_best=False) == partA.MoveLabel.INACCURACY)
    check("engine's own move is never a mistake",
          partA.classify(99, played_best=True) == partA.MoveLabel.BEST)
    check("mate transition severity from non-mate eval",
          partA.classify_mate_transition(-631) == partA.MoveLabel.BLUNDER
          and partA.classify_mate_transition(-941) == partA.MoveLabel.MISTAKE
          and partA.classify_mate_transition(-1500) == partA.MoveLabel.INACCURACY)
    check("elo_to_bucket is a range lookup, not nearest-value",
          [partA.elo_to_bucket(e, {"start": 1100, "end": 2000, "interval": 100}, 11)
           for e in (1099, 1100, 1199, 1200, 1999, 2000)] == [0, 1, 1, 2, 9, 10])
    check("band midpoints round as expected",
          partA.band_midpoint("1500-1599") == 1550 and partA.band_midpoint(">=2000") is None)
    # A.5a: the reported elo comes from the curve's shape, on a 100-point
    # grid, and an open band is reported as a BOUND rather than a point
    # estimate -- extrapolating past it measured R^2 = 0.0007.
    _labels = ["<1100"] + [f"{x}-{x+99}" for x in range(1100, 2000, 100)] + [">=2000"]
    _b = {"start": 1100, "end": 2000, "interval": 100}
    _interior = {l: -(abs(i - 4) ** 2) * 0.25 - 12 for i, l in enumerate(_labels)}
    _rising = {l: -19.3 + i * 0.35 for i, l in enumerate(_labels)}
    _falling = {l: -12 - i * 0.3 for i, l in enumerate(_labels)}
    check("an interior peak interpolates to a 100-point estimate",
          partA.refine_rating(_labels, _interior, _b)[:2] == (1400, "interpolated"),
          str(partA.refine_rating(_labels, _interior, _b)))
    check("the top band reports a floor, not a projection",
          partA.refine_rating(_labels, _rising, _b)[:2] == (2000, "floor"),
          str(partA.refine_rating(_labels, _rising, _b)))
    check("the bottom band reports a ceiling",
          partA.refine_rating(_labels, _falling, _b)[:2] == (1100, "ceiling"))
    check("every reported elo is on the 100 grid",
          all(partA.refine_rating(_labels, c, _b)[0] % 100 == 0
              for c in (_interior, _rising, _falling)))

    has_model = os.path.exists(MODEL)
    rep = partA.analyse(gi, stockfish_path="stockfish", depth=10, multipv=3,
                        maia2_path=MODEL if has_model else None)
    d = rep.to_dict()

    print("A.6 phase + opening")
    check("ECO identified", d["opening"]["eco"] == "C57", d["opening"]["name"])
    check("book exit found", d["opening"]["left_book_move"] == "Nd4",
          f"move {d['opening']['left_book_move_number']}")
    check("real TSV source used", d["opening"]["source"] == "lichess-chess-openings")
    check("phase move counts sum to total",
          sum(p["moves"] for p in d["white"]["phases"]) == d["white"]["moves"])

    print("A.2 engine service")
    check("every position evaluated exactly once",
          d["meta"]["engine_cache"]["misses"] == d["meta"]["plies"] + 1)
    check("multipv recorded", d["meta"]["multipv"] == 3)

    print("A.4 time analysis")
    check("time analysis enabled", d["white"]["time"]["enabled"]
          and d["time_control"]["time_analysis_enabled"])
    check("per-move seconds derived for every move",
          all(m["time_spent"] is not None for m in d["moves"]))
    check("complexity in range on every move",
          all(0 <= m["complexity"] <= 1 for m in d["moves"]))
    noclk = partA.analyse(partA.load_pgn('[Event "x"]\n\n1. f3 e5 2. g4 Qh4# 0-1\n'),
                          stockfish_path="stockfish", depth=10).to_dict()
    check("switches off cleanly with no clocks",
          noclk["white"]["time"] == {"enabled": False})
    check("mate transition recorded as a fact",
          any(m["mate_status_change"] == partA.MATE_NOW_FORCED for m in noclk["moves"]),
          [m["mate_status_change"] for m in noclk["moves"]])

    print("A.5 rating estimate")
    if has_model:
        r = d["white"]["rating_estimate"]
        check("available with weights present", r["available"] is True, str(r.get("reason")))
        check("reports a band label", bool(r["band"]), str(r["band"]))
        check("searches all 11 buckets", len(r["per_band"]) == 11, str(len(r["per_band"])))
        check("no vocabulary misses", r["moves_skipped"] == 0, str(r["moves_skipped"]))
        check("elo is always set, with a method saying how",
              r["elo"] is not None and r["elo_method"] in
              ("interpolated", "floor", "ceiling"),
              f"{r['elo']} / {r['elo_method']}")
        check("an open band is reported as a bound, not a point estimate",
              (partA.band_midpoint(r["band"]) is not None)
              or r["elo_method"] in ("floor", "ceiling"),
              f"{r['band']} / {r['elo_method']}")
        check("model type reported", r["model_type"] in ("rapid", "blitz"), str(r["model_type"]))
    else:
        print("  (weights absent -- availability branch skipped)")
    # The absent branch must degrade with a reason rather than raising.
    gone = partA.analyse(gi, stockfish_path="stockfish", depth=8,
                         maia2_path="models/does_not_exist.onnx").to_dict()
    gr = gone["white"]["rating_estimate"]
    check("degrades with a reason when weights are missing",
          gr["available"] is False and "not found" in (gr["reason"] or ""), str(gr["reason"]))

    print("E.1/E.2 backend")
    from fastapi.testclient import TestClient
    import partE

    with TestClient(partE.app) as c:
        check("/health green", c.get("/health").json()["status"] == "ok")
        check("stockfish probe ok", c.get("/ready").json()["checks"]["stockfish"]["ok"])
        check("no secrets in /config",
              "service" not in json.dumps(c.get("/config").json()).lower())
        r = c.post("/analyse", json={"pgn": PGN, "depth": 8, "commentary": False})
        check("202 + job id", r.status_code == 202 and "job_id" in r.json())
        jid = r.json()["job_id"]
        job = {}
        for _ in range(600):
            job = c.get(f"/jobs/{jid}").json()
            if job["state"] in ("done", "failed"):
                break
            time.sleep(0.1)
        check("job completes", job.get("state") == "done", str(job.get("error")))
        check("result is the Part A report", job["result"]["kind"] == "game")
        check("422 on no source", c.post("/analyse", json={}).status_code == 422)

    print("A.5a the estimate reports how much it actually pins down")
    bands = ["<1100", "1100-1199", "1200-1299", "1300-1399", "1400-1499",
             "1500-1599", "1600-1699", "1700-1799", "1800-1899",
             "1900-1999", ">=2000"]
    bucketing = {"start": 1100, "end": 2000, "interval": 100}

    def interval(lls):
        return partA.likelihood_interval(bands, dict(zip(bands, lls)), bucketing)

    # A real curve from this codebase: eight moves, and the whole span from
    # 1400 to ">=2000" sits inside 0.11 log-likelihood. The argmax was being
    # reported as "2000+" on evidence that rules out almost nothing.
    flat = interval([-10.877, -9.860, -9.311, -8.593, -8.168, -8.816,
                     -8.631, -8.627, -8.427, -8.272, -8.056])
    check("a nearly flat curve is reported as low confidence",
          flat["confidence"] == "low", str(flat["confidence"]))
    check("and its range spans most of the scale",
          flat["buckets"] >= 8, str(flat["buckets"]))

    sharp = interval([-40, -32, -25, -19, -14, -10, -14, -19, -25, -32, -40])
    check("a sharp peak is high confidence",
          sharp["confidence"] == "high", str(sharp["confidence"]))
    check("and resolves to one bucket", sharp["buckets"] == 1,
          str(sharp["buckets"]))
    check("with both edges closed, so it has a width",
          sharp["width_elo"] == 99, str(sharp["width_elo"]))

    mild = interval([-20, -18, -16, -14, -13, -12.5, -12.2, -12.0, -12.3,
                     -13, -15])
    check("a mild peak lands between the two",
          mild["confidence"] in ("low", "medium"), str(mild["confidence"]))
    check("the range is contiguous and contains the peak",
          bands.index(mild["low_band"]) <= bands.index("1700-1799")
          <= bands.index(mild["high_band"]),
          f'{mild["low_band"]}..{mild["high_band"]}')

    # An open-ended bucket has no outer edge, and inventing one would be the
    # same mistake as the extrapolation that measured R^2 = 0.0007.
    check("an open top end reports no ceiling", flat["high_elo"] is None,
          str(flat["high_elo"]))
    check("and therefore no width", flat["width_elo"] is None,
          str(flat["width_elo"]))

    # Guard the cutoff itself: it is the standard LR value for one parameter.
    check("the cutoff is the 95% likelihood-ratio value",
          abs(partA.LR_CUTOFF - 1.92) < 1e-9, str(partA.LR_CUTOFF))

    width = max(len(n) for n, _, _ in _results)
    print()
    for name, ok, detail in _results:
        line = f"  [{'PASS' if ok else 'FAIL'}] {name:<{width}}"
        if detail and not ok:
            line += f"   <- {detail}"
        elif detail and ok:
            line += f"   ({detail})"
        print(line)
    passed = sum(1 for _, ok, _ in _results if ok)
    print(f"\n{passed}/{len(_results)} checks passed")
    return 0 if passed == len(_results) else 1


if __name__ == "__main__":
    sys.exit(main())
