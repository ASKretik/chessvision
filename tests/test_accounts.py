"""
Part C HTTP layer: auth gating, the account endpoints, and the puzzle flow.

Supabase is replaced with an in-memory fake, so this runs offline. What it
covers is the code that RLS does NOT protect: `select()` uses the service
key, which bypasses RLS entirely, so every ownership filter is this file's
responsibility to prove.

The RLS policies themselves are proven inside Postgres by
migrations/002_rls_test.sql -- they cannot be tested from here.

    .venv/bin/python tests/test_accounts.py
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("STOCKFISH_PATH", "stockfish")

import partC                                     # noqa: E402
import partE                                     # noqa: E402

_results: List[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))


USER_A = "aaaaaaaa-0000-0000-0000-00000000000a"
USER_B = "bbbbbbbb-0000-0000-0000-00000000000b"

PUZZLE_ROW = {
    "id": "p1",
    # Player to move (white); Be6+ then Qf8# is the line.
    "fen": "q5nr/1ppknQpp/3p4/1P2p3/4P3/B1PP1b2/B5PP/5K2 w - - 1 18",
    "solution": ["a2e6", "f7f8"],
    "opponent_replies": ["d7d8"],
    "rating": 1500, "rating_deviation": 80, "popularity": 90,
    "nb_plays": 500, "themes": ["mateIn2"], "game_url": None,
    "opening_tags": [],
}


class FakeSupabase:
    """
    Enough PostgREST to exercise the endpoints: eq./gte./lte. filters,
    ordering, limits, and the two RPCs.
    """

    def __init__(self) -> None:
        self.configured = True
        self.tables: Dict[str, List[Dict[str, Any]]] = {
            "puzzles": [dict(PUZZLE_ROW)],
            "analyses": [
                {"id": "an-a", "user_id": USER_A, "source_type": "pgn",
                 "source_ref": "A-owned", "pgn": "1. e4 *",
                 "result_json": {"kind": "game"}, "created_at": "2026-10-01T00:00:00Z"},
                {"id": "an-b", "user_id": USER_B, "source_type": "pgn",
                 "source_ref": "B-owned", "pgn": "1. d4 *",
                 "result_json": {"kind": "game"}, "created_at": "2026-10-02T00:00:00Z"},
            ],
            "user_stats": [
                {"user_id": USER_A, "puzzle_rating": 1500, "puzzle_attempts": 3,
                 "puzzles_solved": 2, "games_analysed": 1,
                 "current_streak": 2, "best_streak": 2},
            ],
            "puzzle_attempts": [],
        }
        self.rpc_calls: List[tuple[str, Dict[str, Any]]] = []

    async def select(self, table, *, columns="*", filters=None, order=None,
                     limit=None, offset=None):
        rows = [dict(r) for r in self.tables.get(table, [])]
        for key, raw in (filters or {}).items():
            for cond in (raw if isinstance(raw, (list, tuple)) else [raw]):
                op, _, value = str(cond).partition(".")
                if op == "eq":
                    rows = [r for r in rows if str(r.get(key)) == value]
                elif op == "gte":
                    rows = [r for r in rows if float(r.get(key, 0)) >= float(value)]
                elif op == "lte":
                    rows = [r for r in rows if float(r.get(key, 0)) <= float(value)]
                else:
                    raise AssertionError(f"fake does not implement {op!r}")
        if order:
            field, _, direction = order.partition(".")
            rows.sort(key=lambda r: r.get(field) or "", reverse=direction == "desc")
        if offset:
            rows = rows[offset:]
        if limit is not None:
            rows = rows[:limit]
        if columns != "*":
            wanted = [c.strip() for c in columns.split(",")]
            rows = [{k: r.get(k) for k in wanted} for r in rows]
        return rows

    async def rpc(self, name, payload):
        self.rpc_calls.append((name, payload))
        if name == "record_puzzle_attempt":
            self.tables["puzzle_attempts"].append({
                "user_id": payload["p_user_id"], "puzzle_id": payload["p_puzzle_id"],
                "solved": payload["p_solved"],
            })
            stats = next((s for s in self.tables["user_stats"]
                          if s["user_id"] == payload["p_user_id"]), None)
            if stats is None:
                stats = {"user_id": payload["p_user_id"], "puzzle_rating": 1200,
                         "puzzle_attempts": 0, "puzzles_solved": 0,
                         "games_analysed": 0, "current_streak": 0, "best_streak": 0}
                self.tables["user_stats"].append(stats)
            stats["puzzle_rating"] = payload["p_rating_after"]
            stats["puzzle_attempts"] += 1
            stats["puzzles_solved"] += 1 if payload["p_solved"] else 0
            stats["current_streak"] = (stats["current_streak"] + 1
                                       if payload["p_solved"] else 0)
            stats["best_streak"] = max(stats["best_streak"], stats["current_streak"])
            return [dict(stats)]
        if name == "bump_games_analysed":
            stats = next((s for s in self.tables["user_stats"]
                          if s["user_id"] == payload["p_user_id"]), None)
            if stats is None:
                stats = {"user_id": payload["p_user_id"], "puzzle_rating": 1200,
                         "puzzle_attempts": 0, "puzzles_solved": 0,
                         "games_analysed": 0, "current_streak": 0, "best_streak": 0}
                self.tables["user_stats"].append(stats)
            stats["games_analysed"] += 1
            return stats["games_analysed"]
        return None

    async def insert(self, table, row):
        self.tables.setdefault(table, []).append(dict(row))
        return dict(row)

    async def verify_jwt(self, token):
        return {"id": USER_A, "email": "a@example.com"} if token == "token-a" else (
            {"id": USER_B, "email": "b@example.com"} if token == "token-b" else None)

    async def ping(self):
        return {"ok": True}

    async def start(self):  # noqa: D102
        pass

    async def stop(self):   # noqa: D102
        pass


def main() -> int:
    from fastapi.testclient import TestClient

    fake = FakeSupabase()
    partE.supabase = fake            # the endpoints read the module global

    A = {"Authorization": "Bearer token-a"}
    B = {"Authorization": "Bearer token-b"}

    with TestClient(partE.app) as c:
        print("C.1 auth gating")
        for path in ("/me", "/me/analyses", "/me/analyses/an-a", "/puzzles/next"):
            check(f"{path} needs a token", c.get(path).status_code == 401,
                  str(c.get(path).status_code))
        check("attempt needs a token",
              c.post("/puzzles/p1/attempt", json={"moves": []}).status_code == 401)
        check("a bad token is rejected",
              c.get("/me", headers={"Authorization": "Bearer nope"}).status_code == 401)
        check("/health needs no token", c.get("/health").status_code == 200)

        print("C.1 /me")
        me = c.get("/me", headers=A)
        check("/me returns the signed-in user", me.status_code == 200
              and me.json()["user_id"] == USER_A, str(me.status_code))
        check("/me returns stats", me.json()["stats"]["puzzle_rating"] == 1500,
              str(me.json()["stats"]))
        check("/me does not leak the email of another user",
              me.json()["email"] == "a@example.com")
        me_b = c.get("/me", headers=B).json()
        check("a user with no stats row gets defaults",
              me_b["stats"]["puzzle_rating"] in (1200, partC.DEFAULT_RATING),
              str(me_b["stats"]))

        print("C.2 ownership (the part RLS does not cover here)")
        mine = c.get("/me/analyses", headers=A).json()["analyses"]
        check("only this user's analyses are listed", len(mine) == 1,
              str(len(mine)))
        check("and it is the right one", mine[0]["source_ref"] == "A-owned",
              str(mine[0]))
        check("the listing is a summary, not whole reports",
              "result_json" not in mine[0], str(sorted(mine[0])))
        theirs = c.get("/me/analyses/an-b", headers=A)
        check("another user's analysis is 404, not 403",
              theirs.status_code == 404, str(theirs.status_code))
        own = c.get("/me/analyses/an-a", headers=A)
        check("your own analysis is readable in full",
              own.status_code == 200 and own.json()["result_json"] is not None)
        check("user B sees only B's analyses",
              [r["source_ref"] for r in c.get("/me/analyses", headers=B)
               .json()["analyses"]] == ["B-owned"])

        print("C.3 serving a puzzle")
        nxt = c.get("/puzzles/next", headers=A)
        check("a puzzle is served", nxt.status_code == 200, str(nxt.status_code))
        body = nxt.json()
        check("the solution is NOT in the response",
              "solution" not in body["puzzle"], str(sorted(body["puzzle"])))
        check("nor are the opponent replies",
              "opponent_replies" not in body["puzzle"])
        check("the response carries the position and the rating window",
              body["puzzle"]["fen"] and body["rating_window"][0] < body["rating_window"][1],
              str(body.get("rating_window")))
        check("the player's own rating is reported", body["your_rating"] == 1500)

        print("E.4 the shared board endpoint")
        START = ("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR"
                 " w KQkq - 0 1")
        bm = c.get("/board/moves", params={"fen": START})
        check("/board/moves needs no token", bm.status_code == 200,
              str(bm.status_code))
        bd = bm.json()
        check("the opening position has 20 legal moves",
              len(bd["legal"]) == 20, str(len(bd["legal"])))
        check("it reports whose turn it is", bd["turn"] == "white", bd["turn"])
        check("every move carries the position it reaches",
              all(m["fen_after"] for m in bd["legal"]))
        check("every move carries from, to and SAN",
              all(m["from"] and m["to"] and m["san"] for m in bd["legal"]))
        e4 = next(m for m in bd["legal"] if m["uci"] == "e2e4")
        check("the reached position is the real one",
              e4["fen_after"].startswith(
                  "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b"),
              e4["fen_after"])
        # The client detects a promotion as several moves sharing from+to,
        # so all four have to be listed separately.
        promo = c.get("/board/moves",
                      params={"fen": "8/P6k/8/8/8/8/7K/8 w - - 0 1"}).json()
        a8 = [m for m in promo["legal"] if m["from"] == "a7" and m["to"] == "a8"]
        check("a promotion is four separate moves", len(a8) == 4, str(len(a8)))
        check("and each names its piece",
              sorted(m["promotion"] for m in a8)
              == ["bishop", "knight", "queen", "rook"],
              str(sorted(m["promotion"] for m in a8)))
        mate = c.get("/board/moves",
                     params={"fen": "6k1/5ppp/8/8/8/8/8/R5K1 w - - 0 1"}).json()
        check("mate in one is flagged",
              any(m["is_checkmate"] for m in mate["legal"]))
        over = c.get("/board/moves",
                     params={"fen": "R5k1/5ppp/8/8/8/8/8/6K1 b - - 1 1"}).json()
        check("a mated position offers no moves",
              over["legal"] == [], str(len(over["legal"])))
        check("and reports the game over with its result",
              over["is_game_over"] is True and over["outcome"] == "1-0",
              str(over["outcome"]))
        check("a malformed FEN is 422, not 500",
              c.get("/board/moves", params={"fen": "not a fen"}).status_code == 422)
        check("an illegal position is 422",
              c.get("/board/moves",
                    params={"fen": "8/8/8/8/8/8/8/8 w - - 0 1"}).status_code == 422)

        print("C.3 stepping a line on the board")
        # The board plays one move at a time, so the server has to hand back
        # the opponent's reply -- it is inside the hidden solution.
        st = c.post("/puzzles/p1/step", json={"moves": ["a2e6"]}, headers=A)
        check("a step is accepted", st.status_code == 200, str(st.status_code))
        sd = st.json()
        check("a right move is reported right", sd["correct"] is True, str(sd))
        check("the line is not finished yet", sd["complete"] is False)
        check("the step carries the opponent's reply",
              sd["opponent_reply"] and sd["fen_after"], str(sd))
        check("a step does NOT reveal the expected move",
              "expected_instead" not in sd and "expected" not in sd,
              str(sorted(sd)))
        done = c.post("/puzzles/p1/step",
                      json={"moves": ["a2e6", "f7f8"]}, headers=A).json()
        check("finishing the line reports complete", done["complete"] is True,
              str(done))
        bad = c.post("/puzzles/p1/step", json={"moves": ["f7g8"]}, headers=A).json()
        check("a wrong move is reported wrong", bad["correct"] is False, str(bad))
        check("and a wrong step gives away nothing",
              bad["opponent_reply"] is None and bad["fen_after"] is None, str(bad))
        check("stepping does not move the rating",
              not any(n == "record_puzzle_attempt" for n, _ in fake.rpc_calls),
              str([n for n, _ in fake.rpc_calls]))
        check("stepping records no attempt",
              len(fake.tables["puzzle_attempts"]) == 0,
              str(len(fake.tables["puzzle_attempts"])))
        check("a step needs a token",
              c.post("/puzzles/p1/step", json={"moves": ["a2e6"]}).status_code == 401)
        check("an empty step is 422",
              c.post("/puzzles/p1/step", json={"moves": []},
                     headers=A).status_code == 422)
        check("stepping an unknown puzzle is 404",
              c.post("/puzzles/nope/step", json={"moves": ["a2e6"]},
                     headers=A).status_code == 404)

        print("C.3 attempting a puzzle")
        wrong = c.post("/puzzles/p1/attempt",
                       json={"moves": ["f7g8"], "ms_taken": 4000}, headers=A).json()
        check("a wrong attempt does not solve", wrong["result"]["solved"] is False)
        check("a wrong attempt lowers the rating", wrong["rating"]["delta"] < 0,
              str(wrong["rating"]))
        check("a wrong attempt does NOT reveal the solution",
              wrong["solution"] is None, str(wrong["solution"]))
        check("a wrong attempt says what was expected",
              wrong["result"]["expected_instead"] == "Be6+",
              str(wrong["result"]["expected_instead"]))

        before = next(s for s in fake.tables["user_stats"]
                      if s["user_id"] == USER_A)["puzzle_rating"]
        right = c.post("/puzzles/p1/attempt",
                       json={"moves": ["a2e6", "f7f8"], "ms_taken": 9000},
                       headers=A).json()
        check("the correct line solves", right["result"]["solved"] is True,
              str(right["result"].get("reason")))
        check("solving raises the rating", right["rating"]["delta"] > 0,
              str(right["rating"]))
        check("the solution is revealed only after solving",
              right["solution"] == ["a2e6", "f7f8"], str(right["solution"]))
        check("stats come back updated", right["stats"]["puzzle_attempts"] >= 1,
              str(right["stats"]))
        check("the rating actually moved in the store",
              next(s for s in fake.tables["user_stats"]
                   if s["user_id"] == USER_A)["puzzle_rating"] != before)

        print("C.3 atomicity and bookkeeping")
        names = [n for n, _ in fake.rpc_calls]
        check("the attempt is recorded through the atomic RPC",
              "record_puzzle_attempt" in names, str(names))
        check("the attempt row was written",
              len(fake.tables["puzzle_attempts"]) == 2,
              str(len(fake.tables["puzzle_attempts"])))
        payload = next(p for n, p in fake.rpc_calls if n == "record_puzzle_attempt")
        check("the RPC carries both ratings, so the DB does no Elo maths",
              payload["p_rating_before"] != payload["p_rating_after"]
              or payload["p_solved"] is not None,
              str(sorted(payload)))
        check("the RPC is scoped to the caller", payload["p_user_id"] == USER_A)

        print("C.3 a repeat puzzle is not served again")
        again = c.get("/puzzles/next", headers=A)
        check("an already-attempted puzzle is not re-served",
              again.status_code == 404, str(again.status_code))
        check("and the message says why",
              "unattempted" in again.json().get("detail", "").lower(),
              str(again.json().get("detail"))[:80])

        print("C.3 errors")
        check("an unknown puzzle is 404",
              c.post("/puzzles/nope/attempt", json={"moves": ["a2e6"]},
                     headers=A).status_code == 404)
        check("an unreadable move is a verdict, not a 500",
              c.post("/puzzles/p1/attempt", json={"moves": ["zzz"]},
                     headers=A).status_code == 200)
        check("too many moves is rejected by validation",
              c.post("/puzzles/p1/attempt", json={"moves": ["a2e6"] * 99},
                     headers=A).status_code == 422)
        check("a negative time is rejected",
              c.post("/puzzles/p1/attempt",
                     json={"moves": ["a2e6"], "ms_taken": -1},
                     headers=A).status_code == 422)

        print("every endpoint this app is supposed to have is registered")
        # An explicit inventory, because a route can vanish silently: an
        # editor saving a stale buffer over partE.py once removed both
        # /api/positions endpoints, and nothing failed -- the running server
        # still had them in memory, and every test passed. The frontend
        # would have broken on the next deploy.
        expected = {
            ("GET", "/health"), ("GET", "/ready"), ("GET", "/config"),
            ("GET", "/ops/memory"), ("GET", "/board/moves"),
            ("POST", "/analyse"), ("GET", "/jobs/{job_id}"),
            ("GET", "/jobs/{job_id}/events"),
            ("GET", "/me"), ("GET", "/me/analyses"),
            ("GET", "/me/analyses/{analysis_id}"),
            ("DELETE", "/me/analyses/{analysis_id}"),
            ("POST", "/api/auth/signup"), ("POST", "/api/auth/login"),
            ("POST", "/api/auth/logout"),
            ("GET", "/api/play/bots"), ("POST", "/api/play/new"),
            ("GET", "/api/play/{game_id}"),
            ("POST", "/api/play/{game_id}/move"),
            ("POST", "/api/play/{game_id}/resign"),
            ("GET", "/puzzles/next"),
            ("POST", "/puzzles/{puzzle_id}/step"),
            ("POST", "/puzzles/{puzzle_id}/attempt"),
        }
        have = {
            (m, getattr(r, "path", ""))
            for r in partE.app.routes
            for m in (getattr(r, "methods", None) or set())
            if m not in ("HEAD", "OPTIONS")
        }
        missing = sorted(expected - have)
        check("no endpoint has gone missing", not missing,
              "not registered: " + ", ".join(f"{m} {p}" for m, p in missing))

        print("E.2 a signed-in user can still stream their own job")
        # EventSource cannot send an Authorization header. Once jobs had
        # owners, every signed-in user's own progress stream started 404ing
        # and the UI silently fell back to polling -- it showed "queued"
        # and never updated. The id is the capability on that endpoint.
        import partE as _pe
        job = _pe.jobs.create(USER_A)
        try:
            owned = _pe._owned_job(job.id, _pe.Caller(USER_A, None, False))
            check("the owner can read their own job", owned.id == job.id)
        except Exception as exc:
            check("the owner can read their own job", False, str(exc))
        other = _pe.Caller("someone-else", None, False)
        try:
            _pe._owned_job(job.id, other)
            check("another user cannot read it", False, "it was readable")
        except Exception:
            check("another user cannot read it", True)
        try:
            streamed = _pe._owned_job(job.id, other, allow_anonymous=True)
            check("but the SSE stream admits anyone holding the job id",
                  streamed.id == job.id)
        except Exception as exc:
            check("but the SSE stream admits anyone holding the job id",
                  False, str(exc))

        print("E.4 the SPA catch-all cannot swallow an API route")
        # spa.py adds GET /{full_path:path}, so every API path has to be
        # listed as reserved or the SPA answers it with index.html. Adding a
        # route and forgetting the list is a silent, total failure of that
        # endpoint, which is why this is asserted and not documented.
        import spa as spa_mod

        segments = set()
        mounts = set()
        for route in partE.app.routes:
            path = getattr(route, "path", "")
            if not path.startswith("/") or path in ("/", "/{full_path:path}"):
                continue
            head = path.lstrip("/").split("/", 1)[0]
            if not head or head.startswith("{"):
                continue
            segments.add(head)
            # A static mount is not an API path: Vite serves its own assets
            # in development and must NOT proxy them to the backend.
            if route.__class__.__name__ == "Mount":
                mounts.add(head)
        unreserved = sorted(segments - spa_mod.RESERVED - spa_mod.SHARED_WITH_SPA)
        check("every API path's first segment is reserved in spa.py",
              not unreserved,
              "not in spa.RESERVED: " + ", ".join(unreserved))
        # The exemptions are exemptions, not oversights: each must really be
        # a prefix where a page and an API coexist.
        check("the SPA-shared prefixes are all real API prefixes",
              spa_mod.SHARED_WITH_SPA <= segments,
              "not actually API prefixes: "
              + ", ".join(sorted(spa_mod.SHARED_WITH_SPA - segments)))
        check("and they are not also reserved, which would 404 the page",
              not (spa_mod.SHARED_WITH_SPA & spa_mod.RESERVED),
              ", ".join(sorted(spa_mod.SHARED_WITH_SPA & spa_mod.RESERVED)))

        # The Vite dev proxy needs the same list, or the API works in
        # production and 404s (as index.html) in development.
        vite = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "frontend", "vite.config.ts")
        if os.path.isfile(vite):
            conf = open(vite).read()
            missing_proxy = sorted(
                seg for seg in segments - mounts
                if f"'/{seg}'" not in conf and f'"/{seg}"' not in conf
            )
            check("every API path is also in the Vite dev proxy",
                  not missing_proxy,
                  "not in vite.config.ts: " + ", ".join(missing_proxy))
            # The two paths resolved by method/specificity rather than by
            # prefix need an explicit bypass, and the puzzle bypass has to
            # name every sub-path that is really an API call.
            check("the Vite puzzle bypass knows about /step",
                  "step" in conf,
                  "bypass would send /puzzles/{id}/step to the SPA")

        print("C feature reporting")
        feats = c.get("/config").json()["features"]
        check("puzzles are reported available", feats["puzzles"] is True, str(feats))

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
