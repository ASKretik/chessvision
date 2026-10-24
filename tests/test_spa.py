"""
E.4/F: the SPA mount, with a stand-in build.

spa.py adds a GET catch-all, which is the single most dangerous route in the
app: get it wrong and either every page deep-link 404s, or every API path
starts answering with HTML. Both are total failures and neither shows up in
a unit test of the endpoints themselves.

The scaffold shipped with the first of those bugs -- its README said
GET /analyse and GET /puzzles would fall through to the SPA, while its own
catch-all 404ed them on the reserved-prefix check first. This file is what
found it.

It runs against the real build when one exists, and writes a stand-in when
one does not -- so it works before the first `npm run build` and after, and
never overwrites real output. The stand-in is removed afterwards; a real
build is left alone.

    .venv/bin/python tests/test_spa.py
"""

from __future__ import annotations

import importlib
import os
import re
import shutil
import sys
from typing import List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "static")
START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"

_results: List[Tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))


def write_stand_in() -> bool:
    """Returns True if we created static/ and should remove it wholesale."""
    created = not os.path.isdir(STATIC)
    os.makedirs(os.path.join(STATIC, "assets"), exist_ok=True)
    with open(os.path.join(STATIC, "index.html"), "w") as fh:
        fh.write("<!doctype html><title>SPA</title><div id=root></div>")
    with open(os.path.join(STATIC, "assets", "main-abc123.js"), "w") as fh:
        fh.write("// bundle")
    with open(os.path.join(STATIC, "favicon.ico"), "w") as fh:
        fh.write("x")
    return created


def main() -> int:
    real_build = os.path.isfile(os.path.join(STATIC, "index.html"))
    created = False
    wrote_stand_in = False
    if real_build:
        print("testing against the real build in static/\n")
    else:
        print("no build present -- testing against a stand-in\n")
        created = write_stand_in()
        wrote_stand_in = True
    try:
        from fastapi.testclient import TestClient

        import spa
        import partE
        importlib.reload(partE)

        check("install_spa reports it mounted something",
              spa.install_spa(partE.app) == 1)

        with TestClient(partE.app) as c:
            # Both spellings: the stand-in writes `id=root`, Vite emits
            # `id="root"`.
            def is_spa(r: Any) -> bool:
                return r.status_code == 200 and (
                    'id="root"' in r.text or "id=root" in r.text
                )

            print("the SPA answers page routes")
            r = c.get("/")
            check("GET / serves the SPA", is_spa(r), str(r.status_code))
            check("index.html is not cached, or a deploy strands old asset "
                  "hashes in browsers",
                  "no-cache" in (r.headers.get("cache-control") or ""),
                  str(r.headers.get("cache-control")))
            check("GET /puzzles is the page, not the API",
                  is_spa(c.get("/puzzles")), str(c.get("/puzzles").status_code))
            check("GET /analyse is the page", is_spa(c.get("/analyse")))
            check("a job deep link is the page",
                  is_spa(c.get("/analyse/9c0592fa73cd43bbbf87909623499aa4")))
            check("a saved-analysis deep link is the page",
                  is_spa(c.get("/analyse/saved/an-a")))
            check("an unknown client route is the page, for the router to "
                  "redirect", is_spa(c.get("/positions")))

            print("the API still owns its own paths")
            check("GET /config", c.get("/config").status_code == 200)
            check("GET /health", c.get("/health").status_code == 200)
            r = c.get("/board/moves", params={"fen": START_FEN})
            check("GET /board/moves returns moves, not HTML",
                  r.status_code == 200 and len(r.json()["legal"]) == 20,
                  str(r.status_code))
            r = c.get("/puzzles/next")
            check("GET /puzzles/next is the API even though /puzzles is a page",
                  r.status_code in (200, 401, 503)
                  and "json" in r.headers["content-type"],
                  f"{r.status_code} {r.headers['content-type']}")
            r = c.post("/analyse", json={})
            check("POST /analyse validates instead of serving a page",
                  r.status_code == 422, str(r.status_code))
            check("the dev harness is untouched by the mount",
                  c.get("/harness").status_code == 200)

            print("unmatched API paths stay JSON")
            for path in ("/jobs/nope", "/me/nope", "/board/nope", "/ops/nope"):
                r = c.get(path)
                check(f"{path} is a JSON 404, not index.html",
                      r.status_code == 404
                      and "json" in r.headers["content-type"],
                      f"{r.status_code} {r.headers['content-type']}")

            print("static files")
            # Read the asset name out of the page rather than assuming one,
            # since a real build fingerprints it.
            page = c.get("/").text
            m = re.search(r'src="(/assets/[^"]+\.js)"', page)
            asset = m.group(1) if m else "/assets/main-abc123.js"
            r = c.get(asset)
            check("the fingerprinted bundle is served",
                  r.status_code == 200 and len(r.content) > 0, f"{asset} {r.status_code}")
            check("and it is served as JavaScript, not HTML",
                  "javascript" in (r.headers.get("content-type") or ""),
                  str(r.headers.get("content-type")))
            r = c.get("/assets/does-not-exist-abcdef.js")
            check("a stale asset hash is a 404, not index.html as JavaScript",
                  r.status_code == 404, str(r.status_code))
            if created:
                r = c.get("/favicon.ico")
                check("a real file beats the SPA fallback",
                      r.status_code == 200 and r.text == "x", str(r.status_code))
            r = c.get("/partE.py")
            check("a source file next to static/ is not served",
                  "install_spa" not in r.text and "FastAPI" not in r.text,
                  "partE.py was served")
            r = c.get("/../partE.py")
            check("a traversal attempt gets the SPA, not the repo",
                  "install_spa" not in r.text, "escaped static/")
    finally:
        # Only ever remove what this file wrote. An earlier version deleted
        # index.html unconditionally and destroyed a real Vite build, which
        # took the whole site down until the next `npm run build`.
        if created:
            shutil.rmtree(STATIC, ignore_errors=True)
        elif wrote_stand_in:
            for p in ("index.html", "favicon.ico",
                      os.path.join("assets", "main-abc123.js")):
                try:
                    os.remove(os.path.join(STATIC, p))
                except OSError:
                    pass
            for d in (os.path.join(STATIC, "assets"), STATIC):
                try:
                    os.rmdir(d)
                except OSError:
                    pass

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


if __name__ == "__main__":
    sys.exit(main())
