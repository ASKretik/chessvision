"""Serve the built SPA from the same FastAPI app as the API.

Drop this file next to partE.py and add two lines at the very END of it,
after every route and router is registered:

    from spa import install_spa
    install_spa(app)

ORDER MATTERS. The catch-all below claims any unmatched GET path, so
anything registered after it becomes unreachable. If you later add a router,
add it above this call.

This module touches nothing inside partE.py and adds no dependency beyond
fastapi.staticfiles, which FastAPI already ships.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# Vite writes here (frontend/vite.config.ts -> build.outDir = '../static').
STATIC_DIR = Path(__file__).resolve().parent / "static"

# First path segments the SPA must never claim, even on GET. An unmatched
# path under one of these is a JSON 404, because returning index.html would
# hand a JSON client an HTML body it cannot parse.
#
# `analyse` and `puzzles` are deliberately NOT here, though the scaffold
# listed them. They are the two prefixes a page and an API share, and
# reserving them broke every deep link into the app: GET /puzzles and
# GET /analyse/{jobId} are pages, and this catch-all 404ed them before the
# SPA ever saw them. Measured, not reasoned about -- tests/test_spa.py.
#
# They need no entry, because FastAPI matches a registered route before a
# catch-all. Everything real under those prefixes is already registered and
# already wins:
#
#   POST /analyse                  a different method; never reaches here
#   GET  /puzzles/next             registered, more specific
#   POST /puzzles/{id}/step        registered
#   POST /puzzles/{id}/attempt     registered
#
# So only paths that match no route at all arrive here, and under those two
# prefixes those are page routes by definition.
#
# The endpoints the spec ADDS (positions, play) cannot be resolved that way,
# because there GET is genuinely ambiguous: GET /positions is both a page and
# a query, and GET /play/{id} is both a page and a game state. Register those
# under /api/ instead. That is the only route convention this adds.
#
# `board`, `ops` and `harness` were added after checking the list against
# partE.py's real routes: /board/moves is the move source every board reads,
# and /harness is the no-framework dev page, which must keep answering when
# the SPA build is missing or broken. tests/test_accounts.py asserts that
# every API prefix appears here, so a new route cannot be forgotten.
RESERVED = frozenset(
    {
        "config",
        "health",
        "ready",
        "jobs",
        "me",
        "board",
        "ops",
        "harness",
        "api",
        "docs",
        "redoc",
        "openapi.json",
        # The fingerprinted bundle. Reserved so a request for an asset hash
        # that no longer exists -- a browser on the previous deploy -- gets a
        # 404 instead of index.html served as JavaScript, which fails with a
        # MIME error that says nothing about the real cause.
        "assets",
    }
)

# Prefixes shared by a page and an API, exempt from RESERVED by the reasoning
# above. Named so the sync test in tests/test_accounts.py can tell "shared on
# purpose" apart from "forgotten".
SHARED_WITH_SPA = frozenset({"analyse", "puzzles"})


def install_spa(app: FastAPI) -> int:
    """
    Mount the built SPA. Returns the number of routes added, so the caller
    can log whether a build was actually found rather than guessing.
    """
    index = STATIC_DIR / "index.html"

    if not index.is_file():
        # No build present. In development Vite serves the UI on :5173 and
        # proxies these paths here, so there is nothing to mount and this is
        # not an error.
        return 0

    assets = STATIC_DIR / "assets"
    if assets.is_dir():
        # Vite fingerprints these filenames, so they are safe to cache hard.
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str):  # noqa: ANN202
        head = full_path.split("/", 1)[0]
        if head in RESERVED:
            # An unmatched path under an API prefix is a 404, not the SPA.
            # Returning index.html here would hand JSON clients an HTML body.
            return JSONResponse({"detail": "Not Found"}, status_code=404)

        # A real file (favicon.ico, robots.txt) wins over the SPA.
        candidate = (STATIC_DIR / full_path).resolve()
        if candidate.is_file() and candidate.is_relative_to(STATIC_DIR):
            return FileResponse(candidate)

        # Every other GET is a client route. index.html must not be cached or
        # a deploy leaves browsers pointing at asset hashes that are gone.
        return FileResponse(
            index,
            headers={"Cache-Control": "no-cache, must-revalidate"},
        )

    return 1


# --- CORS -------------------------------------------------------------------
#
# You do NOT need CORS for the layout above: one service, one origin, and the
# Vite dev proxy also keeps the browser on one origin. Add it only if you
# split the frontend onto its own host (Netlify, Pages, a different Render
# service), in which case put this in partE.py and set VITE_API_BASE in the
# frontend build:
#
#   from fastapi.middleware.cors import CORSMiddleware
#   app.add_middleware(
#       CORSMiddleware,
#       allow_origins=[settings.frontend_origin],   # never ["*"] with auth
#       allow_credentials=True,
#       allow_methods=["GET", "POST"],
#       allow_headers=["Authorization", "Content-Type"],
#   )
#
# Note that SSE through a cross-origin EventSource needs allow_credentials and
# an exact origin, never a wildcard. Staying single-origin avoids all of this.
