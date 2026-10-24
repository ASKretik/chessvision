"""
C.1c -- accounts in Cloud Firestore.

WHY THIS EXISTS
---------------
Render's free tier wipes the local disk on every spin-down, restart and
redeploy, so the SQLite store in accounts.py lost every account after 15 idle
minutes. This is the same contract -- the same methods, return shapes,
password hashing and session rules -- with the rows kept in Firestore, whose
free Spark plan needs no payment method.

partE picks it when FIREBASE_SERVICE_ACCOUNT is set, and SQLite otherwise.

WHY REST AND NOT THE SDK
------------------------
google-cloud-firestore pulls in gRPC and protobuf: tens of MB resident on a
512 MB box that is already at its limit. Firestore's REST API through httpx
(already a dependency) needs nothing but `cryptography`, to sign the
service-account assertion that is exchanged for an OAuth token.

DATA MODEL  (collection/document id: fields)
----------
  users/{uid}                 username, username_key, password_hash, salt,
                              n, r, p, created_at
  usernames/{username_key}    user_id. Created with exists=false in the same
                              commit as the user: this is the uniqueness rule
  sessions/{sha256(token)}    user_id, username, user_created_at, created_at,
                              expires_at. Only the token's HASH, as in SQLite
  user_stats/{uid}            the stats row, plus `attempted`: the ids of
                              every puzzle tried, so choosing the next puzzle
                              is one read instead of one per past attempt
  puzzle_attempts/{uid}__{puzzle_id}
                              user_id, puzzle_id, solved, ms_taken, created_at
  analyses/{id}               the summary columns, plus result_json: the
                              report as zlib-compressed JSON bytes

THE FREE QUOTA
--------------
50,000 reads and 20,000 writes a day. The hot path is one read per signed-in
request (the session document carries the username, so resolving a token
never needs the user document). A puzzle attempt is one transaction.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
import zlib
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

from accounts import (MAX_ANALYSES, SCRYPT_N, SCRYPT_P, SCRYPT_R,
                      SESSION_BYTES, SESSION_TTL_SECONDS, USERNAME_RE,
                      AccountError, AccountStore, AccountStoreUnavailable,
                      User)

FIRESTORE_ROOT = "https://firestore.googleapis.com/v1"
SCOPE = "https://www.googleapis.com/auth/datastore"

# Firestore reserves document ids of the form __name__; a username like that
# would fail with a confusing INVALID_ARGUMENT instead of a clear message.
_RESERVED_ID = re.compile(r"^__.*__$")
_ANALYSIS_ID = re.compile(r"^[0-9a-f]{24}$")
# Ids that become part of a document path are checked before use, so that
# nothing a client sends can contain a "/" and address another document.
_PUZZLE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# A document is capped at 1 MiB. A compressed report is ~15 KB, so this only
# trips on something pathological, and saving is best effort anyway.
_MAX_REPORT_BYTES = 900_000

_DEFAULT_STATS = {
    "puzzle_rating": 1500, "puzzle_attempts": 0, "puzzles_solved": 0,
    "games_analysed": 0, "current_streak": 0, "best_streak": 0,
}
_SUMMARY_FIELDS = (
    "source_type", "source_ref", "white", "black", "result", "played_at",
    "accuracy_white", "accuracy_black", "plies", "depth", "prompt_version",
    "created_at",
)


class FirestoreError(AccountStoreUnavailable):
    """A non-2xx answer from Firestore. `status` is its canonical code,
    e.g. ALREADY_EXISTS or ABORTED."""

    def __init__(self, http_status: int, status: str, message: str) -> None:
        super().__init__(f"Firestore {status or http_status}: {message}")
        self.http_status = http_status
        self.status = status


# --------------------------------------------------------------------------
# Values: Python <-> Firestore's typed JSON
# --------------------------------------------------------------------------
def encode_value(v: Any) -> Dict[str, Any]:
    if v is None:
        return {"nullValue": None}
    if isinstance(v, bool):                       # before int: bool is an int
        return {"booleanValue": v}
    if isinstance(v, int):
        return {"integerValue": str(v)}           # int64 travels as a string
    if isinstance(v, float):
        return {"doubleValue": v}
    if isinstance(v, str):
        return {"stringValue": v}
    if isinstance(v, (bytes, bytearray)):
        return {"bytesValue": base64.b64encode(bytes(v)).decode("ascii")}
    if isinstance(v, (list, tuple)):
        return {"arrayValue": {"values": [encode_value(x) for x in v]}}
    if isinstance(v, dict):
        return {"mapValue": {"fields": encode_fields(v)}}
    raise TypeError(f"Cannot store {type(v).__name__} in Firestore")


def decode_value(v: Dict[str, Any]) -> Any:
    if "nullValue" in v:
        return None
    if "booleanValue" in v:
        return bool(v["booleanValue"])
    if "integerValue" in v:
        return int(v["integerValue"])
    if "doubleValue" in v:
        return float(v["doubleValue"])
    if "stringValue" in v:
        return v["stringValue"]
    if "bytesValue" in v:
        return base64.b64decode(v["bytesValue"])
    if "arrayValue" in v:
        return [decode_value(x) for x in v["arrayValue"].get("values", [])]
    if "mapValue" in v:
        return decode_fields(v["mapValue"].get("fields", {}))
    if "timestampValue" in v:
        return v["timestampValue"]
    raise ValueError(f"Unsupported Firestore value: {sorted(v)}")


def encode_fields(d: Dict[str, Any]) -> Dict[str, Any]:
    return {k: encode_value(v) for k, v in d.items()}


def decode_fields(d: Dict[str, Any]) -> Dict[str, Any]:
    return {k: decode_value(v) for k, v in d.items()}


# --------------------------------------------------------------------------
# Service-account credentials -> OAuth access token
# --------------------------------------------------------------------------
def parse_service_account(raw: str) -> Dict[str, Any]:
    """
    The key file Firebase downloads, as JSON or as base64 of that JSON.
    Validated here so a bad paste fails at boot with a clear reason rather
    than on the first sign-in.
    """
    raw = raw.strip()
    if not raw.startswith("{"):
        try:
            raw = base64.b64decode(raw).decode("utf-8")
        except Exception as exc:
            raise ValueError("FIREBASE_SERVICE_ACCOUNT is neither JSON nor "
                             "base64-encoded JSON") from exc
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"FIREBASE_SERVICE_ACCOUNT is not valid JSON: {exc}") from exc
    missing = [k for k in ("project_id", "client_email", "private_key")
               if not info.get(k)]
    if missing:
        raise ValueError("FIREBASE_SERVICE_ACCOUNT is missing "
                         + ", ".join(missing)
                         + " -- paste the whole key file from Firebase")
    return info


class ServiceAccountTokens:
    """
    Google's OAuth 2.0 service-account flow, by hand: sign a JWT with the
    account's private key, exchange it for an access token, cache the token
    until a minute before it expires.
    """

    def __init__(self, info: Dict[str, Any], http: httpx.Client) -> None:
        from cryptography.hazmat.primitives import serialization  # noqa: PLC0415

        self._email = info["client_email"]
        self._key_id = info.get("private_key_id")
        self._token_uri = info.get("token_uri") or "https://oauth2.googleapis.com/token"
        try:
            self._key = serialization.load_pem_private_key(
                info["private_key"].encode("utf-8"), password=None)
        except Exception as exc:
            raise ValueError("FIREBASE_SERVICE_ACCOUNT private_key could not "
                             f"be read: {exc}") from exc
        self._http = http
        self._lock = threading.Lock()
        self._token: Optional[str] = None
        self._expires = 0.0

    def assertion(self, now: Optional[float] = None) -> str:
        from cryptography.hazmat.primitives import hashes  # noqa: PLC0415
        from cryptography.hazmat.primitives.asymmetric import padding  # noqa: PLC0415

        now = int(now if now is not None else time.time())
        header = {"alg": "RS256", "typ": "JWT"}
        if self._key_id:
            header["kid"] = self._key_id
        claims = {"iss": self._email, "scope": SCOPE, "aud": self._token_uri,
                  "iat": now, "exp": now + 3600}

        def b64(obj: Dict[str, Any]) -> str:
            raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
            return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

        signing_input = f"{b64(header)}.{b64(claims)}".encode("ascii")
        sig = self._key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        return (signing_input.decode("ascii") + "."
                + base64.urlsafe_b64encode(sig).decode("ascii").rstrip("="))

    def __call__(self) -> str:
        with self._lock:
            if self._token and time.time() < self._expires - 60:
                return self._token
            try:
                res = self._http.post(self._token_uri, data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": self.assertion(),
                })
            except httpx.HTTPError as exc:
                raise AccountStoreUnavailable(
                    f"Could not reach Google to sign in to Firestore: {exc}") from exc
            if res.status_code != 200:
                raise AccountStoreUnavailable(
                    "Firebase rejected the service-account key "
                    f"({res.status_code}): {res.text[:200]}")
            body = res.json()
            self._token = body["access_token"]
            self._expires = time.time() + float(body.get("expires_in", 3600))
            return self._token


# --------------------------------------------------------------------------
# A thin Firestore REST client: documents, commits, queries, transactions
# --------------------------------------------------------------------------
Write = Dict[str, Any]


class Firestore:
    def __init__(self, project_id: str, token: Callable[[], str], *,
                 http: httpx.Client, database: str = "(default)",
                 prefix: str = "", root: str = FIRESTORE_ROOT) -> None:
        self.project_id = project_id
        self.prefix = prefix
        self._token = token
        self._http = http
        self._db = f"projects/{project_id}/databases/{database}"
        self._docs = f"{self._db}/documents"
        self._root = root.rstrip("/")

    # -- plumbing ----------------------------------------------------------
    def name(self, collection: str, doc_id: str) -> str:
        return f"{self._docs}/{self.prefix}{collection}/{doc_id}"

    def _request(self, method: str, path: str, *, params: Any = None,
                 body: Any = None) -> Any:
        try:
            res = self._http.request(
                method, f"{self._root}/{path}", params=params, json=body,
                headers={"Authorization": f"Bearer {self._token()}"})
        except httpx.HTTPError as exc:
            raise AccountStoreUnavailable(f"Firestore unreachable: {exc}") from exc
        if res.status_code == 404 and method == "GET":
            return None
        if res.status_code >= 400:
            try:
                err = res.json()
                err = (err[0] if isinstance(err, list) else err).get("error", {})
            except Exception:
                err = {}
            raise FirestoreError(res.status_code, err.get("status", ""),
                                 err.get("message", res.text[:200]))
        return res.json() if res.content else None

    # -- reads -------------------------------------------------------------
    def get(self, collection: str, doc_id: str,
            transaction: Optional[str] = None,
            fields: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
        """One document's fields, or None. `fields` fetches only those."""
        params: Dict[str, Any] = {}
        if transaction:
            params["transaction"] = transaction
        if fields:
            params["mask.fieldPaths"] = fields
        doc = self._request("GET", self.name(collection, doc_id),
                            params=params or None)
        return None if doc is None else decode_fields(doc.get("fields", {}))

    def query_equal(self, collection: str, field: str, value: Any, *,
                    select: Optional[List[str]] = None,
                    transaction: Optional[str] = None
                    ) -> List[Tuple[str, Dict[str, Any]]]:
        """Every document whose `field` equals `value`, as (id, fields).
        A single equality filter needs no composite index."""
        query: Dict[str, Any] = {
            "from": [{"collectionId": f"{self.prefix}{collection}"}],
            "where": {"fieldFilter": {"field": {"fieldPath": field},
                                      "op": "EQUAL",
                                      "value": encode_value(value)}},
        }
        if select is not None:
            query["select"] = {"fields": [{"fieldPath": f} for f in select]}
        body: Dict[str, Any] = {"structuredQuery": query}
        if transaction:
            body["transaction"] = transaction
        rows = self._request("POST", f"{self._docs}:runQuery", body=body) or []
        out = []
        for row in rows:
            doc = row.get("document")
            if doc:
                out.append((doc["name"].rsplit("/", 1)[1],
                            decode_fields(doc.get("fields", {}))))
        return out

    # -- writes ------------------------------------------------------------
    def set(self, collection: str, doc_id: str, fields: Dict[str, Any], *,
            exists: Optional[bool] = None, mask: Optional[List[str]] = None,
            append: Optional[Dict[str, List[Any]]] = None) -> Write:
        """
        A write that sets `fields`. `mask` limits it to those fields and
        leaves the rest of the document alone; `exists` is a precondition;
        `append` adds elements to arrays that are not already there.
        """
        write: Write = {"update": {"name": self.name(collection, doc_id),
                                   "fields": encode_fields(fields)}}
        if mask is not None:
            write["updateMask"] = {"fieldPaths": mask}
        if exists is not None:
            write["currentDocument"] = {"exists": exists}
        if append:
            write["updateTransforms"] = [
                {"fieldPath": f, "appendMissingElements":
                    {"values": [encode_value(x) for x in values]}}
                for f, values in append.items()]
        return write

    def delete(self, collection: str, doc_id: str) -> Write:
        return {"delete": self.name(collection, doc_id)}

    def commit(self, writes: List[Write], transaction: Optional[str] = None) -> None:
        """All writes apply, or none do."""
        body: Dict[str, Any] = {"writes": writes}
        if transaction:
            body["transaction"] = transaction
        self._request("POST", f"{self._docs}:commit", body=body)

    # -- transactions ------------------------------------------------------
    def run_transaction(self, fn: Callable[[str], List[Write]],
                        attempts: int = 5) -> None:
        """
        Read inside `fn(tx)`, return the writes, commit them atomically.
        Firestore aborts a transaction that loses a race; that is retried.
        """
        for attempt in range(attempts):
            tx = self._request("POST", f"{self._docs}:beginTransaction",
                               body={"options": {"readWrite": {}}})["transaction"]
            try:
                self.commit(fn(tx), transaction=tx)
                return
            except FirestoreError as exc:
                self._rollback(tx)
                if exc.status != "ABORTED" or attempt == attempts - 1:
                    raise
                time.sleep(0.05 * (attempt + 1))
            except BaseException:
                self._rollback(tx)
                raise

    def _rollback(self, tx: str) -> None:
        try:
            self._request("POST", f"{self._docs}:rollback", body={"transaction": tx})
        except AccountStoreUnavailable:
            pass                       # the transaction expires on its own


