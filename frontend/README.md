# Frontend — Part E.4

Companion to the UI spec doc, now wired to `partE.py`.

**Integration notes are at the bottom** — what changed from the scaffold and
why, including two things the scaffold had wrong about the backend.

Nothing here has been compiled: the machine it was integrated on has no Node.
`npm install && npm run typecheck` is the first thing to run.

## What is real

| File | Status |
| --- | --- |
| `src/styles/tokens.css` | **Complete.** Both themes, every ratio measured. Untouched. |
| `src/api/types.ts` | **Rewritten** against the real payloads in `docs/examples/`. Checked by `tools/check_frontend_types.py`. |
| `src/api/client.ts` | **Rewritten.** Real endpoints and envelopes; `/board/moves` and `/puzzles/{id}/step` added. |
| `src/hooks/useAnalysisJob.ts` | **Corrected.** The real SSE frame shapes (see below). |
| `src/hooks/useBoardMoves.ts` | **New.** Legal moves from the server, so the client ships no chess rules. |
| `src/components/Board.tsx` | **Working**, plus `isPromotion` and `busy` props. Pieces are Unicode placeholders; premove is stubbed. |
| `src/context/ConfigContext.tsx` | **Complete**, plus `useLimits` and `useSupabase`. |
| `../spa.py` | **Fixed.** Its `RESERVED` list 404ed the page routes — see below. |
| `src/routes/Analyse.tsx` | **Working.** Source form, progressive render, navigable board, move list, report strip. Layout is not the spec's. |
| `src/routes/Puzzles.tsx` | **Working.** Board-driven, one move at a time, server-validated. |
| `src/routes/Play.tsx` | **Stub.** No backend exists (Part D, cut). |
| `src/routes/Positions.tsx` | **Stub.** Needs `GET /api/positions`, which does not exist. |

## Layout — as integrated

```
Project 2/
├─ partE.py            install_spa(app) is the LAST thing it does
├─ spa.py              static mount + SPA fallback
├─ static/             Vite build output. Gitignored, emptied on every build.
├─ harness/            the no-framework dev page, served at /harness
├─ docs/examples/      real payloads; types.ts is checked against these
└─ frontend/           this directory
```

The dev harness moved out of `static/` and off `/`, because Vite owns
`static/` and `emptyOutDir` deletes whatever is in it. It now lives at
`/harness` and still works when the SPA will not build — which is why it was
kept rather than deleted.

## Route collisions — what actually happens

The scaffold's table was right about the mechanism and wrong about the
result. `spa.py`'s catch-all checked `head in RESERVED` **first**, and
`analyse` and `puzzles` were both in that list, so every page deep-link —
`GET /puzzles`, `GET /analyse/{jobId}` — returned a JSON 404 before the SPA
ever saw it.

Those two are now exempt (`SHARED_WITH_SPA` in `spa.py`), and they need no
entry at all: FastAPI matches a registered route before a catch-all, so
everything real under those prefixes already wins.

| Path | Resolution |
| --- | --- |
| `POST /analyse` | A different method. Never reaches the catch-all. |
| `GET /analyse`, `/analyse/:jobId` | Match no route → the SPA. |
| `GET /puzzles/next` | Registered and more specific → the API. |
| `POST /puzzles/:id/step`, `.../attempt` | Registered → the API. |
| `GET /puzzles` | Matches no route → the SPA. |
| `GET /positions`, `/play/:id` | **Genuinely ambiguous.** Register the endpoints the spec adds under `/api/`. |

`tests/test_spa.py` asserts all of it, and `tests/test_accounts.py` fails if
a new API prefix is missing from either `spa.RESERVED` or the Vite proxy.

## Development

Node is required and is not installed on the machine this was integrated on:

```bash
brew install node          # or: https://nodejs.org
```

Then two processes:

```bash
# terminal 1 — from the repo root
.venv/bin/python -m uvicorn partE:app --reload --port 8000

# terminal 2
cd frontend && npm install && npm run dev     # :5173
```

Open `http://localhost:5173`. The Vite proxy forwards API paths to :8000 and
keeps the browser on one origin, so no CORS.

**Run the typecheck first.** Nothing here has been compiled:

```bash
cd frontend && npm run typecheck
```

Two Python checks stand in for what `tsc` would have caught, and both pass:

```bash
.venv/bin/python tools/check_frontend_types.py     # 26/26 interfaces
.venv/bin/python tools/check_frontend_imports.py   # 38/38 imports
```

