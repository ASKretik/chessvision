"""
C.1c -- the Firestore account store, offline.

Firestore is replaced by FakeFirestore: an in-memory implementation of the
handful of REST calls the store makes (get, commit, runQuery, transactions),
mounted on an httpx MockTransport. It is STRICT -- an unknown key in a write
or query, or a malformed typed value, is a test failure -- so the store is
held to the documented request shapes, not just to its own expectations.

Three things are proven:
  1. The same contract as the SQLite store: the same call sequence gives the
     same answers from both (compared side by side).
  2. What is stored: hashes, not passwords or tokens; compressed reports.
  3. The HTTP layer: partE's endpoints run on it unchanged, and a Firestore
     outage is a 503 with a reason, not a 500.

    .venv/bin/python tests/test_firestore_store.py
"""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import time
import zlib
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("STOCKFISH_PATH", "stockfish")

import httpx                                       # noqa: E402

import accounts                                    # noqa: E402
import firestore_store as fsmod                    # noqa: E402

_results: List[Tuple[str, bool, str]] = []
PROJECT = "demo-proj"
DOCS = f"projects/{PROJECT}/databases/(default)/documents"


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))


# --------------------------------------------------------------------------
# A strict fake of the Firestore REST API
# --------------------------------------------------------------------------
VALUE_KEYS = {"nullValue", "booleanValue", "integerValue", "doubleValue",
              "stringValue", "bytesValue", "arrayValue", "mapValue",
              "timestampValue"}
WRITE_KEYS = {"update", "delete", "updateMask", "currentDocument", "updateTransforms"}
QUERY_KEYS = {"from", "where", "select", "orderBy", "limit"}


