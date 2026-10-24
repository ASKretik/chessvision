"""
E.4 -- generate the API contract the frontend builds against.

plan.md E.4: "Do not begin frontend implementation until [the UI spec]
lands. Build backend endpoints to a documented contract in the meantime so
the two can proceed independently."

FastAPI's OpenAPI covers the request side, but every large response is
typed `Dict[str, Any]`, so the schema says `{}` where the actual payload is
a 27-move analysis report. Nobody can code a frontend against that.

So this runs the real pipeline and captures real responses, writing:

    docs/openapi.json          the generated spec
    docs/api-contract.md       endpoints, the SSE event order, field meanings
    docs/examples/*.json       real captured payloads

Generated rather than hand-written on purpose: hand-written examples drift
from the code silently, and a frontend discovering that costs more than
regenerating this file ever will.

    .venv/bin/python tools/dump_api_contract.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("STOCKFISH_PATH", "stockfish")
# Keep generation offline and cheap: the frontend contract does not depend on
# commentary prose, and a real LLM call would spend quota on every rebuild.
os.environ["LLM_API_KEY"] = ""
os.environ["LLM_PROVIDER"] = ""

DOCS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs")
EXAMPLES = os.path.join(DOCS, "examples")

PGN = """[Event "Rated blitz"]
[White "alice"]
[Black "bob"]
[Result "1-0"]
[WhiteElo "1520"]
[BlackElo "1495"]
[TimeControl "300+3"]

1. e4 { [%clk 0:05:00] } e5 { [%clk 0:04:58] } 2. Nf3 { [%clk 0:05:01] } Nc6 { [%clk 0:04:55] }
3. Bc4 { [%clk 0:05:00] } Nf6 { [%clk 0:04:40] } 4. Ng5 { [%clk 0:04:35] } d5 { [%clk 0:04:20] }
5. exd5 { [%clk 0:04:30] } Nxd5 { [%clk 0:04:00] } 6. Nxf7 { [%clk 0:04:10] } Kxf7 { [%clk 0:03:55] }
7. Qf3+ { [%clk 0:04:05] } Ke6 { [%clk 0:03:10] } 8. Nc3 { [%clk 0:03:50] } Nd4 { [%clk 0:02:40] } 1-0
"""


def shape(value: Any, depth: int = 0) -> Any:
    """
    A payload reduced to its shape: types, not values.

    Arrays collapse to one element, because a frontend needs the element
    shape, not forty copies of it.
    """
    if depth > 6:
        return "..."
    if isinstance(value, dict):
        return {k: shape(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [shape(value[0], depth + 1)] if value else []
    if value is None:
        return "null"
    return type(value).__name__


def capture() -> Dict[str, Any]:
    from fastapi.testclient import TestClient
    import partE

    captured: Dict[str, Any] = {}
    sse: List[Dict[str, Any]] = []

    with TestClient(partE.app) as client:
        captured["openapi"] = client.get("/openapi.json").json()
        for name, path in (("health", "/health"), ("ready", "/ready"),
                           ("config", "/config"), ("ops_memory", "/ops/memory")):
            captured[name] = client.get(path).json()

        accepted = client.post("/analyse", json={"pgn": PGN, "depth": 10,
                                                 "multipv": 3, "commentary": True})
        captured["analyse_accepted"] = accepted.json()
        job_id = accepted.json()["job_id"]

        # The SSE order is the part OpenAPI cannot express, and it is exactly
        # what a progressive UI needs: which events arrive, in what order,
        # and which one ends the stream.
        with client.stream("GET", f"/jobs/{job_id}/events") as stream:
            event = None
            for line in stream.iter_lines():
                if line.startswith("event: "):
                    event = line[7:]
                elif line.startswith("data: "):
                    data = json.loads(line[6:])
                    sse.append({"event": event,
                                "stage": data.get("stage") or data.get("state"),
                                "keys": sorted(data)[:8]})
                    if event in ("result", "error"):
                        break
        captured["sse_sequence"] = sse

        for _ in range(900):
            job = client.get(f"/jobs/{job_id}").json()
            if job["state"] in ("done", "failed"):
                break
            time.sleep(0.1)
        captured["job_done"] = job

        fen = client.post("/analyse", json={
            "fen": "r1bqkb1r/pppp1ppp/2n2n2/1B2p3/4P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 6 5",
            "depth": 10, "commentary": False})
        fen_id = fen.json()["job_id"]
        for _ in range(900):
            fj = client.get(f"/jobs/{fen_id}").json()
            if fj["state"] in ("done", "failed"):
                break
            time.sleep(0.1)
        captured["job_done_fen"] = fj

        captured["errors"] = {
            "no_source_422": client.post("/analyse", json={}).json(),
            "unknown_job_404": client.get("/jobs/nope").json(),
            "unauthenticated_401": client.get("/me").json(),
        }

    # Part C responses need a database, so they come from the fake used in
    # tests rather than being hand-written -- same shapes, no Supabase.
    sys.path.insert(0, os.path.join(os.path.dirname(DOCS), "tests"))
    try:
        from test_accounts import FakeSupabase
        import partE as pe

        pe.supabase = FakeSupabase()
        with TestClient(pe.app) as client:
            hdr = {"Authorization": "Bearer token-a"}
            captured["me"] = client.get("/me", headers=hdr).json()
            captured["my_analyses"] = client.get("/me/analyses", headers=hdr).json()
            captured["puzzle_next"] = client.get("/puzzles/next", headers=hdr).json()
            # E.4: the board the client draws and the step that feeds it.
            captured["board_moves"] = client.get(
                "/board/moves",
                params={"fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR"
                               " w KQkq - 0 1"}).json()
            pid = captured["puzzle_next"]["puzzle"]["id"]
            captured["puzzle_step"] = client.post(
                f"/puzzles/{pid}/step", json={"moves": ["a2e6"]},
                headers=hdr).json()
            captured["puzzle_attempt_wrong"] = client.post(
                "/puzzles/p1/attempt", json={"moves": ["f7g8"], "ms_taken": 3000},
                headers=hdr).json()
            captured["puzzle_attempt_right"] = client.post(
                "/puzzles/p1/attempt", json={"moves": ["a2e6", "f7f8"], "ms_taken": 8000},
                headers=hdr).json()
    except Exception as exc:                                  # pragma: no cover
        captured["part_c_error"] = f"{type(exc).__name__}: {exc}"
    return captured


# --------------------------------------------------------------------------
# The contract document
# --------------------------------------------------------------------------
HEADER = """# API contract