# --------------------------------------------------------------------------
# The account store
# --------------------------------------------------------------------------
class FirestoreAccountStore:
    """accounts.AccountStore's contract, backed by Firestore."""

    backend = "firestore"

    def __init__(self, fs: Firestore) -> None:
        self.fs = fs
        self.project_id = fs.project_id

    @classmethod
    def from_env(cls, raw: str, *, prefix: str = "",
                 timeout: float = 10.0) -> "FirestoreAccountStore":
        info = parse_service_account(raw)
        http = httpx.Client(timeout=timeout)
        tokens = ServiceAccountTokens(info, http)
        return cls(Firestore(info["project_id"], tokens, http=http, prefix=prefix))

    # -- accounts --------------------------------------------------------
    def create_user(self, username: str, password: str) -> Tuple[User, str]:
        AccountStore.validate_credentials(username, password)
        key = username.lower()
        if _RESERVED_ID.match(key):
            raise AccountError("Usernames cannot start and end with two underscores.")
        salt = secrets.token_bytes(16)
        digest = AccountStore._hash(password, salt)
        uid = secrets.token_hex(16)
        now = time.time()
        try:
            # One commit: the name is claimed only if the user is created,
            # and the user is created only if the name was free.
            self.fs.commit([
                self.fs.set("usernames", key, {"user_id": uid}, exists=False),
                self.fs.set("users", uid, {
                    "username": username, "username_key": key,
                    "password_hash": digest, "salt": salt,
                    "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P,
                    "created_at": now}, exists=False),
                self.fs.set("user_stats", uid,
                            {"user_id": uid, **_DEFAULT_STATS, "attempted": []},
                            exists=False),
            ])
        except FirestoreError as exc:
            if exc.status in ("ALREADY_EXISTS", "FAILED_PRECONDITION"):
                raise AccountError("That username is taken.") from exc
            raise
        user = User(uid, username, now)
        return user, self._new_session(user)

    def login(self, username: str, password: str) -> Tuple[User, str]:
        key = (username or "").lower()
        ref = row = None
        # A name that fails the signup rules cannot exist, so it is never
        # looked up -- and so never becomes part of a document path.
        if USERNAME_RE.match(key) and not _RESERVED_ID.match(key):
            ref = self.fs.get("usernames", key)
            row = self.fs.get("users", ref["user_id"]) if ref else None
        if row is None:
            # Hash anyway, so a missing username and a wrong password take
            # the same time and the endpoint cannot enumerate accounts.
            AccountStore._hash(password or "", b"decoy-salt-16byt")
            raise AccountError("Wrong username or password.")
        actual = AccountStore._hash(password or "", row["salt"],
                                    row["n"], row["r"], row["p"])
        if not hmac.compare_digest(row["password_hash"], actual):
            raise AccountError("Wrong username or password.")
        user = User(ref["user_id"], row["username"], row["created_at"])
        return user, self._new_session(user)

    # -- sessions --------------------------------------------------------
    @staticmethod
    def _session_id(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _new_session(self, user: User) -> str:
        token = base64.urlsafe_b64encode(
            secrets.token_bytes(SESSION_BYTES)).decode("ascii").rstrip("=")
        now = time.time()
        self.fs.commit([self.fs.set("sessions", self._session_id(token), {
            "user_id": user.id, "username": user.username,
            "user_created_at": user.created_at,
            "created_at": now, "expires_at": now + SESSION_TTL_SECONDS})])
        return token

    def session_user(self, token: str) -> Optional[User]:
        if not token:
            return None
        sid = self._session_id(token)
        row = self.fs.get("sessions", sid)
        if row is None:
            return None
        now = time.time()
        if row["expires_at"] < now:
            self.fs.commit([self.fs.delete("sessions", sid)])
            return None
        # Extend a session in use, at most once a day, as the SQLite store does.
        if row["expires_at"] - now < SESSION_TTL_SECONDS - 86400:
            self.fs.commit([self.fs.set("sessions", sid,
                                        {"expires_at": now + SESSION_TTL_SECONDS},
                                        mask=["expires_at"], exists=True)])
        return User(row["user_id"], row["username"], row["user_created_at"])

    def logout(self, token: str) -> None:
        if token:
            self.fs.commit([self.fs.delete("sessions", self._session_id(token))])

    # -- progress --------------------------------------------------------
    @staticmethod
    def _stats_row(user_id: str, row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        row = row or {}
        return {"user_id": user_id,
                **{k: int(row.get(k, d)) for k, d in _DEFAULT_STATS.items()}}

    def stats(self, user_id: str) -> Dict[str, Any]:
        row = self.fs.get("user_stats", user_id)
        if row is None:
            try:
                self.fs.commit([self.fs.set(
                    "user_stats", user_id,
                    {"user_id": user_id, **_DEFAULT_STATS, "attempted": []},
                    exists=False)])
            except FirestoreError as exc:
                if exc.status not in ("ALREADY_EXISTS", "FAILED_PRECONDITION"):
                    raise
                row = self.fs.get("user_stats", user_id)
        return self._stats_row(user_id, row)

    def record_attempt(self, user_id: str, puzzle_id: str, solved: bool,
                       rating_after: int, ms_taken: Optional[int] = None
                       ) -> Dict[str, Any]:
        """The attempt and the stats move together or not at all."""
        if not _PUZZLE_ID.match(puzzle_id or ""):
            raise ValueError(f"Not a puzzle id: {puzzle_id!r}")
        out: Dict[str, Any] = {}

        def txn(tx: str) -> List[Write]:
            cur = self._stats_row(user_id, self.fs.get("user_stats", user_id, tx))
            streak = cur["current_streak"] + 1 if solved else 0
            new = {
                "user_id": user_id,
                "puzzle_rating": int(rating_after),
                "puzzle_attempts": cur["puzzle_attempts"] + 1,
                "puzzles_solved": cur["puzzles_solved"] + (1 if solved else 0),
                "games_analysed": cur["games_analysed"],
                "current_streak": streak,
                "best_streak": max(cur["best_streak"], streak),
            }
            out.clear()
            out.update(new)
            return [
                self.fs.set("puzzle_attempts", f"{user_id}__{puzzle_id}", {
                    "user_id": user_id, "puzzle_id": puzzle_id,
                    "solved": bool(solved), "ms_taken": ms_taken,
                    "created_at": time.time()}),
                self.fs.set("user_stats", user_id, new, mask=list(new),
                            append={"attempted": [puzzle_id]}),
            ]

        self.fs.run_transaction(txn)
        return dict(out)

    def attempted_ids(self, user_id: str) -> List[str]:
        row = self.fs.get("user_stats", user_id) or {}
        return [str(x) for x in row.get("attempted", [])]

    # -- saved analyses --------------------------------------------------
    def save_analysis(self, user_id: str, *, source_type: str,
                      source_ref: Optional[str], report: Dict[str, Any],
                      depth: Optional[int] = None,
                      prompt_version: Optional[str] = None) -> str:
        """
        Store one analysis, keeping at most MAX_ANALYSES per user, in one
        transaction. Re-analysing the same game overwrites that document --
        its id is derived from the source -- instead of evicting the oldest.
        """
        blob = zlib.compress(
            json.dumps(report, separators=(",", ":")).encode("utf-8"), 6)
        if len(blob) > _MAX_REPORT_BYTES:
            raise ValueError(f"report is {len(blob)} bytes compressed, over "
                             "Firestore's document limit")
        headers = report.get("headers") or {}
        white = report.get("white") or {}
        black = report.get("black") or {}
        if source_ref:
            analysis_id = hashlib.sha256(
                f"{user_id}\0{source_ref}".encode("utf-8")).hexdigest()[:24]
        else:
            analysis_id = secrets.token_hex(12)
        doc = {
            "user_id": user_id, "source_type": source_type,
            "source_ref": source_ref,
            "white": white.get("name"), "black": black.get("name"),
            "result": headers.get("Result"), "played_at": headers.get("Date"),
            "accuracy_white": white.get("accuracy"),
            "accuracy_black": black.get("accuracy"),
            "plies": len(report.get("moves") or []),
            "result_json": blob, "depth": depth,
            "prompt_version": prompt_version, "created_at": time.time(),
        }

        def txn(tx: str) -> List[Write]:
            mine = self.fs.query_equal("analyses", "user_id", user_id,
                                       select=["created_at"], transaction=tx)
            writes = [self.fs.set("analyses", analysis_id, doc)]
            others = [(d.get("created_at") or 0.0, i) for i, d in mine
                      if i != analysis_id]
            overflow = len(others) + 1 - MAX_ANALYSES
            if overflow > 0:
                for _, old_id in sorted(others)[:overflow]:
                    writes.append(self.fs.delete("analyses", old_id))
            return writes

        self.fs.run_transaction(txn)
        return analysis_id

    def list_analyses(self, user_id: str) -> List[Dict[str, Any]]:
        """Summaries only -- `result_json` is never fetched for a list."""
        rows = self.fs.query_equal("analyses", "user_id", user_id,
                                   select=list(_SUMMARY_FIELDS))
        out = [{"id": i, **{f: d.get(f) for f in _SUMMARY_FIELDS}}
               for i, d in rows]
        # Sorted here, not by Firestore: ordering on a second field would
        # need a composite index someone has to create by hand.
        out.sort(key=lambda r: r["created_at"] or 0.0, reverse=True)
        return out

    def get_analysis(self, user_id: str, analysis_id: str
                     ) -> Optional[Dict[str, Any]]:
        """Not found and not yours are the same answer."""
        if not _ANALYSIS_ID.match(analysis_id or ""):
            return None
        row = self.fs.get("analyses", analysis_id)
        if row is None or row.get("user_id") != user_id:
            return None
        out = {"id": analysis_id, "user_id": user_id,
               **{f: row.get(f) for f in _SUMMARY_FIELDS}}
        out["report"] = json.loads(zlib.decompress(row["result_json"]))
        return out

    def delete_analysis(self, user_id: str, analysis_id: str) -> bool:
        if not _ANALYSIS_ID.match(analysis_id or ""):
            return False
        row = self.fs.get("analyses", analysis_id, fields=["user_id"])
        if row is None or row.get("user_id") != user_id:
            return False
        self.fs.commit([self.fs.delete("analyses", analysis_id)])
        return True

    # -- ops ---------------------------------------------------------------
    def ping(self) -> Dict[str, Any]:
        """One read, to prove the credentials and the database both work."""
        started = time.perf_counter()
        self.fs.get("usernames", "-ping-")
        return {"ok": True, "backend": self.backend, "project": self.project_id,
                "latency_ms": round((time.perf_counter() - started) * 1000)}

    def close(self) -> None:
        self.fs._http.close()
