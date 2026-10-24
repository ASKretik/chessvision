# Chess Analysis Web App — Project Plan

> **Scope warning, read first.** Parts A–F as specified total **50–75 hours**. At 2–3 h/day
> that is your entire remaining budget to end of October, for *all four* course projects,
> with zero slack. Part D was the designated cut. See §7.
>
> **Status: A, B, C, D.1, E and F are built.** D.2 (the position generator) is the only
> part cut, and the Positions tab that briefly implemented it was removed on request.
> What the plan did not budget for, and what actually consumed the time, was not the
> features — it was the measurement: nearly every section below carries an amendment
> that exists because something was verified and found wrong.

> **Build order ≠ letter order.** Do **E.1 (backend skeleton + deploy pipeline) first**, before
> Part A. Deploying for the first time on day 20 is how projects die.

---

## 0. Stack

| Layer | Choice | Cost |
|---|---|---|
| Backend | FastAPI in Docker, on **Render** | Free (750 h/mo, 512 MB, sleeps 15 min idle, ~60 s wake) |
| DB + Auth | **Supabase** (Postgres + Auth + Storage) | Free (500 MB; pauses after ~1 week idle) |
| Frontend | **Vercel** or Netlify | Free |
| Engine | Stockfish binary via `python-chess` UCI | Free |
| Human model | **Maia-2 → ONNX → `onnxruntime`** | Free |
| Commentary | LLM API (provider TBD, §8) | Metered — see B.4 |

**Not used, deliberately:** AWS (steep curve, expiring free tier, billing risk),
Fly.io (free tier removed 2024), Railway (now credit-based), Vercel serverless for
analysis (10 s timeout — a full-game pass takes minutes).

---

## PART A — Analysis core

### A.1 Input adapters

Four sources, one normalised internal representation (`PGN + metadata`).

| Source | Method | Notes |
|---|---|---|
| Lichess URL | `GET /game/export/{id}` | Easiest. Returns PGN with `%clk` and often `%eval`. **Build this first.** |
| Chess.com URL | `api.chess.com/pub/player/{user}/games/{YYYY}/{MM}` | No get-by-URL endpoint — fetch the monthly archive and filter by URL. Awkward; build second. |
| PGN paste/upload | direct parse | Trivial |
| FEN | single-position mode | Different output shape — no time analysis, no rating estimate. See §8. |

Detect `%clk` presence. If absent, disable time analysis cleanly rather than emitting
garbage.

### A.2 Engine service

- Stockfish binary, driven by `python-chess` UCI.
- **MultiPV enabled** — needed for complexity (A.4) and for "was this the only good move"
  in Part B.
- Cache evaluations by FEN. Opening positions repeat heavily across users.

### A.3 Move classification

Port Lichess's published formulas:

```
Win%      = 50 + 50 * (2 / (1 + exp(-0.00368208 * centipawns)) - 1)
Accuracy% = 103.1668 * exp(-0.04354 * (winPercentBefore - winPercentAfter)) - 3.1669
```

Thresholds: 30 / 20 / 10 Win% drop → blunder / mistake / inaccuracy.

Display **Accuracy%** in the UI — it's a legitimate, well-known metric. Do **not** try to
derive rating from it (see A.5).

### A.4 Time analysis

Heuristic, no model.

- **Complexity** per position, from MultiPV: number of moves within X cp of best, plus the
  spread across the top-k.
- **Time spent** per move from `%clk` deltas, normalised by the time control's base+increment.
- Flag: high time on low complexity (wasted), low time on high complexity (rushed).
- Output: total seconds wasted, and the top 3–5 moments in each direction.

### A.5 Rating estimate — Maia-2 via ONNX

**Why not a formula:** no published accuracy→rating mapping exists. Lichess publishes
accuracy only; Chess.com's CAPS is proprietary. Inventing a mapping would mean fitting one,
which is out of scope. Maia-2 is the principled route.

**Conversion (one-time, offline, NOT in the deployed image):**