**Generated by `tools/dump_api_contract.py` — do not edit by hand.**
Regenerate after changing any endpoint or response shape.

This exists because of plan.md E.4: the UI spec has not landed, so the
frontend and backend proceed independently against a documented contract.
FastAPI's OpenAPI (`docs/openapi.json`) covers requests and status codes, but
every large response is typed `Dict[str, Any]` — the schema says `{}` where
the real payload is a full analysis report. The shapes below fill that gap,
and the examples in `docs/examples/` are captured from real runs.

Base URL is whatever Render serves; `GET /config` tells the client what is
switched on, so nothing needs to be hardcoded.

## Authentication

There are no sign-up or sign-in endpoints — that is deliberate (plan.md C.1).
The client talks to **Supabase Auth directly** with the client-safe anon key
from `GET /config`, then sends the resulting JWT here:

    Authorization: Bearer <supabase access token>

`/health`, `/ready`, `/config` and the analysis endpoints accept anonymous
callers. Everything under `/me` and `/puzzles` requires a token and returns
**401** without one. A resource owned by another user returns **404**, not
403, so a response never confirms that someone else's id exists.
"""

TABS = """
## What the frontend needs to build

plan.md E.4, independent of the UI spec:

- **Four tabs**: Analyse / Puzzles / Play / Positions. Play and Positions are
  Part D, which plan.md §7 designates as the first thing to cut — if it stays
  cut, only **two** tabs have a backend to talk to. Worth confirming before
  building four.
- **One shared board component**, used by every tab. plan.md calls this "the
  single biggest frontend saving available".
- **Progressive rendering**: analysis results stream in. See the SSE section.
"""

SSE_INTRO = """
## The job lifecycle (and the part OpenAPI cannot express)

An analysis takes minutes, so `POST /analyse` never blocks. It returns a job
id immediately, and the client either polls `GET /jobs/{id}` or subscribes to
`GET /jobs/{id}/events` for Server-Sent Events.

SSE is the path a progressive UI should use. Events observed in a real run,
in order:
"""

SSE_OUTRO = """
Reading the stream:

