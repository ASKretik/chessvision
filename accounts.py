"""
C.1b -- local accounts: username, password, and persistent progress.

WHY THIS EXISTS
---------------
Part C.1 put accounts in Supabase, which means a deployment without a
Supabase project has no accounts at all -- and the fallback was a dict in
process memory, so a restart lost every rating and every solved puzzle. That
is fine for a demo and useless for a user who wants to come back tomorrow.

This is the same contract backed by SQLite: one file, no server, no network,
and it survives a restart. The Supabase path in partE.py is untouched and
still wins when it is configured; this takes over when it is not.

PASSWORD STORAGE
----------------
scrypt from the standard library, per-user random salt, parameters chosen to
cost ~100ms per hash on this hardware. Passwords are never stored, never
logged, and never returned by any endpoint. `verify` compares with
`hmac.compare_digest` so a wrong password costs the same time as a right one.

scrypt rather than PBKDF2 because it is memory-hard, which is what makes a
GPU attack expensive; rather than bcrypt/argon2 because those are third-party
and this has to run inside a 512 MB image with no build toolchain.

SESSIONS
--------
A session token is 32 random bytes, base64url. The DATABASE STORES ONLY ITS
HASH: a leaked database backup must not hand out live sessions, which is the
same reason passwords are hashed. Tokens expire, and `touch` extends a
session in use so an active user is not logged out mid-puzzle.

WHAT THIS DELIBERATELY DOES NOT DO
----------------------------------
No email, no password reset, no OAuth. A reset flow needs a mail path and an
out-of-band token, and getting that wrong is worse than not having it. The
honest consequence is stated to the user at signup: a forgotten password
cannot be recovered.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

# Measured ~30ms per hash on an M-series laptop. n is the dominant cost and
# also the memory cost: scrypt uses 128*n*r bytes, so 2**14 is 16 MB PER
# CONCURRENT HASH. That is the binding constraint here, not time -- the
# deployment target is a 512 MB container (E.3), and 2**15 would put two
# simultaneous logins at 64 MB. Raise it on bigger hardware: the parameters
# are stored per row, so existing passwords keep working.
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32

SESSION_BYTES = 32
SESSION_TTL_SECONDS = 60 * 60 * 24 * 30      # 30 days
# How many analyses a user keeps. Enforced on write, in the same
# transaction as the insert.
MAX_ANALYSES = 30
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,24}$")
MIN_PASSWORD = 8

# Enough to be a real check without pretending to be a breach corpus. These
# are the passwords a throwaway account actually gets.
COMMON_PASSWORDS = frozenset({
    "password", "password1", "password123", "12345678", "123456789",
    "qwerty123", "letmein1", "welcome1", "abc12345", "iloveyou",
    "chessgame", "chess123", "football", "baseball", "sunshine",
})


class AccountError(Exception):
    """A problem the user caused and can fix. Safe to show them."""


@dataclass
class User:
    id: str
    username: str
    created_at: float


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL,
    -- Lowercased, so "Alice" and "alice" cannot both be registered. The
    -- display form is kept in `username` so the user sees what they typed.
    username_key  TEXT NOT NULL UNIQUE,
    password_hash BLOB NOT NULL,
    salt          BLOB NOT NULL,
    -- The scrypt parameters this hash was made with, so they can be raised
    -- later without locking out existing users.
    n INTEGER NOT NULL, r INTEGER NOT NULL, p INTEGER NOT NULL,
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    -- The HASH of the token, never the token. A database backup must not be
    -- a set of working credentials.
    token_hash  BLOB PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS user_stats (
    user_id         TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    puzzle_rating   INTEGER NOT NULL DEFAULT 1500,
    puzzle_attempts INTEGER NOT NULL DEFAULT 0,
    puzzles_solved  INTEGER NOT NULL DEFAULT 0,
    games_analysed  INTEGER NOT NULL DEFAULT 0,
    current_streak  INTEGER NOT NULL DEFAULT 0,
    best_streak     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS analyses (
    id             TEXT PRIMARY KEY,
    user_id        TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source_type    TEXT NOT NULL,
    -- The game's URL, or a hash of the PGN. The unique index below turns
    -- re-analysing the same game into an UPDATE instead of a 31st row that
    -- evicts someone's oldest.
    source_ref     TEXT,
    white          TEXT,
    black          TEXT,
    result         TEXT,
    played_at      TEXT,
    accuracy_white REAL,
    accuracy_black REAL,
    plies          INTEGER,
    -- Denormalised above, whole report below. The list view needs names,
    -- result, date and accuracy; pulling a 100 KB blob per row to render a
    -- list is megabytes of work for a few hundred bytes of output.
    --
    -- zlib-compressed JSON. A 40-move game with commentary is 80-150 KB
    -- raw and ~15 KB compressed, so the 30-game cap is well under half a
    -- megabyte per user -- which matters on a 512 MB box.
    result_json    BLOB NOT NULL,
    -- What produced it. A saved report is a SNAPSHOT, not a live query, so
    -- these are how a stale one is identified rather than merely old.
    depth          INTEGER,
    prompt_version TEXT,
    created_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS analyses_user ON analyses(user_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS analyses_dedup
    ON analyses(user_id, source_ref) WHERE source_ref IS NOT NULL;

CREATE TABLE IF NOT EXISTS puzzle_attempts (
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    puzzle_id  TEXT NOT NULL,
    solved     INTEGER NOT NULL,
    ms_taken   INTEGER,
    created_at REAL NOT NULL,
    PRIMARY KEY (user_id, puzzle_id)
);
"""