class FakeFirestore:
    def __init__(self) -> None:
        self.docs: Dict[str, Dict[str, Any]] = {}     # full name -> fields (typed)
        self.calls: List[str] = []
        self.reads = 0
        self.abort_next_commits = 0
        self.down = False
        self._tx = 0

    # -- validation -------------------------------------------------------
    def _value(self, v: Any) -> None:
        assert isinstance(v, dict) and len(v) == 1, f"bad value {v!r}"
        (k, inner), = v.items()
        assert k in VALUE_KEYS, f"unknown value type {k}"
        if k == "integerValue":
            assert isinstance(inner, str) and inner.lstrip("-").isdigit(), inner
        if k == "bytesValue":
            base64.b64decode(inner, validate=True)
        if k == "arrayValue":
            for x in inner.get("values", []):
                self._value(x)
        if k == "mapValue":
            for x in inner.get("fields", {}).values():
                self._value(x)

    def _fields(self, fields: Dict[str, Any]) -> None:
        for v in fields.values():
            self._value(v)

    # -- transport ----------------------------------------------------------
    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == "Bearer test-token", \
            "no bearer token"
        path = request.url.path.removeprefix("/v1/")
        self.calls.append(f"{request.method} {path.rsplit('/', 1)[-1]}")
        if self.down:
            return self._error(503, "UNAVAILABLE", "the service is down")
        if request.method == "GET":
            return self._get(path, request.url.params)
        body = json.loads(request.content or b"{}")
        if path == f"{DOCS}:commit":
            return self._commit(body)
        if path == f"{DOCS}:runQuery":
            return self._query(body)
        if path == f"{DOCS}:beginTransaction":
            assert body == {"options": {"readWrite": {}}}, body
            self._tx += 1
            return httpx.Response(200, json={"transaction": f"tx{self._tx}"})
        if path == f"{DOCS}:rollback":
            assert "transaction" in body
            return httpx.Response(200, json={})
        raise AssertionError(f"unexpected call {request.method} {path}")

    @staticmethod
    def _error(code: int, status: str, msg: str) -> httpx.Response:
        return httpx.Response(code, json={"error": {"code": code, "status": status,
                                                    "message": msg}})

    def _get(self, name: str, params: httpx.QueryParams) -> httpx.Response:
        assert set(params) <= {"transaction", "mask.fieldPaths"}, dict(params)
        self.reads += 1
        if name not in self.docs:
            return self._error(404, "NOT_FOUND", f"No document to get: {name}")
        fields = self.docs[name]
        mask = params.get_list("mask.fieldPaths")
        if mask:
            fields = {k: v for k, v in fields.items() if k in mask}
        return httpx.Response(200, json={"name": name, "fields": fields})

    def _commit(self, body: Dict[str, Any]) -> httpx.Response:
        assert set(body) <= {"writes", "transaction"}, set(body)
        if self.abort_next_commits:
            self.abort_next_commits -= 1
            return self._error(409, "ABORTED", "Transaction lock timeout")
        writes = body["writes"]
        # Preconditions are checked for every write before any is applied:
        # a commit is atomic.
        for w in writes:
            assert set(w) <= WRITE_KEYS, set(w)
            assert ("update" in w) != ("delete" in w), w
            name = w["update"]["name"] if "update" in w else w["delete"]
            assert name.startswith(DOCS + "/"), name
            if "update" in w:
                self._fields(w["update"].get("fields", {}))
            pre = w.get("currentDocument")
            if pre is not None:
                assert set(pre) == {"exists"}, pre
                if pre["exists"] is False and name in self.docs:
                    return self._error(409, "ALREADY_EXISTS",
                                       f"Document already exists: {name}")
                if pre["exists"] is True and name not in self.docs:
                    return self._error(404, "NOT_FOUND", f"No document to update: {name}")
        for w in writes:
            if "delete" in w:
                self.docs.pop(w["delete"], None)
                continue
            name, fields = w["update"]["name"], w["update"].get("fields", {})
            if "updateMask" in w:
                doc = dict(self.docs.get(name, {}))
                for f in w["updateMask"]["fieldPaths"]:
                    if f in fields:
                        doc[f] = fields[f]
                    else:
                        doc.pop(f, None)
            else:
                doc = dict(fields)
            for t in w.get("updateTransforms", []):
                assert set(t) == {"fieldPath", "appendMissingElements"}, t
                cur = doc.get(t["fieldPath"], {"arrayValue": {}})
                values = list(cur["arrayValue"].get("values", []))
                for x in t["appendMissingElements"]["values"]:
                    self._value(x)
                    if x not in values:
                        values.append(x)
                doc[t["fieldPath"]] = {"arrayValue": {"values": values}}
            self.docs[name] = doc
        return httpx.Response(200, json={"writeResults": [{} for _ in writes]})

    def _query(self, body: Dict[str, Any]) -> httpx.Response:
        assert set(body) <= {"structuredQuery", "transaction"}, set(body)
        q = body["structuredQuery"]
        assert set(q) <= QUERY_KEYS, set(q)
        (coll,) = [c["collectionId"] for c in q["from"]]
        ff = q["where"]["fieldFilter"]
        assert ff["op"] == "EQUAL", ff
        self._value(ff["value"])
        field, value = ff["field"]["fieldPath"], ff["value"]
        select = [f["fieldPath"] for f in q.get("select", {}).get("fields", [])]
        out = []
        for name, fields in self.docs.items():
            parent, _ = name.rsplit("/", 1)
            if parent == f"{DOCS}/{coll}" and fields.get(field) == value:
                self.reads += 1
                shown = {k: v for k, v in fields.items() if not select or k in select}
                out.append({"document": {"name": name, "fields": shown},
                            "readTime": "2026-10-25T00:00:00Z"})
        return httpx.Response(200, json=out or [{"readTime": "2026-10-25T00:00:00Z"}])

    # -- inspection -------------------------------------------------------
    def collection(self, coll: str) -> Dict[str, Dict[str, Any]]:
        prefix = f"{DOCS}/{coll}/"
        return {n[len(prefix):]: fsmod.decode_fields(f)
                for n, f in self.docs.items() if n.startswith(prefix)}


def make_store(fake: FakeFirestore) -> fsmod.FirestoreAccountStore:
    http = httpx.Client(transport=httpx.MockTransport(fake.handler))
    return fsmod.FirestoreAccountStore(
        fsmod.Firestore(PROJECT, lambda: "test-token", http=http))