| event | meaning |
|---|---|
| `state` | the job changed state (`queued` → `running` → `done`/`failed`) |
| `progress` | a stage advanced. `data.stage` names it, `data.progress` is 0–1, and `data.partial` may carry results already available |
| `analysis` | **Part A's report, with NO `commentary` key.** See the warning below |
| `comments` | a batch of Part B comments, as each one lands (B.4 streams them) |
| `result` | the final payload, identical to `GET /jobs/{id}`. **Ends the stream.** |
| `error` | the job failed; `data.error` is safe to display. **Ends the stream.** |

> **The `analysis` and `result` frames are different shapes.** `analysis`
> carries Part A's report *before Part B has run*, so it has no `commentary`
> key at all — not an empty one. Render the board, the move timeline and the
> accuracy figures from it immediately; that is the point of it, and it
> arrives seconds ahead of `result`. But every read of `commentary` must
> guard, or the page crashes on the frame that was supposed to make it fast.
> This cost a blank analysis page once: the frontend typed `commentary` as
> required, and polling-based tests never saw the difference because polling
> only ever returns the finished job.

Both frames carry the report at the **top level**. There is no `{report: …}`
wrapper, and the failure frame is `{error: …}`, not `{detail: …}`.

Two behaviours worth relying on:

- **Past events are replayed on subscribe**, so attaching late — or
  reconnecting after Render's ~60 s cold start — still shows the full
  timeline.
- A `: keep-alive` comment arrives every 15 s. Ignore it; it exists because
  Render closes idle streams.