The first diffs every interface in `types.ts` against the real captured
payloads; the second resolves every local import against what the target
actually exports. Neither is a type checker.

## Build and deploy

This project deploys as a **Docker** image (`render.yaml` → `runtime: docker`),
and `python:3.12-slim` has no Node — so the build happens in a separate stage
rather than in a Render build command:

```dockerfile
FROM node:20-slim AS frontend
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi
COPY frontend/ ./
RUN npm run build          # writes /static

# ... in the runtime stage:
COPY --from=frontend /static /app/static
```

Two consequences worth knowing:

- **A type error fails the deploy**, because `npm run build` is
  `tsc -b && vite build`. That is the right behaviour, but it means the first
  Docker build is also the first compile. Run `npm run typecheck` locally.
- **Commit `package-lock.json`** once you have run `npm install`. The
  Dockerfile falls back to `npm install` without it, which does not pin
  transitive versions.

`static/` is gitignored and in `.dockerignore`, so a local build can never
shadow the one the image builds.

## What the scaffold had wrong about the backend

Found by reading the real payloads, not by reasoning:

1. **`types.ts` named fields the backend does not send.** It had
   `classification`, `eval_cp`, `win_pct`, `clock_ms`, `time_spent_ms` and a
   flat `comments: Record<number, string>`. The backend calls those `label`,
   `cp_before`/`cp_after`, `win_before`/`win_after`, `clock_after`,
   `time_spent`, and sends `comments` as an array of objects. Every one of
   those reads as `undefined` at runtime with no error anywhere — so
   `tools/check_frontend_types.py` now fails on any such drift.

2. **`MoveClass` listed `book`.** There is no such label: partA classifies an
   opening move on its merits and reports book membership separately, in
   `Report.opening`. The real sixth value is `excellent`.

3. **`attempt()` sent one move.** `POST /puzzles/{id}/attempt` takes the whole
   line (`{moves: [...]}`), because it validates a line, not a move.

4. **`/puzzles/next` returns an envelope**, not a bare puzzle:
   `{puzzle, your_rating, rating_window}`.

5. **The SSE frames are not wrapped.** `analysis` and `result` carry the
   report at the top level, and `error` carries `error`, not `detail` — so the
   hook was reporting every failed job as "Analysis failed".

6. **`spa.py`'s `RESERVED` 404ed the page routes.** Covered above.

7. **The Vite proxy was missing `/board` and `/puzzles/{id}/step`**, so the
   board would have had no legal moves and every puzzle would have been
   unplayable in dev — both looking like frontend bugs.

Three things the scaffold asked for were added to the backend rather than
worked around: `reasons` and `limits` on `/config` (its own "Known gaps" note
asked for the limits), and `fen_after` on each move so a board can step
through a game without a rules engine.

## Known gaps

- **Pieces are Unicode glyphs.** Replace with the cburnett SVG set (CC-BY-SA,
  lichess's default) before shipping, and attribute it. The `.piece` font-size
  hack in `Board.css` disappears with them.
- **Premove is stubbed.** `Board` accepts the prop and queues one move, but
  execution-on-turn-arrival belongs to the Play page, which has no backend.
- ~~`/config` does not expose the numeric limits.~~ **Done.** `/config` now
  carries `limits` (rate limit, comment cap, job TTL, depth and multipv
  bounds, max moves per attempt) and `reasons`. `ANALYSE_DEPTH_*` in
  `partE.py` are the single source of truth, validated by the request model
  and published here, so the depth slider cannot offer a value the server
  rejects.
- **Nothing is compiled.** No Node on the integration machine. The two Python
  checkers are not a substitute for `tsc`.
- **Page layouts are not the spec's.** `Analyse` and `Puzzles` are built to
  working, not to the design: the spec doc describing the layouts was not
  available here. Tokens, spacing and the colour code are the spec's
  throughout; the arrangement is mine and is meant to be replaced.
- **`chess.js` is an unused dependency.** The client holds no chess rules —
  legality comes from `GET /board/moves`. It is left in `package.json` for
  the Positions editor, which needs local placement rules. Drop it if that
  page stays cut.
- **EventSource cannot send an `Authorization` header.** Fine today because
  job streams accept anonymous access. If that changes, move
  `useAnalysisJob` to a fetch-based reader rather than putting a token in the
  query string.