class AccountStore:
    """
    SQLite-backed accounts. One connection, one lock.

    `check_same_thread=False` with an explicit lock rather than a connection
    per thread: the workload is a handful of tiny statements per request, so
    the lock is never contended, and one connection keeps WAL behaviour
    predictable under uvicorn's thread pool.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.executescript(SCHEMA)
            self._db.commit()

    # -- passwords -------------------------------------------------------

    @staticmethod
    def _hash(password: str, salt: bytes, n: int = SCRYPT_N,
              r: int = SCRYPT_R, p: int = SCRYPT_P) -> bytes:
        return hashlib.scrypt(password.encode("utf-8"), salt=salt,
                              n=n, r=r, p=p, dklen=SCRYPT_DKLEN)

    @staticmethod
    def validate_credentials(username: str, password: str) -> None:
        """Raise AccountError with a message the user can act on."""
        if not USERNAME_RE.match(username or ""):
            raise AccountError(
                "Username must be 3-24 characters, using letters, digits, "
                "dot, dash or underscore."
            )
        if len(password or "") < MIN_PASSWORD:
            raise AccountError(
                f"Password must be at least {MIN_PASSWORD} characters."
            )
        if password.lower() in COMMON_PASSWORDS:
            raise AccountError("That password is too common. Pick another.")
        if password.lower() == username.lower():
            raise AccountError("Password must not be your username.")

    # -- accounts --------------------------------------------------------

    def create_user(self, username: str, password: str) -> Tuple[User, str]:
        self.validate_credentials(username, password)
        key = username.lower()
        salt = secrets.token_bytes(16)
        digest = self._hash(password, salt)
        uid = secrets.token_hex(16)
        now = time.time()
        with self._lock:
            exists = self._db.execute(
                "SELECT 1 FROM users WHERE username_key = ?", (key,)).fetchone()
            if exists:
                raise AccountError("That username is taken.")
            self._db.execute(
                "INSERT INTO users (id, username, username_key, password_hash,"
                " salt, n, r, p, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (uid, username, key, digest, salt,
                 SCRYPT_N, SCRYPT_R, SCRYPT_P, now))
            self._db.execute(
                "INSERT INTO user_stats (user_id) VALUES (?)", (uid,))
            self._db.commit()
        return User(uid, username, now), self._new_session(uid)

    def login(self, username: str, password: str) -> Tuple[User, str]:
        key = (username or "").lower()
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM users WHERE username_key = ?", (key,)).fetchone()
        if row is None:
            # Hash anyway, so a missing username and a wrong password take
            # the same time and the endpoint cannot be used to enumerate
            # accounts.
            self._hash(password or "", b"decoy-salt-16byt")
            raise AccountError("Wrong username or password.")
        expected = bytes(row["password_hash"])
        actual = self._hash(password or "", bytes(row["salt"]),
                            row["n"], row["r"], row["p"])
        if not hmac.compare_digest(expected, actual):
            raise AccountError("Wrong username or password.")
        return (User(row["id"], row["username"], row["created_at"]),
                self._new_session(row["id"]))

    # -- sessions --------------------------------------------------------

    @staticmethod
    def _token_hash(token: str) -> bytes:
        return hashlib.sha256(token.encode("utf-8")).digest()

    def _new_session(self, user_id: str) -> str:
        token = base64.urlsafe_b64encode(
            secrets.token_bytes(SESSION_BYTES)).decode("ascii").rstrip("=")
        now = time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO sessions (token_hash, user_id, created_at,"
                " expires_at) VALUES (?,?,?,?)",
                (self._token_hash(token), user_id, now,
                 now + SESSION_TTL_SECONDS))
            self._db.commit()
        return token

    def session_user(self, token: str) -> Optional[User]:
        """The user a token belongs to, or None. Expired sessions are swept."""
        if not token:
            return None
        now = time.time()
        with self._lock:
            row = self._db.execute(
                "SELECT u.id, u.username, u.created_at, s.expires_at"
                " FROM sessions s JOIN users u ON u.id = s.user_id"
                " WHERE s.token_hash = ?",
                (self._token_hash(token),)).fetchone()
            if row is None:
                return None
            if row["expires_at"] < now:
                self._db.execute("DELETE FROM sessions WHERE token_hash = ?",
                                 (self._token_hash(token),))
                self._db.commit()
                return None
            # Extend a session in use, so an active user is not logged out
            # mid-puzzle. Only when it is more than a day old, to keep this
            # from writing on every single request.
            if row["expires_at"] - now < SESSION_TTL_SECONDS - 86400:
                self._db.execute(
                    "UPDATE sessions SET expires_at = ? WHERE token_hash = ?",
                    (now + SESSION_TTL_SECONDS, self._token_hash(token)))
                self._db.commit()
        return User(row["id"], row["username"], row["created_at"])

    def logout(self, token: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM sessions WHERE token_hash = ?",
                             (self._token_hash(token),))
            self._db.commit()

    # -- progress --------------------------------------------------------

    def stats(self, user_id: str) -> Dict[str, Any]:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM user_stats WHERE user_id = ?",
                (user_id,)).fetchone()
            if row is None:
                self._db.execute(
                    "INSERT INTO user_stats (user_id) VALUES (?)", (user_id,))
                self._db.commit()
                row = self._db.execute(
                    "SELECT * FROM user_stats WHERE user_id = ?",
                    (user_id,)).fetchone()
        return dict(row)

    def record_attempt(self, user_id: str, puzzle_id: str, solved: bool,
                       rating_after: int, ms_taken: Optional[int] = None
                       ) -> Dict[str, Any]:
        """
        Record one attempt and update the stats, in a single transaction.

        Attempt and stats move together or not at all -- the same reason the
        Supabase path uses one Postgres function (C.2). A crash between the
        two would leave a rating that does not match its history.
        """
        now = time.time()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute(
                    "INSERT OR REPLACE INTO puzzle_attempts"
                    " (user_id, puzzle_id, solved, ms_taken, created_at)"
                    " VALUES (?,?,?,?,?)",
                    (user_id, puzzle_id, 1 if solved else 0, ms_taken, now))
                self._db.execute(
                    "INSERT OR IGNORE INTO user_stats (user_id) VALUES (?)",
                    (user_id,))
                self._db.execute(
                    "UPDATE user_stats SET"
                    "  puzzle_rating = ?,"
                    "  puzzle_attempts = puzzle_attempts + 1,"
                    "  puzzles_solved = puzzles_solved + ?,"
                    "  current_streak = CASE WHEN ? THEN current_streak + 1"
                    "                        ELSE 0 END,"
                    "  best_streak = MAX(best_streak,"
                    "                    CASE WHEN ? THEN current_streak + 1"
                    "                         ELSE 0 END)"
                    " WHERE user_id = ?",
                    (rating_after, 1 if solved else 0,
                     1 if solved else 0, 1 if solved else 0, user_id))
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
        return self.stats(user_id)

    # -- saved analyses --------------------------------------------------

    def save_analysis(self, user_id: str, *, source_type: str,
                      source_ref: Optional[str], report: Dict[str, Any],
                      depth: Optional[int] = None,
                      prompt_version: Optional[str] = None) -> str:
        """
        Store one analysis, keeping at most MAX_ANALYSES per user.

        The insert and the eviction are ONE transaction. A separate cleanup
        job is a second thing that can fail, and between the two the user
        would briefly have 31 -- doing it inline means the invariant is
        never false.

        Re-analysing the same game updates that row rather than adding
        another, so a user who re-runs one game does not lose their oldest.
        """
        import json as _json                        # noqa: PLC0415
        import zlib                                 # noqa: PLC0415

        blob = zlib.compress(
            _json.dumps(report, separators=(",", ":")).encode("utf-8"), 6)
        headers = report.get("headers") or {}
        white = report.get("white") or {}
        black = report.get("black") or {}
        now = time.time()
        analysis_id = secrets.token_hex(12)

        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = None
                if source_ref:
                    row = self._db.execute(
                        "SELECT id FROM analyses WHERE user_id = ?"
                        " AND source_ref = ?", (user_id, source_ref)).fetchone()
                if row is not None:
                    analysis_id = row["id"]
                    self._db.execute(
                        "UPDATE analyses SET source_type=?, white=?, black=?,"
                        " result=?, played_at=?, accuracy_white=?,"
                        " accuracy_black=?, plies=?, result_json=?, depth=?,"
                        " prompt_version=?, created_at=? WHERE id=?",
                        (source_type, white.get("name"), black.get("name"),
                         headers.get("Result"), headers.get("Date"),
                         white.get("accuracy"), black.get("accuracy"),
                         len(report.get("moves") or []), blob, depth,
                         prompt_version, now, analysis_id))
                else:
                    self._db.execute(
                        "INSERT INTO analyses (id, user_id, source_type,"
                        " source_ref, white, black, result, played_at,"
                        " accuracy_white, accuracy_black, plies, result_json,"
                        " depth, prompt_version, created_at)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (analysis_id, user_id, source_type, source_ref,
                         white.get("name"), black.get("name"),
                         headers.get("Result"), headers.get("Date"),
                         white.get("accuracy"), black.get("accuracy"),
                         len(report.get("moves") or []), blob, depth,
                         prompt_version, now))
                    self._db.execute(
                        "DELETE FROM analyses WHERE user_id = ? AND id NOT IN ("
                        "  SELECT id FROM analyses WHERE user_id = ?"
                        "  ORDER BY created_at DESC LIMIT ?)",
                        (user_id, user_id, MAX_ANALYSES))
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
        return analysis_id

    def list_analyses(self, user_id: str) -> List[Dict[str, Any]]:
        """Summaries only -- `result_json` is deliberately not selected."""
        with self._lock:
            rows = self._db.execute(
                "SELECT id, source_type, source_ref, white, black, result,"
                " played_at, accuracy_white, accuracy_black, plies, depth,"
                " prompt_version, created_at FROM analyses"
                " WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,)).fetchall()
        return [dict(r) for r in rows]

    def get_analysis(self, user_id: str, analysis_id: str
                     ) -> Optional[Dict[str, Any]]:
        """
        One full report, or None.

        Filtered by user_id as well as id: not found and not yours must be
        the same answer, or the endpoint reveals that an id exists.
        """
        import json as _json                        # noqa: PLC0415
        import zlib                                 # noqa: PLC0415
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM analyses WHERE id = ? AND user_id = ?",
                (analysis_id, user_id)).fetchone()
        if row is None:
            return None
        out = dict(row)
        # `report`, not `result`: the summary already uses `result` for the
        # game's outcome ("1-0"), and reusing the name for the whole report
        # made a field that is a string in one shape and an object in the
        # other -- which the frontend's type checker refused outright.
        out["report"] = _json.loads(zlib.decompress(bytes(row["result_json"])))
        out.pop("result_json", None)
        return out

    def delete_analysis(self, user_id: str, analysis_id: str) -> bool:
        with self._lock:
            cur = self._db.execute(
                "DELETE FROM analyses WHERE id = ? AND user_id = ?",
                (analysis_id, user_id))
            self._db.commit()
        return cur.rowcount > 0

    def attempted_ids(self, user_id: str) -> List[str]:
        with self._lock:
            rows = self._db.execute(
                "SELECT puzzle_id FROM puzzle_attempts WHERE user_id = ?",
                (user_id,)).fetchall()
        return [r["puzzle_id"] for r in rows]

    def close(self) -> None:
        with self._lock:
            self._db.close()
