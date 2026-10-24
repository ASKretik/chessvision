"""
C.1b / D.1 -- local accounts and the bots.

No network, no Supabase. The account store gets a temporary SQLite file;
the bots get the real Maia-2 session when the weights are present and are
skipped with a message when they are not.

    .venv/bin/python tests/test_accounts_local.py
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from typing import List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import chess                                       # noqa: E402
import accounts                                    # noqa: E402
import bots                                        # noqa: E402

_results: List[Tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="chess-accounts-")
    db = os.path.join(tmp, "app.db")
    store = accounts.AccountStore(db)

    print("C.1b credentials")
    user, token = store.create_user("darkmoon", "a good long passphrase")
    check("an account can be created", user.username == "darkmoon")
    check("and comes with a session", bool(token) and len(token) > 20)
    check("the session resolves to the user",
          (store.session_user(token) or user).id == user.id)
    check("a nonsense token resolves to nobody",
          store.session_user("not-a-token") is None)

    for username, password, why in [
        ("ab", "longenough1", "username under three characters"),
        ("has spaces", "longenough1", "username with a space"),
        ("newuser", "short", "password under eight characters"),
        ("newuser", "password", "a password from the common list"),
        ("chessfan", "chessfan", "password identical to the username"),
        ("darkmoon", "a good long passphrase", "a username already taken"),
    ]:
        try:
            store.create_user(username, password)
            check(f"rejects {why}", False, "it was accepted")
        except accounts.AccountError as exc:
            check(f"rejects {why}", True, str(exc))

    print("C.1b what is actually stored")
    raw = sqlite3.connect(db)
    raw.row_factory = sqlite3.Row
    row = raw.execute("SELECT * FROM users").fetchone()
    check("the password is not in the database",
          b"passphrase" not in bytes(row["password_hash"]))
    check("nor is it recoverable from the salt",
          bytes(row["salt"]) != bytes(row["password_hash"]))
    check("the scrypt parameters travel with the hash, so they can be raised",
          row["n"] == accounts.SCRYPT_N and row["r"] == accounts.SCRYPT_R)
    srow = raw.execute("SELECT token_hash FROM sessions").fetchone()
    check("the session token is stored HASHED, not raw -- a database backup "
          "must not be a set of working logins",
          bytes(srow["token_hash"]) != token.encode())

    print("C.1b sign-in")
    try:
        store.login("darkmoon", "wrong password entirely")
        check("a wrong password is refused", False, "it was accepted")
    except accounts.AccountError as exc:
        check("a wrong password is refused", True)
        check("and the message does not say WHICH half was wrong",
              "password" in str(exc).lower() and "username" in str(exc).lower(),
              str(exc))
    try:
        store.login("ghost", "whatever at all")
        check("an unknown user is refused", False, "it was accepted")
    except accounts.AccountError as exc:
        check("an unknown user is refused", True)
        check("with the SAME message, so accounts cannot be enumerated",
              str(exc) == "Wrong username or password.", str(exc))

    again, token2 = store.login("DARKMOON", "a good long passphrase")
    check("sign-in is case-insensitive on the username", again.id == user.id)
    check("each sign-in issues a new session", token2 != token)
    check("and the old one still works until it is used to sign out",
          store.session_user(token) is not None)
    store.logout(token)
    check("signing out kills that session server-side",
          store.session_user(token) is None)
    check("but not the other one", store.session_user(token2) is not None)

    print("C.1b progress survives")
    store.record_attempt(user.id, "p1", True, 1532, 4000)
    store.record_attempt(user.id, "p2", True, 1560, 3000)
    store.record_attempt(user.id, "p3", False, 1528, 9000)
    stats = store.stats(user.id)
    check("attempts are counted", stats["puzzle_attempts"] == 3, str(stats))
    check("solves are counted", stats["puzzles_solved"] == 2, str(stats))
    check("the rating is the latest one", stats["puzzle_rating"] == 1528,
          str(stats["puzzle_rating"]))
    check("a failure resets the streak", stats["current_streak"] == 0)
    check("but the best streak is kept", stats["best_streak"] == 2,
          str(stats["best_streak"]))
    check("attempted puzzles are remembered, so none is served twice",
          sorted(store.attempted_ids(user.id)) == ["p1", "p2", "p3"])

    store.close()
    reopened = accounts.AccountStore(db)
    check("ALL OF IT survives a restart -- the point of the whole module",
          reopened.stats(user.id)["puzzle_rating"] == 1528
          and len(reopened.attempted_ids(user.id)) == 3)
    check("and so does the session", reopened.session_user(token2) is not None)
    reopened.close()

    print("C.1c saved analyses")
    import json as _json
    import zlib as _zlib

    def fake_report(white="alice", black="bob", moves=40):
        return {
            "kind": "game",
            "headers": {"White": white, "Black": black, "Result": "1-0",
                        "Date": "2026.01.01"},
            "white": {"name": white, "accuracy": 91.2},
            "black": {"name": black, "accuracy": 84.0},
            "moves": [{"san": "e4", "fen_before": "x" * 90} for _ in range(moves)],
        }

    store2 = accounts.AccountStore(db)
    owner, _ = store2.create_user("studyuser", "yet another passphrase")

    report = fake_report()
    aid = store2.save_analysis(owner.id, source_type="pgn", source_ref="g-1",
                               report=report, depth=14, prompt_version="b-x")
    check("an analysis can be saved", bool(aid))

    raw2 = sqlite3.connect(db)
    raw2.row_factory = sqlite3.Row
    blob = bytes(raw2.execute(
        "SELECT result_json FROM analyses WHERE id = ?", (aid,)).fetchone()[0])
    plain = len(_json.dumps(report))
    check("the report is stored compressed, not raw",
          len(blob) < plain / 2, f"{plain} -> {len(blob)}")
    check("and it round-trips exactly",
          _json.loads(_zlib.decompress(blob)) == report)

    rows = store2.list_analyses(owner.id)
    check("the listing excludes the blob -- thirty reports would be megabytes",
          "result_json" not in rows[0], str(sorted(rows[0])))
    check("but carries what a list needs, denormalised",
          rows[0]["white"] == "alice" and rows[0]["result"] == "1-0"
          and rows[0]["plies"] == 40, str(rows[0]))
    check("and what produced it, so a stale snapshot is identifiable",
          rows[0]["depth"] == 14 and rows[0]["prompt_version"] == "b-x")

    full = store2.get_analysis(owner.id, aid)
    check("the full report reads back", full is not None
          and len(full["report"]["moves"]) == 40)
    check("under `report`, not `result` -- `result` is the game's outcome",
          full["result"] == "1-0" and isinstance(full["report"], dict))

    # The cap, enforced on write.
    for i in range(accounts.MAX_ANALYSES + 5):
        store2.save_analysis(owner.id, source_type="pgn",
                             source_ref=f"bulk-{i}", report=fake_report())
    kept = store2.list_analyses(owner.id)
    check(f"never more than {accounts.MAX_ANALYSES} are kept",
          len(kept) == accounts.MAX_ANALYSES, str(len(kept)))
    check("the newest survive and the oldest are the ones dropped",
          all(kept[i]["created_at"] >= kept[i + 1]["created_at"]
              for i in range(len(kept) - 1)))

    before = len(store2.list_analyses(owner.id))
    store2.save_analysis(owner.id, source_type="pgn", source_ref="bulk-3",
                         report=fake_report(white="changed"))
    after = store2.list_analyses(owner.id)
    check("re-analysing a game already stored UPDATES it",
          len(after) == before, f"{before} -> {len(after)}")
    check("and the update is visible",
          any(r["white"] == "changed" for r in after))

    # `aid` itself is gone by now -- the cap evicted it, which is the
    # behaviour under test above -- so isolation is checked on a row that
    # still exists.
    check("the first analysis was evicted by the cap, as intended",
          store2.get_analysis(owner.id, aid) is None)
    live = store2.list_analyses(owner.id)[0]["id"]

    stranger, _ = store2.create_user("stranger", "a different passphrase")
    check("another user cannot read one of yours",
          store2.get_analysis(stranger.id, live) is None)
    check("nor delete it", store2.delete_analysis(stranger.id, live) is False)
    check("and it is still there afterwards",
          store2.get_analysis(owner.id, live) is not None)
    check("the owner can delete it", store2.delete_analysis(owner.id, live) is True)
    check("and then it is gone", store2.get_analysis(owner.id, live) is None)
    store2.close()

    print("D.1 bots")
    check("there are bots to choose from", len(bots.BOTS) >= 4)
    check("every bot id is unique",
          len({b.id for b in bots.BOTS}) == len(bots.BOTS))
    check("exactly one is the engine",
          sum(1 for b in bots.BOTS if b.is_engine) == 1)
    check("the Maia bots span a real range",
          max(b.elo for b in bots.BOTS if b.elo)
          - min(b.elo for b in bots.BOTS if b.elo) >= 800)

    game = bots.Game(id="g1", bot_id="maia1400", user_color=chess.WHITE)
    check("a new game starts with the user to move", game.user_to_move)
    check("and is not over", not game.over and game.outcome() is None)
    black = bots.Game(id="g2", bot_id="maia1400", user_color=chess.BLACK)
    check("playing Black means the bot moves first", not black.user_to_move)

    # Fool's mate, so the result wording is checked rather than assumed.
    lost = bots.Game(id="g3", bot_id="maia1400", user_color=chess.WHITE)
    for san in ("f3", "e5", "g4", "Qh4"):
        lost.board.push_san(san)
    check("a checkmate ends the game", lost.over)
    check("and says who won from the USER's point of view",
          lost.outcome() == "Bot wins by checkmate", str(lost.outcome()))
    quit_game = bots.Game(id="g4", bot_id="maia1400", user_color=chess.WHITE)
    quit_game.resigned = True
    check("resigning ends it too", quit_game.over
          and quit_game.outcome() == "You resigned")

    model = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "models", "maia2.onnx")
    if not os.path.exists(model):
        print("  (Maia-2 weights absent; skipping the move-choice checks)")
    else:
        import partA                                # noqa: PLC0415
        session = partA.Maia2Session(model)
        session.load()
        book = partA.get_opening_book()

        board = chess.Board()
        move = bots.pick_move(session, board, bots.BOTS_BY_ID["maia1700"],
                              book=book)
        check("a bot's move is legal", move in board.legal_moves)
        board.push(move)
        check("and keeps the game in known theory at move one",
              book.contains(board), board.fen())

        # The regression this guards: with no repetition filter the policy
        # answered Nf3 with Ng1 and the bots shuffled knights forever.
        board = chess.Board()
        line = []
        for _ in range(12):
            if board.is_game_over():
                break
            mv = bots.pick_move(session, board, bots.BOTS_BY_ID["maia1700"],
                                book=book)
            line.append(board.san(mv))
            board.push(mv)
        check("twelve plies of self-play repeat no position",
              not board.is_repetition(2), " ".join(line))
        check("and develop something",
              len({m for m in line}) >= 8, " ".join(line))

        # An only-move position must come out as the only move. (The first
        # position tried here was checkmate, not a forced move -- zero legal
        # moves rather than one -- which is a different thing entirely.)
        forced = chess.Board("k7/8/1K6/8/8/8/8/7R b - - 0 1")
        only = list(forced.legal_moves)
        check("a forced position leaves one legal move", len(only) == 1,
              str(len(only)))
        check("and the bot plays it",
              bots.pick_move(session, forced, bots.BOTS_BY_ID["maia1100"])
              == only[0])

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