Stages prefixed `commentary:` come from Part B, so a UI can show analysis
progress and commentary progress separately.
"""


def write_docs(cap: Dict[str, Any]) -> None:
    os.makedirs(EXAMPLES, exist_ok=True)

    with open(os.path.join(DOCS, "openapi.json"), "w") as fh:
        json.dump(cap["openapi"], fh, indent=1)

    examples = {
        "health": "health", "ready": "ready", "config": "config",
        "ops-memory": "ops_memory", "analyse-accepted": "analyse_accepted",
        "job-done-game": "job_done", "job-done-fen": "job_done_fen",
        "me": "me", "my-analyses": "my_analyses", "puzzle-next": "puzzle_next",
        "puzzle-attempt-wrong": "puzzle_attempt_wrong",
        "puzzle-attempt-right": "puzzle_attempt_right",
        "board-moves": "board_moves", "puzzle-step": "puzzle_step",
        "errors": "errors",
    }
    written = []
    for name, key in examples.items():
        if key in cap:
            path = os.path.join(EXAMPLES, f"{name}.json")
            with open(path, "w") as fh:
                json.dump(cap[key], fh, indent=1)
            written.append((name, os.path.getsize(path)))

    lines = [HEADER, TABS]

    lines.append("\n## Endpoints\n")
    lines.append("| method | path | auth | purpose |")
    lines.append("|---|---|---|---|")
    rows = [
        ("GET", "/health", "none", "liveness. Cheap, no dependencies — Render polls it"),
        ("GET", "/ready", "none", "readiness: Supabase, Stockfish, memory, each core"),
        ("GET", "/config", "none", "**call this first.** Feature flags + the Supabase anon key"),
        ("GET", "/ops/memory", "none", "E.3 memory report and tier verdict"),
        ("POST", "/analyse", "optional", "start an analysis → job id"),
        ("GET", "/jobs/{id}", "optional", "poll a job"),
        ("GET", "/jobs/{id}/events", "optional", "SSE stream (preferred)"),
        ("GET", "/me", "**required**", "the signed-in user and their stats"),
        ("GET", "/me/analyses", "**required**", "past analyses, newest first (summaries)"),
        ("GET", "/me/analyses/{id}", "**required**", "one full analysis"),
        ("GET", "/board/moves", "none", "every legal move from a FEN — the client holds no chess rules"),
        ("POST", "/api/auth/signup", "none", "create an account -> session token"),
        ("POST", "/api/auth/login", "none", "sign in -> session token"),
        ("POST", "/api/auth/logout", "**required**", "kill the session server-side"),
        ("GET", "/api/play/bots", "none", "the opponents on offer"),
        ("POST", "/api/play/new", "optional", "start a game against a bot"),
        ("GET", "/api/play/{id}", "none", "game state"),
        ("POST", "/api/play/{id}/move", "none", "your move + the bot's reply, one round trip"),
        ("POST", "/api/play/{id}/resign", "none", "end it"),
        ("GET", "/puzzles/next", "**required**", "a puzzle near the user's rating"),
        ("POST", "/puzzles/{id}/step", "**required**", "validate the line so far, return the opponent's reply (no rating change)"),
        ("POST", "/puzzles/{id}/attempt", "**required**", "submit moves; returns the verdict and rating change"),
    ]
    for verb, path, auth, why in rows:
        lines.append(f"| `{verb}` | `{path}` | {auth} | {why} |")

    lines.append(SSE_INTRO)
    lines.append("```")
    for e in cap.get("sse_sequence", []):
        stage = f"  {e['stage']}" if e.get("stage") else ""
        lines.append(f"event: {e['event']:<10}{stage}")
    lines.append("```")
    lines.append(SSE_OUTRO)

    lines.append("\n## Response shapes\n")
    lines.append("Types, not values. Arrays are collapsed to one element — the "
                 "element shape is what matters. Real payloads are in "
                 "`docs/examples/`.\n")

    shapes = [
        ("GET /config", "config",
         "Drives the whole UI. `features.analysis` off means the analysis "
         "core is missing; `features.commentary` off means there is no LLM "
         "key, so B.1 facts arrive without prose; `features.puzzles` off "
         "means no database. `supabase_anon_key` is client-safe by design."),
        ("POST /analyse", "analyse_accepted",
         "202 Accepted. Exactly one source: `lichess_url`, `chesscom_url` "
         "(+ `chesscom_username`, optionally `chesscom_month`), `pgn` or "
         "`fen`. Anything else is 422."),
        ("GET /jobs/{id} — a game", "job_done",
         "`result.kind == \"game\"`. The big one: per-move timeline plus "
         "per-player summaries. See the field notes below."),
        ("GET /jobs/{id} — a FEN", "job_done_fen",
         "`result.kind == \"position\"` — a **different shape**, deliberately "
         "(plan.md §8.2). Branch on `kind`. `unavailable` lists what does not "
         "apply to a single position, so the UI hides those panels rather "
         "than rendering empty ones."),
        ("GET /me", "me", "User plus puzzle rating and counters."),
        ("GET /me/analyses", "my_analyses",
         "Summaries only — `result_json` is excluded because twenty full "
         "reports would be megabytes. Fetch one by id for the detail."),
        ("GET /puzzles/next", "puzzle_next",
         "**The solution is not here, and never will be.** Validation happens "
         "server-side; a client that could see the answer makes the feature "
         "pointless."),
        ("GET /board/moves", "board_moves",
         "Everything a board needs to accept move input, so the client ships "
         "no chess rules: every legal move with its SAN, its from/to squares "
         "and the FEN it reaches. Playing a move is \"find it in `legal`, "
         "swap in `fen_after`\". A promotion appears as four entries sharing "
         "from+to, which is how a client detects one without knowing the "
         "promotion rules. No auth -- it reads no data."),
        ("POST /puzzles/{id}/step", "puzzle_step",
         "For an interactive puzzle board: validate the line so far and, when "
         "it is right, return the opponent's scripted reply and the position "
         "after it -- those live inside the hidden solution, so only the "
         "server can supply them. **It does not change the rating** and it "
         "does not say what the right move was; send the finished line to "
         "`/attempt` for both."),
        ("POST /puzzles/{id}/attempt", "puzzle_attempt_right",
         "`result.solved` is the verdict. `rating` is the Elo change. "
         "`solution` is populated **only when solved** — a failed attempt "
         "returns null, so submitting a deliberate wrong move cannot reveal "
         "the answer."),
    ]
    for title, key, note in shapes:
        if key not in cap:
            continue
        lines.append(f"### `{title}`\n")
        lines.append(f"{note}\n")
        lines.append("```json")
        lines.append(json.dumps(shape(cap[key]), indent=1))
        lines.append("```\n")

    lines.append("""
## Field notes for the analysis report

Things a UI will get wrong without being told:

- **`moves[].label`** is one of `best`, `excellent`, `good`, `inaccuracy`,
  `mistake`, `blunder`. Thresholds are 15/10/5 Win% drop — **not** the
  30/20/10 in plan.md A.3, which is wrong by 2x (see decisions.md 2.8).
  A move the engine itself recommended is always `best`, whatever the drop.
- **`moves[].cp_before` / `cp_after`** are centipawns **from White's point of
  view**, always. Flip the sign for Black's perspective. `win_drop` is
  already from the mover's perspective.
