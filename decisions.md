# Decisions log — Parts E.1 + A

Every judgement call made while implementing `partE.py` and `partA.py`, with the
reasoning. Companion to `plan.md`: the plan says *what*, this says *why it ended
up like that*.

Each entry marks whether it is **settled** (changing it means rework) or
**tunable** (a constant to adjust once there is real data). Constants are named
in code, never inlined, so the tunable ones are all greppable.

Status: **Parts A, B and C complete** (C pending one Supabase deploy to apply
the migration and seed puzzles). E.1, E.2, E.3 complete; E.5 covered by the
skeleton. Test counts: 55/55 core, 172/172 commentary, 60/60 puzzles, 68/68
accounts, 39/39 board UI, 23/23 SPA mount, 33/33 memory, 12/12 ONNX, 3/3 live input sources, classification
99.80% vs Lichess.
Not started: D (the designated cut), E.4 frontend **implementation** (blocked
on the UI spec — the contract it will build against is now generated, 3e.1).

Two things open, both flagged below: §8.7 (Render free vs Starter) is
measurable but unmeasured (3.18), and **Part B's live LLM path is unverified**
because this machine has no Anthropic credentials (6.1).

---

## 0. Build order

### 0.1 `partE.py` written first, and genuinely standalone — settled
`plan.md` says do E.1 before Part A. Rather than honour that nominally, `partE`
imports `partA` lazily inside `_load_analysis_core()` and treats its absence as
a disabled feature: `/health` returns 200, `/ready` reports
`analysis_core: {ok: false}`, and `POST /analyse` returns 503 with the import
error.

So the skeleton was deployable on day 1 with nothing behind it, which is the
entire point of the ordering. It also means a syntax error in `partA` degrades
one endpoint instead of taking the service down.

### 0.2 Resolved only the §8 decisions that Part A needs — settled
§8.1 (opponent Elo), §8.2 (FEN mode) and §8.6 (phase rule) block Part A, so
they are resolved below. §8.3 (LLM provider), §8.4 (puzzle subset), §8.5
(position generator) and §8.7 (Render tier) belong to B/C/D and were left open
deliberately — see §9.

---

## 1. Open decisions from plan.md §8

### 1.1 §8.1 Opponent Elo for Maia-2 — settled, configurable
`OpponentEloMode` in `partA.py`. Default `HEADER`: use the opponent's actual
rating from the PGN, which is the true conditioning value and is present on
essentially every Lichess and Chess.com game. Falls back to `TIED` (opponent =
the band being tested) when the header is missing, which assumes a roughly
matched game — what both sites' pairing produces. `FIXED` exists for
reproducible cross-game comparisons.

**Joint iteration over both players' bands was rejected.** It is bands²
inference passes for a number `plan.md` asks to report as a point estimate with
no interval. The cost does not buy anything that gets displayed.

### 1.2 §8.2 What FEN mode shows — settled
A separate return type, `PositionReport`, not a `GameReport` with empty fields.
It carries engine lines, a complexity read, the ECO name if the position is
book, and an explicit `unavailable: [...]` list naming each analysis that does
not apply and why.

The list is the useful part: the frontend hides those panels instead of
rendering empty ones, and nobody has to guess whether a missing rating means
"no signal" or "the job broke". `analyse()` dispatches on
`GameInput.is_position_only`, so the two shapes never interleave.

### 1.3 §8.6 Phase boundary rule — settled, tunable constants
Both criteria, each governing the boundary it actually describes well:

- **opening → middlegame**: the ply the player left book, capped at
  `OPENING_PLY_CAP = 24` (move 12). Book exit is the meaningful signal; the cap
  stops a deeply theoretical game being scored as "all opening".
- **middlegame → endgame**: non-pawn material (Q=9, R=5, B=N=3, both sides
  summed) at or below `ENDGAME_NONPAWN_MATERIAL = 13`, **or** at most
  `ENDGAME_PIECE_COUNT = 6` non-king pieces. Material, not move number — a
  queenless position on move 15 is an endgame.

The endgame transition is **monotonic**: once in, never out. Without that, a
promotion flips the phase back and forth and the per-phase aggregates become
nonsense.

---

## 2. Part A — analysis core

### 2.1 A.2 — evaluate N+1 positions, not 2N — settled
Classification needs the eval before and after each move, but the position
after move *i* is the position before move *i+1*. One sequential pass over the
N+1 positions in a game gives both sides of every classification.

This is the largest cost saving in Part A: it halves the engine work, which is
essentially all of the wall-clock time. Asserted in the smoke test
(`misses == plies + 1`) so a future refactor cannot quietly undo it.

### 2.2 A.2 — cache keyed on EPD, not FEN — settled
`(board.epd(), multipv, depth)`. EPD omits the halfmove and fullmove counters,
which do not affect the evaluation, so transpositions and repeated openings
share entries — which is the point of the cache per `plan.md` A.2.

Within a single game the hit rate is ~0 (positions are distinct); the win is
across users and games, so the cache belongs to the `EngineService` and should
outlive one request if it is ever moved up a scope.

### 2.3 A.2 — scores stored White-POV — settled
`EngineLine.cp_white`, with `cp_for(color)` to flip. Mixing side-to-move-relative
scores across plies is the classic source of sign errors in this kind of code;
one canonical frame and an explicit accessor makes that impossible.
Mate is folded to ±`MATE_CP` (10000) so everything downstream is one integer type.

### 2.4 A.2 — cache eviction clears everything — tunable
`cache_size = 20_000`, then `dict.clear()`. Crude but bounded, and it never
evicts mid-game. A real LRU is strictly better if the cache ever becomes
long-lived; not worth the code today.

### 2.5 A.3 — `CP_CAP = 1000` before any averaging — settled
Evals beyond ±1000 cp say "winning", not "more winning". Letting +4000 into an
average makes ACPL meaningless — one lost piece in an already-won position
would dominate a whole game's score. Lichess caps for the same reason.

### 2.6 A.3 — `played_best` short-circuits the drop thresholds — settled
**This was a bug found in testing.** The thresholds ran first, so the engine's
own top choice could be labelled a blunder when the eval dropped across a
forced sequence or from depth instability. Incoherent: the engine's choice *is*
the measuring stick, so it cannot be a mistake against itself. `classify()` now
returns `BEST` for `played_best` before looking at the drop.

### 2.7 A.3 — game accuracy is a plain mean — settled, documented as such
Lichess blends a volatility-weighted mean with a harmonic mean, and that
weighting is **not published as a formula**. Rather than approximate something
unstated, `game_accuracy()` takes the arithmetic mean and
`meta.accuracy_method` says so in every response.

Same principle as `plan.md` A.5's refusal to invent an accuracy→rating mapping:
if the real formula is not available, report a defensible simpler number and
label it, rather than a guess that looks authoritative.

### 2.8 A.3 — thresholds are 15/10/5, not plan.md's 30/20/10 — settled
**plan.md A.3 is wrong by a factor of 2.** lila computes "winning chances" on
a **[-1, 1]** scale and judges on deltas of 0.3 / 0.2 / 0.1. Win% is
`50 + 50*wc`, so those same constants are **15 / 10 / 5** in Win% points. The
plan carried the numbers across without rescaling.

plan.md contains both the wrong constants (A.3) and the acceptance test that
exposes them (F: "Classification matches Lichess on 3–5 reference games").
Those cannot both hold; F is what defines correctness, so the constants were
corrected with the user's agreement.

Measured over 9,524 judged plies in 120 Lichess-analysed games, fed Lichess's
*own* evals so engine differences cannot confound it:

| thresholds | agreement with Lichess's labels |
|---|---|
| 30 / 20 / 10 | 86.38% |
| **15 / 10 / 5** | **99.80%** |

Under 30/20/10 we silently missed 746 of Lichess's inaccuracies and
downgraded 301 mistakes and 245 blunders. The residual 0.2% at 15/10/5 are
19 moves Lichess declines to label at all.

### 2.8a A.3 — mate transitions judged on their own scale — settled
Once a mate is forced, Win% is pinned near 0 or 100, so the *drop* stops
carrying information: walking into mate from a position already lost by 9
pawns is a ~3-point drop, which Lichess calls a mistake, while the same
transition from a 6-pawn deficit is a blunder. Severity tracks how decided
the position already was, not the swing.

`classify_mate_transition` therefore keys off the **non-mate side's
evaluation**, with boundaries measured over 162 mate-status transitions:

```
|cp| <  700  -> blunder     (observed blunders up to |cp| 699)
|cp| <  1000 -> mistake     (observed mistakes 702 .. 997)
|cp| >= 1000 -> inaccuracy  (observed inaccuracies from 1009)
```

The boundaries fall cleanly at 700 and 1000 with no overlap, which is why
this is a measured rule rather than a fitted one. Agreement where Lichess
gave a label: **137/138 = 99.3%**.

Inaccuracy-vs-unlabelled is **not** reproducible: in the `|cp| >= 1000`
bucket Lichess labels some transitions and silently skips others, presumably
its advice de-duplication, which the API does not expose. Those are reported
as inaccuracies, costing 24 of 162.

I initially proposed a much simpler rule — mate-created → mistake,
mate-lost → blunder — and claimed it would reach 100%. That was wrong: both
outcomes appear with both labels. Recorded here because the simple version is
the obvious thing to reach for again later.

### 2.8b A.3 — `mate_status_change` is also recorded as a fact — settled
Separate from `label`, `MoveRecord.mate_status_change` carries
`"mate_now_forced"` / `"forced_mate_lost"` / `None`. A severity and a
statement about what happened are different kinds of information, and B.1's
fact layer needs the latter to ground a comment like "checkmate is now
unavoidable" rather than inferring it back out of a label.

### 2.9 A.3 — labels beyond the plan's three — settled
The plan specifies blunder/mistake/inaccuracy at 30/20/10 Win% drop. Added
`best` (played the engine move), `excellent` (<2% drop) and `good` for the rest,
because a timeline where 80% of moves have no label reads as broken. Deliberately
did **not** add Lichess's "brilliant"/"great" — those need tactical grounding
that is Part B's job (B.1 motif detection), not a threshold.

### 2.10 A.4 — complexity heuristic and its weights — tunable
`complexity_of()`: `0.60 * spread + 0.25 * (1 - choice) + 0.15 * mobility`,
each term normalised to 0–1.

- wide top-k spread → the choice matters → harder
- many moves clustered near best (`NEAR_BEST_CP = 50`) → hard to go wrong → easier
- more legal moves → more to look at → slightly harder

The weights are chosen so the **"only move" position scores highest**: one line
near best, everything else far worse. That is the hardest kind of position and
the one most worth flagging when someone moved instantly. Confirmed working on
live games — the rushed moments it caught were `only_move: true` at complexity
1.0 with 0 s spent.

These are guesses with a sensible shape, not fitted values. They are the first
thing to tune against real games, and the formula is one function.

### 2.11 A.4 — time normaliser is `base/40 + increment` — tunable
`TimeControl.expected_per_move()`. Assumes a 40-move game. Crude, but it is what
makes "slow" comparable between bullet and rapid — without a normaliser, every
rapid move looks slow and no bullet move ever does.

### 2.12 A.4 — a move must fail two tests to be flagged — settled
`wasted` needs ratio ≥ 2.0 **and** at least 3 s spent **and** complexity < 0.33.
`rushed` needs ratio ≤ 0.4 **and** complexity > 0.66.

The complexity condition is what separates this from a stopwatch: spending 30 s
on a genuinely hard position is not a mistake. The 3 s floor keeps bullet games
from flagging every move on rounding noise.

### 2.13 A.4 — `seconds_wasted` counts excess on flagged moves only — settled
`sum(spent - expected)` over `wasted` moves, not total time spent. It answers
"how much time did you throw away in positions that did not need it", which is
the actionable number. Total time used is reported separately.

### 2.14 A.4 — negative clock deltas become `None`, not 0 — settled
A negative delta means added time, a Lichess berserk, or a mid-game adjustment.
`None` propagates as "unknown" and drops out of the aggregates; 0 would silently
claim the move was instant and pull the median down.

### 2.15 A.4 — detect `%clk`, do not trust `TimeControl` — settled
`_from_pgn_text` scans for actual clock comments. Daily games and imported PGNs
routinely have one without the other. Time analysis needs **both** (clocks for
the deltas, the header for the normaliser); if either is missing the whole
section returns `{"enabled": false}` rather than partial numbers.
Verified in both directions.

### 2.16 A.5 — the encoding was reconstructed, and was wrong — settled
The first version of `encode_board()` was written from an understanding of
how Maia/lc0-style nets encode a board, without reading maia2's source. It
was wrong. Verified plane-by-plane against the real
`maia2.utils.board_to_tensor`:

| plane | real maia2 | first attempt | |
|---|---|---|---|
| 0–11 | W/B P,N,B,R,Q,K | mover/opp pieces | ✅ equivalent under mirroring |
| 12 | **side-to-move** | castling mover-K | ❌ |
| 13–16 | castling W-K, W-Q, B-K, B-Q | castling mover-Q, opp-K, opp-Q, EP | ❌ shifted |
| 17 | en passant | all-ones (invented) | ❌ no such plane |

The real model has a side-to-move plane at index 12, which shifts everything
after it by one, and the reconstruction invented an all-ones plane that does
not exist. Planes 0–11 were right only because mirroring makes "white pieces
of the mirrored board" identical to "the mover's pieces".

The lesson is the generalisable part: **the encoding is the one thing nothing
downstream can detect.** A wrong plane layout still produces a normalised
probability distribution over legal moves, so the rating estimate looks
entirely plausible and is simply wrong. Hence check 1 in
`tools/verify_maia2_onnx.py` asserts byte-equality against the real function,
and `ENCODING` was renamed from `maia2-v1` to `maia2-board-tensor-18ch` so a
stale `.onnx`/metadata pair cannot silently load.

### 2.17 A.5 — 11 Elo buckets, via a range lookup — settled
`create_elo_dict()` returns **11** buckets, not the 5 first assumed:
index 0 = `"<1100"`, 1–9 = `1100-1199` … `1900-1999`, 10 = `">=2000"`, and
`elo_embedding` has exactly 11 rows.

`elo_to_bucket()` reproduces `maia2.utils.map_to_category` — a half-open
range lookup — from parameters carried in the metadata. The first version
used nearest-value matching, which disagrees at **every** boundary.

### 2.18 A.5 — search all 11 buckets; report band + midpoint — settled
DECISION (resolved with the user). The MLE searches every bucket the model
has, including the two open-ended ones, so a player well outside 1100–2000 is
not pinned at an artificial edge.

`band` is always the model's own label. `elo` is the band midpoint for the
nine closed bands and **None** for `"<1100"` and `">=2000"`, where the model
expressed no midpoint and inventing one (1050 / 2050) would fake precision
the estimate does not have. `per_band` carries all 11 log-likelihoods so the
shape of the curve is inspectable, not just its argmax.

Rejected: the nine closed bands only — a 2400 player would come back as
`1900-1999` with no signal that the estimate had been clipped.

### 2.19 A.5 — illegal moves are masked OUT of the softmax — settled
`log_probs` sets illegal moves to `-inf` before the softmax, so every row is
a distribution over the position's real choice set and sums to 1.0 there.

This matters for correctness, not tidiness: normalising across all 1880
moves when ~30 are legal spreads mass onto moves that were never available,
understating the played move's likelihood by a factor that varies with the
position — which biases the argmax over buckets, i.e. the reported rating.

maia2 **0.9** multiplied logits by the 0/1 mask and softmaxed over the full
vocabulary (illegal moves get `exp(0) = 1` each, polluting the denominator).
maia2 **0.9.1** replaced that with `masked_fill(~legal, -inf)`. The
implementation here matches 0.9.1.

### 2.20 A.5 — one inference run per bucket, not per move — settled
`log_probs()` batches every position for a player into a single ONNX run,
called once per bucket: 11 runs per player per game instead of ~40 × 11.
The difference between seconds and minutes.

### 2.21 A.5 — unavailable is a first-class result — settled
Missing weights return `RatingEstimate(available=False, reason=...)`, never
an exception. The model file is 1 of 6 sub-parts of Part A; its absence must
not cost the other 5. The reason string is surfaced in the API response, so
the cause is visible without reading server logs. Both branches are tested.

### 2.22 A.5 — metadata is a contract, and mandatory — settled
`Maia2Session` refuses to load without `maia2_meta.json`, which the export
writes off the real package: move vocabulary (1880 entries, from
`get_all_possible_moves()`), `elo_dict`, bucketing parameters, input names,
output indices, `plane_layout`, encoding id, versions and the `.onnx`
sha256.

The vocabulary's **ordering is part of the trained weights**. A wrong order
maps logits to the wrong moves and yields confident nonsense rather than an
error, so the runtime never guesses it and the export never regenerates it
independently — it reads it from the same package version that produced the
weights. `plane_layout` is written as documentation so the contract the
runtime implements is diffable rather than implied.