```
pip install maia2            # local only, Python 3.12 (maia2 pins torch==2.4.0)
torch.onnx.export(...)  →  maia2.onnx   (93 MB)
```

**This is the point:** the production container ships `onnxruntime` + the `.onnx` file and
**never installs PyTorch**. Torch's footprint would not fit Render's 512 MB.

At 93 MB the `.onnx` is 7 MB short of GitHub's hard file limit, so it is **not** committed
— the Dockerfile fetches it from a release asset at build time (never at boot, which would
add 93 MB to a cold start). Only `maia2_meta.json` is committed.

**Method — maximum likelihood over the model's rating buckets:**

```
for b in buckets:               # 11 of them, see below
    score[b] = Σ_moves log P(move_played | position, elo_bucket=b)
peak = argmax(score)
```

The buckets are **fixed by the trained weights**, not chosen by us. `maia2.utils`
`create_elo_dict()` gives exactly eleven:

```
 0  <1100        5  1500-1599
 1  1100-1199    6  1600-1699
 2  1200-1299    7  1700-1799
 3  1300-1399    8  1800-1899
 4  1400-1499    9  1900-1999
                10  >=2000
```

**The ceiling is real and cannot be coded around.** The embedding table has eleven rows, so
the model was never trained to tell 2100 from 2500 — index 11 does not exist. Any finer
number above 2000 is inference, not measurement.

**Reporting on a 100-point grid.** `argmax` alone throws away the shape of the curve, which
does carry information *inside* the trained range: a 1400-rated player's likelihood peaks in
the interior and turns over. Fitting a parabola through the peak and its neighbours and
taking the vertex gives a genuine sub-bucket maximum — a real 100-point estimate for
**1100–2000**.

**Above 2000 it is not available, and this was measured rather than assumed.** The obvious
idea is to project past the last bucket from the curve's final slope (a 2400 player's curve
is still climbing there). `tools/calibrate_rating.py` fitted exactly that against **186
players of known rating**:

```
R² = 0.0007      mean abs error 273 Elo      fitted coefficient came out negative
```

The slope explains essentially nothing. That is not surprising in hindsight — the model was
never trained to separate those players, so nothing in its output does. Reporting a projected
2300 on that basis would be precisely the invented mapping this section rules out.

So the contract is:

| peak location | `elo` | `elo_method` | meaning |
|---|---|---|---|
| interior | vertex, to 100 | `interpolated` | a point estimate |
| top bucket | 2000 | `floor` | **a lower bound** — render as "2000+" |
| bottom bucket | 1100 | `ceiling` | an upper bound |

Report `band`, `elo` and `elo_method` together, so a UI never presents a bound as a point
estimate. No interval, per spec. Computed separately for both players.

If >2000 resolution is genuinely needed, the honest routes are: use the player's own site
rating (already in the PGN header), or fit a rating model on move-quality features against
labelled games — a separate piece of work, not a tweak to this one.

**Both of those were tried, and both were removed. Final state: ONE rating number.**

- **A move-quality → rating formula** was fitted and cross-validated against the labelled
  games available here. It beat predicting the mean by **4%** — the in-sample correlation
  of −0.45 evaporated under cross-validation. Not shipped.
- **A performance rating** (`opponent + 400·(W−L)/N`) was shipped and then removed. It is a
  real formula, but with N = 1 it has three possible outputs, and it measures the **result,
  not the play**: a 1200 who wins because a queen was hung scores opponent+400.
- **An error-profile posterior over rating bands** (A.5b) was built and then removed. The
  design is sound; the calibration table it needed was never fitted, and its placeholder
  values were not even mutually consistent. It correctly refused to report a number, which
  meant the UI showed a refusal where a rating should have been — worse than nothing.