def fake_report(white: str = "alice", black: str = "bob", moves: int = 40) -> Dict[str, Any]:
    return {"kind": "game",
            "headers": {"White": white, "Black": black, "Result": "1-0",
                        "Date": "2026.01.01"},
            "white": {"name": white, "accuracy": 91.2},
            "black": {"name": black, "accuracy": 84.0},
            "moves": [{"san": "e4", "fen_before": "x" * 90} for _ in range(moves)]}


# --------------------------------------------------------------------------
def test_values() -> None:
    print("values")
    sample = {"n": None, "b": True, "i": 42, "neg": -7, "f": 91.25, "s": "héllo",
              "raw": b"\x00\xffbytes", "arr": [1, "two", [3.5]],
              "map": {"k": {"deep": False}}, "empty": []}
    back = fsmod.decode_fields(fsmod.encode_fields(sample))
    check("every Python type round-trips through Firestore's typed JSON",
          back == sample, str(back))
    check("a bool is not stored as an integer",
          fsmod.encode_value(True) == {"booleanValue": True})
    check("int64 travels as a string, as the API requires",
          fsmod.encode_value(2 ** 40) == {"integerValue": str(2 ** 40)})


def test_credentials() -> None:
    print("credentials")
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM,
                            serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    info = {"type": "service_account", "project_id": PROJECT,
            "private_key_id": "kid-1", "private_key": pem,
            "client_email": "bot@demo-proj.iam.gserviceaccount.com",
            "token_uri": "https://oauth2.googleapis.com/token"}

    check("the key file is accepted as JSON",
          fsmod.parse_service_account(json.dumps(info))["project_id"] == PROJECT)
    check("and as base64 of that JSON",
          fsmod.parse_service_account(
              base64.b64encode(json.dumps(info).encode()).decode())["project_id"] == PROJECT)
    for bad, why in [("not json at all {", "garbage"),
                     (json.dumps({"project_id": PROJECT}), "a partial key")]:
        try:
            fsmod.parse_service_account(bad)
            check(f"rejects {why} at boot", False, "accepted")
        except ValueError as exc:
            check(f"rejects {why} at boot, with a reason", bool(str(exc)), str(exc))

    token_calls: List[Dict[str, str]] = []

    def token_endpoint(request: httpx.Request) -> httpx.Response:
        form = dict(httpx.QueryParams(request.content.decode()))
        token_calls.append(form)
        return httpx.Response(200, json={"access_token": "ya29.test",
                                         "expires_in": 3599, "token_type": "Bearer"})

    tokens = fsmod.ServiceAccountTokens(
        info, httpx.Client(transport=httpx.MockTransport(token_endpoint)))
    first, second = tokens(), tokens()
    check("an access token is obtained", first == "ya29.test")
    check("and cached, not fetched per request", second == first and len(token_calls) == 1)
    form = token_calls[0]
    check("the JWT-bearer grant is used",
          form.get("grant_type") == "urn:ietf:params:oauth:grant-type:jwt-bearer")

    head, claims, sig = form["assertion"].split(".")
    pad = lambda s: s + "=" * (-len(s) % 4)                      # noqa: E731
    h = json.loads(base64.urlsafe_b64decode(pad(head)))
    c = json.loads(base64.urlsafe_b64decode(pad(claims)))
    check("the assertion is RS256 with the key id", h == {"alg": "RS256", "typ": "JWT",
                                                          "kid": "kid-1"}, str(h))
    check("its claims name the account, the Firestore scope and Google's token URI",
          c["iss"] == info["client_email"] and c["scope"] == fsmod.SCOPE
          and c["aud"] == info["token_uri"] and c["exp"] - c["iat"] == 3600, str(c))
    try:
        key.public_key().verify(base64.urlsafe_b64decode(pad(sig)),
                                f"{head}.{claims}".encode(), padding.PKCS1v15(),
                                hashes.SHA256())
        check("and the signature verifies against the account's public key", True)
    except Exception as exc:
        check("and the signature verifies against the account's public key", False, str(exc))

    refused = fsmod.ServiceAccountTokens(info, httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(400, json={"error": "invalid_grant"}))))
    try:
        refused()
        check("a revoked key is reported, not swallowed", False, "no error")
    except accounts.AccountStoreUnavailable as exc:
        check("a revoked key is reported, not swallowed", "rejected" in str(exc), str(exc))