- **`moves[].mate_status_change`** is `mate_now_forced`, `forced_mate_lost`
  or null — a fact, separate from `label`.
- **`moves[].fen_before` / `fen_after`** let a board step through the game
  without a rules engine: `fen_before` is the position the move was played
  from, `fen_after` the one it reached. The last move's `fen_after` is the
  final position of the game, which has no row of its own.
- **`moves[].complexity`** is 0–1 with `complexity_band` as `low`/`medium`/
  `high`. `only_move` means there was exactly one good move.
- **`time_control.time_analysis_enabled`** false means the game had no usable
  clocks: hide the whole time panel rather than showing zeroes.
- **`white.rating_estimate.interval`** is the one to render, not `elo` alone.
  The estimate is a maximum likelihood over eleven Elo buckets, and on a
  single game that curve is often nearly flat: a real case put every bucket
  from `1100-1199` to `>=2000` inside the standard likelihood-ratio cutoff
  while `elo` read "2000+". `interval.confidence` is `low` whenever the range
  spans more than three buckets, which is the common case for one game.
  `interval.mass` describes that flatness under a uniform prior over buckets
  — it is **not** a calibrated probability, because that prior is wrong
  (most players are not above 2000).
- **`white.rating_estimate.available`** false is normal, not an error —
  `reason` says why. When true, `band` is always set and `elo` is a point
  estimate on a 100-point grid (2000, 2100, 2200 …) for **every** band,
  including the open-ended ones. Read `elo_method` with it: `interpolated` is
  a real sub-bucket maximum, while `floor` and `ceiling` mean the likelihood
  curve never turned over, so the number is a bound in that direction — show
  it as "2000+" rather than as a measurement. `elo` and `band` can disagree by
  one step (band `1600-1699`, elo `1700`): `band` is the raw argmax bucket,
  while `elo` comes from the shape of the whole curve and is then snapped to
  the 100 grid. That is not a bug — present `elo` as the estimate and `band`
  as the argmax, not as a range `elo` must sit inside.
- **Accuracy and the rating estimate answer different questions** and will
  often disagree, including in the direction that looks wrong. Accuracy and
  ACPL are Stockfish: how close to best were the moves. The rating estimate
  is Maia-2: which rating's player most plausibly produces these exact moves.
  A player can be accurate in a simple position and still look like a weaker
  player, and vice versa. Neither is a bug; do not present one as a check on
  the other.
- **`commentary.available`** false still carries `commentary.facts` — the
  deterministic B.1 layer (material, hanging pieces, motifs, king safety).
  Useful on its own, and it needs no API key.
- **`commentary.comments[]`** is sparse: only selected moves get one, and a
  comment that failed fact-validation is withheld entirely rather than shown
  wrong. `usage.rejected` says which and why.

## Error shapes
""")
    lines.append("```json")
    lines.append(json.dumps(cap.get("errors", {}), indent=1))
    lines.append("```\n")
    lines.append("""
| status | when |
|---|---|
| 401 | missing or invalid token on a protected route |
| 404 | unknown job, puzzle, or a resource owned by someone else |
| 422 | malformed request (zero or multiple analysis sources, bad move list) |
| 429 | per-caller rate limit (`RATE_LIMIT_PER_HOUR`), protects the LLM bill |
| 503 | a core is unavailable, or the server is at its memory limit (`Retry-After` is set) |

A **failed job** is not an HTTP error: `POST /analyse` returns 202, and the
failure arrives as `state: "failed"` with a user-safe `error` string. Bad
PGNs, unknown game ids and unreachable upstreams all land here.
""")

    lines.append("\n## Captured examples\n")
    for name, size in sorted(written):
        lines.append(f"- `docs/examples/{name}.json` — {size/1000:.1f} KB")

    with open(os.path.join(DOCS, "api-contract.md"), "w") as fh:
        fh.write("\n".join(lines) + "\n")
    return written


if __name__ == "__main__":
    print("capturing real responses ...")
    cap = capture()
    written = write_docs(cap)
    print("\nwrote docs/api-contract.md")
    print(f"      docs/openapi.json ({len(cap['openapi']['paths'])} endpoints)")
    print(f"      docs/examples/ ({len(written)} files)")
    print(f"\nSSE sequence captured: "
          f"{' -> '.join(e['event'] for e in cap.get('sse_sequence', []))}")
    if "part_c_error" in cap:
        print(f"\nWARNING: Part C examples not captured: {cap['part_c_error']}")
