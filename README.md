# ChessVision

A chess game analyser that tells you not just *which* move went wrong, but *why*, and
grounds every sentence it writes in something the engine actually found.

**[Open it](https://askretik.github.io/chessvision/)**

Paste a Lichess or Chess.com link, drop in a PGN, or set up any position from a FEN.
Stockfish grades every move — best, inaccuracy, mistake, blunder — using Lichess's
published Win% and Accuracy formulas, so the numbers line up with the site most players
already trust. A win-probability graph shows where the game turned. Maia-2, a network
trained on human games, estimates the strength each side actually played at. Then a
language model writes a coaching comment on the moments that mattered.

Around the report: an adaptive puzzle trainer drawn from the Lichess CC0 puzzle database,
five sparring bots from 1100 to full Stockfish, and accounts that keep your puzzle rating
and a library of saved analyses.

The part worth looking at is that **the commentary is not allowed to make things up**.
Chess.com's comments feel cheap because they are templates with no grounding in what
happened on the board. ChessVision builds the comments in three layers: a deterministic
fact extractor (material, hanging pieces, forks, pins, skewers, king safety, the engine's
line), one game-level LLM pass that finds the narrative arc and each player's recurring
weakness, and a batched move-level pass that writes the comment from those facts. A claim
guard then re-checks the material arithmetic of every line the model describes, and
rejects any comment that says a sequence "wins the exchange" when it doesn't.

**The rating estimate is a likelihood, not a guess.** Maia-2 is run per rating band over
every decision in the game, and the estimate is the band that makes the moves you played
most probable. Error counts are taken *per decision* rather than per move, so a 70-move
endgame grind and a 25-move miniature are comparable at the same strength.

---

## Run it

```sh
# backend (Python 3.12, Stockfish on PATH)
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # LLM key optional; without it, comments are switched off
uvicorn partE:app --reload      # http://127.0.0.1:8000

# frontend (Node 18+)
cd frontend
npm install
npm run dev                     # http://localhost:5173, proxies the API to :8000
```

```sh
npm run build                   # builds the SPA into ../static, served by partE.py
npm run typecheck
npm run smoke                   # mounts the built bundle under jsdom against a live backend
pytest tests/                   # analysis core, classification vs Lichess, puzzles, accounts
```

The rating model (`models/maia2.onnx`, 93 MB) is not in the repo — GitHub's file limit is
100 MB. `tools/export_maia2_onnx.py` produces it; [models/README.md](models/README.md)
explains how the Docker build fetches it from a release asset. Without it, everything
else works and the rating panel says why it is missing.

### Pages

`/` landing page · `/analyse` new analysis · `/analyse/:jobId` a live or finished report ·
`/puzzles` trainer · `/play` bots · `/study` saved analyses (signed in).

A job id is a real URL, not component state: the backend keeps a job for an hour and the
event stream replays from the start, so a report link can be shared or reloaded mid-run.

---

## How a report is built

| stage | what runs | where |
|---|---|---|
| import | Lichess export API, Chess.com monthly archive search, PGN parse, FEN | `partA.py` A.1 |
| engine | Stockfish over UCI, MultiPV, FEN-keyed cache | `partA.py` A.2 |
| grading | Lichess Win% and Accuracy formulas, move classification | `partA.py` A.3 |
| time | MultiPV complexity against `%clk` deltas, when the PGN has clocks | `partA.py` A.4 |
| rating | Maia-2 via onnxruntime, max-likelihood over rating bands | `partA.py` A.5, `rating_profile.py` |
| phase | phase verdict, ECO code, the move that left book | `partA.py` A.6 |
| facts | deterministic motif and material extraction, no LLM | `partB.py` B.1 |
| comments | one game-level pass, batched move-level passes | `partB.py` B.2–B.3 |
| guard | material arithmetic along every claimed line | `claim_guard.py` |

The report streams over server-sent events, so the board and move list fill in while the
engine is still working. If the stream drops, the client falls back to polling.

**The client ships no chess rules.** Every board asks `GET /board/moves` for the legal
moves and the position each one reaches, so playing a move is a lookup and the server
stays the single source of truth.

---

## Layout

```
partA.py          analysis core: adapters, engine, grading, time, rating, phase
partB.py          the commentary agent: facts, game pass, move pass
claim_guard.py    material arithmetic, and the guard on what the LLM may claim
batch_budget.py   token budgeting and salvage, so a truncated batch loses nothing
rating_profile.py per-decision error profile behind the rating estimate
partC.py          puzzles: the Lichess CC0 set, served one step at a time
accounts.py       local accounts on SQLite: username, password, puzzle rating
firestore_store.py  the same accounts in Cloud Firestore, over REST (used on Render)
bots.py           Maia-2 bots at four strengths, plus Stockfish
partE.py          FastAPI app: jobs, SSE, config, memory budget, rate limits
spa.py            serves the built frontend from the same app
frontend/         React + Vite + TypeScript
  src/routes      Landing, Analyse, GameView, Puzzles, Play, Study
  src/components  Board, EvalBar, EvalGraph, MoveList, TimePanel, ...
  src/styles      tokens.css -- the only file that names a colour
migrations/       Postgres schema with Row Level Security (Supabase path)
data/openings/    ECO tables, CC0
tools/            offline only: ONNX export, puzzle subsetting, contract checks
tests/            pytest suites plus the board UI test
docs/             API contract, OpenAPI schema, captured real payloads
harness/          a no-framework dev page at /harness that works when the SPA won't build
```

The frontend's types are checked against real captured payloads in `docs/examples/` by
`tools/check_frontend_types.py`, because a hand-written interface that is simply wrong
passes the type checker every time.

---

## Deploying

The frontend is live at **https://askretik.github.io/chessvision/**.

Every push to `main` runs `.github/workflows/pages.yml`, which builds the frontend under
the `/chessvision/` base path and publishes it to GitHub Pages, with `404.html` as the
SPA fallback for deep links.

GitHub Pages serves static files only. The landing page is fully static and always
works; the analysis, puzzle and play pages need the Python backend. That deploys as one
Docker image to Render (`render.yaml`, free tier, 512 MB — the memory budget is the
binding constraint and is enforced at runtime). To connect the two:

1. Deploy the backend and set `FRONTEND_ORIGIN=https://askretik.github.io` so CORS
   allows the Pages site.
2. In the GitHub repo, add the Actions variable `VITE_API_BASE` with the backend URL and
   re-run the Pages workflow.

Until then the app pages say the engine is offline instead of spinning forever. The
Docker image can also serve the frontend and API together from one origin, with no CORS
at all.

**Accounts survive Render's free tier through Firestore.** Render's free disk is wiped
whenever the service sleeps, so with `FIREBASE_SERVICE_ACCOUNT` set, accounts, puzzle
progress and saved analyses are stored in Cloud Firestore instead (`firestore_store.py`,
Firebase's free Spark plan, no payment method). It talks to Firestore's REST API with
httpx rather than Google's SDK, which would cost tens of MB on a 512 MB instance.
`GET /ready` reports where accounts live and whether that store answers.

---

Chess pieces by Colin M.L. Burnett, CC BY-SA 3.0. Puzzles and opening tables from
Lichess, CC0. Accuracy and Win% use Lichess's published formulas.