def test_contract() -> None:
    """The same calls against SQLite and Firestore must give the same answers."""
    print("same contract as the SQLite store")
    fake = FakeFirestore()
    fs = make_store(fake)
    sq = accounts.AccountStore(os.path.join(tempfile.mkdtemp(), "app.db"))

    users = {}
    for name, store in (("sqlite", sq), ("firestore", fs)):
        u, t = store.create_user("darkmoon", "a good long passphrase")
        users[name] = (u, t)
    fu, ftok = users["firestore"]
    su, stok = users["sqlite"]
    check("an account can be created", fu.username == "darkmoon" and len(ftok) > 20)
    check("the session resolves to the user", (fs.session_user(ftok) or fu).id == fu.id)
    check("a nonsense token resolves to nobody", fs.session_user("not-a-token") is None)

    for username, password, why in [
        ("ab", "longenough1", "username under three characters"),
        ("has spaces", "longenough1", "username with a space"),
        ("newuser", "short", "password under eight characters"),
        ("newuser", "password", "a password from the common list"),
        ("chessfan", "chessfan", "password identical to the username"),
        ("darkmoon", "a good long passphrase", "a username already taken"),
        ("DarkMoon", "a good long passphrase", "the same name in other case"),
        ("__init__", "a good long passphrase", "a name Firestore reserves"),
    ]:
        msgs = []
        for store in (sq, fs):
            try:
                store.create_user(username, password)
                msgs.append(None)
            except accounts.AccountError as exc:
                msgs.append(str(exc))
        if why == "a name Firestore reserves":
            check(f"rejects {why} with a clear message", msgs[1] is not None, str(msgs))
        else:
            check(f"rejects {why}, with SQLite's exact message",
                  msgs[1] is not None and msgs[0] == msgs[1], str(msgs))

    for store, name in ((sq, "sqlite"), (fs, "firestore")):
        for uname, pw in (("darkmoon", "wrong password entirely"),
                          ("ghost", "whatever at all"),
                          ("../users", "path tricks"), ("", "")):
            try:
                store.login(uname, pw)
                check(f"{name}: refuses {uname!r}", False, "accepted")
            except accounts.AccountError as exc:
                check(f"{name}: refuses {uname!r} with the one generic message",
                      str(exc) == "Wrong username or password.", str(exc))

    again, tok2 = fs.login("DARKMOON", "a good long passphrase")
    check("sign-in is case-insensitive", again.id == fu.id and again.username == "darkmoon")
    check("each sign-in issues a new session", tok2 != ftok)
    fs.logout(ftok)
    check("signing out kills that session", fs.session_user(ftok) is None)
    check("but not the other one", fs.session_user(tok2) is not None)

    print("progress, side by side")
    seq = [("p1", True, 1532, 4000), ("p2", True, 1560, 3000),
           ("p3", False, 1528, 9000), ("p4", True, 1540, None),
           ("p1", True, 1555, 2000)]
    for pid, solved, rating, ms in seq:
        a = sq.record_attempt(su.id, pid, solved, rating, ms)
        b = fs.record_attempt(fu.id, pid, solved, rating, ms)
        a.pop("user_id"), b.pop("user_id")
        check(f"after {pid} {'solved' if solved else 'failed'}: identical stats",
              a == b, f"sqlite={a} firestore={b}")
    a, b = sq.stats(su.id), fs.stats(fu.id)
    check("stats() has the same keys in both", list(a) == list(b), f"{list(a)} {list(b)}")
    check("stats() agree", {**a, "user_id": 0} == {**b, "user_id": 0}, f"{a} {b}")
    check("attempted puzzles agree, without duplicates",
          sorted(sq.attempted_ids(su.id)) == sorted(fs.attempted_ids(fu.id))
          == ["p1", "p2", "p3", "p4"], str(fs.attempted_ids(fu.id)))
    try:
        fs.record_attempt(fu.id, "../../users/x", True, 1500)
        check("a puzzle id cannot address another document", False, "accepted")
    except ValueError:
        check("a puzzle id cannot address another document", True)

    print("saved analyses, side by side")
    for store, uid in ((sq, su.id), (fs, fu.id)):
        store.save_analysis(uid, source_type="pgn", source_ref="g-1",
                            report=fake_report(), depth=14, prompt_version="b-x")
    la, lb = sq.list_analyses(su.id), fs.list_analyses(fu.id)
    strip = lambda r: {k: v for k, v in r.items() if k not in ("id", "created_at")}  # noqa: E731
    check("the listing has the same keys", sorted(la[0]) == sorted(lb[0]),
          f"{sorted(la[0])} vs {sorted(lb[0])}")
    check("and the same values", strip(la[0]) == strip(lb[0]), f"{la[0]} {lb[0]}")
    check("the listing never carries the report blob", "result_json" not in lb[0])
    ga = sq.get_analysis(su.id, la[0]["id"])
    gb = fs.get_analysis(fu.id, lb[0]["id"])
    check("one analysis reads back with the same keys", sorted(ga) == sorted(gb),
          f"{sorted(ga)} vs {sorted(gb)}")
    check("and the same report", ga["report"] == gb["report"] == fake_report())

    for i in range(accounts.MAX_ANALYSES + 5):
        for store, uid in ((sq, su.id), (fs, fu.id)):
            store.save_analysis(uid, source_type="pgn", source_ref=f"bulk-{i}",
                                report=fake_report())
        time.sleep(0.001)          # distinct created_at, as in real use
    kept = fs.list_analyses(fu.id)
    check(f"never more than {accounts.MAX_ANALYSES} kept",
          len(kept) == accounts.MAX_ANALYSES == len(sq.list_analyses(su.id)), str(len(kept)))
    check("newest first", all(kept[i]["created_at"] >= kept[i + 1]["created_at"]
                              for i in range(len(kept) - 1)))
    check("the oldest were the ones evicted",
          {r["source_ref"] for r in kept} == {f"bulk-{i}" for i in range(5, 35)},
          str(sorted(r["source_ref"] for r in kept)[:3]))
    fs.save_analysis(fu.id, source_type="pgn", source_ref="bulk-20",
                     report=fake_report(white="changed"))
    after = fs.list_analyses(fu.id)
    check("re-analysing a stored game updates it instead of adding one",
          len(after) == accounts.MAX_ANALYSES and after[0]["white"] == "changed"
          and after[0]["source_ref"] == "bulk-20", str(after[0]))

    stranger, _ = fs.create_user("stranger", "a different passphrase")
    live = after[0]["id"]
    check("another user cannot read one of yours", fs.get_analysis(stranger.id, live) is None)
    check("nor delete it", fs.delete_analysis(stranger.id, live) is False
          and fs.get_analysis(fu.id, live) is not None)
    check("a malformed id is simply not found",
          fs.get_analysis(fu.id, "../users") is None
          and fs.delete_analysis(fu.id, "../users") is False)
    check("the owner can delete it", fs.delete_analysis(fu.id, live) is True
          and fs.get_analysis(fu.id, live) is None)

    print("what is actually stored")
    stored_users = fake.collection("users")
    row = stored_users[fu.id]
    check("the password is not stored", all(b"passphrase" not in (
        v if isinstance(v, bytes) else str(v).encode()) for v in row.values()))
    check("the scrypt parameters travel with the hash",
          row["n"] == accounts.SCRYPT_N and row["r"] == accounts.SCRYPT_R)
    check("the username is claimed in usernames/", fake.collection("usernames")
          .get("darkmoon") == {"user_id": fu.id})
    sessions = fake.collection("sessions")
    check("sessions are stored under the token's HASH, never the token",
          tok2 not in sessions and all(tok2 not in str(s) for s in sessions.values())
          and fsmod.FirestoreAccountStore._session_id(tok2) in sessions)
    blob = next(iter(fake.collection("analyses").values()))["result_json"]
    check("reports are stored compressed",
          isinstance(blob, bytes) and len(blob) < len(json.dumps(fake_report())) / 2)
    attempts = fake.collection("puzzle_attempts")
    check("every attempt is kept as its own document",
          {d["puzzle_id"] for d in attempts.values()} == {"p1", "p2", "p3", "p4"})

    print("cost on the free quota")
    fake.reads = 0
    fs.session_user(tok2)
    check("resolving a session is ONE read (50,000 a day are free)",
          fake.reads == 1, str(fake.reads))
    fake.reads = 0
    fs.attempted_ids(fu.id)
    check("choosing the next puzzle is one read, however many were played",
          fake.reads == 1, str(fake.reads))

    print("failure handling")
    fake.abort_next_commits = 2
    before = fs.stats(fu.id)["puzzle_attempts"]
    fs.record_attempt(fu.id, "p9", True, 1600)
    check("a transaction that loses a race is retried, and counted once",
          fs.stats(fu.id)["puzzle_attempts"] == before + 1)
    fake.down = True
    try:
        fs.session_user(tok2)
        check("an outage is AccountStoreUnavailable", False, "no error")
    except accounts.AccountStoreUnavailable as exc:
        check("an outage is AccountStoreUnavailable, not a crash", "UNAVAILABLE" in str(exc),
              str(exc))
    fake.down = False
    sq.close()