What survives is Maia-2's estimate, **because it is the only one that reports its own
uncertainty**: a likelihood curve over eleven bands, from which the interval is derived
rather than assumed. On one game that curve is usually flat, and the UI says so ("Very
rough: 1100+"). The rest promised precision they could not support.

**Still open and stated plainly:** even this one is *unvalidated in the 1100–1999 band it
is used in*. Every measurement of it here drew on a cache that is 99% players above the
model's ceiling (median 3023). It is a gap in evidence, not a known defect.

The error profile — blunders per 100 **decisions**, ACPL, engine-best agreement — stays,
and is now the headline. It is a measurement, not an estimate, and needs no calibration to
be true.

### A.6 Phase + opening insights

- Segment game into opening / middlegame / endgame (by move number and material count —
  pick one rule, document it).
- Aggregate ACPL and blunder count per phase → "your middlegame is the weak phase."
- Identify the opening via ECO code, using the CC0 `lichess-org/chess-openings` TSV.
- Note the move at which the player left book.

Keep this shallow per spec — a phase verdict plus the opening name, not a treatise.

---

## PART B — Comments / agent system

**This is the differentiating feature. Everything else on this list exists on other sites.**

The reason chess.com's comments feel cheap is that they're templates with no grounding in
what actually happened. The fix is grounding, in three layers.

### B.1 Layer 1 — Fact extraction (deterministic, no LLM)

Per move, extract concrete facts with `python-chess` + Stockfish:

- material delta
- pieces newly hanging (attack-map diff before vs after)
- tactical motifs: fork, pin, skewer, discovered attack, back-rank weakness
- king safety change (attacker count near king)
- eval swing and Win% swing
- engine's best move and principal variation
- MultiPV spread — was this the only reasonable move, or one of many?
- opening book status
- phase

**This layer is the real work of Part B.** Motif detection is fiddly. Budget accordingly.

### B.2 Layer 2 — Game-level pass (one LLM call per game)

Input: the full fact timeline.
Output: game narrative arc, list of critical moments, each player's recurring weakness.

This is what lets move comments reference the game rather than floating free.

### B.3 Layer 3 — Move-level pass (batched)

Input: move facts + game narrative + classification.
Output: the comment.

Target quality: *"This hangs the knight on f6 — after Nxf6 you lose a piece for nothing,
and it's the third time this game a piece was left undefended on an open file."*
Not: *"Good move!"*

### B.4 Cost and latency control — design this up front

~40 commentable moves per game, called naively = minutes of latency and unbounded cost on
a public app.

- Comment only on moves above an eval-swing threshold, plus a few representative quiet ones
- Batch several moves per LLM call
- Cache by `(FEN, move_played)`
- Rate-limit per authenticated account
- Stream comments to the UI as they arrive

### B.4b The output budget — learned the hard way

Batching interacts badly with **strict JSON-schema decoding**, and the failure is total
rather than partial: the model emits one JSON object, hits `max_completion_tokens`
mid-object, and strict decoding cannot produce a valid object — so the request is
rejected outright. **You are billed for the full output and receive nothing**, including
the comments that had already finished generating. A JSON object truncated at 95% is
worth exactly as much as one truncated at 5%.

Four rules, all of which cost real comments before they were written down:

1. **Reasoning tokens are billed as output and are spent before any JSON is emitted**, so
   they must be *reserved*, not hoped for. On `gpt-oss-120b`: low ≈ 650, medium ≈ 1,400,
   high > 4,800.
2. **Use the lowest effort the pass actually needs.** The game pass (B.2) synthesises an
   arc across forty moves and needs to reason. The move pass (B.3) restates facts that
   have already been computed — reasoning there buys nothing and costs most of the budget.
3. **Bound the answer in the schema** (`maxLength` on the comment), not in the prompt. An
   unbounded comment makes the budget uncomputable, and an over-long one is not trimmed —
   it destroys the whole batch.
4. **Size the budget from the batch, with real headroom.** A constant leaves a margin that
   varies with content, and reasoning grows with how much there is to say — so the batches
   that fail are the eventful ones, which is precisely where the commentary was worth
   having.

And when a batch does fail: **split it and retry rather than dropping it.** A truncated
batch is not a permanent failure, it is a batch that was too big for this budget; halving
it almost always clears. Only truncation is worth splitting — retrying a dead key or an
exhausted daily quota just burns what is left of it.

Anything that still cannot be commented on gets a **visible per-move gap** with its
reason. Silence in the move list is indistinguishable from the feature not working, and
cannot be debugged from the outside.

---

## PART C — Accounts and puzzles

### C.1 Auth

**Supabase Auth. Do not write your own.** Password hashing, sessions, reset flows, and
email verification are solved problems and a reliable way to lose two days.

**Amended in practice (C.1b).** This holds while Supabase is configured, and it is still
the path that wins when it is. But the fallback it implies — no accounts at all — meant a
deployment without a Supabase project kept progress in a dict in process memory, which a
restart wiped. That is not an account.

`accounts.py` is the same contract backed by SQLite, and the advice above is what shaped
its scope: **scrypt with per-user salts, hashed session tokens, and deliberately NO reset
flow or email verification.** Those are the parts that are genuinely hard to get right,
so rather than writing a bad version the limitation is stated to the user at signup — a
forgotten password cannot be recovered. What is implemented is the part that is simple
and well understood; what is skipped is skipped openly.

### C.2 Schema (Postgres, with Row Level Security)

```
users            -- managed by Supabase Auth
analyses         -- id, user_id, source_type, source_ref, pgn, result_json, created_at
puzzle_attempts  -- id, user_id, puzzle_id, solved, ms_taken, created_at
user_stats       -- user_id, puzzle_rating, games_analysed
```

RLS: users read/write only their own rows.

### C.3 Puzzles

- Source: `lichess_db_puzzle.csv.zst` from database.lichess.org. CC0. Millions of puzzles
  with theme tags and difficulty ratings, derived from real games.
- **Do not load the full set** — it will not fit in 500 MB alongside everything else.
  Subset it (see §8).
- Flow: serve puzzle → user plays moves on the board → validate against solution line →
  record attempt → update the user's puzzle rating with a simple Elo-style update
  (arithmetic, not ML).

**Built: 2,000 puzzles**, stratified across eight rating bands rather than sampled
randomly — a random sample mirrors the dump's middle-heavy distribution, so a strong
player runs out while a weak one sees the same themes forever. 406 KB on disk.

Two things worth carrying forward:

- The subset file is **not** the same shape as the raw dump. Its FEN is already past the
  opponent's setup move and the line is split into the player's moves and the replies, so
  parsing it with the raw-dump parser silently produces puzzles whose first move has
  already been played. Separate parsers, deliberately.
- **An interactive board needs a `step` endpoint.** The opponent's replies live inside the
  hidden solution, so the client cannot play the line out one move at a time without the
  server. That endpoint must not touch the rating; the finished line still goes to
  `/attempt`.

---

## PART D — Bots and position generator

> **Was the cut candidate. D.1 is now built; D.2 was cut for real.**
>
> D.1 (bots) came in far under the estimate because of the reuse below — the Maia-2
> session is already loaded for A.5, so the bots cost no extra memory and no extra
> model. D.2 (the position generator) was built as a Positions tab and then **removed
> on request**: a filtered view over the puzzle set duplicated what Puzzles already
> did, and §8.5's random-position reading remains pedagogically useless.
>
> What replaced it in the tab bar is **Study** (C.1c), which keeps the last 30
> analyses per user — a feature that only became possible once accounts were real.

### D.1 Play vs bots

- Strength via Stockfish `UCI_LimitStrength` + `UCI_Elo`, or `Skill Level`.
- **Reuse the Maia-2 ONNX model from A.5** for humanlike mid-range opponents — it's already
  loaded in the container, so this is nearly free. Skill-limited Stockfish plays
  near-perfectly then falls off a cliff; Maia misses things the way people do.
- Needs: live game state, move submission, engine reply, persistence, resign/takeback.

**Built, with two things the plan did not anticipate.** Maia-2 is trained with the
opening plies *skipped* — those moves are memorised rather than chosen — so it has
barely seen the starting position and plays nonsense there (measured at band 1700:
Nf3 0.394, e4 0.046, while mate-in-one in the same check scores 0.989). The fix is
to restrict it to A.6's opening book while the game is still in theory. Separately,
the policy has no memory, so nothing stops it answering Nf3 with Ng1; candidate moves
that repeat a position already reached are dropped.

Games are **not** persisted, against the line above: losing one costs a few minutes,
and a schema plus a cleanup job for something nobody asked to resume is not worth it.

### D.2 Position generator

**Underspecified — see §8.** Three readings:

1. FEN editor — drag pieces onto a board, export FEN
2. Theme-filtered positions pulled from the puzzle DB ("give me a rook endgame")
3. Random legal positions — easy, pedagogically useless

Recommendation: **2 + 1**. Option 2 reuses Part C's data; option 1 is a self-contained
board component.

---

## PART E — Backend, frontend, deployment

### E.1 Backend skeleton — DO THIS FIRST

FastAPI + Dockerfile + Render deploy + Supabase connection + a `/health` endpoint,
deployed and reachable, **before writing any analysis code.**

### E.2 Long-running job handling

A full analysis takes minutes. An HTTP request must not block on it.

- `POST /analyse` → returns a job id immediately
- Background task runs the pass
- Client polls, or receives Server-Sent Events
- Results stream in: classifications first, comments as they generate

### E.3 Memory budget — the binding constraint

512 MB total must hold: Python + FastAPI + `onnxruntime` + Maia-2 weights + Stockfish
process + its hash table.

- Keep Stockfish hash small (16–32 MB)
- `onnxruntime`, never `torch`
- Profile under load before declaring this done (F)

If it doesn't fit: Render Starter at ~$7/month is the fallback.

### E.4 Frontend

**The spec landed** (Claude Design) and is integrated as `frontend/` — React +
TypeScript + Vite, built to `static/` and served by `partE.py` through `spa.py`.
What arrived with it:

- `src/styles/tokens.css` — both themes, every contrast ratio measured against
  WCAG AA at the use it is given, with four lichess values replaced and the
  failing ratio recorded for each. Used verbatim.
- The move-classification colour code is **Okabe-Ito**, so inaccuracy is blue,
  not yellow: red/orange/yellow are indistinguishable to a deuteranope and this
  is the one colour code the analysis page depends on.
- `Board.tsx` — click-click, drag, promotion overlay, keyboard grid, ARIA
  gridcells and a live region announcing each move with its classification.
- `backend/spa.py` — static mount, SPA fallback, reserved API prefixes.
- Cold-start handling: Render's free tier sleeps, so `/config` gets a designed
  waking state (quiet for 2 s, then an explicit card with an elapsed counter)
  rather than a spinner.

**The layouts are built.** Analyse renders the spec's page: player strip, eval
bar beside the board, player strip, transport controls and Flip/FEN/PGN on the
left; evaluation, best line, eval graph and a two-column move list with
comments opening inline on the right. Puzzles and Positions follow the same
visual language.

Pieces are the real **cburnett set** (Colin M.L. Burnett, CC BY-SA 3.0) —
lichess's default, inlined as SVG. The licence requires attribution, which is
in the page footer.

**All four tabs are built**, though they are not the four this plan listed.

Known requirements regardless of the spec:
- Tabs: **Analyse / Puzzles / Play / Study**. Positions was built and then removed on
  request — it duplicated Puzzles — and **Study** took its place: the last 30 analyses
  per user, which only became possible once accounts were real (C.1b/C.1c). Study is
  the one tab that appears *conditionally*, on being signed in, because without an
  account there is nowhere to keep anything and a permanently empty tab is worse than
  no tab.
- A **shared board component** used by all of them — build once, reuse. This is the single
  biggest frontend saving available.
- **The board takes move input everywhere.** Typing moves is not an acceptable primary
  input in any tab: click a piece, click a destination. Clicking the same piece again
  deselects it, so selection is a toggle and the move hints can always be dismissed.
  That covers playing a game, answering a puzzle, and exploring a variation from any
  position in an analysis.
  Specifically:
  - **Analyse** — step through the game (first/prev/next/last, arrow keys, and clicking a
    move row), and play a move from any position to explore a variation, with a way back
    to the game line.
  - **Puzzles** — answer by playing moves on the board, one at a time, with the
    opponent's reply appearing between them. Undo before committing. Typed entry survives
    only as a fallback behind a disclosure.
  - **Play / Positions** — same component, same input.
- **The client holds no chess rules.** Legality, SAN, and the position a move reaches all
  come from the backend (`GET /board/moves`), so there is one implementation of the rules
  and the client cannot disagree with the server about what is legal. Promotion is
  detected as several legal moves sharing a from/to square, which the board offers as a
  picker without knowing the promotion rules itself.
- **A puzzle's opponent replies stay server-side.** They live inside the hidden solution,
  so an interactive board needs `POST /puzzles/{id}/step` to validate the line so far and
  supply the reply. That endpoint does not touch the rating; the finished line still goes
  to `/attempt`.
- Progressive rendering of analysis results. The `analysis` SSE event carries
  the whole Part A report before commentary runs, so the board, the timeline
  and the accuracy figures render while the LLM is still working. Do not wait
  for `result`.
- **One service, one origin.** `partE.py` serves the built SPA, and the Vite
  dev proxy keeps the browser on a single origin in development, so CORS is
  never needed. Splitting the frontend onto its own host is what would require
  it — and SSE through a cross-origin EventSource needs an exact origin and
  credentials, never a wildcard.
- **The API contract is generated, not hand-written** (`docs/api-contract.md`,
  `docs/examples/`). The frontend's types are hand-written against those
  captured payloads, because the endpoints are annotated `-> Dict[str, Any]`
  and the published OpenAPI schema therefore says nothing useful.
  `tools/check_frontend_types.py` fails on drift between the two.

### E.5 Secrets and config

LLM API key server-side only, never in client code or the repo. Supabase anon key is
client-safe; the service key is not. CORS restricted to the deployed frontend origin.

---

## PART F — Integration and testing

| Check | Why |
|---|---|
| Classification matches Lichess on 3–5 reference games | Validates the ported formula |
| ONNX output == PyTorch output on a fixed batch | Conversion can silently change numbers |
| Memory profile under concurrent load | E.3 is the most likely production failure |
| Cold-start behaviour after 15 min idle | First visitor's experience |
| Rate limiting actually fires | Protects the LLM bill |
| End-to-end on each of the 4 input sources | Chess.com path is the fragile one |
| RLS: user A cannot read user B's analyses | Security |

---

## 7. Time budget

| Part | Hours | Cuttable? |
|---|---|---|
| A — Analysis core | 11–14 | No |
| B — Comments / agent | 7–10 | No — this is the project |
| C — Accounts + puzzles | 8–12 | Puzzles yes, accounts no |
| D — Bots + position generator | 8–13 | **Yes — cut first** |
| E — Backend + frontend + deploy | 12–20 | No |
| F — Integration + testing | 4–6 | No |
| **Total** | **50–75** | |
| **Without Part D** | **42–62** | |

Trappy bot: **not in scope.** 12–18 h on top of the above.

---

## 8. Open decisions — needed before implementation

1. ~~**Maia-2 opponent Elo.**~~ **Resolved.** Use the opponent's actual rating from the PGN
   header, falling back to the bucket under test when it is missing (`OpponentEloMode`).
   Joint iteration was rejected: buckets² inference passes for a number reported as a point
   estimate. Searching all eleven buckets, not 1100–1900, so a player outside that range is
   not pinned at an artificial edge — see A.5 for how the 100-point figure is derived.
2. **FEN input mode.** A single position has no clock data, no game history, no rating
   signal. What does the app actually show? Just engine lines and one comment?
3. **LLM provider** for Part B, and the commenting threshold (all moves vs. notable only).
4. **Puzzle subset size** — how many of the millions to load into 500 MB, and selected how
   (rating spread? theme coverage?).
5. **Position generator interpretation** (D.2) — which of the three readings?
6. **Phase boundary rule** (A.6) — move number, material count, or both?
7. **Render free tier vs. $7 Starter** — decided by the E.3 memory profile, but worth
   knowing your appetite now.