### 2.23 A.5 — export runs in a separate pinned Python 3.12 venv — settled
`tools/` imports torch and is in `.dockerignore`, so plan.md E.3's "never
install torch in the image" is structural rather than a matter of
discipline: `partA.py` contains no reference to torch, and the export script
is not in the image.

The export venv is **Python 3.12 with `torch==2.4.0`**, matching maia2's own
pin, rather than the 3.14 runtime venv. torch 2.4.0 has no 3.14 wheels, and
matching the pin removes a whole class of "did a newer torch change the
numbers" doubt — which is exactly what the Part F check exists to catch.
3.12 also matches the Dockerfile's `python:3.12-slim` base.

### 2.24 A.5 — the 93 MB .onnx is fetched at Docker build time — settled
DECISION (resolved with the user). plan.md A.5 says "committed to repo", but
that predates knowing the size: the export is **93.2 MB**, which is 7 MB
from GitHub's hard 100 MB file limit and would be added to every clone.

Only `maia2_meta.json` (~21 KB) is committed. The Dockerfile takes
`MAIA2_ONNX_URL` (plus an optional `MAIA2_ONNX_SHA256`, verified) and fetches
the weights from a release asset at **build** time — never at boot, because a
cold start after Render's 15-minute idle sleep must not also wait on a 93 MB
download (plan.md F's cold-start check).

An unset URL still builds; A.5 then reports unavailable per 2.21. Rejected:
Git LFS (bandwidth quota, extra clone step) and int8 quantisation (~23 MB but
it shifts the numbers, defeating the Part F check).

### 2.25 A.5 — rapid model, switchable by env var — settled
DECISION (resolved with the user). maia2 ships `rapid` and `blitz`. Rapid is
the better general default: trained on longer games, so moves reflect
understanding rather than clock panic, which is what post-game analysis
reasons about.

`MAIA2_MODEL_TYPE` selects which gets exported, and the live model's type is
recorded in the metadata and reported as
`rating_estimate.model_type`, so the deployed model is never a guess.
Shipping both was rejected: ~186 MB and two onnxruntime sessions will not fit
512 MB without lazy loading and eviction.

Worth revisiting — the first live game tested was 60+0 bullet, so fast games
are clearly in the traffic.

### 2.26 A.5 — `onnxruntime` pinned to one thread — settled
`_single_threaded_options()`. onnxruntime defaults to a thread per core and
each thread brings its own memory arena — the fastest way to blow a 512 MB
budget (E.3). Also `OMP_NUM_THREADS=1` in the Dockerfile.

### 2.27 A.6 — opening book indexes two tiers — settled
**This was a bug found in testing.** Indexing only the position each TSV row
*names* meant 1.e4 was not in the book, so every game reported "left book at
move 1". Now:

- `_named`: EPD → (eco, name) for positions a row names. Source of the label.
- `_in_book`: every EPD any line passes through, prefixes included. Source of
  the book-exit ply.

They answer different questions and conflating them was the bug. After the fix,
a test game correctly reads *C57 Fried Liver Attack, left book at move 8 (Nd4)* —
a real deviation from theory.

### 2.28 A.6 — shortest line wins a contested name, deepest named position wins the label — settled
`setdefault` when building `_named`, so the first (shorter) row to reach a
position keeps it — a short line describes a position more generally than a deep
transposition into it. Then `identify()` keeps overwriting as it walks, so the
label is the **deepest** named position actually reached, which is the most
specific correct description of what was played.

### 2.29 A.6 — opening TSVs are a committed asset — settled
**This was a deployment bug found in review.** `data/openings/` was in both
`.gitignore` and `.dockerignore`, so production would have silently fallen back
to the 10-line stub table and reported bad ECO names forever.

Now committed (400 KB of CC0 data) and shipped in the image. No fetch at build
time (non-deterministic builds) and none at boot (adds seconds to the first
visitor's wait after a 15-min idle sleep — `plan.md` F cold-start check).
`OpeningBook.download()` exists for refreshing the files locally, not for
runtime use, and the fallback path now logs loudly enough to diagnose.

### 2.30 A.6 — weakest phase needs ≥4 moves in a phase — settled
`_weakest_phase()` ignores phases with fewer. Three endgame moves is not
evidence, and "your endgame is your weak phase" off a 1-move sample is the kind
of output that makes a tool untrustworthy. Returns `None` when no phase
qualifies, and the UI should say nothing rather than guess.

### 2.31 A.1 — Chess.com requires a username, accepts an optional month — settled
There is no get-game-by-URL endpoint, and a Chess.com game URL
(`/game/live/123456789`) carries no player or date, so the archive **must** be
scanned and the username is unavoidable. `month="YYYY/MM"` narrows it to one
request and should be an optional field in the UI.

### 2.32 A.1 — the archive scan is bounded to 24 months — settled, tunable
**Found in testing.** The scan walked every archive newest-first;
a decade-old account has ~120 archives of several MB each (the test account had
154), so a mistyped URL cost minutes and hundreds of MB before erroring.
`CHESSCOM_MAX_MONTHS = 24`, and the error names the window searched and suggests
passing a month.

### 2.33 A.1 — Chess.com URLs normalised before comparison — settled
Strip scheme, `www.`, query string and trailing slash. People paste all of these
forms; a string compare against the API's canonical URL fails on every variant
but one. All five forms verified to match.

### 2.34 A.1 — 404 messages name what was not found — settled
**Found in testing.** `_http_get` took a `what` parameter because a 404 on the
Chess.com *archives* endpoint means "no such user" but was reporting "Game not
found", pointing the user at the wrong problem.

### 2.35 A.1 — `InputError` is the user-safe error class — settled
One exception type for everything a user can cause (bad URL, unparseable PGN,
illegal FEN, missing Stockfish), with messages written to be displayed.
`partE` surfaces the string directly into `job.error`. Anything else is a bug
and gets logged with a traceback instead.

### 2.36 A.1 — Lichess 12-char ids trimmed to 8 — settled
Lichess URLs carry a 12-char player-scoped id for in-progress games; the first 8
are the game id the export endpoint wants. Accepts a full URL, a bare id, or
either length.

### 2.37 Chess variants get no special handling — settled
Chess960 and other variants work without a line of variant-specific code, because
python-chess reads the start position from the PGN's FEN header and the analysis
is position-based throughout. Verified on a live Chess960 game: engine,
classification and time analysis all correct; the opening book reports no ECO,
which is right.

Decided **not** to add variant detection or rejection. It would be new code and
new failure modes to achieve less than what falls out for free.

---

## 3. Part E.1 — backend skeleton

### 3.1 Thin PostgREST client instead of `supabase-py` — settled
`SupabaseClient` wraps `httpx` against `/rest/v1/` and `/auth/v1/`. Every call
Part C needs is a single-table read or write, and the SDK pulls a dependency
tree into a 512 MB image for no benefit (E.3). Swap it in if Part C needs
realtime or storage.

### 3.2 JWT verification delegated to Supabase — settled
`verify_jwt()` calls `/auth/v1/user` rather than decoding locally. No key
material to manage or rotate, and revoked tokens stop working immediately
instead of at expiry. Costs one HTTP round trip per authenticated request;
revisit with a short-TTL cache only if it shows up in latency.

### 3.3 Two health endpoints with different contracts — settled
- `/health` — liveness. Cheap, no dependencies, cannot fail. Render polls it to
  decide whether the instance is up, so it must **not** go red because Supabase
  is asleep (the free tier pauses after ~1 week idle) or because `partA` is
  missing. Returns only version and uptime.
- `/ready` — readiness. Probes Supabase, the Stockfish binary and the analysis
  core, and reports job stats.

Collapsing these is a classic way to get an instance restarted because a
downstream was briefly unavailable. A missing analysis core does not fail
`/ready` either — that is the expected state pre-Part-A.

### 3.4 `/config` serves the anon key to the browser — settled
The Supabase anon key is client-safe and is published at `/config`, so the
frontend needs no build-time copy and the two deploys cannot drift. The service
key and LLM key are read from the environment and never appear in any response —
`Settings.public()` is an explicit allowlist, not a filtered dump, so adding a
secret to the config cannot accidentally expose it. Asserted in the smoke test.

### 3.5 Production fails loudly at boot on missing config — settled
`lifespan` raises when `ENV=production` and a required var is unset; development
logs a warning and continues. Better to fail the deploy than to 500 on the first
real request with a confusing error.

### 3.6 Part A runs in a thread executor — settled
Part A is synchronous and CPU-bound (it drives a subprocess). Called directly it
would block the event loop, and `/health` would start timing out *during* an
analysis — causing Render to restart a healthy instance mid-job. `_run_analysis`
hands it to `run_in_executor`.

Part A was kept synchronous on purpose: it is also usable from a plain script,
and an async engine wrapper would mean two event loops for no gain.

### 3.7 Progress callbacks hop back to the loop — settled
`on_progress` wraps its work in `loop.call_soon_threadsafe`, so the subscriber
queues are only ever touched from the event loop thread even though the callback
fires from the worker. Without this it is a race that shows up as rare,
unreproducible dropped SSE events.

### 3.8 In-process job store, with the swap point documented — settled
Render free runs a single instance that sleeps after 15 min idle, so a Redis
queue buys nothing today. `JobStore` is the only class that would change when a
second instance exists; the replacement (rows in C.2's `analyses` table) is named
in a comment so the decision is re-findable rather than re-derived.

### 3.9 SSE replays past events on subscribe — settled
`subscribe()` seeds the queue with everything already published. A client that
attaches late — or reconnects after the Render cold-start wake — still sees the
full timeline instead of waiting for the next event. Paired with a 15 s
keep-alive comment, because Render idles out silent streams.

### 3.10 Results publish progressively — settled
`classified` fires with the full move timeline at 88%, before the rating
estimate runs. E.2 requires classifications first and comments later; wiring
that ordering now means Part B's comments slot into an existing stream rather
than needing the mechanism built later.

### 3.11 Concurrency capped by semaphore, single uvicorn worker — settled
`MAX_CONCURRENT_JOBS = 2` and `--workers 1`. Each worker would own its own
Stockfish process *and* onnxruntime session, so workers multiply the memory
budget; the semaphore bounds concurrency without that. This is the lever that
keeps E.3 honest, and `Hash = 16 MB` keeps each engine small.

### 3.12 Validation before availability check — settled
**Found in testing.** The 503 for a missing analysis core fired before request
validation, so a malformed body got a 503 and the client learned nothing about
the actual problem. Shape errors now return 422 either way.

### 3.13 Anonymous callers allowed — settled, revisit in Part C
`current_caller` returns an anonymous `Caller` rather than 401, so the analyse
flow works before Part C's auth lands. `require_user` exists for the endpoints
that must not be public. Rate limiting falls back to client IP for anonymous
callers.

### 3.14 Anonymous jobs are readable by whoever holds the id — settled
`_owned_job`: a job created while signed in is readable only by that user; an
anonymous job is readable by anyone with the opaque 128-bit id. Not-found is
returned instead of forbidden, so the endpoint does not confirm that a job id
exists. Revisit alongside C.2's RLS policies.

### 3.15 A failed persist does not fail a completed analysis — settled
`_persist` catches and logs. The analysis succeeded; losing the database write
should cost the history row, not the result the user is waiting for.

### 3.16 Rate limiter is a fixed window, in process — settled, tunable
Its job is to stop one caller running up the Part B LLM bill (B.4), not to be a
fair scheduler. `RATE_LIMIT_PER_HOUR = 20`. Moves to Supabase or Redis with the
job store if a second instance appears.

### 3.17 Stockfish from Debian apt, running as non-root — settled
`apt-get install stockfish` rather than compiling: no build toolchain in the
final image, and the binary is ~1 MB. Runs as uid 10001 as Render expects.

---

### 3.18 E.3 — measure the cgroup, and refuse to guess otherwise — settled
The number that decides whether this fits 512 MB is the **cgroup's**, not
Python's RSS: on Render the kernel enforces the limit on the cgroup, counts
each page exactly once, and includes the Stockfish subprocess and page cache.
`MemoryBudget` reads `memory.current`/`memory.max` (v2), falls back to v1,
then to summed **PSS** from `smaps_rollup` on Linux without a cgroup.

On macOS the container total is **genuinely unmeasurable** — psutil raises
`AccessDenied` reading a child's USS — so the reader reports a `"partial"`
source, declines to produce a tier verdict, and says to profile on Render.

That honesty is load-bearing, because the alternative actively misled me.
Summing child RSS reported a **946 MB peak against a 512 MB budget** and
recommended paying for Starter. The real figure is ~208 MB: Stockfish mmaps
its NNUE network and reports ~378 MB RSS of which only a few MB are private,
so summing it roughly tripled the total. An honest "unknown" beats a
plausible wrong number, and `tests/test_memory_budget.py` pins it.

### 3.19 E.3 — two peaks, because mixing sources lies — settled
**This was the bug behind the 946 MB false alarm.** `peak_mb` is set only by
authoritative readings; `peak_observed_mb` records any reading, for
diagnosis. Before the split, a `partial` sample taken while Stockfish was
alive set the peak, and a later child-free sample reported
`source: "uss"` — so the report showed an inflated peak labelled with a
trustworthy source. Admission control and the verdict now consult only
`peak_mb`, and a 9999 MB partial reading provably cannot move it.

### 3.20 E.3 — one shared Maia-2 session per process — settled
The single biggest memory lever, and it was wrong: `_estimate_both` built a
`Maia2Session` per call, so **every job reloaded 93 MB of weights from
disk**, and two concurrent jobs held two copies. `partA.analyse` now accepts
an injected `maia2_session` and partE owns a process-wide singleton behind a
lock — without the lock, two jobs starting together both build one and
briefly hold 186 MB, which is precisely the spike that breaks the budget.

Measured: **+4.4 MB across two jobs**, against ~+186 MB before. Load failure
is cached too, so a missing file does not re-raise per job. `maia2_path`
still works for scripts and tests.

`MAIA2_PRELOAD=1` loads the weights at startup rather than on first request,
so the footprint is predictable and the ~0.2 s cost lands in the deploy,
where Render's health check tolerates it, instead of in a user's first
analysis.

### 3.21 E.3 — the opening book is also a process-wide singleton — settled
Same bug, smaller: `OpeningBook` was constructed per `analyse_game`, so five
TSVs were reparsed into ~8000 positions on **every job**. Caught by seeing
"Opening book: 3865 named" logged twice during a two-job run. Now
`get_opening_book()` behind a lock — the first fix was unlocked and the
concurrent run still loaded it twice.

### 3.22 E.3 — admission control, not OOM — settled
`POST /analyse` refuses with **503 + Retry-After** when free memory is below
`MEMORY_HEADROOM_MB`, checked *before* a job id is issued so the caller gets
an honest answer rather than an id that will die. An OOM kill takes every
in-flight job with it and costs the next visitor a ~60 s cold start, so a 503
is the cheaper failure. Headroom must exceed `STOCKFISH_HASH_MB` with slack:
that is what an admitted job is about to allocate.

Memory is a **readiness** failure, not a liveness one — it appears in
`/ready` but never in `/health`, so Render does not restart an instance that
is merely busy.

### 3.23 E.3 — continuous sampling, and a load profiler — settled
plan.md E.3 says "profile under load before declaring this done". Two
mechanisms: a background sampler every 5 s so `/ops/memory` reports a true
high-water mark from real traffic rather than whatever was true when someone
looked, plus the cgroup's own `memory.peak`, which catches spikes between
samples.

`tools/profile_memory.py` drives N concurrent analyses in-process and reports
peak, steady-state retention and a verdict. It deliberately over-subscribes
(`MAX_CONCURRENT_JOBS + 1`) so the semaphore is exercised, and raises the
rate limit, which would otherwise reject the burst before memory became the
constraint.

Its most useful output is **steady-state retention**, which is meaningful
even from a partial source because it is the same measurement before and
after: +20 MB after three jobs confirms the shared session and opening book
are actually being reused.

## 3b. Part B — comments / agent system

### 3b.1 B — two providers, Groq by default — settled
plan.md §8.3 left the provider open. Resolved to **two**, chosen by
`LLM_PROVIDER`:

| provider | model | cost | why |
|---|---|---|---|
| **groq** (default) | `openai/gpt-oss-120b` | free tier | the user's budget constraint |
| anthropic | `claude-opus-5` | ~$0.14/game | higher quality when it matters |

`openai/gpt-oss-120b` rather than the comparably-sized
`llama-3.3-70b-versatile`: it is a **production** model (not preview, so it
will not vanish at short notice) and one of the few on Groq supporting
**strict** JSON-schema decoding. Both passes depend on schema-shaped output,
and llama-3.3 offers only `json_object` mode — valid JSON with no schema
guarantee. Groq's `reasoning_effort` (low/medium/high on gpt-oss) maps
directly onto the effort levels the two passes already used.

The first-party `groq` SDK is used rather than pointing the `openai` client
at Groq's OpenAI-compatible base URL: same result, one less layer.

### 3b.1a B — the provider boundary is one method — settled
`BaseCommentator` holds everything: both passes, the prompts, the schemas,
usage accounting, the discard of unrequested plies. Subclasses implement only
`_request`, which receives the prompt already split into system / stable /
volatile blocks.

That split is not cosmetic. On a provider with prompt caching the stable
blocks are identical across every batch of a game, so they sit behind a cache
breakpoint and are not re-read at full price. Groq has no prompt caching, so
it concatenates them — and on a free tier the saving is moot anyway, since the
binding constraint there is requests per minute, which B.4's batching already
minimises.

Consequence worth stating: the prompts and schemas are **shared**, so output
*quality* is the only thing that varies between providers, not output shape.
Both are tested with the same assertions against per-provider fake clients.

### 3b.2 B.1 — the fact layer carries no API dependency — settled
B.1 imports `chess` and nothing else, and reads Part A's report rather than
re-running the engine. Three consequences, all deliberate:

- Facts are returned **even when commentary is unavailable** — no key, no
  network, a failed game pass. Same degradation contract as A.5 (2.21).
- `/config` reports `move_facts` and `commentary` separately, because one
  needs a key and the other does not.
- The engine is never paid for twice: positions are rebuilt from
  `fen_before` and the move pushed.

### 3b.3 B.1 — motif detection states its limits — settled
`hanging_squares` is an attacker/defender count, not a static exchange
evaluation: it does not resolve long capture sequences and ignores pinned
defenders. Every detector documents what it misses, because **a comment
grounded in a wrong fact is worse than one with fewer facts**, and Part A's
engine eval remains the authority on whether material was actually lost.
These facts exist to say *which piece, on what square*.

### 3b.4 B.1 — the king outranks everything for pin/skewer geometry — settled
`PIECE_VALUE[KING] = 0` so kings never enter material counts. Reusing that
table for pin/skewer geometry made the king the *least* valuable piece, so
every skewer through a king was reported as a pin. `_pin_value` maps the king
to 100 for that comparison only.

### 3b.5 B.1 — pins created vs pins suffered are different facts — settled
`find_pins_and_skewers(board, mover)` reports pins the mover *created*;
`own_pinned_pieces(board, mover)` reports the mover's own pinned pieces. The
first version emitted both for the same king-pin, describing one thing twice.
A created pin against a king also carries `absolute: true`, since that is
legally binding where a relative pin only costs material.

### 3b.6 B.1 — recurring patterns are counted, not inferred — settled
`_recurring_patterns` counts loose-piece incidents, motif frequencies,
blunders per phase and time flags per player. This is what makes the plan's
target sentence possible — *"it's the third time this game a piece was left
undefended"* — as a **stated count rather than a model's guess**. The
aggregate goes to B.2, which is why its weakness claims can be concrete.

### 3b.7 B.4 — selection is tiered, not weighted — settled
Weight-tuning alone kept ranking impressive-but-correct moves above the
game's actual errors, so selection is now three explicit tiers:

1. **flagged** — every move the engine faulted, or that changed mate status
2. **notable** — strong or sharp moves clearing `NOTABILITY_GATE`
3. **quiet** — a few unremarkable good moves, evenly spaced

Tier 1 is a guarantee: a move Part A called a mistake is always commented
before any move it approved, because that is what a review is for. Tier 3
exists because a timeline of nothing but blunders reads as a scolding and
teaches less than one that also says why the good moves worked.
`_notability` now only orders moves *within* a tier.

Three weighting bugs were fixed on the way, all the same error — penalising
something the engine had already endorsed:

- material scaled on *gains*, so routine recaptures outranked the blunder
- the in-book discount applied to faulted moves, dropping the Fried Liver's
  key mistake (`...Nxd5`, a 15-point drop that is also a known line)
- `win_drop` counted in full on the engine's own top move, where it is depth
  instability rather than the player's error (Part A's `classify` already
  short-circuits `played_best` for this reason)

### 3b.8 B.4 — a proportional cap as well as a fixed one — settled
`MAX_COMMENTS_PER_GAME = 18` alone commented most of a short game.
`MAX_COMMENT_FRACTION = 0.35` caps it relative to length, so a 27-ply game
gets 9 comments rather than 16 — cheaper and a better read.

### 3b.9 B.4 — cached comments are position-local, and critical moments bypass — settled
plan.md B.4 says cache by `(FEN, move_played)`. The key also carries the
label, prompt version and model, because a comment is a function of all four
and serving one written by an older prompt would misrepresent the pipeline.

The tension worth recording: a cached comment **cannot** reference this
particular game's narrative, since the same position recurs across games. So
the moments B.2 identifies as critical **bypass the cache entirely** and
always get a fresh, narrative-aware comment, while ordinary moves take the
cheap path. Hit rate therefore comes mostly from openings, where the
narrative matters least anyway.

### 3b.10 B.2/B.3 — prompt caching on the two stable prefixes — settled
The SDK renders `tools → system → messages`, so breakpoints go on what does
not change: the system prompt (identical every call) and, for B.3, the game
narrative block (identical across every batch of one game). Per-batch move
facts come **after** the last breakpoint so they never invalidate the prefix.
Asserted in the tests, since a silent invalidator is invisible otherwise.

### 3b.11 B.2/B.3 — structured output, non-streaming — settled
Both passes use `output_config.format` with hand-written JSON schemas
(`additionalProperties: false`, all fields required) and validate the result.
Schemas are written by hand rather than generated from Pydantic to keep full
control of the strict-mode requirements and avoid `$ref` indirection.

Non-streaming, against the general advice to stream: structured output via
`output_config.format` is the documented path, and these payloads are small —
a fact timeline is a few thousand tokens, a batch of comments a few hundred.
B.4's "stream comments to the UI" requirement is met a level up, by
publishing each batch the moment it lands.

Effort is tuned per pass: `high` for B.2 (one synthesis call per game, where
quality matters most) and `medium` for B.3 (mechanical given pre-extracted
facts). Adaptive thinking on both.

### 3b.12 B.3 — replies for unrequested plies are discarded — settled
`move_pass` filters the response against the plies it asked about. Cheap
guard against a model returning a ply that was never in the batch, which
would otherwise attach a comment to the wrong move.

### 3b.13 B — failure is isolated at three levels — settled
- a **failed batch** loses its own comments; other batches still land
- a **failed game pass** degrades to B.1 facts only
- **no key** degrades to B.1 facts only, with a reason

And commentary never fails the job: Part A's report is published as an
`analysis` partial *before* commentary runs, so the client can render the
board, timeline and accuracy figures while the LLM works. All four paths are
tested.

### 3b.14 B — one commentator and one cache per process — settled
Same reasoning as E.3's Maia-2 singleton (3.20): one SDK client, and a cache
that accumulates hits across jobs instead of starting empty each time. Behind
a lock, for the same reason.

### 3b.15 B.1 — every fact that names a piece carries its colour — settled
The single largest source of wrong comments, found only by reading live
output. B.1 emitted `"knight@e6"` and `"by": "rook"` with **no colour**, and
nothing told the model which side it was addressing. So it inferred ownership
and got it wrong roughly a third of the time: *"your c4 pawn"* for a white
pawn while addressing Black, *"the white bishop on e6"* for a black knight,
*"your king is exposed to"* a discovered attack that was Black's own threat.

Fixed in the data, not the prompt: every piece entry carries `color`, every
motif carries `by_color` and per-target colours, and each move states
`you_are` / `your_pieces_are` / `opponent_pieces_are` outright. Prompt
nagging had already failed twice; the ownership errors disappeared in one
run once the facts were unambiguous.

Worth noting the diagnosis I got wrong on the way: I called *"a discovered
attack on d2"* and *"the a2 pawn"* hallucinations. They were real facts —
B.1 genuinely reported a black-queen discovery onto White's d2 pawn and a
skewer naming a2. I had judged the prose without checking it against the
fact payload. The actual defect was narrower than claimed, and the fix is
correspondingly narrower.

### 3b.16 B.1 — `squares_in_facts` bounds what prose may mention — settled
Each move carries the exhaustive list of squares its own facts refer to, and
the prompt may not name any square outside it. Cheap guard against invented
geometry, and it is derived by walking the fact structures rather than listing
fields, so a new detector's squares are covered automatically.

### 3b.17 B — three fields are withheld from B.3 — settled
`for_prompt()` sends a narrower view than `to_dict()`, each omission measured:

- `fen_before` — given the board, the model reasons from the board instead of
  the facts. B.1 exists so nothing must be inferred from the position;
  handing over the position defeats it.
- `cp_before` / `cp_after` — leaked into prose as "+0.68" and were read as a
  verdict. `win_drop` already carries the magnitude.
- `time_flag` / `time_spent` — read as a quality judgement, so a move
  labelled `best` was called "a wasted move". Also removed from B.2's
  timeline for the same reason. Clock pressure belongs in A.4's report.

### 3b.18 B — Groq's free tier dictates the token budget — settled
Measured, not assumed. The free tier allows **8,000 tokens per minute**, and
Groq charges `input + max_completion_tokens` against that window when it
admits a request — so the original `max_tokens=16000` exceeded the entire
budget on its own and was rejected with **413** as soon as any of the window
was spent. That silently killed the first of two batches, losing the comments
on the game's actual blunder and mistake.

Reasoning effort turned out to be the binding constraint, not answer length.
Measured output for the B.2 game pass on `openai/gpt-oss-120b`:

| effort | output tokens |
|---|---|
| low | 647 |
| medium | 1,402 |
| high | >4,800 — truncated mid-JSON, request rejected |

With ~3,300 tokens of input there is no room to fund high reasoning, so
`clamp_effort` caps Groq at medium. That is a real quality difference from the
Anthropic path and it is the price of running free. Per-pass budgets
(B.2 3,000 / B.3 2,500) and a batch size of 3 keep every request inside the
window; 413 and 429 are retried using Groq's own `x-ratelimit-reset-tokens`
header.

Note the failure mode: with strict schema decoding, too small a budget does
**not** degrade gracefully. The model is cut off mid-JSON and the whole
request is rejected with `json_validate_failed`, which is now reported as a
budget problem rather than a schema one.

### 3b.19 B — prompt edits that silently did not apply — settled, cautionary
Three prompt changes across two rounds failed their anchor match and were
written as applied. The model kept narrating moves because the rule I thought
I had strengthened was still a single line. Every prompt-affecting edit is now
verified by asserting on the live string (`"BAD " in SYSTEM_MOVE`), and
`PROMPT_VERSION` is bumped on every change so cached comments from an older
prompt are never served.

### 3b.20 B.1 — motifs name roles, not geometry — settled
`by` / `near` / `far` described a motif's *shape*, leaving its *direction* to
be inferred — and it was inferred wrongly in about a third of comments: a
bishop pinning an enemy pawn was described as being itself pinned, and a
discovered attack by Black was reported as helping White.

Motifs now name participants by role, in full prose, plus an explicit
`benefits`:

```
{"motif": "pin", "pinner": "white bishop on d5",
 "pinned": "black pawn on b7", "pinned_against": "black rook on a8",
 "absolute": false, "benefits": "white"}
```

**Not yet measured at scale.** Only one live run completed after this change
(6 comments: 5 sound, 1 pin still misattributed to the queen rather than the
bishop) before the day's free quota ran out — see 3b.25. The three-run
measurement showing 3/9 direction errors was taken *before* role naming, so
it is the baseline, not the result. Treat the improvement as plausible and
unverified until another run is possible.

### 3b.21 B.1 — `pieces_on_those_squares` says what is actually there — settled
The square allowlist told the model which squares mattered but not what stood
on them, so it wrote *"a hanging knight on e4"* when the black king was
standing there. Each move now carries a square → piece map for the position
**after** the move, with empty squares marked `"empty"` rather than omitted.

### 3b.22 B.3 — a wrong comment is withheld, not shipped — settled
`validate_comment` machine-checks each generated comment against the facts
and the board. A comment that fails is **discarded entirely**, and the move
gets no comment at all. The reader cannot tell a confident falsehood from a
fact, so a gap is the safer failure — and `usage.rejected` records the text
and the reasons, so the gap is visible rather than silent. A rejected comment
is never cached.

Two things are checkable without re-deriving chess:

1. **Squares** outside the move's allowlist.
2. **Piece-on-square claims** matching neither the position before the move
   nor the one after. Both frames are allowed, because a comment legitimately
   describes what was just captured as well as what now stands there.

Deliberately **not** checked, because it cannot be done reliably: claims
about lines rather than squares ("clearing the d-file"); a stale present-tense
reference that was true before the move (it matches the before-frame and
passes); anything semantic. So this narrows the error surface without closing
it, and `pieces_on_those_squares` does more of the work than the check does.

### 3b.23 B.3 — the allowlist had to be widened, carefully — settled
The first allowlist was motif participants plus the move's own squares, which
**rejected true statements**: the engine's recommended continuation (the
prompt asks the model to cite `best_move`), the kings, and the piece about to
capture a loose one — *"White's queen on f7 is poised to take on e6"* is both
true and the most useful thing to say about that position.

It now also includes the squares of `best_move` / `best_pv`, both kings, and
any piece **attacking a square already in the list**. That last rule is the
principled one: a piece with a real tactical relationship to a relevant square
is a legitimate thing to mention. Still tight — 11 of 64 squares on a
representative move — and verified both ways: 6/6 true statements accepted,
4/4 false ones rejected.

A false rejection costs a real comment, so the tests assert acceptance as hard
as they assert rejection.

### 3b.24 B — Groq rate-limit retries read the wrong header — settled
`_retry_after` consulted `x-ratelimit-reset-tokens` before `retry-after`.
That header reports when the continuously-refilling window will admit a small
request, which reads as a fraction of a second even when the minute's budget
is spent — so three retries burned inside two seconds and the game pass
failed outright.

`retry-after` is now authoritative on a 429, with reset-tokens as the 413
fallback, a 2s floor, a 65s cap, and a linear backoff toward a full window
when no header is usable. Also relevant for anyone running this: three full
games back to back exceeds the free tier, so the live harness needs ~75s
between runs.

### 3b.25 B — Groq's binding limit is tokens per DAY, not per minute — settled
Diagnosed only by reading the 429 body, which says exactly what is wrong:

```
on tokens per day (TPD): Limit 200000, Used 197094, Requested 3843.
Please try again in 6m44.7s
```

A full 27-ply game costs roughly 10-15k tokens, so the free tier allows on the
order of **15-20 analysed games per day**, not an unlimited stream. That is
the real operating constraint, and it is per organisation, not per key.

Three fixes followed, all of them things the code was getting wrong:

- **Surface Groq's own numbers.** The handler replaced a message naming the
  limit, the usage and the retry delay with a generic "rate limit reached;
  try again shortly". `_groq_limit_summary` now reports the limit's name,
  usage and retry window verbatim.
- **Do not retry a daily cap.** It will not clear in 65 seconds, so retrying
  wasted three minutes per attempt and consumed more quota. Daily limits now
  fail immediately; per-minute limits still retry.
- **Disable the SDK's own retries.** `groq.Groq` defaults to `max_retries=2`,
  so each of our attempts became up to three HTTP requests, each *reserving*
  input + `max_completion_tokens` against the window. One game pass could
  fire twelve requests reserving ~6,300 tokens each. The retries were
  themselves causing rate limiting.

Operationally: one game at a time, expect 60-90s, and do not loop runs. The
live harness needs a gap between games, and a measurement campaign needs a
quota budget planned in advance.

## 3c. Part C — accounts and puzzles

### 3c.1 C.1 — there are no auth endpoints, deliberately — settled
plan.md: *"Supabase Auth. Do not write your own."* So there is no sign-up,
sign-in, password-reset or email-verification route. The client talks to
Supabase Auth directly with the client-safe anon key and sends the resulting
JWT here as a bearer token; this service only verifies it (3.2). **Nothing in
this codebase stores, hashes or sees a password.**

What Part C added to partE is three account endpoints (`/me`,
`/me/analyses`, `/me/analyses/{id}`) and two puzzle endpoints — all behind
`require_user`.

### 3c.2 C.3 — the Lichess FEN is one move behind — settled
The format detail that makes or breaks this feature: the dump's `FEN` is the
position **before the opponent's move**, and `Moves[0]` is that move. The
player's position is `FEN` with `Moves[0]` applied; `Moves[1]` begins the
solution, and from there the player's moves are the odd indices.

Taking `Moves[0]` as the player's first move — the obvious reading — makes
every puzzle unsolvable, and the symptom looks like a bad data dump rather
than a parsing bug. Confirmed against the published documentation rather than
assumed, `Puzzle.from_row` applies the setup move at parse time, and the
database stores the already-advanced position so the mistake cannot re-enter
later. The test suite asserts it against Lichess's own documented example.

### 3c.3 C.3 — the solution never reaches the client — settled
`Puzzle.to_dict()` withholds `solution` and `opponent_replies` unless asked
explicitly, and `/puzzles/next` uses the default. Validation happens
server-side in `check_attempt`.

This is reinforced at the database layer: the `puzzles` table has RLS enabled
with **no select policy at all**, so no client token can read it. Puzzles are
served only through the backend, which holds the service key. A client that
could query the table could read the answer out of its own network tab, and a
client that decided whether it was right could award itself any rating.

The solution is returned in the attempt response **only when the attempt
solved it** — returning it on a failure would let anyone submit a deliberate
wrong move to get the answer.

### 3c.4 C.3 — strict line matching, with one exception — settled
An attempt must follow the solution line exactly, except that **when the
expected move is mate, any mating move is accepted** (and flagged
`accepted_alternative`). Lichess does the same, and without it a puzzle with
two mates in one marks a correct answer wrong.

Validation stops at the first wrong move and reports what was expected there
— including when the submitted move was illegal or unreadable, because
reporting a failure without the answer makes the puzzle unlearnable. SAN and
UCI are both accepted so the frontend can send either.

### 3c.5 C.3 — the puzzle's rating does not move — settled
Only the player's rating updates. The puzzle's rating comes from Lichess,
derived from far more attempts than this app will ever see, so adjusting it
from one user's result would add noise and nothing else.

K-factor falls with history (64 provisional → 32 → 16 established) so a new
account converges in a handful of puzzles rather than fifty. Ratings are
clamped to 400–3000 so an unlucky streak cannot land a user where no puzzle
exists to match them. Arithmetic only, per plan.md C.3.

### 3c.6 C.3 — the serving window aims slightly high and widens — settled
`rating_window` centres 25 points **above** the player: a puzzle you solve
~60% of the time teaches more than one you always solve, and the Elo update
means a diet of easy puzzles stops moving the rating at all. The window
widens on each retry (±100 → ±600) because a 50,000-puzzle subset has thin
spots, and an empty band must degrade into "slightly off-rating" rather than
into an error.

Already-attempted puzzles are excluded, and exhausting the window returns a
404 that names the likely cause (a subset needing a wider spread) rather than
a bare error.

### 3c.7 C.3 — §8.4 resolved: 50,000, stratified — settled
The open decision. Numbers with their reasoning, all three overridable as CLI
flags on `tools/build_puzzle_subset.py`:

| | choice | why |
|---|---|---|
| size | 50,000 (~12 MB) | comfortably inside 500 MB beside `analyses`; large enough that no band is exhaustible |
| selection | stratified by rating band, then theme | a random sample mirrors the dump's middle-heavy distribution, so a 2200 user runs out while a 1200 one sees the same themes forever |
| quality | `popularity ≥ 70`, `nb_plays ≥ 50` | low community scores are usually dual-solution or dubious puzzles, and one of those costs more trust than a missing puzzle |

Verified on a synthetic dump built to be middle-heavy like the real one: the
source ranged 2–962 puzzles per band, the subset came out 1–140 — flattened,
limited only by what the source contained. Reservoir sampling per bucket, so
one pass and memory proportional to the subset rather than the source;
deterministic for a given seed, so a rebuild is reproducible and a reseed is
a deliberate act.

### 3c.8 C.2 — RLS, and the limit of what it protects — settled
Every user-data table has RLS enabled with policies keyed on `auth.uid()`.
Deliberate omissions: no update policy on `analyses` (a report is a record of
what the engine found — re-run it rather than edit it), no update or delete on
`puzzle_attempts` (letting a client delete a failed attempt would make the
rating meaningless), and no client write on `user_stats` (a client that could
write its rating could set itself to 3000).

**The limit worth stating:** the backend uses the service key, which bypasses
RLS entirely. So RLS protects against a leaked anon key and against a client
talking to Supabase directly — it does **not** protect against a bug in
partE. Every ownership filter in `select()` is this code's responsibility,
which is why `tests/test_accounts.py` asserts cross-user isolation at the
HTTP layer and `/me/analyses/{id}` filters on `user_id` as well as `id`.

Another user's analysis returns **404, not 403**, so the response does not
confirm that an id exists under a different account.

### 3c.9 C.2 — attempt and stats update in one transaction — settled
`record_puzzle_attempt` is a Postgres function doing the insert and the stats
update together. Two round trips would lose an update whenever a user has two
tabs open — a read-modify-write on the rating. The Elo maths stays in Python
and the function takes the computed ratings, so the logic lives in one place
and the transaction lives in the other. `bump_games_analysed` does the same
for the analysis counter.

### 3c.10 C — a `select()` bug that would have served wrong puzzles — settled
PostgREST expresses a range as the **same** parameter twice
(`rating=gte.1400&rating=lte.1600`). The first `select()` built its query from
a dict, which silently collapses that to the last value — so every rating
window would have become an open-ended `>=` and served puzzles of any
difficulty, with no error anywhere. Parameters are now a list of pairs and a
filter value may be a list.

## 3e. Part E.4 — the frontend contract

### 3e.1 E.4 — a generated API contract, not a hand-written one — settled
plan.md E.4 says not to start the frontend until the UI spec lands, and to
*"build backend endpoints to a documented contract in the meantime so the two
can proceed independently."*

FastAPI's OpenAPI was not that contract. Every large response is typed
`Dict[str, Any]`, so the schema literally said `{}` where the payload is a
27-move analysis report — nothing could be built against it.

`tools/dump_api_contract.py` runs the real pipeline and captures real
responses, writing `docs/api-contract.md`, `docs/openapi.json` and 13 example
payloads under `docs/examples/`. Generated rather than hand-written because a
hand-written example drifts from the code silently, and the frontend finding
that out costs more than regenerating ever will. Regenerate after any
endpoint change.

It documents three things OpenAPI cannot:

- **The SSE event order**, captured from a live run
  (`state → progress×9 → analysis → state → progress → result`), including
  that `analysis` carries the complete Part A report *before* commentary runs,
  so a progressive UI renders the board and timeline immediately.
- **Response shapes as types**, field by field, for both report kinds.
- **The field traps**, which are where a UI would silently go wrong:
  `cp_before`/`cp_after` are always White-POV while `win_drop` is
  mover-POV; `rating_estimate.elo` is **null** for the open-ended bands even
  when `available` is true; `kind == "position"` is a different shape from
  `kind == "game"`; `commentary.available: false` still carries B.1 facts;
  and the labels use the corrected 15/10/5 thresholds, not plan.md's
  30/20/10.

Generation is deliberately offline (`LLM_API_KEY` blanked), so rebuilding the
contract never spends LLM quota, and Part C's examples come from the test
suite's fake Supabase rather than needing a live database.

### 3e.2 E.4 — four tabs may only need two — flagged, not decided
plan.md E.4 lists four tabs: Analyse / Puzzles / Play / Positions. **Play and
Positions are Part D**, which §7 designates as the first thing to cut. If D
stays cut, two of the four tabs have no backend to talk to, and E.4's 8–14
hours shrinks accordingly.

Recorded rather than resolved because it is a scope decision, and it should
be settled *before* the UI spec is drawn — a spec covering four tabs invites
building four.

### 3e.3 E.4 — the board takes move input, and the client holds no chess rules — settled
Typing moves was the placeholder, and for puzzles it was unusable: answering
a two-move puzzle meant writing `a2e6` and `f7f8` by hand, which is both
tedious and a different skill from seeing the move. So every board in the app
accepts clicks: click a piece, click a destination.

The interesting decision is *where the rules live*. The obvious move is to
ship a chess library to the client (chess.js, or a port) and let it generate
legal moves. Rejected: that is a second implementation of the rules, and the
two can disagree. A client that thinks a move is legal when the server does
not produces an attempt the server rejects for reasons the player cannot see,
and the bug is invisible until someone castles through check.

Instead `GET /board/moves?fen=` returns every legal move with its SAN, its
from/to squares and the FEN it reaches. The board is then a renderer with no
chess knowledge at all:

- selecting a piece = filtering the list by `from`
- playing a move = swapping in its `fen_after`
- a promotion = **several legal moves sharing a from/to square**, which the
  board offers as a picker without knowing that promotion exists as a rule
- check, checkmate and game-over all come from the response

One implementation of the rules, server-side, which is also the one that
validates attempts. The cost is a round trip per position; it is a few
hundred bytes against localhost or a CDN-adjacent API, and it buys the
guarantee that the board and the validator can never disagree.

`MoveRecord` gained `fen_after` for the same reason — the analyse board steps
through a game without computing anything, and the final position of a game
(which has no move row of its own) is the last move's `fen_after`.

### 3e.4 C.3 — an interactive puzzle board needs a step endpoint — settled
A puzzle board has a problem a static one does not: after the player's move,
the opponent answers, and **the opponent's replies are inside the hidden
solution**. The client cannot know them, by design (3c.1). So clicking one
move at a time is impossible without the server.

`POST /puzzles/{id}/step` takes the line so far, validates it, and returns
the scripted reply plus the position after it. It reuses `check_attempt`
directly — the per-move `checks[]` it already produced carried
`opponent_reply` and `fen_after` all along, so there is no second validator.

Three properties were chosen deliberately:

1. **It does not touch the rating.** Only `/attempt` does. The client
   auto-submits the moment the line is complete *or* the moment a step comes
   back wrong, so in normal use the rating is still recorded exactly once.
2. **It does not say what the right move was.** `/attempt` does that
   (3c.4), and `/attempt` is also the call that records the loss — so the
   answer reaches the player only through the path that costs them rating.
3. **A wrong step returns no reply and no position**, so a wrong guess
   reveals nothing about the line beyond "not that".

**Known limitation, stated rather than hidden:** a hand-written client can
call `/step` repeatedly to find the solution for free and then submit a clean
line to `/attempt`. This was verified by doing exactly that — a loop over
`/board/moves` and `/step` solved a dev puzzle in two steps with no rating
cost. Closing it needs per-attempt server state: a row written when the
puzzle is served, so the rating is committed before the first step and
`/attempt` only reports it. That is a schema change plus an attempt lifecycle,
and it is not worth it while the rating is a single-player progress number
with nothing staked on it. Revisit before any leaderboard.

### 3e.6 E.4 — the harness page is served `no-store`, and the board reports its own failures — settled
Reported as "the pieces in the puzzles tab are not movable". The code was
correct; the browser was running a copy of the page from before the board was
rewritten. The server log is what settled it: the tab had called
`/puzzles/next` four times and `/board/moves` **zero** times, and the new
board cannot draw a single legal move without that call — so the JS in the
tab was the old version, which parsed FENs client-side and needed no such
request.

Two changes, because "stale page" should not be able to masquerade as a
broken feature again:

1. **`Cache-Control: no-store` on `GET /`.** `FileResponse` sends an ETag and
   `Last-Modified`, which is correct for assets and wrong for a harness that
   changes every few minutes — a tab left open never revalidates at all.
2. **The board reports a failed load.** `Board.set()` had
   `catch (e) { /* a static board is still useful */ }`, which turned any
   failure of `/board/moves` into a board that drew pieces and ignored every
   click — visually identical to the bug that was reported. It now records
   `this.error`, prints to the console, and renders the reason under the
   board. An empty move list on a position that is not game-over is treated
   as a failure too, since a live board always has moves.

A diagnostic lesson worth keeping: **the access log distinguished a stale
client from a broken one in seconds.** A frontend that holds no rules has to
talk to the server to do anything, so "did the request arrive?" is a complete
test of whether the new code is running.

### 3e.7 E.4 — a headless test for the board — settled
`tests/test_board_ui.js` drives the `Board` class against a stub DOM under
`jsc` (macOS, no install) or node. It exists because two frontend bugs got
through everything else:

- a **duplicate `const PIECES`** — a SyntaxError that killed the entire
  script, so the page rendered as dead markup. No Python test can see this.
- the **swallowed fetch failure** above.

The suite extracts the class from `harness/index.html` and runs it, so merely
executing is a syntax check on the whole page. It covers selecting a piece,
playing a move, the resulting position, last-move highlighting, a locked
board ignoring clicks, a flipped board keeping its square mapping, a
promotion raising a picker instead of silently choosing a queen, and all four
load-failure modes, and that selecting a piece is a toggle. 39 checks.

The stub DOM makes `className` and `classList` the same underlying set, as a
browser does. An earlier version did not, and it reported a passing test for
a note it had not actually rendered — a stub that is wrong in the direction
of leniency is worse than no stub.

### 3e.8 E.4 — the UI spec, integrated; what it had wrong about the backend — settled
The Claude Design scaffold (React + TS + Vite) arrived as `fe/` and is now
`frontend/`, built to `static/` and served by `partE.py` via `spa.py`. Its
design work — `tokens.css` with every contrast ratio measured, the Okabe-Ito
classification colours, the ARIA board, the cold-start card — was adopted
verbatim. Its **assumptions about the API were not**, because they were
written before Part A existed.

Seven mismatches, found by diffing against `docs/examples/`:

1. `types.ts` named fields the backend does not send: `classification`,
   `eval_cp`, `win_pct`, `clock_ms`, `time_spent_ms`, and `comments` as a
   `Record<number, string>`. The real names are `label`, `cp_before`/
   `cp_after`, `win_before`/`win_after`, `clock_after`, `time_spent`, and an
   array of objects. **Each one reads as `undefined` at runtime with no error
   anywhere** — which is why this is now a test, not a review item.
2. `MoveClass` listed `book`. No such label exists: an opening move is
   classified on its merits and book membership is reported separately. The
   real sixth value is `excellent`.
3. `attempt()` posted a single `move`. The endpoint validates a *line*:
   `{moves: [...]}`.
4. `/puzzles/next` returns `{puzzle, your_rating, rating_window}`, not a bare
   puzzle.
5. The SSE frames are unwrapped — `analysis` and `result` carry the report at
   the top level — and the failure frame is `{error}`, not `{detail}`. The
   hook was therefore reporting every failed job as the generic "Analysis
   failed" and discarding the real reason.
6. `spa.py`'s `RESERVED` list contained `analyse` and `puzzles`, so the
   catch-all 404ed them before the SPA saw them. See 3e.9.
7. The Vite proxy was missing `/board` and the `/puzzles/{id}/step` bypass,
   so in development the board would have had no legal moves and every puzzle
   would have been unplayable — both presenting as frontend bugs.

Three things the scaffold asked for were added to the backend rather than
worked around, because each removed a place the UI would otherwise guess:
`reasons` and `limits` on `/config` (its own "Known gaps" asked for the
limits), and `fen_after` on each move.

`ANALYSE_DEPTH_MIN/MAX/DEFAULT` and `PUZZLE_MAX_MOVES` are now named
constants that both the request model and `/config` read, so the depth slider
cannot offer a value the validator rejects.

### 3e.9 E.4 — the SPA catch-all, and why it 404ed every page link — settled
`spa.py` adds `GET /{full_path:path}`. It is the most dangerous route in the
app: too greedy and every API path answers with HTML, too strict and every
deep link 404s. The scaffold shipped the second one.

Its catch-all tested `head in RESERVED` **first**, and `analyse` and
`puzzles` were both reserved — so `GET /puzzles` and `GET /analyse/{jobId}`
returned JSON 404 while its own README said they would fall through. The
README described the mechanism correctly and the conclusion wrongly.

Those two need no entry at all, because **FastAPI matches a registered route
before a catch-all**. Everything real under those prefixes already wins:
`POST /analyse` is a different method, and `/puzzles/next`,
`/puzzles/{id}/step` and `/puzzles/{id}/attempt` are all registered and more
specific. Only paths matching no route reach the catch-all, and under those
two prefixes those are page routes by definition. They are now exempt via
`SHARED_WITH_SPA`, named so a test can tell "shared on purpose" from
"forgotten".

Two guards, because this is silent when wrong:

- `tests/test_spa.py` (22 checks) writes a stand-in build, reloads partE and
  asserts the whole matrix: page routes serve the SPA, API routes stay JSON,
  unmatched paths under a reserved prefix are JSON 404s rather than HTML,
  fingerprinted assets are served, `index.html` is not cached, and a path
  traversal cannot reach the repo.
- `tests/test_accounts.py` walks `app.routes` and fails if any API prefix is
  missing from `spa.RESERVED` **or** from the Vite dev proxy. It immediately
  caught `/redoc` missing from the proxy.

### 3e.10 E.4 — the client still holds no chess rules, now in React — settled
The scaffold's `Board.tsx` takes `legalMoves: Map<from, to[]>` and documents
it as "from chess.js" — a second implementation of the rules, which 3e.3
rejected. The component itself holds no rules, though, so the prop is fed
from `GET /board/moves` instead via a new `useBoardMoves` hook, and the
decision stands unchanged across the rewrite.

Two details the swap needed:

- `Board` detected promotion itself, by checking for a pawn reaching the last
  rank — a chess rule. It now takes an `isPromotion` callback, which answers
  from the legal move list (four moves sharing a from/to pair) rather than
  from a reimplemented rule. The local check remains as the fallback for
  editor mode, where there is no move list.
- A `busy` prop was added. Puzzles are a round trip per move, and without it
  a second click can race the first.

`chess.js` stays in `package.json` though nothing imports it: the Positions
editor needs local placement rules. It should be dropped if that page stays
cut.

### 3e.11 E.4 — two Python checkers, because there is no Node — settled
Node is not installed on this machine, so `tsc` cannot run and **none of the
TypeScript has been compiled**. Rather than claim the integration was
verified, two checks stand in for the specific failures that cost the most:

- `tools/check_frontend_types.py` (26/26) diffs every interface in `types.ts`
  against the real captured payloads, by field name. Names are where the
  damage is — a wrong one is `undefined` at runtime with no error. It caught
  `/config` missing `reasons`/`limits` in the examples before I noticed the
  docs were stale.
- `tools/check_frontend_imports.py` (38/38) resolves every local import
  against what the target actually exports, and checks every `api.<method>`
  call exists.

Neither is a type checker and the README says so. `npm run typecheck` is the
first thing to run once Node is installed.

The import checker initially reported a clean 34/34 while **silently skipping
every `import type { ... }` line** — its regex did not allow the `type`
keyword, so four files' imports were never read. It now asserts that no file
with relative imports produced zero checks. A checker that is lenient in a
way you cannot see is worse than no checker; this is the second time in this
project a stub or parser passed by not looking (the other was the board
test's DOM stub, 3e.7).

### 3e.12 E.4 — the dev harness moved rather than being deleted — settled
Vite's `outDir` is `../static` with `emptyOutDir: true`, which would delete
the harness on every build. The harness moved to `harness/index.html`, served
at `/harness`.

It was kept, not replaced, because the two answer different questions: the
SPA is the product, and the harness is a no-build page that still works when
the SPA will not compile — which, with no Node on this machine, is the state
it is in right now. It is excluded from production unless `SERVE_HARNESS=1`.

### 3e.13 E.4 — the frontend builds in the Docker image, not in a Render command — settled
The scaffold's README gives a Render build command assuming a native Node
runtime. This project deploys `runtime: docker` on `python:3.12-slim`, which
has no Node, so the build is a separate `node:20-slim` stage whose `/static`
is copied into the runtime image. That also keeps ~200 MB of `node_modules`
out of the final layer, which the 512 MB budget cannot spare.

Consequence to accept deliberately: `npm run build` is `tsc -b && vite
build`, so **a type error fails the deploy**. That is right, but it means the
first Docker build is also the first compile. `package-lock.json` is not
committed yet (it needs an `npm install` to exist), so the stage falls back
to `npm install`; committing the lockfile is the first thing to do after
installing Node.

### 3e.14 E.4 — the design spec, built — settled
The screenshot of the intended Analyse page settled the layout, and it is now
what the app renders: player strip, eval bar beside the board, player strip,
transport controls and Flip/FEN/PGN on the left; evaluation, best line, eval
graph and a two-column move list with comments opening inline on the right.

Decisions inside that worth keeping:

- **The eval bar and graph are plotted on win percentage, not centipawns.**
  A linear cp axis spends most of its range on positions that are already
  decided, so +1 and +9 look nearly identical. partA already computes win%
  with Lichess's curve, so both are just that number. A forced mate pins the
  bar rather than showing a capped evaluation as "almost winning".
- **Only faults are coloured** in the move list, with `??`/`?`/`?!` suffixes.
  Colouring every move leaves nothing standing out. The colours are the
  spec's Okabe-Ito set, so inaccuracy is blue, not yellow.
- **The comment opens under its own move**, not in a panel elsewhere: a note
  about 14.Nbd2 that is not beside 14.Nbd2 makes the reader hold both in
  their head. Each card shows the B.1 facts it was grounded in, because a
  claim the reader can check is worth more than one they must trust.
- **The clock is the position's clock**, from `clock_after` on the last move
  that player made at or before the cursor — stepping through the game has to
  move it or it is decoration. Before either side has moved it falls back to
  the time control's base, since a blank clock reads as missing data.
- **Captured material is computed from the timeline**, by reading the victim
  off `fen_before` at the move's target square. No new endpoint.
- **The eval graph seeks on click.** With a 120-ply game it is the only
  control that makes the move list navigable.

Pieces are the real **cburnett set** (Colin M.L. Burnett, CC BY-SA 3.0), the
one lichess ships and the one the spec asked for, fetched from Wikimedia and
inlined. The licence requires attribution, so it is in the page footer, not
only in a source comment. They are injected with `dangerouslySetInnerHTML`,
which is safe here and nowhere else: compile-time constants from our own
bundle, no interpolation. Converting every `style="…"` and hyphenated SVG
attribute to JSX by hand is how a path gets silently mangled.

### 3e.15 E.4 — a jsdom smoke test, because a typecheck is not a render — settled
`npm run smoke` executes the **built** bundle under jsdom against a live
backend. It is not a browser and cannot judge layout or paint. It exists
because `tsc` passed clean on code with three real bugs in it, all of which
this caught in minutes:

1. **`Comment.text` does not exist.** The real field is `comment`. Every
   comment card rendered an empty paragraph above a facts line. A
   hand-written interface was simply wrong, and TypeScript cannot know that.
2. **The clock was blank at the start of every game.**
3. **Nine comments rendered as six.** A row holding two commented moves used
   `find()` and silently dropped the second.

`tools/check_frontend_types.py` should have caught the first. It did not,
because the captured `job-done-game.json` has `comments: []` — the contract
run's game draws no comments, so that interface was checked against nothing
and reported PASS. Two fixes: a `comment.json` example captured from a real
run, and an **empty array is now a failure rather than a skip**, since
"unchecked" was being reported as "correct". That is the third time in this
project a check passed by not looking (the board test's DOM stub, 3e.7; the
import regex skipping `import type`, 3e.11). The pattern is consistent enough
to be worth stating: when a check cannot see its subject, it must fail.

A second script clicks a move on a fixed position and asserts the selection
marker, the destination dots, the played SAN and the advanced position —
the board is the one component that looks identical whether or not clicking
does anything. Its first version asked `/puzzles/next` for a position while
the page had rendered a *different* puzzle, and reported a failure that was
its own fault; it now drives the Positions study page, whose position comes
from the URL.

### 3e.16 E.4 — Positions, and the endpoint it needed — settled
`GET /api/positions` is a thin filter over the same table the puzzles come
from, returning rows **without** solutions, plus `/api/positions/themes` for
the filter chips. Under `/api/` because `GET /positions` is genuinely
ambiguous — both a page and a query — which the SPA catch-all cannot resolve
by method or specificity the way it does for `/analyse` and `/puzzles`.

Reusing the puzzle set is the point: every position in it is one a real game
reached and a human got wrong, which beats anything generated. plan.md §8.5's
random-position generator stays cut and is not missed.

The page has two modes: a filtered grid, and a study board reached from a
card or from "Train this position →" in an analysis, which plays out legally
and can be handed to the engine as an ordinary FEN analysis.

One bug caught in review before it ran: the first version passed its filters
to `supabase.select()` as a list of pairs, but that helper takes a **dict
whose values may be lists** — it calls `.items()`, so the Supabase path would
have raised `AttributeError` while dev mode (a different branch) looked fine.

### 3e.17 E.4 — the blank analysis page, and the test that reported it clean — settled
Reported as "the analysis tab goes to a blank page after analysing". It did,
every time, for every game.

**Cause.** The `analysis` SSE frame carries Part A's report *before Part B
runs*, so it has **no `commentary` key at all**. `GameView` read
`report.commentary.comments` unguarded, which threw, which unmounted the
React tree — a blank page. The frontend's type said `commentary: Commentary`,
required, and that was simply wrong about the frame that exists specifically
to render early.

Fixed by making the field optional in `types.ts` and guarding all three
reads. The type now carries the reason, because "optional" alone invites
someone to make it required again.

**Why every check passed.** This is the more useful half.

- `tsc` was clean: the interface was internally consistent and wrong.
- `tools/check_frontend_types.py` was clean: it diffs against
  `job-done-game.json`, which is a **finished** job and does have
  `commentary`. The intermediate frame is not captured anywhere.
- `npm run smoke` was clean: jsdom has no `EventSource`, and the stub I wrote
  reported `CLOSED` so the hook fell back to polling. **Polling only ever
  returns the finished job.** The stub did not merely fail to test the SSE
  path — it actively replaced it with the one path that cannot show the bug.

The fix is in the harness, not just the page: `scripts/smoke-render.mjs` now
implements a real `EventSource` over fetch streaming and parses the frames,
and `node scripts/smoke-render.mjs new` **starts a job and opens it
immediately**, so the page mounts while the job is still running and renders
the intermediate frame. Opening a finished job cannot test this, which is
exactly how a crash on every analysis survived a clean smoke run.

Verified by reinstating the bug: the harness reports `root children: 0` and
`TypeError: Cannot read properties of undefined (reading 'comments')` — the
blank page, caught. `--poll` keeps the old stub for when the polling fallback
is what is under test.

**The pattern, now four for four.** A board stub that did not link
`className` to `classList` (3e.7). An import regex that skipped
`import type` (3e.11). A payload example with an empty array, so an interface
was checked against nothing (3e.15). And now a transport stub that swapped
the untested path for the tested one. Every one reported PASS by not looking
at the thing it claimed to check. **A check that cannot see its subject must
fail, not pass** — and a stub that silently substitutes a different code path
is the most dangerous kind, because the run is not merely uninformative, it
is misleading.

Also documented in `docs/api-contract.md`: the two frames are different
shapes, and that is deliberate.

### 3e.5 A.5a — `elo` and `band` can disagree by one step — settled
A real report: `band: "1600-1699"`, `elo: 1700`. Not a bug, but it reads as
one, so both the UI and the contract now say what the two numbers are.

`band` is the raw argmax bucket. `elo` is the vertex of a parabola fitted
through the peak and its neighbours, then snapped to the 100 grid. A curve
skewed upward puts the vertex at, say, 1670, which rounds to 1700 — outside
the argmax band. Clamping `elo` into the band was rejected: it would throw
away precisely the sub-bucket information the interpolation exists to
recover. The UI labels the band `argmax 1600-1699` instead, and the contract
tells a frontend not to treat it as a range `elo` must sit inside.

## 3a2. A.5a — reporting a rating on a 100-point grid

### 3a2.1 A.5a — interpolate the curve; the model's buckets are not the limit — settled
`argmax` over eleven buckets throws away the curve's shape, which carries
real information: a 1400-rated player's likelihood peaks in the interior and
turns over, a 2400-rated player's is still climbing at the last bucket.

`refine_rating` fits a parabola through the peak bucket and its neighbours
and takes the vertex — a genuine sub-bucket maximum-likelihood estimate, no
new assumptions — then rounds to 100. That gives a real 100-point figure
across **1100–2000**, which is what was asked for inside the trained range.

### 3a2.2 A.5a — above 2000 it is a floor, and that is a measured result — settled
The request was 100-point resolution above 2000 too (2100, 2200, …). **That
is not available, and the reason is in the weights, not the code:**
`elo_embedding` has exactly eleven rows, the last being `>=2000`, so the model
was never trained to separate a 2100 from a 2500. Index 11 does not exist.

The plausible workaround — project past the last bucket using the curve's
final slope — was **implemented, then measured** rather than shipped on
intuition. `tools/calibrate_rating.py` fitted it against 186 players of known
rating drawn from real Lichess games:

```
R² = 0.0007        mean absolute error 273 Elo
fitted coefficient came out NEGATIVE (physically backwards)
```

The slope explains essentially nothing, which is obvious in hindsight: the
model cannot distinguish those players, so nothing in its output does. A
projected "2300" on that basis would be exactly the invented accuracy→rating
mapping plan.md A.5 rules out — so the extrapolation was **removed** after
being built.

The top band now reports `elo: 2000` with `elo_method: "floor"`, which is a
**lower bound**, and the UI renders it `2000+`. Bottom band mirrors it as a
ceiling. Every response carries `elo_method`, so a bound can never be
mistaken for a point estimate.

Worth keeping the negative result: the calibration tool and its output stay
in the repo so the next person does not spend the same afternoon rediscovering
that the slope is uninformative. If >2000 resolution is genuinely needed the
honest routes are the player's own site rating (already in the PGN header) or
a rating model fitted on move-quality features against labelled games — a
separate piece of work.

### 3a2.4 A.5a — the estimate now reports how little it knows — settled
Raised by a user looking at a real report: White had the better accuracy
(84.5% vs 80.8%), the lower ACPL (60.7 vs 90.3) and fewer blunders, yet the
lower estimate (1500 vs 1800). "This seems wrong."

**It was not a bug, and the wiring was checked first.** `_estimate_both`
selects `boards[i]` where `boards[i].turn == color`, passes the *opponent's*
header rating as the opponent Elo, and returns `(white, black)` in that
order, which `_player_summary` consumes in that order. No colour swap.

**The two numbers answer different questions**, and this is by design:

- Accuracy and ACPL are **Stockfish**: how close to the engine's best were
  the moves.
- The estimate is **Maia-2**: which rating's player most plausibly produces
  *these exact moves*.

A player can be accurate because the position was simple or forced, and
still look like a weaker player; a player can blunder and still choose moves
characteristic of stronger play. Neither validates the other. That is now
stated in `docs/api-contract.md` so a UI does not present one as a check on
the other.

**But the question exposed a real defect in the reporting.** The estimate was
printed as a bare number with no indication of how much the moves support it.
Looking at an actual likelihood curve from the captured contract run:

```
1400-1499  -8.168      <- within 0.11 of the peak
1900-1999  -8.272
>=2000     -8.056      <- argmax; reported to the user as "2000+"
```

Eight moves, and the span from 1400 to 2000+ covers 0.11 log-likelihood — a
likelihood ratio of 1.12. That is not evidence of anything. The app was
printing noise with a number on it.

So `likelihood_interval()` now reports the contiguous run of buckets within
`LR_CUTOFF = 1.92` of the peak — the standard likelihood-ratio cutoff for one
parameter, which is what reading this curve by eye amounts to — plus a coarse
`confidence` and the posterior mass of that run. On the curve above the honest
answer is **1100 to 2000+, confidence low**.

The UI leads with the range whenever it spans more than one bucket:

> **1900** estimated from 11 moves
> Very rough: **1100+** — one game rarely pins this down, and this curve is
> nearly flat

Two deliberate limits, stated rather than hidden:

- `mass` uses a **uniform prior over buckets**, which is wrong — most players
  are not above 2000. It is reported to show flatness, not to claim coverage,
  and both the docstring and the contract say so.
- An open-ended bucket gets `high_elo: null` and `width_elo: null` rather
  than an invented edge. Inventing one would repeat the extrapolation mistake
  that measured R² = 0.0007 (3a2.2).

Ten checks added to `tests/test_analysis_core.py` over flat, sharp and mild
curves, including that the cutoff is the 95% LR value and that an open end
yields no width.

The broader lesson is the same one as 3a2.2: this project has twice now
presented a number more confidently than the measurement supported, and both
times the fix was to report the uncertainty that was already sitting in the
data. `per_band` carried the whole curve from the start — nothing new had to
be computed, only shown.

### 3a2.5 A.5a — no colour bias, and a validation gap I cannot close with this data — settled
The colour-swap hypothesis behind 3a2.4 was measured, not just reasoned
about. `tools/check_rating_bias.py` runs the estimate over every cached
rated game and compares against the players' real Lichess ratings:

```
white  n=119  mean error  -993   median -1061   mean|err| 1008
black  n=119  mean error -1027   median -1106   mean|err| 1031

white-minus-black bias: +34 Elo
better Lichess accuracy also got the higher estimate in 63/79 games (80%)
```

**Two conclusions, and they point in different directions.**

1. **No colour bias.** +34 Elo between the two is nothing against a
   ±1000 spread. The wiring in `_estimate_both` is correct, as the code read
   suggested.

2. **The 80% agreement figure is the useful one for the original question.**
   Within a game the better Lichess accuracy usually does come with the
   higher estimate — but one game in five disagrees, which is exactly the
   case the user hit. Disagreement is normal, not a symptom.

**The -993 is not a model defect, and must not be quoted as one.** 237 of
the 240 players in this cache are rated ≥ 2000, median **3023** — they are
elite bullet games, because the cache was built for the classification test,
which needed games Lichess had already analysed. Maia-2's top bucket *is*
`>=2000`, so a 3023-rated player can score at most 2000 and the error is
mechanically −1023. The number measures the test set, not the model. The
tool now prints that distribution above its own results so the figure cannot
be read alone.

**The gap this exposes is real and is NOT closed.** Only three players in
the whole cache sit inside 1100–1999, which is the band the feature is
actually for. So:

> The rating estimate has **never been validated in the range where it is
> used.** Everything measured about it so far — this, and the R² = 0.0007
> extrapolation result in 3a2.2 — comes from players above the model's
> ceiling.

3a2.2's conclusion is unaffected, because it was explicitly a question about
projecting *above* 2000 and used the right population for that. But nothing
here supports or refutes the estimate's accuracy at, say, 1400.

Closing it needs a sample of sub-2000 rated games with known ratings, which
this cache cannot provide and which the tool would then measure unchanged.
Until that exists, the honest position is that the estimate is **unvalidated
in band**, which is also why 3a2.4's interval matters more than it otherwise
would: the UI reports a wide range and low confidence because that is what
the likelihood curve supports, not because the point estimate has been shown
to be good.

### 3a2.6 A.5 — a move-quality→rating formula, measured and NOT shipped — settled
Asked for directly: a simple formula for the rating a player projected in a
game, no training. Worth taking seriously, because the Maia-2 estimate is
heavy and unvalidated in band (3a2.5).

It was fitted and cross-validated on the 120 labelled games, using Lichess's
own per-player ACPL — legitimate because partA's ACPL was already verified to
match Lichess's within 0.7 cp mean, so a fit on theirs transfers to ours.

```
corr(rating, acpl)            -0.131      flat, but restriction of range
corr(rating, accuracy)        +0.086
within a game:
corr(d_rating, d_acpl)        -0.451      promising
5-fold cross-validated:
  mean |error|                200 Elo
  predicting the mean         207 Elo
  improvement over no model     4%
```

**4%.** The within-game correlation of −0.45 looks like signal and evaporates
under cross-validation. A formula on this basis would be the invented
accuracy→rating mapping plan.md A.5 rules out, and the second time this
project shipped a number the data did not support (after 3a2.2's R² = 0.0007
extrapolation). It is not shipped.

The restriction-of-range caveat from 3a2.5 applies here too — this sample is
99% players above 2000 — so this is "not demonstrable with available data",
not "proven impossible". `tools/check_rating_bias.py` would answer it
properly given a rating-diverse sample.

**What was shipped instead: the standard performance rating.**

    Rp = opponent rating + 400 * (W - L) / N

This is a real formula with a real definition, not a fit: it is the ordinary
chess meaning of "what rating did this result represent", it needs no model,
and it is exactly what was asked for. Its weakness is granularity, not
honesty — one game can only say +400, 0 or −400 — so `games: 1` travels with
it and the UI prints "(one game, so ±400)" next to every value. It reports
`available: false` with a reason when the opponent has no rating in the
headers, rather than anchoring to a guess.

The two rating numbers now shown answer three different questions, and the UI
labels each: Maia-2 ("which rating plays moves like these"), performance
rating ("what this result was worth"), and the header rating ("what the site
thinks they are").

### 3a4.1 A.4 — the time analysis was computed and never shown — settled
Reported as "the time usage analysis model, I can't see that one being put to
use". Correct: A.4 had been computing per-move time, ratios, flags, and
ranked offender lists since Part A, and the UI was rendering one summary line
of it.

`TimePanel` now shows, per player: total, median, seconds lost, and the
ranked **top_wasted / top_rushed** moments — each row clickable to seek the
board to that ply, because a time report you cannot click is a list of
regrets.

The design point is that the rows show **complexity**, not just seconds. "28s
on a low-complexity move" is the finding; "28s" is a statistic. A long think
on a hard position is not waste, and a fast move in a simple one is not
rushing — which is exactly how partA flags them:

```
wasted  ratio >= 2.0  AND  spent >= 3s  AND  complexity < 0.33
rushed  ratio <= 0.4  AND                    complexity > 0.66
```

My first draft of the panel's explanatory copy said "slow past twice that and
rushed below a third of it" — wrong on the threshold (0.4, not a third) and,
worse, it **omitted the complexity condition entirely**, which is the whole
point of the metric. A panel that explains a metric incorrectly is worse than
one that shows the number bare. The copy now states both halves exactly.

A game without clocks renders the reason rather than an empty panel, and the
smoke PGN was changed to carry `%clk` and Elo headers — without them the time
panel and the performance rating both correctly render "not available", and
the smoke run would have passed while testing neither.

### 3a2.3 A.5a — the calibration needs no engine — settled
Worth recording because it made the measurement cheap: the rating estimate
uses only the moves and Maia-2, never Stockfish. So 186 labelled curves were
computed from cached PGNs in ~19 minutes with no analysis passes at all. Any
future rating work can be calibrated the same way.

## 3f. Dev harness and local modes

### 3f.1 A dev inspection harness, explicitly not E.4 — settled
plan.md E.4 said not to begin frontend implementation until the UI spec
landed. `harness/index.html` is a single-file harness — no framework, no
build step, no design system — for exercising the API by hand and seeing
what the models produce. It is labelled as such in the page itself, served
only when `harness/` exists, and **skipped in production unless
`SERVE_HARNESS=1`**, so a debug UI cannot ship by accident.

It covers the Analyse flow (all four input types, SSE progress, board,
move table, A.4 time analysis, B.1 facts per move, B.2/B.3 commentary),
Puzzles, and a raw-JSON view.

**Updated when the spec landed (3e.12):** it moved from `static/index.html`
at `/` to `harness/index.html` at `/harness`, because Vite owns `static/`
and empties it on every build. It was *not* deleted as originally planned.
The two answer different questions — the SPA is the product, the harness is
a page that still works when the SPA will not compile, which is exactly the
state the SPA is in while this machine has no Node.

### 3f.2 C.3 — a local puzzle sample, so the feature is visible without a database — settled
Part C.3 keeps puzzles in Postgres, so without a Supabase project the whole
tab was dark and the feature impossible to look at while the database was
still being set up.

When Supabase is absent (and not in production), puzzles are served from
`data/puzzles/dev_sample.csv` — 11 **Stockfish-verified** mate-in-1-to-3
puzzles, generated by walking random games to positions with a forced mate
and stepping one move back so the stored FEN matches the Lichess convention.
Every one is asserted solvable from its stored position.

The important property: it runs the **same partC code** — parsing,
validation, the Elo update — so it exercises the real logic rather than
mocking it. Only persistence is swapped, and ratings live in process memory.
`/config` reports `puzzles_dev_sample: true` so the UI says so rather than
implying the real 50,000-puzzle set is loaded.

### 3f.3 A triple-gated dev identity — settled
Every Part C endpoint verifies a real Supabase JWT, so the local puzzle mode
was unreachable without a database. `require_user` falls back to a synthetic
dev user, guarded by three independent conditions that must all hold:

- `ENV` is not production
- Supabase is **not** configured — the moment a real database exists, real
  tokens are required again and this path is dead
- `ALLOW_DEV_USER` is not explicitly disabled

This is the only auth bypass in the codebase. It is reported in `/config` as
`dev_user: true` so it is never silent, and the Supabase condition means it
cannot survive into a real deployment even if someone forgets the env var.

## 4. Bugs found during testing

Recorded because each one explains why a piece of code looks the way it does.

| # | Bug | Found by | Fix |
|---|---|---|---|
| 1 | Opening book keyed only named positions → "left book at 1.e4" on every game | Running a real game; output obviously wrong | Two-tier index (2.27) |
| 2 | Engine's own top move could be labelled a blunder | Low-depth run showing `best` beside a large cp loss | `played_best` short-circuits (2.6) |
| 3 | Chess.com archive scan unbounded, ~120 multi-MB requests | Test account had 154 archives | 24-month cap (2.32) |
| 4 | 404 on the archives endpoint reported "Game not found" | Unknown-user error-path test | `what` parameter (2.34) |
| 5 | Opening TSVs gitignored *and* dockerignored → silent stub fallback in prod | Packaging review | Committed as an asset (2.29) |
| 6 | Malformed request returned 503 instead of 422 | Validation test | Reordered checks (3.12) |
| 7 | `maia2_meta.json` path built by string-replacing "maia2" in the whole path | Reading the code back | Use `dirname` |
| 8 | **`encode_board` planes 12–17 all wrong** (invented all-ones plane, missing side-to-move, castling + EP shifted) | Reading the real maia2 source | Faithful port + byte-equality check (2.16) |
| 9 | **Elo buckets: 5 assumed, 11 real**, and nearest-value matching instead of a range lookup | Reading `create_elo_dict` / `map_to_category` | `elo_to_bucket` (2.17) |
| 10 | Export declared 2 ONNX outputs; the model returns 3 | Calling the real `forward()` | 3 output names |
| 11 | Export searched for vocabulary attribute names that do not exist | Running it against the real package | Call `get_all_possible_moves()` (2.22) |
| 12 | **plan.md's 30/20/10 thresholds wrong by 2×** → 86.4% agreement with Lichess | Feeding Lichess's own evals to `classify` | 15/10/5 (2.8) |
| 13 | Proposed mate rule ("created → mistake, lost → blunder") was wrong; both appear with both labels | Widening the sample from 6 to 162 transitions | Non-mate-eval rule (2.8a) |
| 14 | Test script skipped `mate` entries, inflating an ACPL gap to 49 cp | ACPL cross-check disagreeing only for the losing side | Map mate → ±`CP_CAP`; gap fell to 0.7 cp |
| 15 | Hammering the Lichess API got the connection reset | Repeated full-sample refetches | Cache under `tests/.cache` |
| 16 | **93 MB of Maia-2 weights reloaded on every job**; 2 concurrent jobs held 2 copies | E.3 instrumentation showing per-job growth | Shared locked singleton (3.20) |
| 17 | Opening book reparsed per job (~8000 positions) | "Opening book:" logged twice in a 2-job run | Locked singleton (3.21) |
| 18 | **Summed child RSS reported a 946 MB peak vs a 512 MB budget**, recommending a paid tier on a false reading | Per-component probe disagreeing with the profiler by 5x | USS/PSS, `partial` source, no verdict (3.18) |
| 19 | Peak tracker mixed sources: an inflated `partial` peak was reported under a trustworthy `source` label | Profiler printing `source=uss` beside a 946 MB peak | Split `peak_mb` / `peak_observed_mb` (3.19) |
| 20 | First opening-book cache was unlocked, so a concurrent run still loaded it twice | Re-reading the log after the first fix | Double-checked lock (3.21) |
| 21 | **Skewers through a king reported as pins**, because `PIECE_VALUE[KING] = 0` | Unit-testing both geometries side by side | `_pin_value` (3b.4) |
| 22 | King-pins emitted twice, once as created and once as suffered | Reading the motif list on a real game | Split the two facts (3b.5) |
| 23 | **Selection ranked routine recaptures above the game's blunder** | Printing the notability ranking | Tiered selection (3b.7) |
| 24 | In-book discount dropped `...Nxd5`, a faulted book move, from the timeline | Checking every flagged move was selected | Discount only non-faulted (3b.7) |
| 25 | Selection commented 16 of 27 plies, defeating B.4's cost control | Counting selections on a short game | Proportional cap (3b.8) |
| 26 | A `sed` cleaning unused imports **deleted every blank line in partB.py** | Reading the file back | Programmatic restore of structure and docstring breaks |
| 27 | Pinned `anthropic==0.69.0`, which predates the 1.x SDK | Install resolved 1.11.0 instead | Re-pinned and verified every SDK name used |
| 28 | **`.env.example` said "copy to .env" but nothing ever read `.env`** — every documented local variable was silently ignored | Setting up the live commentary check | 15-line loader in partE; real env vars still win |
| 29 | **`max_tokens=16000` exceeded Groq's entire 8,000/min window**, so batch 1 of 2 was rejected with 413 and the blunder's comment was lost | First live run | Per-pass budgets + effort clamp (3b.18) |
| 30 | `reasoning_effort="high"` needs >4,800 output tokens; unfundable on the free tier, and strict decoding rejects the truncated JSON outright | Measuring output per effort level | `clamp_effort` (3b.18) |
| 31 | **B.1 named pieces without colour**, so ~1/3 of comments attributed pieces to the wrong player | Reading live comments against the facts | `color` on every fact (3b.15) |
| 32 | `fen_before` in the B.3 payload made the model reason from the board instead of the facts | Live comments inventing geometry | `for_prompt()` (3b.17) |
| 33 | `time_flag` reached both passes and was read as move quality — a `best` move described as "a wasted move" | Live narrative | Withheld from both (3b.17) |
| 34 | `win_drop` quoted as "14.93 cp" — a Win% swing misreported as centipawns | Live narrative | No-raw-numbers rule in B.2 |
| 35 | B.2 wrote "19.Qf7" using the ply index as a move number | Live narrative | `ply` declared index-only in the prompt |
| 36 | **Three prompt edits silently failed their anchor match** and were reported as applied | Grepping the file instead of trusting the diff | Assert on the live prompt string (3b.19) |
| 37 | **Motifs described geometry, not direction** — "the pinner is pinned" in ~1/3 of comments | Three measured runs | Role names + `benefits` (3b.20) |
| 38 | Allowlist named squares but not their occupants — "a hanging knight on e4" when a king stood there | Live comment vs the board | `pieces_on_those_squares` (3b.21) |
| 39 | **First allowlist rejected true statements**, including the engine move the prompt asks the model to cite | Testing acceptance, not just rejection | Widened to PV, kings, attackers (3b.23) |
| 40 | Retry read `x-ratelimit-reset-tokens` before `retry-after`, so 429s retried in 0.5s and gave up | A run failing with "rate limit reached after 3 attempts" | Correct precedence + 2s floor (3b.24) |
| 41 | **Groq SDK's own `max_retries=2` multiplied every attempt**, each reserving tokens — the retries caused the rate limiting | Small requests succeeding while game passes 429'd | `max_retries=0` (3b.25) |
| 42 | Rate-limit errors discarded Groq's message, which named the limit and the retry delay | Reading the raw 429 body | `_groq_limit_summary` (3b.25) |
| 43 | Retried a tokens-per-DAY cap with 65s waits, burning minutes and more quota | The 429 body saying "per day (TPD)" | Daily caps fail fast (3b.25) |
| 44 | **The "offline" regression suite made real API calls** once `.env` existed, spending quota and timing out | 40/40 dropping to 39/40 | `commentary: false` in that suite |
| 45 | `available: False` with `reason: None` when every comment was rejected | A log line reading "unavailable (None)" | `_unavailable_reason` |
| 46 | I recorded a 3-run measurement as evidence for role naming when those runs predated the change | Re-checking the claim against the transcript | Corrected in 3b.20 |
| 47 | **`select()` built its query from a dict**, so a PostgREST range collapsed to one bound — every rating window would have served any-difficulty puzzles, silently | Rendering the query string by hand | List of pairs (3c.10) |
| 48 | Stray `class AttemptOutcome(str, Enum := ...)` placeholder left in partC | pyflakes, then reading it back | Removed |
| 49 | An illegal or unreadable move reported the failure without the expected move, making the puzzle unlearnable | Testing the error paths | `expected_instead` on every failure (3c.4) |
| 50 | **Built a rating extrapolation on an assumption, then measured it at R²=0.0007** — the curve slope does not predict rating above 2000 | Fitting against 186 rated players | Removed it; report a floor (3a2.2) |
| 51 | Wrote the extrapolation into plan.md *before* measuring whether it worked | Re-reading plan.md after the calibration failed | Corrected with the measurement |
| 52 | Dev puzzle mode was unreachable — every C endpoint requires a real JWT | Testing the local sample | Triple-gated dev identity (3f.3) |

### 4.1 An upstream bug, worked around rather than fixed
maia2 **0.9.1**'s `inference_each` calls `.nonzero()` on an already
unsqueezed `(1, N)` tensor, so `.flatten()` interleaves the row index `0`
with the real column indices. It returns **twice** the legal moves, half of
them `vocab[0]` (`"a1h8"`), each with probability 0. Harmless — the bogus
entries are masked to zero — but it makes a naive dict comparison fail. The
batch path (`get_preds`) indexes `legal_moves[i]`, is 1-D, and is correct.

`tools/verify_maia2_onnx.py` therefore compares against
`_masked_softmax` + `preprocessing` (the authoritative path) and carries an
explicit check that the quirk is *still present*, so if upstream fixes it
the stale comment is flagged rather than silently outliving the bug.

## 5. Verification performed

Four runnable suites, live services included. All green.

| suite | what it proves | result |
|---|---|---|
| `tests/test_analysis_core.py` | A.1–A.6 + A.5a + E.1/E.2, no network | **45/45** |
| `tools/verify_maia2_onnx.py` | A.5 encoding, conversion, end-to-end | **12/12** |
| `tests/test_classification_vs_lichess.py` | A.3 vs Lichess's own labels | **99.80%** ordinary, **99.3%** mate |
| `tests/test_end_to_end_sources.py` | live Lichess, live Chess.com, pasted PGN | **3/3** |
| `tests/test_memory_budget.py` | E.3 cgroup parsing, admission, verdicts | **33/33** |
| `tools/profile_memory.py` | E.3 under concurrent load | 3/3 jobs, +20 MB steady state |
| `tests/test_commentary.py` | B.1 motifs/materiality, B.1b material guard, B.4b budget/salvage | **172/172** |
| `tests/test_commentary_live.py` | **real Groq calls**: auth, 413/429 handling, grounding | 9/9 comments, 4 calls, free |
| `tests/test_puzzles.py` | C.3 parsing, validation, Elo, subset stratification | **60/60** |
| `tests/test_accounts.py` | C.1/C.2/C.3 HTTP layer, cross-user isolation, E.4 board + step | **68/68** |
| `tests/test_board_ui.js` | E.4 board: clicks, toggle, promotion, flip, lock, failure modes | **39/39** |
| `tests/test_spa.py` | E.4 SPA mount: page routes, API routes, 404s, assets, traversal | **23/23** |
| `tools/check_frontend_types.py` | types.ts vs the real captured payloads | **27/27** |
| `tools/check_frontend_imports.py` | every local import resolves to a real export | **63/63** |
| `tools/check_rating_bias.py` | A.5a colour bias over 238 player-estimates | **+34 Elo — none** |
| `frontend` `npm run typecheck` | the whole TypeScript app, strict | **clean** |
| `frontend` `npm run smoke` | the BUILT bundle under jsdom, against a live backend | renders + plays a move |
| `migrations/002_rls_test.sql` | RLS policies, inside Postgres | pending a deploy |

Specifics worth keeping:

- **A.5 encoding** is byte-identical to `maia2.utils.board_to_tensor` on a
  fixed 9-position batch covering both sides to move, full and partial
  castling rights, en passant, promotions and a bare endgame.
- **ONNX vs PyTorch**: max |diff| 2.5e-05 on the policy head, 3.0e-07 on the
  value head, identical argmax on every position. Tolerance 1e-4.
- **Masking**: every row sums to 1.0 over legal moves, and the count of
  non-zero entries equals the legal-move count exactly, position by position.
- **A.3** fed Lichess's own evals over 9,524 judged plies in 120 games, so
  engine differences cannot confound the formula test. ACPL agrees to a mean
  of **0.7 cp**, 238/240 player-games within 5 cp.
- **A.1** all four adapters against real data: live Lichess, a real
  Chess.com game found by archive scan, pasted PGN, FEN. All five Chess.com
  URL spellings resolve.
- **Failure paths**: bad PGN, unknown Lichess id, unknown Chess.com user and
  a broken Stockfish path all produce failed jobs with user-safe messages
  while `/health` stays green.
- **Edge cases**: terminal/checkmate positions, mate as the final move,
  Chess960, clocks without a parseable `TimeControl`, and weights absent.

## 6. Known incomplete

### 6.1 Part B's live path is verified; quality is provider-bound
The live path now runs: real Groq calls, 9/9 comments on a 27-ply game, 4 LLM
calls, ~50s, free. `tests/test_commentary_live.py` is the harness — `--probe`
for a one-call shape check, then a full game that prints the narrative and
every comment for reading, and fails on a lost grounding check.

Eight live runs drove the factual error rate from roughly 45% of comments to
roughly 10%. The fixes that mattered were all **data** fixes (3b.15–3b.17),
not prompt tuning; two rounds of prompt tightening alone changed almost
nothing.

What remains is a model ceiling, not a defect: `openai/gpt-oss-120b` at
medium reasoning occasionally wobbles on the *direction* of a motif (who is
pinning whom). Higher reasoning is unfundable on the free tier (3b.18).
`LLM_PROVIDER=anthropic` swaps the whole layer with no code change — same
prompts, same schemas, same facts — so comment quality is a dial rather than
an architectural limit. **Whether the remaining gap is the model or the
prompts is still unmeasured**; one Anthropic run (~$0.14) would answer it.

### 4.2 Two endpoints disappeared from partE.py, and nothing noticed
`GET /api/positions` and `GET /api/positions/themes` vanished from the file.
Most likely an editor saving a stale buffer over it — `partE.py` was open in
the IDE, and the rest of the file's later edits survived, so it was a
region-sized revert rather than a wholesale overwrite.

**What makes this worth recording is how invisible it was.** At the moment of
discovery:

- `curl /api/positions` returned **200**, because the running uvicorn process
  had imported the module before the overwrite and still held the routes in
  memory.
- Every test passed. Nothing asserted that a given endpoint exists.
- `pyflakes` was clean and the file parsed — it was valid Python, just
  missing two functions.

So the only symptom was that the next restart or deploy would 404 the
Positions page. It was caught because the user asked, not because anything
flagged it.

Fixed by restoring the two endpoints, and by adding an **explicit endpoint
inventory** to `tests/test_accounts.py`: a literal set of (method, path)
pairs checked against `app.routes`. Verified by commenting the route out and
watching the test fail with `not registered: GET /api/positions`.

The inventory duplicates information that is already in the file, which is
normally a smell. It earns its place here because the failure mode is
*deletion*, and a check derived from the source cannot detect something the
source no longer says. The existing route tests were all of the form "every
registered route must also be in spa.RESERVED" — they validate consistency
among what exists, and are structurally blind to absence.

Same family as the four checks in 3e.17 that passed by not looking, with one
difference worth keeping in mind: those stubs looked at the wrong thing,
while this suite had nothing to look at and therefore saw nothing wrong.

---

### 3b.1 B.1 — the three motif bugs behind the "skewer" comment — settled
A reported comment said a knight move "now skewers the knight on g6, winning
the pawn on h7". The model had not hallucinated: `find_pins_and_skewers`
genuinely emitted `{attacker: white queen on d3, front_piece: black knight on
g6, piece_won_behind: black pawn on h7}`, and the validator passed it because
both squares were real. Three separate defects, all in the fact layer.

**1. Motifs were not diffed, so standing geometry was attributed to whatever
move came next.** `find_discovered_attacks` compares before/after;
`find_pins_and_skewers` was called on `after` only. That queen had been
aiming down the diagonal for ten moves. The facts are handed to the model
*under a move*, so they read as that move's doing, and the model duly wrote
"now skewers". `own_pinned_pieces`'s docstring even claimed the detector
"reports pins the mover *created*" — which it did not.

Now diffed on a geometry key of (motif, the three participant squares).
**Measured over 60 games / 4,956 moves: 1,395 motifs reported, 497 genuinely
created by the move — 64% were pre-existing geometry.**

**2. No materiality filter.** A knight in front of a PAWN scored as a skewer
because `_pin_value(KNIGHT) > _pin_value(PAWN)`. The prize must now be worth
taking (knight or better) and the ends must differ by ≥2. **Removes 86% of
skewers over the same 60 games** (258 → 37); absolute pins untouched.

A skewer also now requires the threat to be credible — would the front piece
actually have to move? In the reported position the queen was "attacking" a
knight defended by a pawn, so nothing was forced and Qxg6 simply loses the
queen. This test is **skewer-only**: a pin's value is the bind, not the
capture, and applying it to pins would reject a rook pinning a knight against
a queen, which is a textbook relative pin.

**3. The actual reason the move was bad was computed and thrown away.**
`analyse_game` had `after = evals[i + 1]` in scope and read only `cp_after`
from it. `after.best` is the opponent's best reply — the refutation, the
concrete explanation for almost every bad move. It now travels as
`refutation_move` / `refutation_pv` on `MoveRecord`, into `MoveFacts` **only
on faulted moves** (on a good move the same field is an ordinary reply, and a
comment would dress it up as a punishment), with the prompt requiring a
faulted comment to lead with it. Before and after on the same position:

```
before  "Black's refutation Nxf7..."            (and on an unrelated move,
                                                 a pre-existing skewer)
after   "White can reply Nxf7, forcing Kxf7, Qf3+ and Ke6, which recovers
         the pawn and erases the material advantage you gained."
```

### 3b.2 B.1 — three further defects the fix surfaced — settled
Found while implementing 3b.1, none of them in the original analysis.

**The PV allowlist had been dead past its first move.** `_squares_mentioned`
walked `[best_move] + best_pv`, but `best_pv[0]` *is* `best_move`, so the
second parse of the same SAN raised and `break` discarded the rest. Every
square beyond the first move of every engine line was unsayable, while the
comment above it claimed the whole PV was allowlisted — so comments citing a
real continuation were being rejected for naming squares the allowlist had
silently dropped. `_line_squares()` now walks a line once, inserting the lead
only when it is not already the first move.

**The refutation shipped without a colour.** Every other fact names its side
and the prompt tells the model to check it; the first live run produced
"Black's refutation Nxf7" on a move where Nxf7 was White's. `refutation_by`
is now carried explicitly and is always the opposite of `you_are`.

**Giving the model a PV invited it to analyse inside it.** A run produced
"Rd1 pins the black queen on b6 against the king, winning the queen" — three
plies deep, where no motif data exists. Walking the line showed it is not
even check there, and the real tactic pins the knight on d5. Every square in
the sentence was real, so the validator passed it. The prompt now permits
naming the moves of the line and what is won at the end of it, and forbids
claiming a tactic inside it; the PV is capped at four plies to leave less
room to narrate. This is a prompt-level mitigation, not a check — validating
tactics inside a variation would mean running the detectors at every node.

### 3b.3 B.1 — what was measured, and what was deliberately not filtered — settled
`tools/audit_motifs.py` replays cached games through the fact layer, dumps
every motif to CSV with its FEN for hand-checking, and reports counts with
the gates disabled for comparison. It needs no engine and no network.

It also stopped an over-reach. An intermediate version applied the pawn rule
to **both** ends — the prize for a skewer, the pinned piece for a pin — on
the theory that a pinned pawn was never going anywhere. It removed 270 of 346
relative pins, and the regression caught why that was wrong: it rejected
*"your bishop on d5 pins the b7 pawn to the a8 rook"*, which is true and an
entirely standard annotation. There is no labelled data here saying those are
unwanted, so filtering them would have been a guess dressed as a rule. The
rule now tests the prize end only.

**Open, and recorded rather than decided:** pin volume is still ~7.7 new pins
per game after diffing, 270 of them pawns pinned against something. Whether
that is noise needs the hand-labelled sample the audit tool was built to
produce — 40 rows marked real/relevant gives a precision number per detector.
That is the honest next step, and it may well show one detector should be
deleted rather than tuned.

**Not done, on purpose: no model training.** The failure was factual, not
stylistic — the model was handed `piece_won_behind: black pawn on h7` and
wrote exactly that. A fine-tuned model given the same input produces the same
wrong claim more fluently, and the labelled "which motifs matter" data does
not exist.

---

### 3b.4 B.1b — material is computed, never claimed (claim_guard.py) — settled
A comment said a line "would have won the exchange" when no rook was
captured by anyone, and that another line left "material even" when it ended
a pawn down. The diagnosis in three-fixes.md is right and general: **the
model was asked to explain a quantity it cannot derive from the data it was
given.** Material outcome is not readable off a list of SAN moves without
playing them, so it filled the gap with a chess cliche that matched the
shape of the line.

`claim_guard.walk_line()` now plays every line out and returns a finished
phrase; the prompt must use that phrase and make no other material claim.
`_material_problems()` rejects a comment whose claim contradicts everything
computed. Verified against the original sentence: both claims rejected.

Five defects found while wiring it, none of them in the supplied module's
design and all of them mine:

1. **The guard was weaker than the describer.** `describe_material` detects
   the exchange structurally (a rook won for a minor), but `MATERIAL_CLAIMS`
   predicates only see the net delta, so "wins the exchange" passed on ANY
   +2 line -- including two pawns, which is the exact conflation the module
   exists to prevent. Exchange claims are now checked against structural
   `exchange_won` / `exchange_lost` flags instead.
2. **`describe_material` inferred piece identity from the number.** +2 was
   rendered "wins two pawns"; in the Fried Liver it is a knight for a pawn.
   A piece is now named only when the capture list proves it, and otherwise
   the phrasing is the neutral "ends up N pawns ahead".
3. **`line_facts` did not report `truncated_at`.** A line that does not fit
   its position plays nothing, so its delta is 0 and it describes itself as
   "no material changes hands" -- a broken line turned into a confident
   false statement. Now carried out, and a truncated line is never used as a
   candidate delta.
4. **The refutation's material was measured from the wrong side.**
   `walk_line` measures from whoever moves first, which for a refutation is
   the OPPONENT. A live run produced "you lose a rook" for a line in which
   the player won one. All phrases are now flipped to the reader's point of
   view, matching `material_delta`, so the two cannot disagree on a sign.
5. **The phrase was computed over a different line than the model saw.** The
   facts used the full 6-ply PV while the prompt showed 4, so the phrase
   described a capture the model could not see -- and it guessed who won it.
   `SHOWN_PV_PLIES` is now the single length used by both.

Also added: `material_explains`. A move can be a blunder while the engine's
line still ends with the player materially ahead -- the cost is positional,
or beyond the PV. Handing "wins a rook" to the generator under a blunder
produced exactly the wrong sentence, so when the material outcome does not
point the same way as the verdict the prompt is told not to lead with it.

### 3b.5 B.3 — the fact layer grew past Groq's rate limit — settled
Adding line facts to every move pushed a move batch past Groq's per-minute
window. The failure mode is nasty: a 413 on one batch of two, silently
costing six of seven comments while the job still reported success.

Three fixes, each measured rather than guessed:

- **`for_prompt` trims the line facts** to what the model must restate
  (588 -> 196 bytes per move). The PVs were being re-sent inside them
  although they were already in the payload. Validation still sees the full
  dicts; only the prompt is trimmed.
- **Token reservations lowered** from 3,000 to 1,800 (move) / 2,200 (game).
  Groq reserves `max_completion_tokens` against the window whether used or
  not, and measured output was ~1,170 per call -- the old figure reserved
  more than twice what the pass has ever needed.
- **A 413 now has its own retry floor of 22s.** `x-ratelimit-reset-tokens`
  answers "when would a SMALL request fit", which on a 413 reads as a second
  or two; the three retries were being spent inside ten seconds. The code
  already warned about trusting that header and still applied only a 2s
  floor to it.

A full game with commentary now completes in ~110s with 7/7 comments and no
errors. That is the free tier working as intended, not a regression.

### 3a4.2 A.4 — the `rushed` flag was unreachable — settled
three-fixes.md calls this a genuine bug and it is. Reproduced directly from
the live formula:

```
spread  complexity  band     only_move   rushed can fire?
   100       0.570  medium   True        NO
   140       0.650  medium   True        NO
   150       0.670  high     True        yes
```

`rushed` needs `complexity > 0.66`, and with multipv=3 the spread term
saturates at 300cp while the other two contribute at most 0.40 -- so an
only-move position with a 100-150cp spread scored "medium". "Only move,
medium complexity" is self-contradictory on its face, and those are exactly
the positions worth flagging for a rushed decision.

Fixed with the one-line floor the document suggests: an only-move position
scores at least `ONLY_MOVE_FLOOR = 0.70`. The unnamed literal `100` that
defined "only move" in two places is now `ONLY_MOVE_SPREAD_CP`, so the flag
and the floor cannot drift apart.

Also added the conjunctions that were asked for and never existed:
`critical_rush` (hard position, played fast, AND it cost something) and
`wasted_think` (easy position, long think, AND nothing was at stake). The
consequence term is what makes either worth showing -- rushing a hard
position and finding the move anyway is not a mistake.

### 3a2.7 A.5b — the error profile ships, the Elo does not (rating_profile.py) — settled
`performance_rating` measures the RESULT, not the play, and needs the
opponent's rating as input. The replacement reports the error profile --
blunders, ACPL and engine agreement **per decision** rather than per move --
and scores it as a posterior over rating bands.

The denominator is the substance. A move is a decision only when going wrong
was possible: more than one legal move, not an only-move, not still in book,
position not already decided. Measured on a real game: **24-28 decisions of
43 moves**, which is a usable basis. On a theory-heavy miniature it reported
**1 decision of 11 moves** -- book through ply 16 plus two only-moves -- and
that is the correct answer, not a bug.

**The Elo is deliberately not shown.** The module refuses while
`fitted: false`, and the shipped calibration table is a placeholder whose
own columns are mutually inconsistent (the document demonstrates this: an
ACPL of 110 implies 16 blunders/100 against a table value of 9). That
refusal is preserved. The UI leads with the profile, which is defensible,
and prints the refusal reason where the Elo would be.

Fitting it needs labelled games with features from this pipeline, and the
cache available here is 99% players above Maia-2's ceiling (3a2.5) -- so the
honest position is unchanged: **the rating estimate remains unvalidated in
the band it is used in**, and now there are two unfitted estimators saying so
rather than one silently guessing.

---

### 3c.11 C.1b — real accounts, in SQLite (accounts.py) — settled
Supabase was the only account system, so a deployment without it had no
accounts and the fallback was a dict in process memory: a restart wiped every
rating and every solved puzzle. That is not an account.

`accounts.py` is the same contract backed by SQLite -- one file, no server,
survives a restart. The Supabase path is untouched and still wins when it is
configured; this takes over when it is not, and `_dev_user_allowed()` now
also switches OFF when local accounts work, because an anonymous bypass
would quietly give every visitor the same shared progress.

Decisions worth keeping:

- **scrypt, not PBKDF2 or bcrypt.** Memory-hard is what makes a GPU attack
  expensive, and it is in the standard library -- bcrypt and argon2 are
  third-party and this has to build in a 512 MB image with no toolchain.
  n = 2**14 measured at ~30 ms, and the binding constraint is MEMORY, not
  time: scrypt uses 128*n*r bytes, so that is 16 MB per concurrent hash and
  2**15 would put two simultaneous logins at 64 MB. The parameters are
  stored per row so they can be raised on bigger hardware without locking
  anyone out.
- **The database stores the HASH of a session token, never the token.** Same
  reasoning as the password: a leaked backup must not be a set of working
  logins.
- **A failed login hashes anyway when the user does not exist**, and returns
  the identical message either way, so the endpoint cannot be used to find
  out which usernames are registered.
- **The attempt and the stats update are one transaction**, the same
  guarantee C.2 gets from a Postgres function. A rating that disagrees with
  its own history is worse than no rating.
- **No email, no password reset, no OAuth.** A reset flow needs a mail path
  and an out-of-band token, and a bad one is worse than none. The
  consequence is stated at signup rather than discovered later: a forgotten
  password cannot be recovered.

The token lives in `localStorage`, which is readable by any script on the
origin -- an XSS bug becomes session theft. The alternative, an httpOnly
cookie, needs CSRF handling the single-origin deploy does not have yet.
Sessions are revocable server-side and expire in 30 days, which bounds it.
Recorded as a known trade, not an oversight.

### 3d.1 D.1 — bots, and why the opening book was not optional — settled
Part D was the designated cut and the Play page had been an honest "not
implemented". It is built now, on weights already in the image.

**Maia-2 rather than a weakened engine.** The usual weak bot is a hobbled
strong one -- shallow depth, random blunders, a skill knob -- which produces
long stretches of engine-perfect play punctuated by a piece dropped for no
reason. Nobody plays like that, so practising against it teaches the wrong
reflexes. Maia-2 is a policy trained on human games at eleven bands; asking
it for a move at a band IS a human-like opponent of that strength. The ONNX
session is already loaded once per process for A.5, so the bots cost no
extra memory. Five opponents, not eleven: adjacent bands are not
distinguishable in a single game.

**Two filters on the policy, both found by playing it rather than reading
it:**

1. *Repetition.* Self-play produced `Nf3 Nf6 Nc3 Nc6 Ng1 Nb8` repeating
   forever. The policy has no memory, so nothing stops it answering Nf3 with
   Ng1. Candidate moves that repeat a position the game has already had are
   dropped when there is any alternative.
2. *Opening book.* Even with that, the first moves were wrong: measured at
   band 1700 the starting position gives **Nf3 0.394, Nc3 0.211, e4 0.046**,
   which is not human play. The model is not broken -- mate-in-one in the
   same check scores **0.989** -- it is being asked outside its training
   distribution, because Maia models are trained with the opening plies
   skipped (those moves are memorised, not chosen). So while the game is in
   known theory the choice is restricted to moves that STAY in theory, using
   A.6's existing book via a new `OpeningBook.contains()`. Maia still has
   preferences; they are just confined to lines people play.

Temperatures are below 1 on purpose. At 1.0 the sampler follows the policy
exactly, which sounds right and is not: the tail holds moves a player of
that band would consider and reject. The weakness should come from the
BAND -- already trained on how those players move -- not from extra noise on
top of it.

Games live in memory with a TTL and are deliberately not persisted: losing
one costs a few minutes, and persisting them means a schema and a cleanup
job for something nobody asked to resume.

### 3c.12 C.3 — 2,000 real puzzles — settled
The local set was eleven hand-verified samples. `tools/build_puzzle_subset.py`
already existed; it needed the 250 MB Lichess dump (downloaded to /tmp, never
into the iCloud-synced project folder) and `zstandard`, which was in
requirements.txt but not installed.

The result is 2,000 puzzles, 406 KB, stratified across eight rating bands
from under 1000 to 2500+ -- not a random sample, which would mirror the
dump's middle-heavy distribution and leave a 2200 player with nothing.

One trap: the subset CSV is **not** the same shape as the raw dump. Its FEN
is already past the opponent's setup move and the line is split into the
player's moves and the replies, so parsing it with `Puzzle.from_row` would
push a move that has already been played. `Puzzle.from_subset_row` is a
separate parser for that reason, and all 2,000 were verified to parse with a
legal first solution move.

`features.puzzles_dev_sample` now means what it says -- the fallback sample
is in use -- so with the subset built the UI stops warning about a set that
is no longer small.

### 3e.18 E.4 — Positions removed — settled
Cut on request. The page, its CSS, `GET /api/positions`,
`/api/positions/themes`, the client methods and the nav entry are all gone;
`/positions` now falls through to the SPA and redirects, so old links do not
404. The endpoint inventory in `tests/test_accounts.py` was updated in the
same change, which is the test that would otherwise have caught the removal
as a regression.

---

### 3c.13 C.1c — saved analyses, and the Study tab — settled
Analyses were computed and thrown away: jobs live in memory with a one-hour
TTL, so closing the tab lost the work. They are now kept, up to 30 per user,
in the same SQLite store as the accounts.

The schema follows the design sketched when this was asked about:

- **Denormalised list columns** (`white`, `black`, `result`, `played_at`,
  `accuracy_white/black`, `plies`) next to the blob. The list view needs
  exactly those; fetching thirty 80-150 KB reports to render a list would be
  megabytes of work for a few hundred bytes of output.
- **zlib-compressed JSON**, measured at roughly a tenth of the raw size, so
  30 games is well under half a megabyte per user -- which matters on a
  512 MB box.
- **The cap is enforced on write, inside the insert's transaction.** A
  separate cleanup job is a second thing that can fail, and between the two
  the user would briefly have 31.
- **A unique index on `(user_id, source_ref)`**, so re-analysing a game
  UPDATES that row. Without it, re-running one game would evict someone's
  oldest analysis for no reason. A pasted PGN has no URL, so its moves are
  hashed to give it a stable identity.
- **`depth` and `prompt_version` travel with the report**, because a saved
  analysis is a SNAPSHOT rather than a live query -- that is the right
  choice (it is reproducible and immune to later depth or prompt changes),
  but it means a stale one must be identifiable rather than merely old.

Two naming decisions worth recording. `get_analysis` returns the report
under **`report`**, not `result`, because the summary already uses `result`
for the game's outcome ("1-0") -- reusing it produced a field that was a
string in one shape and an object in the other, which the frontend's type
checker refused outright. And a `DELETE` endpoint exists rather than relying
on the cap, because the cap evicts the OLDEST, which is not necessarily the
one the user wants gone.

**The Study tab only exists when signed in.** Without an account there is
nowhere to keep anything, and a tab that is permanently empty is worse than
no tab. Signed out, `/study` still resolves and shows the sign-in form, so a
shared link is not a dead end.

One check earned its place immediately: the test tried to delete the first
analysis it saved and failed, because the cap had already evicted it. The
test was wrong and the behaviour was right -- it now asserts the eviction
explicitly and checks isolation on a row that still exists.

`AnalysisSummary`'s denormalised fields are optional in `types.ts` and
recorded in the type checker's IGNORED list with the reason: the captured
example comes from the Supabase path, which returns the thinner row, while
the local store returns the full one.

---

### 3b.6 B.4b — the truncation failure, and the one fix that was not available — settled
Reported as moves 22-39 carrying labels but no comments, with an error string
that turned out to be three batch failures joined together. Each had silently
dropped its whole batch:

```python
except CommentaryError as exc:
    errors.append(str(exc)); continue        # ~5 moves of commentary, gone
```

**The mechanism.** Both providers use strict JSON-schema decoding. The model
emits one object, hits `max_completion_tokens` mid-object, and strict
decoding cannot produce a valid object -- so the request is rejected
outright. You are billed for the full output and receive nothing, including
the comments that had already finished. A JSON object truncated at 95% is
worth exactly as much as one truncated at 5%.

**The analysis asked which value was live, and the answer changed the fix.**
It had `move_pass_tokens = 3000` and the error reported 1800. 1800 is live
and deliberate: it was lowered from 3000 in 3b.5 because Groq reserves
`max_completion_tokens` against the 8,000/minute window whether or not they
are used, and 3000 made two calls in a minute impossible -- a 413 that cost
six of seven comments. So **raising the budget is not available**: the window
constrains it from the other side. Only the fixes that reduce demand apply,
which is also the order the document ranked them in.

Four changes:

1. **`effort="low"` on the move pass.** Reasoning tokens are billed as output
   and spent BEFORE any JSON is emitted, so at medium they were the majority
   of the budget. B.2 synthesises an arc across forty moves and needs to
   reason; B.3 restates facts already computed. Measured on gpt-oss-120b:
   low 647, medium 1,402.
2. **`maxLength: 240` in MOVE_SCHEMA.** Without a bound the model decides how
   long a comment is, which makes the budget uncomputable -- and an over-long
   one is not trimmed, it destroys the batch. Same change the commentary work
   wanted for a different reason (3b.4): the verbosity carrying the
   fabricated material claims is the verbosity blowing the budget.
3. **Budget scaled to the batch**, via `batch_budget.move_pass_budget`, with
   the provider ceiling as a clamp. The constant left ~16% headroom against a
   cost that is not deterministic and that GROWS with how much there is to
   say -- so the batches that failed were the eventful ones, which is exactly
   where the commentary was worth having.
4. **Split and retry rather than drop.** A truncated batch is a batch too big
   for this budget, not a permanent failure; halving it almost always clears.
   Non-truncation errors are NOT split -- retrying a dead key or an exhausted
   daily quota just burns what is left.

Measured effect at the live 1800 ceiling: a batch of 5 needs **1026 tokens at
low effort instead of 1800**, and the largest safe batch rises from 4 to 8.
The 86-ply game that previously produced zero comments now produces 17 of 18,
with no errors and no gaps, and the comments are a third of their former
length.

Two reporting fixes, both small and both about legibility: **per-move gap
placeholders** rendered inline in the move list (silence is indistinguishable
from the feature not working and cannot be debugged from outside), and
**deduped error strings** (three batches failing identically read as three
different problems).

**Known limit, not fixed:** the material lexicon in 3b.4 checks phrases like
"wins a pawn" and "N pawns ahead" but not every paraphrase -- a comment
saying "a four-pawn advantage" is not currently matched. The guard narrows
the error surface; it does not close it, and that was already true of the
square validator.

### 3e.19 plan.md brought in line with what was built
The plan still said Part D was the designated cut, listed Positions as a tab,
and described a puzzle subset that had not been built. Amended rather than
rewritten, so the original intent and the correction both stay visible:

- **Part D**: D.1 (bots) is built and came in under estimate because the
  Maia-2 session was already loaded for A.5. D.2 was built as Positions and
  then removed on request -- it duplicated Puzzles. **Study** took the tab.
- **C.1 ("Supabase Auth. Do not write your own.")** kept, with an amendment.
  The advice is right and it shaped the scope of `accounts.py`: the parts
  that are genuinely hard -- reset flows, email verification -- are the parts
  deliberately NOT implemented, and the limitation is stated to the user at
  signup rather than half-built.
- **C.3**: 2,000 puzzles, stratified; plus the two traps worth carrying
  forward (the subset file is not the raw-dump shape, and an interactive
  board needs a non-rating `step` endpoint).
- **B.4b** added: the output-budget rules above, written as rules rather than
  as an incident, since every one of them cost real comments.

---

### 3a2.8 A.5 — two rating estimates removed; one survives, with its uncertainty — settled
Requested, with the reason: "they are not being accurate." Correct on both
counts, and both were things this project built and then had to measure.

**`performance_rating` removed.** `opponent + 400·(W−L)/N` is a real formula,
not an invented mapping -- but with N = 1 it has exactly three possible
outputs, and it measures **the result, not the play**. A 1200 who wins
because a queen was hung scores opponent+400; a 2000 who outplays everyone
and flags scores opponent−400. It also needs the opponent's rating as input,
so most of what it reported was what it was told.

**`profile_rating` and the whole calibration layer removed.** The design was
sound -- a posterior over rating bands, with the interval derived from the
evidence rather than fixed. What it needed was a fitted calibration table,
and that was never available: the placeholder values were not even mutually
consistent (an ACPL of 110 implies ~16 blunders per 100 against a table value
of 9). It correctly refused to report a number, which meant **the UI showed a
refusal where a rating should have been** -- worse than showing nothing. A
feature that is honest about being unusable is still unusable.

Removed with it: `models/rating_calibration.json`, `tools/calibrate_rating.py`,
`rating_profile`'s entire estimator half (281 lines), and A.5a's dead
extrapolation constants -- a table of zeros that nothing read. Keeping them
"for the record" was the wrong instinct: a negative result belongs in this
file, where it is read, not in a module where the next person has to work out
that it is dead.

**What survives, and why it is the right one to keep.** Maia-2's estimate is
the only one that reports its own uncertainty: a likelihood curve over eleven
bands, from which `likelihood_interval` derives a range rather than assuming
one. On a single game that curve is usually nearly flat and the UI says so --
"Very rough: 1100+". The other two promised a precision they could not
support, which is exactly the complaint.

**The error profile stays and is now the headline.** "4.2 blunders per 100
real decisions, ACPL 62, engine-best agreement 34%" is a MEASUREMENT. It
needs no calibration to be true, and the per-decision denominator is what
makes it comparable across games. That distinction -- measurement versus
estimate -- is the whole basis of what was cut and what was kept.

**Unchanged and still open:** the surviving estimate is unvalidated in the
1100-1999 band it is actually used in (3a2.5). Removing the other two does
not fix that; it just stops three numbers of unknown quality from
corroborating each other.

This is the third time in this project a number was published more
confidently than the measurement supported (after 3a2.2's extrapolation and
3a2.4's bare point estimate). The pattern is consistent enough to be worth
stating once: **when a number cannot report its own uncertainty, that is the
signal it should not be reported at all.**

### 4.3 E.2 — accounts silently broke the progress stream
Found while verifying the rating removal, not by anything that was looking
for it.

`client.ts` carried the note "job streams accept anonymous access", which was
true when it was written. Once jobs had owners (C.1b), `_owned_job` began
404ing any request whose caller did not match -- and **EventSource cannot
send an Authorization header**, so every signed-in user's own analysis stream
404ed. The UI fell back to 3-second polling and showed "queued" while the
analysis ran to completion behind it.

Fixed by letting the SSE endpoint alone authenticate on the job id, which is
32 hex characters from `secrets` -- the same id-as-capability rule already
relied on for anonymous jobs. The alternatives were a token in the query
string, which every proxy in the path logs, or moving to a fetch-based
reader, which is the right long-term answer and not a small change. What the
relaxation exposes is one analysis of a public game, for an hour, to whoever
already has an unguessable id.

**The smoke harness had been hiding it.** Its EventSource shim never checked
`res.ok`, so a 404 looked like a stream that opened and ended, and the page
sitting on "queued" read as "still working". It now reports a non-OK stream
as an error. That is the fifth time a check passed by not looking (3e.17),
and the first where the thing it failed to see was a real production
regression rather than a bug in the code under test.

---

## 7. Still open (not Part A)

Carried forward from `plan.md` §8, unchanged:

- **§8.3 LLM provider and commenting threshold** (Part B). `LLM_API_KEY` is
  read and reported through `/config.features.commentary`; nothing depends on
  the choice yet.
- **§8.4 Puzzle subset size and selection** (Part C). `.gitignore` reserves
  `data/puzzles/`; `zstandard` is already in `requirements.txt` for the `.zst`
  dump.
- **§8.5 Position generator interpretation** (Part D — the cut candidate).
- **§8.7 Render free vs. $7 Starter.** Still open, but no longer guesswork:
  `/ops/memory` now answers it with a measured `verdict`. Needs one deploy
  plus a load run.

Opened since, and not carried from §8:

- **The rating estimate is unvalidated in the 1100–1999 band it is used in**
  (3a2.5). Every measurement of it so far — the colour-bias run and the
  R² = 0.0007 extrapolation fit — drew on a cache that is 99% players above
  Maia-2's `>=2000` ceiling, median 3023. Closing this needs a sample of
  sub-2000 rated games with known ratings; `tools/check_rating_bias.py`
  would then measure it unchanged. This is the largest unknown left in Part
  A, and it is a gap in *evidence*, not a known defect.

---

## 8. Local setup, not committed

Three things live on disk and none are committed:

- **`.venv/`** — Python 3.14 runtime venv, plus a brew-installed Stockfish.
  `requirements.txt` pins what production needs; the Dockerfile installs its
  own Stockfish at `/usr/games/stockfish`.
- **`.venv-export/`** — Python 3.12 with `torch==2.4.0`, for the offline A.5
  export and verification only (2.23).
- **`maia2_models/`** — 273 MB of maia2 `.pt` weights and config, downloaded
  by `from_pretrained()` for the export. Gitignored.
- **`tests/.cache/`** — 120 cached Lichess-analysed games, so the label test
  reruns offline. Refetch with `--refresh`.

Reproducing A.5 from scratch:

```
python3.12 -m venv .venv-export
.venv-export/bin/pip install torch==2.4.0 maia2 onnx onnxruntime chess==1.10.0
.venv-export/bin/python tools/export_maia2_onnx.py --out models/ --type rapid
.venv-export/bin/python tools/verify_maia2_onnx.py --out models/
```