def test_http_layer() -> None:
    """partE's endpoints, unchanged, on the Firestore store."""
    print("HTTP layer on Firestore")
    import partE
    from fastapi.testclient import TestClient

    fake = FakeFirestore()
    partE._accounts = make_store(fake)
    partE._accounts_error = None

    with TestClient(partE.app) as c:
        r = c.post("/api/auth/signup", json={"username": "webuser",
                                             "password": "a fine passphrase"})
        check("sign-up works", r.status_code == 200, r.text[:200])
        token = r.json().get("token", "")
        auth = {"Authorization": f"Bearer {token}"}
        check("and the user lands in Firestore",
              "webuser" in fake.collection("usernames"))

        r = c.get("/puzzles/next", headers=auth)
        check("a puzzle is served to the signed-in user", r.status_code == 200, r.text[:200])
        if r.status_code == 200:
            pid = r.json()["puzzle"]["id"]
            r = c.post(f"/puzzles/{pid}/attempt", headers=auth,
                       json={"moves": ["a1a2"], "ms_taken": 1000})
            check("an attempt is accepted", r.status_code == 200, r.text[:200])
            stats = fake.collection("user_stats")
            (row,) = stats.values()
            check("and the stats are written to Firestore",
                  row["puzzle_attempts"] == 1 and pid in row["attempted"], str(row))

        r = c.post("/api/auth/login", json={"username": "WEBUSER",
                                            "password": "a fine passphrase"})
        check("sign-in works", r.status_code == 200, r.text[:200])

        r = c.get("/ready")
        acc = r.json()["checks"]["accounts"]
        check("/ready shows where accounts are stored, and that it answers",
              acc.get("ok") and acc.get("backend") == "firestore"
              and acc.get("persistent") is True, str(acc))

        r = c.post("/api/auth/logout", headers=auth)
        r = c.get("/puzzles/next", headers=auth)
        check("after sign-out the token is refused", r.status_code == 401, str(r.status_code))

        fake.down = True
        r = c.post("/api/auth/login", json={"username": "webuser",
                                            "password": "a fine passphrase"})
        check("a Firestore outage is a 503 with the reason, not a 500",
              r.status_code == 503 and "Account storage is unavailable" in r.text,
              f"{r.status_code} {r.text[:150]}")
        fake.down = False
    partE._accounts = None


def main() -> int:
    test_values()
    test_credentials()
    test_contract()
    test_http_layer()
    width = max(len(n) for n, _, _ in _results) + 2
    failed = 0
    for name, ok, detail in _results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<{width}}"
              + ("" if ok else f" -- {detail}"))
        failed += not ok
    print(f"\n{len(_results) - failed}/{len(_results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
