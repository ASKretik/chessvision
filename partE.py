"""
Part E.1 -- Backend skeleton + deploy pipeline.

Built and deployed BEFORE Part A, per the build-order note in plan.md:
a reachable /health endpoint on Render, talking to Supabase, is the first
milestone. Everything analysis-shaped is optional here and degrades to a
503 if partA.py is absent -- that is deliberate, so this file can ship on
day 1 with nothing behind it.

Also covers:
  E.2  long-running job handling (job id + background task + SSE/poll)
  E.3  memory budget knobs (small Stockfish hash, onnxruntime never torch)
  E.5  secrets and config (server-side keys only, CORS pinned to frontend)

Run locally:
    uvicorn partE:app --reload --port 8000
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
import hashlib
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import chess
import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

log = logging.getLogger("chessapp")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

APP_VERSION = "0.1.0"

# Request bounds, named once. The analysis request model validates against
# these and GET /config publishes them, so the frontend's sliders and copy
# cannot drift from what the server will actually accept.
ANALYSE_DEPTH_DEFAULT = 14
ANALYSE_DEPTH_MIN = 6
ANALYSE_DEPTH_MAX = 22
ANALYSE_MULTIPV_DEFAULT = 3
ANALYSE_MULTIPV_MAX = 5
PUZZLE_MAX_MOVES = 40


def load_dotenv(path: str = ".env") -> List[str]:
    """
    Read KEY=value lines from `.env` into the environment, for local dev.

    .env.example tells you to copy it to .env, and nothing was reading it --
    so every documented local-dev variable was silently ignored. Hand-rolled
    rather than adding python-dotenv: it is fifteen lines and the image does
    not need another dependency (E.3).

    Real environment variables always win, so this never overrides what
    Render or a shell export already set. Returns the names it loaded.
    """
    if not os.path.exists(path):
        return []
    loaded = []
    try:
        with open(path) as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip().strip('"').strip("'")
                if key and value and key not in os.environ:
                    os.environ[key] = value
                    loaded.append(key)
    except OSError as exc:
        log.warning("Could not read %s: %s", path, exc)
    return loaded


# Before Settings reads the environment.
_dotenv_loaded = load_dotenv(os.getenv("ENV_FILE", ".env"))
if _dotenv_loaded:
    log.info("Loaded %d variable(s) from .env: %s",
             len(_dotenv_loaded), ", ".join(sorted(_dotenv_loaded)))


# --------------------------------------------------------------------------
# E.5  Config and secrets
#
# Rules encoded here:
#   - LLM_API_KEY and SUPABASE_SERVICE_KEY are server-side only. They are
#     never returned by any endpoint, never logged, and never sent to the
#     client. SUPABASE_ANON_KEY is client-safe and IS exposed, via /config,
#     so the frontend does not need its own build-time copy.
#   - CORS is pinned to FRONTEND_ORIGIN. No wildcard with credentials.
# --------------------------------------------------------------------------
class Settings:
    """Environment-backed config. Read once at import, validated at startup."""

    def __init__(self) -> None:
        self.env: str = os.getenv("ENV", "development")
        self.port: int = int(os.getenv("PORT", "8000"))

        # Supabase (DB + Auth + Storage)
        self.supabase_url: str = os.getenv("SUPABASE_URL", "")
        self.supabase_anon_key: str = os.getenv("SUPABASE_ANON_KEY", "")
        self.supabase_service_key: str = os.getenv("SUPABASE_SERVICE_KEY", "")

        # LLM (Part B). Unset is fine: B.1's deterministic facts are still
        # produced, and commentary reports itself unavailable with a reason.
        self.llm_api_key: str = os.getenv("LLM_API_KEY", "") or os.getenv("GROQ_API_KEY", "")
        # "groq" (free tier) or "anthropic" (metered). partB.make_commentator
        # resolves the default; an empty string here means "let it decide".
        self.llm_provider: str = os.getenv("LLM_PROVIDER", "")
        # Empty means the provider's own default model.
        self.llm_model: str = os.getenv("LLM_MODEL", "")
        # B.4 cost control. The per-game cap bounds one analysis; the
        # per-hour rate limit above bounds one caller.
        self.max_comments_per_game: int = int(os.getenv("MAX_COMMENTS_PER_GAME", "18"))
        self.comment_batch_size: int = int(os.getenv("COMMENT_BATCH_SIZE", "6"))
        # C.1b. SQLite file for local accounts and puzzle progress. Outside
        # the image on a real deploy (a mounted disk), beside the code here.
        self.accounts_db_path: str = os.getenv("ACCOUNTS_DB", "data/app.db")

        # CORS. Comma-separated; localhost is added in development only.
        origins = [
            o.strip()
            for o in os.getenv("FRONTEND_ORIGIN", "").split(",")
            if o.strip()
        ]
        if self.env == "development":
            origins += ["http://localhost:3000", "http://localhost:5173"]
        self.cors_origins: List[str] = origins

        # E.3 memory budget: 512 MB must hold Python + FastAPI +
        # onnxruntime + Maia-2 weights + Stockfish + its hash table.
        self.stockfish_path: str = os.getenv("STOCKFISH_PATH", "stockfish")
        self.stockfish_hash_mb: int = int(os.getenv("STOCKFISH_HASH_MB", "16"))
        self.stockfish_threads: int = int(os.getenv("STOCKFISH_THREADS", "1"))
        self.maia2_onnx_path: str = os.getenv("MAIA2_ONNX_PATH", "models/maia2.onnx")

        # Render free is 512 MB. Overridable so the same image can be
        # profiled against the Starter tier's limit without a rebuild.
        self.memory_budget_mb: int = int(os.getenv("MEMORY_BUDGET_MB", "512"))
        # Must exceed STOCKFISH_HASH_MB with slack: that is what a newly
        # admitted job is about to allocate.
        self.memory_headroom_mb: int = int(os.getenv("MEMORY_HEADROOM_MB", "64"))
        # Load the 93 MB of weights at startup rather than on the first
        # request. Makes the footprint predictable and moves the cost into
        # the deploy, where Render's health check tolerates it, instead of
        # into a user's first analysis.
        self.maia2_preload: bool = os.getenv("MAIA2_PRELOAD", "1") not in ("0", "false", "")
        self.memory_sample_seconds: float = float(os.getenv("MEMORY_SAMPLE_SECONDS", "5"))

        # E.2 job handling
        self.max_concurrent_jobs: int = int(os.getenv("MAX_CONCURRENT_JOBS", "2"))
        self.job_ttl_seconds: int = int(os.getenv("JOB_TTL_SECONDS", "3600"))

        # Rate limit (B.4 protects the LLM bill; applied here at the edge)
        self.rate_limit_per_hour: int = int(os.getenv("RATE_LIMIT_PER_HOUR", "20"))

    def missing_required(self) -> List[str]:
        """Names of unset vars that production genuinely cannot run without."""
        required = {
            "SUPABASE_URL": self.supabase_url,
            "SUPABASE_ANON_KEY": self.supabase_anon_key,
            "SUPABASE_SERVICE_KEY": self.supabase_service_key,
            "FRONTEND_ORIGIN": ",".join(self.cors_origins),
        }
        return [k for k, v in required.items() if not v]

    def public(self) -> Dict[str, Any]:
        """Only what is safe to hand the browser."""
        return {
            "env": self.env,
            "version": APP_VERSION,
            "supabase_url": self.supabase_url,
            "supabase_anon_key": self.supabase_anon_key,
            "features": {
                "analysis": _analysis_available(),
                # Facts (B.1) need no API key; prose (B.2/B.3) does.
                "move_facts": _load_commentary_core() is not None,
                "commentary": _commentary_available(),
                "rating_estimate": os.path.exists(self.maia2_onnx_path),
                # C.1/C.3: accounts need Supabase; puzzles need the module
                # and a database to hold them.
                "accounts": bool(self.supabase_url and self.supabase_anon_key),
                "puzzles": _puzzles_available(),
                # True when puzzles come from the local sample rather than a
                # database, so the UI can say so instead of implying the real
                # 50,000-puzzle set is loaded.
                # True only when the FALLBACK sample is in use. With the
                # 2,000-puzzle subset built this is false, and the UI stops
                # warning about a set that is no longer small.
                "puzzles_dev_sample": _dev_mode() and not os.path.exists(
                    SUBSET_PUZZLE_CSV),
                "local_accounts": _local_accounts_active(),
                "bots": _bots_available(),
                # Local-only auth bypass; false the moment Supabase exists.
                "dev_user": _dev_user_allowed(),
            },
            # Why a feature is off, keyed the same as `features`. The
            # frontend states the server's own reason instead of rendering a
            # blank panel or guessing at one -- without this it had four
            # places where its copy could drift from reality.
            "reasons": _feature_reasons(),
            # Numbers the UI would otherwise hardcode. Each one is a limit a
            # user can actually hit, so the copy has to match the server.
            "limits": {
                "rate_limit_per_hour": self.rate_limit_per_hour,
                "max_comments_per_game": self.max_comments_per_game,
                "job_ttl_seconds": self.job_ttl_seconds,
                "depth_default": ANALYSE_DEPTH_DEFAULT,
                "depth_min": ANALYSE_DEPTH_MIN,
                "depth_max": ANALYSE_DEPTH_MAX,
                "multipv_default": ANALYSE_MULTIPV_DEFAULT,
                "multipv_max": ANALYSE_MULTIPV_MAX,
                "max_moves_per_attempt": PUZZLE_MAX_MOVES,
            },
            # Which provider and model are live, so the UI can say so rather
            # than the user having to guess which backend wrote a comment.
            "llm_provider": (self.llm_provider or None) if self.llm_api_key else None,
            # The effective model, not just the override: with LLM_MODEL
            # unset this used to report null next to a live provider, which
            # reads as "broken" rather than "using the default".
            "llm_model": _effective_llm_model() if self.llm_api_key else None,
        }


settings = Settings()


# --------------------------------------------------------------------------
# Supabase connection
#
# Thin REST (PostgREST) client rather than the supabase-py SDK: fewer
# dependencies inside a 512 MB image, and every call here is a simple
# single-table read or write. Swap for the SDK if Part C needs more.
# --------------------------------------------------------------------------
class SupabaseClient:
    def __init__(self, url: str, service_key: str, timeout: float = 10.0) -> None:
        self.url = url.rstrip("/")
        self._service_key = service_key
        self._client: Optional[httpx.AsyncClient] = None
        self._timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.url and self._service_key)

    async def start(self) -> None:
        if not self.configured:
            log.warning("Supabase not configured -- persistence disabled")
            return
        self._client = httpx.AsyncClient(
            base_url=self.url,
            timeout=self._timeout,
            headers={
                "apikey": self._service_key,
                "Authorization": f"Bearer {self._service_key}",
                "Content-Type": "application/json",
            },
        )

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def ping(self) -> Dict[str, Any]:
        """Cheap liveness probe used by /health. Never raises."""
        if self._client is None:
            return {"ok": False, "detail": "not configured"}
        started = time.perf_counter()
        try:
            # HEAD on the REST root: no table needed, so this works before
            # any Part C migration has been applied.
            r = await self._client.head("/rest/v1/", timeout=5.0)
            ok = r.status_code < 500
            return {
                "ok": ok,
                "status": r.status_code,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            }
        except Exception as exc:  # network, DNS, paused project
            return {"ok": False, "detail": type(exc).__name__}

    async def insert(self, table: str, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if self._client is None:
            return None
        r = await self._client.post(
            f"/rest/v1/{table}",
            json=row,
            headers={"Prefer": "return=representation"},
        )
        r.raise_for_status()
        rows = r.json()
        return rows[0] if rows else None

    async def select(
        self,
        table: str,
        *,
        columns: str = "*",
        filters: Optional[Dict[str, Any]] = None,
        order: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        PostgREST select with the service key, which bypasses RLS.

        Because it bypasses RLS, every caller must filter by user_id itself
        -- the database is not the backstop here, this code is. The RLS
        policies protect against a leaked anon key and against a client
        talking to Supabase directly; they do not protect against a bug in
        this file.
        """
        if self._client is None:
            return []
        # A list of pairs, not a dict: PostgREST expresses a range as the
        # SAME parameter twice (rating=gte.1400&rating=lte.1600), which a
        # dict silently collapses to the last one -- so a rating window
        # would have become an open-ended ">=" and served puzzles of any
        # difficulty. A filter value may therefore be a list.
        params: List[Tuple[str, Any]] = [("select", columns)]
        for key, value in (filters or {}).items():
            if isinstance(value, (list, tuple)):
                params.extend((key, v) for v in value)
            else:
                params.append((key, value))
        if order:
            params.append(("order", order))
        if limit is not None:
            params.append(("limit", limit))
        if offset is not None:
            params.append(("offset", offset))
        r = await self._client.get(f"/rest/v1/{table}", params=params)
        r.raise_for_status()
        return r.json()

    async def rpc(self, name: str, payload: Dict[str, Any]) -> Any:
        """
        Call a Postgres function.

        Used for the writes that must be one transaction --
        record_puzzle_attempt and bump_games_analysed. Two round trips would
        lose an update whenever a user has two tabs open.
        """
        if self._client is None:
            return None
        r = await self._client.post(f"/rest/v1/rpc/{name}", json=payload)
        r.raise_for_status()
        return r.json() if r.content else None

    async def verify_jwt(self, token: str) -> Optional[Dict[str, Any]]:
        """
        Resolve a Supabase Auth access token to a user (C.1).

        Verification is delegated to Supabase's /auth/v1/user rather than
        decoding the JWT locally -- no key material to manage, and revoked
        tokens stop working immediately.
        """
        if self._client is None:
            return None
        try:
            r = await self._client.get(
                "/auth/v1/user",
                headers={"Authorization": f"Bearer {token}"},
            )
        except Exception:
            return None
        if r.status_code != 200:
            return None
        return r.json()


supabase = SupabaseClient(settings.supabase_url, settings.supabase_service_key)


# --------------------------------------------------------------------------
# E.3  Memory budget -- the binding constraint
#
# 512 MB must hold Python + FastAPI + onnxruntime + Maia-2 weights (93 MB) +
# the Stockfish process + its hash table. Three things make that survivable,
# and all three live here:
#
#   1. ONE shared Maia2Session. The weights are 93 MB; building a session per
#      request reloads them from disk each time and, under concurrency, holds
#      a copy per in-flight job. A singleton makes the cost fixed.
#   2. Real measurement. On Render the limit is enforced by the cgroup, not
#      by Python, so RSS is the wrong number to watch -- memory.current is
#      what the OOM killer reads. Measured, not estimated.
#   3. Admission control. Refusing a job with a 503 is strictly better than
#      being OOM-killed, which drops every in-flight job and costs a ~60 s
#      cold start for the next visitor.
#
# plan.md E.3 says "profile under load before declaring this done". The
# sampler below records peak usage continuously in production, and
# tools/profile_memory.py drives concurrent load locally. If it does not fit,
# the documented fallback is Render Starter at ~$7/month (plan.md §8.7).
# --------------------------------------------------------------------------
MB = 1024 * 1024


# Measurement sources that actually account for the whole container,
# counting shared pages once. Only these may drive admission control or a
# tier verdict -- see MemoryBudget for why the alternatives mislead.
AUTHORITATIVE_SOURCES = frozenset({"cgroup-v2", "cgroup-v1", "pss"})


@dataclass
class MemoryReading:
    """One measurement. `source` says how much to trust it."""

    used_mb: float
    limit_mb: Optional[float]
    source: str
    detail: Dict[str, Any] = field(default_factory=dict)

    @property
    def authoritative(self) -> bool:
        """
        Whether this number can be compared against the limit.

        A partial reading is worse than no reading: it looks like a
        measurement and silently drives the wrong decision.
        """
        return self.source in AUTHORITATIVE_SOURCES

    @property
    def free_mb(self) -> Optional[float]:
        if self.limit_mb is None:
            return None
        return round(self.limit_mb - self.used_mb, 1)

    @property
    def fraction(self) -> Optional[float]:
        if not self.limit_mb:
            return None
        return round(self.used_mb / self.limit_mb, 4)


class MemoryBudget:
    """
    Reads actual memory usage and decides whether another job fits.

    Measurement precedence, most to least authoritative:

      cgroup-v2  /sys/fs/cgroup/memory.current + memory.max   (Render)
      cgroup-v1  memory.usage_in_bytes + memory.limit_in_bytes
      pss        /proc/<pid>/smaps_rollup across the tree      (Linux, no cgroup)
      partial    this process only; children unmeasurable      (macOS)

    The cgroup number is the one that matters in production: the kernel
    counts each page once, includes the Stockfish subprocess and the page
    cache, and compares exactly this figure against the limit.

    Everything below cgroup exists for local development, and the ordering
    is about one specific trap. Summing RSS across a process tree
    double-counts every shared page, and the error is large, not marginal:
    Stockfish mmaps its NNUE network and reports ~378 MB RSS of which only a
    few MB are private. Summing it produced a 946 MB "peak" against a 512 MB
    budget here -- a confident recommendation to pay for Render Starter,
    based on nothing. PSS divides shared pages by the number of sharers and
    is correct; it is Linux-only.

    On macOS, psutil cannot read a child's USS at all (AccessDenied on the
    Stockfish process), so the container total is genuinely unmeasurable.
    This reports the "partial" source, refuses to produce a tier verdict,
    and says to profile on Render instead. An honest unknown beats a
    plausible wrong number.
    """

    CG2_CURRENT = "/sys/fs/cgroup/memory.current"
    CG2_MAX = "/sys/fs/cgroup/memory.max"
    CG2_PEAK = "/sys/fs/cgroup/memory.peak"
    CG2_STAT = "/sys/fs/cgroup/memory.stat"
    CG1_USAGE = "/sys/fs/cgroup/memory/memory.usage_in_bytes"
    CG1_LIMIT = "/sys/fs/cgroup/memory/memory.limit_in_bytes"
    CG1_PEAK = "/sys/fs/cgroup/memory/memory.max_usage_in_bytes"

    # A cgroup with no limit reports a sentinel near 2^63; anything above
    # this is "unlimited", not a real ceiling.
    UNLIMITED_ABOVE = 1 << 50

    def __init__(self, budget_mb: int, headroom_mb: int) -> None:
        self.configured_budget_mb = budget_mb
        self.headroom_mb = headroom_mb
        # Two peaks, deliberately. Mixing sources is how a 378 MB shared
        # mapping became a 946 MB "peak": only authoritative readings may
        # set the figure used for admission and the tier verdict.
        self.peak_mb = 0.0                       # authoritative readings only
        self.peak_at: Optional[float] = None
        self.peak_observed_mb = 0.0              # any reading, for diagnostics
        self.peak_observed_source: Optional[str] = None
        self.rejections = 0
        self._source: Optional[str] = None

    # -- measurement -----------------------------------------------------
    def read(self) -> MemoryReading:
        for reader in (self._read_cgroup_v2, self._read_cgroup_v1,
                       self._read_pss, self._read_partial):
            reading = reader()
            if reading is not None:
                self._source = reading.source
                self._record_peak(reading)
                return reading
        return MemoryReading(0.0, float(self.configured_budget_mb), "unavailable")

    def _read_cgroup_v2(self) -> Optional[MemoryReading]:
        used = _read_int(self.CG2_CURRENT)
        if used is None:
            return None
        raw_limit = _read_int(self.CG2_MAX)   # may be the literal "max"
        limit = self._effective_limit(raw_limit)
        detail: Dict[str, Any] = {}
        # memory.current includes the page cache, which Linux grows to fill
        # whatever the limit allows. Inactive file pages are the first thing
        # the kernel reclaims under pressure, so they are not "used" for
        # admission -- otherwise a warm container sits at its limit and
        # refuses every job. Same working-set figure the kubelet uses.
        inactive_file = _read_stat(self.CG2_STAT).get("inactive_file", 0)
        if inactive_file:
            detail["reclaimable_cache_mb"] = round(inactive_file / MB, 1)
            used = max(used - inactive_file, 0)
        kernel_peak = _read_int(self.CG2_PEAK)
        if kernel_peak is not None:
            # The kernel's own high-water mark: survives our sampling gaps,
            # so it catches a spike between two samples.
            detail["kernel_peak_mb"] = round(kernel_peak / MB, 1)
            self._record_peak(MemoryReading(detail["kernel_peak_mb"], limit, "cgroup-v2"))
        return MemoryReading(round(used / MB, 1), limit, "cgroup-v2", detail)

    def _read_cgroup_v1(self) -> Optional[MemoryReading]:
        used = _read_int(self.CG1_USAGE)
        if used is None:
            return None
        limit = self._effective_limit(_read_int(self.CG1_LIMIT))
        detail = {}
        kernel_peak = _read_int(self.CG1_PEAK)
        if kernel_peak is not None:
            detail["kernel_peak_mb"] = round(kernel_peak / MB, 1)
            self._record_peak(MemoryReading(detail["kernel_peak_mb"], limit, "cgroup-v1"))
        return MemoryReading(round(used / MB, 1), limit, "cgroup-v1", detail)

    def _read_pss(self) -> Optional[MemoryReading]:
        """
        Linux without a cgroup: sum PSS over the process tree.

        PSS ("proportional set size") splits each shared page across the
        processes mapping it, so a tree total neither double-counts nor
        undercounts. Read from smaps_rollup, which the kernel aggregates for
        us. Linux-only -- there is no macOS equivalent.
        """
        if not sys.platform.startswith("linux"):
            return None
        pids = [os.getpid()] + self._child_pids()
        total_kb = 0.0
        seen = 0
        for pid in pids:
            pss = _read_pss_kb(pid)
            if pss is None:
                continue
            total_kb += pss
            seen += 1
        if not seen:
            return None
        return MemoryReading(
            round(total_kb / 1024, 1), self._effective_limit(None), "pss",
            {"processes": seen, "note": "proportional set size; shared pages counted once"},
        )

    def _read_partial(self) -> Optional[MemoryReading]:
        """
        Last resort (macOS): this process's unique memory only.

        Child processes are listed for information but NOT added, because
        their private cost cannot be read here and their RSS is dominated by
        shared mappings. Marked non-authoritative so it cannot gate traffic
        or produce a tier verdict.
        """
        try:
            import psutil  # noqa: PLC0415 -- optional dependency
        except ImportError:
            import resource

            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            peak_mb = peak / 1024 if sys.platform != "darwin" else peak / MB
            return MemoryReading(
                round(peak_mb, 1), self._effective_limit(None), "partial",
                {"note": "self peak only; install psutil for a current reading"},
            )

        try:
            proc = psutil.Process()
            try:
                self_mb = proc.memory_full_info().uss / MB
                basis = "uss"
            except psutil.Error:
                self_mb = proc.memory_info().rss / MB
                basis = "rss"

            children = []
            for child in proc.children(recursive=True):
                try:
                    children.append({
                        "pid": child.pid, "name": _safe_name(child),
                        "rss_mb": round(child.memory_info().rss / MB, 1),
                        "private_mb": None,   # unreadable on this platform
                    })
                except psutil.Error:
                    continue
            return MemoryReading(
                round(self_mb, 1), self._effective_limit(None), "partial",
                {"self_basis": basis, "children": children,
                 "note": ("this process only -- child private memory is not readable "
                          "on this platform, so the container total is unknown")},
            )
        except Exception:
            return None

    def _child_pids(self) -> List[int]:
        try:
            import psutil

            return [c.pid for c in psutil.Process().children(recursive=True)]
        except Exception:
            return []

    def _effective_limit(self, raw: Optional[int]) -> Optional[float]:
        """
        The binding ceiling: the cgroup limit, or our configured budget if
        the cgroup is unlimited. Whichever is lower wins -- a container with
        a 1 GB cgroup but a 512 MB budget should still be held to 512 MB.
        """
        configured = float(self.configured_budget_mb)
        if raw is None or raw >= self.UNLIMITED_ABOVE:
            return configured
        return min(round(raw / MB, 1), configured)

    def _record_peak(self, reading: MemoryReading) -> None:
        if reading.used_mb > self.peak_observed_mb:
            self.peak_observed_mb = reading.used_mb
            self.peak_observed_source = reading.source
        if reading.authoritative and reading.used_mb > self.peak_mb:
            self.peak_mb = reading.used_mb
            self.peak_at = time.time()

    # -- admission -------------------------------------------------------
    def admit(self) -> Tuple[bool, Optional[str]]:
        """
        Is there room for another analysis pass?

        Checked before accepting a job rather than before starting one, so
        the caller gets an immediate, honest 503 instead of a job id that
        will die. Each pass needs a Stockfish process and its hash table,
        so `headroom_mb` should exceed STOCKFISH_HASH_MB with slack.
        """
        reading = self.read()
        if not reading.authoritative or reading.limit_mb is None:
            # A partial or absent reading must not gate traffic: it would
            # reject work the instance can actually do, on a guess.
            return True, None
        if reading.free_mb is not None and reading.free_mb < self.headroom_mb:
            self.rejections += 1
            return False, (
                f"{reading.used_mb:.0f} MB of {reading.limit_mb:.0f} MB used, "
                f"{reading.free_mb:.0f} MB free, need {self.headroom_mb} MB headroom"
            )
        return True, None

    # -- reporting -------------------------------------------------------
    def report(self) -> Dict[str, Any]:
        reading = self.read()
        return {
            "used_mb": reading.used_mb,
            "limit_mb": reading.limit_mb,
            "free_mb": reading.free_mb,
            "fraction_used": reading.fraction,
            "authoritative": reading.authoritative,
            "peak_mb": round(self.peak_mb, 1),
            "peak_at": self.peak_at,
            # Any-source high-water mark. Diagnostic only: on a platform
            # where children are unmeasurable this can be wildly off, which
            # is exactly why it is kept separate from peak_mb.
            "peak_observed_mb": round(self.peak_observed_mb, 1),
            "peak_observed_source": self.peak_observed_source,
            "headroom_required_mb": self.headroom_mb,
            "admits_new_job": self.admit()[0],
            "rejections": self.rejections,
            "source": reading.source,
            "detail": reading.detail,
            # The knobs plan.md E.3 names, so a profile run records the
            # configuration it was measured under.
            "knobs": {
                "budget_mb": self.configured_budget_mb,
                "stockfish_hash_mb": settings.stockfish_hash_mb,
                "stockfish_threads": settings.stockfish_threads,
                "max_concurrent_jobs": settings.max_concurrent_jobs,
                "omp_num_threads": os.getenv("OMP_NUM_THREADS"),
                "maia2_preload": settings.maia2_preload,
                "maia2_loaded": _maia2_is_loaded(),
            },
            "verdict": self._verdict(reading),
        }

    def _verdict(self, reading: MemoryReading) -> str:
        """plan.md §8.7: does this fit the free tier, or is Starter needed?"""
        if reading.limit_mb is None or not reading.authoritative:
            return (
                f"unknown: source={reading.source!r} cannot see the whole container "
                "(child process memory is not readable here). Deploy, drive load with "
                "tools/profile_memory.py, then read /ops/memory for the cgroup figure "
                "that decides plan.md §8.7."
            )

        peak = max(self.peak_mb, reading.used_mb)
        frac = peak / reading.limit_mb
        if frac >= 1.0:
            return "over budget: upgrade to Render Starter, or cut the model"
        if frac >= 0.9:
            return "tight: under 10% headroom at peak, expect OOM under load"
        if frac >= 0.75:
            return "ok: fits, but little room for Part B's additions"
        return "comfortable"


def _read_pss_kb(pid: int) -> Optional[float]:
    """Pss from /proc/<pid>/smaps_rollup, in kB. None if unreadable."""
    try:
        with open(f"/proc/{pid}/smaps_rollup") as fh:
            for line in fh:
                if line.startswith("Pss:"):
                    return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def _safe_name(proc: Any) -> str:
    try:
        return proc.name()
    except Exception:
        return "?"


def _read_stat(path: str) -> Dict[str, int]:
    """Parse a cgroup memory.stat file ("key value" per line). Absent -> {}."""
    out: Dict[str, int] = {}
    try:
        with open(path) as fh:
            for line in fh:
                key, _, value = line.partition(" ")
                if value.strip().isdigit():
                    out[key] = int(value)
    except OSError:
        pass
    return out


def _read_int(path: str) -> Optional[int]:
    """Read a single integer from a cgroup file. 'max' and absence -> None."""
    try:
        with open(path) as fh:
            raw = fh.read().strip()
    except (OSError, ValueError):
        return None
    if not raw or raw == "max":
        return None
    try:
        return int(raw.split()[0])
    except ValueError:
        return None


memory = MemoryBudget(settings.memory_budget_mb, settings.memory_headroom_mb)


# --------------------------------------------------------------------------
# E.3  Shared Maia-2 session
#
# The single biggest memory lever. 93 MB of weights, loaded once per process
# and reused by every job, instead of once per job.
#
# A lock, not just a cached global: two jobs starting together would
# otherwise both build a session and briefly hold 186 MB -- precisely the
# spike that breaks a 512 MB budget.
# --------------------------------------------------------------------------
_maia2_session: Any = None
_maia2_error: Optional[str] = None
_maia2_lock = threading.Lock()


def get_maia2_session() -> Any:
    """
    The process-wide session, or None if the weights are unusable.

    Failure is cached: without this, every job retries a missing file and
    pays the exception each time. A.5 then reports unavailable with a reason
    and the other five sub-parts are unaffected.
    """
    global _maia2_session, _maia2_error

    if _maia2_session is not None:
        return _maia2_session
    if _maia2_error is not None:
        return None

    core = _load_analysis_core()
    if core is None:
        return None

    with _maia2_lock:
        if _maia2_session is not None:
            return _maia2_session
        if _maia2_error is not None:
            return None
        session = core.Maia2Session(settings.maia2_onnx_path)
        try:
            before = memory.read().used_mb
            session.load()
            after = memory.read().used_mb
            log.info(
                "Maia-2 session loaded once for this process (+%.1f MB, now %.1f MB)",
                after - before, after,
            )
            _maia2_session = session
        except Exception as exc:
            _maia2_error = f"{type(exc).__name__}: {exc}"
            log.warning("Maia-2 unavailable: %s", _maia2_error)
            return None
    return _maia2_session


def _maia2_is_loaded() -> bool:
    return _maia2_session is not None and getattr(_maia2_session, "loaded", False)


async def _memory_sampler(interval: float = 5.0) -> None:
    """
    Background high-water-mark sampler.

    plan.md E.3 requires profiling under load, and the load that matters is
    real traffic. Sampling continuously means /ops/memory reports a true
    peak rather than whatever happened to be true when someone looked.
    Cheap: one small file read every few seconds.
    """
    while True:
        try:
            await asyncio.sleep(interval)
            memory.read()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("memory sampler hiccup", exc_info=True)


# --------------------------------------------------------------------------
# E.2  Long-running job handling
#
# A full-game pass takes minutes, so POST /analyse must not block on it.
# Contract: POST returns a job id -> client polls GET /jobs/{id} or
# subscribes to GET /jobs/{id}/events (SSE). Partial results are published
# as they are produced: classifications first, comments later (Part B).
#
# In-process store, intentionally. Render free runs a single instance that
# sleeps after 15 min idle, so a Redis queue would buy nothing today.
# Replace with Supabase-backed rows (the `analyses` table in C.2) when a
# second instance exists -- JobStore is the only thing that would change.
# --------------------------------------------------------------------------
class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class Job:
    id: str
    user_id: Optional[str]
    state: JobState = JobState.QUEUED
    progress: float = 0.0
    stage: str = "queued"
    created_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    # Partial payloads already handed to subscribers; replayed on reconnect
    # so a client that attaches late still sees everything.
    events: List[Dict[str, Any]] = field(default_factory=list)
    _waiters: List[asyncio.Queue] = field(default_factory=list, repr=False)

    def public(self) -> Dict[str, Any]:
        return {
            "job_id": self.id,
            "state": self.state.value,
            "progress": round(self.progress, 3),
            "stage": self.stage,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "result": self.result,
        }


class JobStore:
    def __init__(self, ttl_seconds: int) -> None:
        self._jobs: Dict[str, Job] = {}
        self._ttl = ttl_seconds

    def create(self, user_id: Optional[str]) -> Job:
        self.evict_expired()
        job = Job(id=uuid.uuid4().hex, user_id=user_id)
        self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def evict_expired(self) -> int:
        cutoff = time.time() - self._ttl
        stale = [
            jid
            for jid, j in self._jobs.items()
            if j.finished_at is not None and j.finished_at < cutoff
        ]
        for jid in stale:
            del self._jobs[jid]
        return len(stale)

    def publish(self, job: Job, event: str, data: Dict[str, Any]) -> None:
        """Fan a partial result out to every live subscriber."""
        payload = {"event": event, "data": data, "t": time.time()}
        job.events.append(payload)
        for q in list(job._waiters):
            q.put_nowait(payload)

    def subscribe(self, job: Job) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        for past in job.events:  # replay
            q.put_nowait(past)
        job._waiters.append(q)
        return q

    def unsubscribe(self, job: Job, q: asyncio.Queue) -> None:
        if q in job._waiters:
            job._waiters.remove(q)

    def stats(self) -> Dict[str, int]:
        by_state: Dict[str, int] = {}
        for j in self._jobs.values():
            by_state[j.state.value] = by_state.get(j.state.value, 0) + 1
        return {"total": len(self._jobs), **by_state}


jobs = JobStore(settings.job_ttl_seconds)

# Caps concurrent analysis passes. Each one owns a Stockfish process with
# its own hash table, so this is the lever that keeps E.3 honest.
analysis_semaphore = asyncio.Semaphore(settings.max_concurrent_jobs)


# --------------------------------------------------------------------------
# Rate limiting (fixed window, per identity)
#
# Crude on purpose. Its job is to stop one caller running up the LLM bill
# in Part B, not to be a fair scheduler.
# --------------------------------------------------------------------------
class RateLimiter:
    def __init__(self, per_hour: int) -> None:
        self.per_hour = per_hour
        self._hits: Dict[str, List[float]] = {}

    def check(self, identity: str) -> tuple[bool, int]:
        now = time.time()
        window = self._hits.setdefault(identity, [])
        window[:] = [t for t in window if now - t < 3600]
        if len(window) >= self.per_hour:
            return False, 0
        window.append(now)
        return True, self.per_hour - len(window)


limiter = RateLimiter(settings.rate_limit_per_hour)


# --------------------------------------------------------------------------
# C.1b -- local accounts
#
# Supabase stays the authority when it is configured. Without it the choice
# used to be "no accounts at all" or a dict in memory that a restart wiped,
# which is not an account. accounts.py is the same contract backed by
# SQLite: a username, a password, and progress that is still there tomorrow.
# --------------------------------------------------------------------------
_accounts: Optional[Any] = None
_accounts_error: Optional[str] = None


def get_accounts() -> Optional[Any]:
    """The local account store, or None if it could not be opened."""
    global _accounts, _accounts_error
    if _accounts is not None or _accounts_error is not None:
        return _accounts
    try:
        import accounts as accounts_mod            # noqa: PLC0415
        _accounts = accounts_mod.AccountStore(settings.accounts_db_path)
        log.info("Local accounts at %s", settings.accounts_db_path)
    except Exception as exc:
        _accounts_error = f"{type(exc).__name__}: {exc}"
        log.warning("Local accounts unavailable: %s", _accounts_error)
    return _accounts


def _local_accounts_active() -> bool:
    """Local accounts are the fallback, never an override for Supabase."""
    return not supabase.configured and get_accounts() is not None


# --------------------------------------------------------------------------
# Auth dependency (C.1 -- Supabase Auth, not ours)
# --------------------------------------------------------------------------
@dataclass
class Caller:
    user_id: Optional[str]
    email: Optional[str]
    anonymous: bool

    @property
    def identity(self) -> str:
        return self.user_id or "anon"


async def current_caller(
    authorization: Optional[str] = Header(default=None),
) -> Caller:
    """
    Resolves a bearer token to a user, or an anonymous caller.

    Anonymous is allowed so the analyse flow works before Part C lands;
    tighten to `require_user` on the endpoints that must not be public.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        return Caller(user_id=None, email=None, anonymous=True)
    token = authorization.split(" ", 1)[1].strip()

    # A local session token, when Supabase is not the authority. Checked
    # first so a deployment without Supabase never reaches the JWT path,
    # which would reject every local token with a confusing message.
    if _local_accounts_active():
        store = get_accounts()
        local = store.session_user(token) if store else None
        if local is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                                "Session expired. Sign in again.")
        return Caller(user_id=local.id, email=local.username, anonymous=False)

    user = await supabase.verify_jwt(token)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    return Caller(user_id=user.get("id"), email=user.get("email"), anonymous=False)


# A synthetic identity for local development only. Guarded by three
# independent conditions, all of which must hold:
#
#   * ENV is not production
#   * Supabase is NOT configured -- so the moment a real database exists,
#     real tokens are required again and this path is dead
#   * ALLOW_DEV_USER is not explicitly disabled
#
# Without this the puzzle tab is unreachable without a Supabase project,
# since every C endpoint verifies a real JWT. It is the one piece of auth
# bypass in the codebase and it is deliberately loud about it.
DEV_USER_ID = "00000000-0000-0000-0000-0000000000de"


def _dev_user_allowed() -> bool:
    # Now also off whenever local accounts work: there is a real sign-in to
    # use, so an anonymous bypass would quietly give everyone the SAME
    # shared progress and make the account pointless.
    return (settings.env != "production"
            and not supabase.configured
            and not _local_accounts_active()
            and os.getenv("ALLOW_DEV_USER", "1") not in ("0", "false", ""))


async def require_user(caller: Caller = Depends(current_caller)) -> Caller:
    if caller.anonymous:
        if _dev_user_allowed():
            return Caller(user_id=DEV_USER_ID, email="dev@localhost",
                          anonymous=False)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign-in required")
    return caller


# --------------------------------------------------------------------------
# Optional Part A wiring
#
# partA.py does not exist when this file is first deployed, and must not be
# required for /health to go green. Imported lazily; absence is reported as
# a disabled feature rather than a crash.
# --------------------------------------------------------------------------
_part_a: Any = None
_part_a_error: Optional[str] = None


def _load_analysis_core() -> Any:
    global _part_a, _part_a_error
    if _part_a is not None or _part_a_error is not None:
        return _part_a
    try:
        import partA  # noqa: PLC0415 -- deliberately deferred

        _part_a = partA
        log.info("Part A analysis core loaded")
    except Exception as exc:
        _part_a_error = f"{type(exc).__name__}: {exc}"
        log.warning("Part A not available: %s", _part_a_error)
    return _part_a


def _analysis_available() -> bool:
    return _load_analysis_core() is not None


# --------------------------------------------------------------------------
# Optional Part B wiring
#
# Same contract as Part A: absence is a disabled feature, not a crash. Part
# B degrades twice over -- without partB there is no commentary at all, and
# without an API key there are still B.1 facts, which are deterministic and
# useful on their own.
# --------------------------------------------------------------------------
_part_b: Any = None
_part_b_error: Optional[str] = None
_commentator: Any = None
_comment_cache: Any = None
_part_b_lock = threading.Lock()


def _load_commentary_core() -> Any:
    global _part_b, _part_b_error
    if _part_b is not None or _part_b_error is not None:
        return _part_b
    try:
        import partB  # noqa: PLC0415 -- deliberately deferred

        _part_b = partB
        log.info("Part B commentary core loaded")
    except Exception as exc:
        _part_b_error = f"{type(exc).__name__}: {exc}"
        log.warning("Part B not available: %s", _part_b_error)
    return _part_b


def get_commentator() -> Any:
    """
    The process-wide commentator and comment cache.

    Singletons for the same reasons as the Maia-2 session (E.3): one SDK
    client and one cache, so B.4's cache actually accumulates hits across
    jobs instead of starting empty every time.
    """
    global _commentator, _comment_cache
    core = _load_commentary_core()
    if core is None:
        return None, None
    if _commentator is not None:
        return _commentator, _comment_cache
    with _part_b_lock:
        if _commentator is None:
            _commentator = core.make_commentator(
                settings.llm_provider or None,
                api_key=settings.llm_api_key or None,
                model=settings.llm_model or None,
            )
            log.info("Commentary provider: %s (%s)",
                     _commentator.provider, _commentator.model)
            _comment_cache = core.CommentCache()
    return _commentator, _comment_cache


def _commentary_available() -> bool:
    return _load_commentary_core() is not None and bool(settings.llm_api_key)


# --------------------------------------------------------------------------
# Optional Part C wiring
#
# Puzzles need both the module and a Supabase connection to hold them, so
# the feature flag reports those separately -- "no puzzles" because the
# table is empty is a different problem from a missing module.
# --------------------------------------------------------------------------
_part_c: Any = None
_part_c_error: Optional[str] = None


def _load_puzzle_core() -> Any:
    global _part_c, _part_c_error
    if _part_c is not None or _part_c_error is not None:
        return _part_c
    try:
        import partC  # noqa: PLC0415 -- deliberately deferred

        _part_c = partC
        log.info("Part C puzzle core loaded")
    except Exception as exc:
        _part_c_error = f"{type(exc).__name__}: {exc}"
        log.warning("Part C not available: %s", _part_c_error)
    return _part_c


# --------------------------------------------------------------------------
# Dev-only local puzzle store
#
# Part C.3 proper keeps puzzles in Postgres. Without a Supabase project the
# whole tab is dark, which makes the feature impossible to look at while the
# database is still being set up.
#
# So when Supabase is absent, a small Stockfish-verified sample
# (data/puzzles/dev_sample.csv) is served from memory instead. It runs the
# SAME partC code -- parsing, validation, Elo -- so it exercises the real
# logic rather than mocking it; only persistence is swapped. Ratings live in
# process memory and die with it.
#
# Never used when Supabase is configured, and never in production.
# --------------------------------------------------------------------------
DEV_PUZZLE_CSV = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "puzzles", "dev_sample.csv")
# The real set: 2,000 puzzles, stratified by rating, built offline by
# tools/build_puzzle_subset.py. Committed (416 KB, CC0) so a deploy has it.
SUBSET_PUZZLE_CSV = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "puzzles", "subset.csv")

_dev_puzzles: Optional[List[Any]] = None
_dev_stats: Dict[str, Dict[str, Any]] = {}
_dev_attempts: Dict[str, set] = {}


def _dev_mode() -> bool:
    """Local puzzles only when there is no database and not in production."""
    return (not supabase.configured
            and settings.env != "production"
            and _load_puzzle_core() is not None
            and os.path.exists(DEV_PUZZLE_CSV))


def _dev_puzzle_list() -> List[Any]:
    """
    The local puzzle set.

    Prefers data/puzzles/subset.csv -- 2,000 puzzles stratified by rating
    band, built from the Lichess dump by tools/build_puzzle_subset.py -- and
    falls back to the handful in dev_sample.csv so a checkout without the
    subset still has something to serve.

    The two files are NOT the same shape. The subset is already parsed (its
    FEN is after the opponent's setup move); the sample is raw Lichess rows
    that still need that move applied. Using the wrong parser on either
    silently produces puzzles whose first move is already played.
    """
    global _dev_puzzles
    if _dev_puzzles is not None:
        return _dev_puzzles

    core = _load_puzzle_core()
    out: List[Any] = []
    if os.path.exists(SUBSET_PUZZLE_CSV):
        import csv as _csv                          # noqa: PLC0415
        with open(SUBSET_PUZZLE_CSV, newline="", encoding="utf-8") as fh:
            for row in _csv.DictReader(fh):
                try:
                    out.append(core.Puzzle.from_subset_row(row))
                except Exception:
                    continue
        log.info("Puzzle subset loaded: %d puzzles from %s",
                 len(out), SUBSET_PUZZLE_CSV)
    if not out:
        for row in core.iter_puzzle_rows(DEV_PUZZLE_CSV):
            try:
                out.append(core.Puzzle.from_row(row))
            except Exception:
                continue
        log.info("Puzzle sample loaded: %d puzzles (no subset built)", len(out))
    _dev_puzzles = out
    return _dev_puzzles


def _stats_for(user_id: str) -> Dict[str, Any]:
    """
    Progress for a user, persisted when there is somewhere to persist it.

    Routes to the SQLite account store when local accounts are active and
    falls back to the in-memory dict otherwise, so every caller gets the
    same shape without having to know which backend it came from.
    """
    store = get_accounts() if _local_accounts_active() else None
    if store and user_id and user_id != "dev":
        return store.stats(user_id)
    return _dev_stat(user_id)


def _dev_stat(user_id: str) -> Dict[str, Any]:
    core = _load_puzzle_core()
    return _dev_stats.setdefault(user_id, {
        "user_id": user_id, "puzzle_rating": core.DEFAULT_RATING,
        "puzzle_attempts": 0, "puzzles_solved": 0, "games_analysed": 0,
        "current_streak": 0, "best_streak": 0, "dev_mode": True,
    })


def _effective_llm_model() -> Optional[str]:
    """The model a comment would actually be written by, default included."""
    if settings.llm_model:
        return settings.llm_model
    core = _load_commentary_core()
    provider = getattr(core, "PROVIDERS", {}).get(settings.llm_provider) if core else None
    return getattr(provider, "default_model", None) or None


def _feature_reasons() -> Dict[str, str]:
    """
    Why each off feature is off, keyed like `features`.

    An absent key means the feature is on. The frontend renders these
    verbatim, so they are written for a user who did not deploy this --
    naming the thing to set, not the exception that was raised.
    """
    out: Dict[str, str] = {}
    if not _analysis_available():
        out["analysis"] = (
            f"The analysis core did not load ({_part_a_error}). "
            "Stockfish and partA.py are both required."
        )
    if _load_commentary_core() is None:
        out["move_facts"] = f"The commentary core did not load ({_part_b_error})."
    elif not _commentary_available():
        out["commentary"] = (
            "No LLM key is configured, so moves come with facts but no prose. "
            "Set LLM_PROVIDER and LLM_API_KEY to turn on written comments."
        )
    if not os.path.exists(settings.maia2_onnx_path):
        out["rating_estimate"] = (
            f"The Maia-2 model is not present at {settings.maia2_onnx_path}, "
            "so no rating can be estimated from the moves."
        )
    if not (settings.supabase_url and settings.supabase_anon_key):
        out["accounts"] = (
            "Supabase is not configured, so there are no accounts and "
            "nothing is saved between visits. Set SUPABASE_URL and "
            "SUPABASE_ANON_KEY."
        )
    if not _puzzles_available():
        out["puzzles"] = (
            f"Puzzles are unavailable ({_part_c_error})."
            if _load_puzzle_core() is None else
            "No puzzle source: configure Supabase and load a subset with "
            "tools/build_puzzle_subset.py, or run in development to use the "
            "bundled sample."
        )
    return out


def _puzzles_available() -> bool:
    core = _load_puzzle_core()
    if core is None:
        return False
    return supabase.configured or _dev_mode()


# --------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    missing = settings.missing_required()
    if missing:
        msg = f"Unset config: {', '.join(missing)}"
        if settings.env == "production":
            # Fail loudly at boot rather than 500 on the first real request.
            raise RuntimeError(msg)
        log.warning("%s (tolerated in %s)", msg, settings.env)

    await supabase.start()
    _load_analysis_core()

    # E.3: take a baseline before the weights land, so the report can show
    # what the model actually costs.
    baseline = memory.read()
    log.info("Baseline memory: %.1f MB of %s MB (%s)",
             baseline.used_mb, baseline.limit_mb, baseline.source)
    if settings.maia2_preload:
        get_maia2_session()

    sampler = asyncio.create_task(_memory_sampler(settings.memory_sample_seconds))

    report = memory.report()
    log.info("Startup complete: env=%s version=%s | memory %.1f/%s MB -- %s",
             settings.env, APP_VERSION, report["used_mb"], report["limit_mb"],
             report["verdict"])
    yield

    sampler.cancel()
    try:
        await sampler
    except asyncio.CancelledError:
        pass
    log.info("Shutdown: peak memory %.1f MB of %s MB",
             memory.peak_mb, report["limit_mb"])
    await supabase.stop()


app = FastAPI(
    title="ChessVision API",
    version=APP_VERSION,
    description="Analysis backend. Part E.1 skeleton; analysis core in partA.py.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


# --------------------------------------------------------------------------
# E.1  Health
#
# Two endpoints, different jobs:
#   /health  -- liveness. Must stay cheap and dependency-free: Render pings
#               it to decide whether the instance is up, so it cannot fail
#               because Supabase is asleep.
#   /ready   -- readiness. Checks the things a real request needs.
# --------------------------------------------------------------------------
_boot_time = time.time()


@app.get("/health", tags=["ops"])
async def health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "version": APP_VERSION,
        "env": settings.env,
        "uptime_seconds": round(time.time() - _boot_time, 1),
    }


@app.get("/ready", tags=["ops"])
async def ready() -> Dict[str, Any]:
    db = await supabase.ping()
    engine = _engine_probe()
    mem = memory.report()
    checks = {
        "supabase": db,
        "stockfish": engine,
        "analysis_core": {
            "ok": _analysis_available(),
            "detail": _part_a_error,
        },
        # Neither failure blocks readiness: Part A alone is a working product.
        "commentary_core": {
            "ok": _load_commentary_core() is not None,
            "detail": _part_b_error,
            "llm_key_set": bool(settings.llm_api_key),
        },
        # E.3: being out of memory is a readiness failure. The instance is
        # alive (so /health stays green and Render does not restart it) but
        # it cannot take work until something finishes.
        "memory": {"ok": mem["admits_new_job"], "used_mb": mem["used_mb"],
                   "limit_mb": mem["limit_mb"], "source": mem["source"]},
    }
    # Analysis core missing is expected pre-Part-A; it does not fail ready.
    ok = db["ok"] and engine["ok"] and mem["admits_new_job"]
    return {"ready": ok, "checks": checks, "jobs": jobs.stats()}


@app.get("/ops/memory", tags=["ops"])
async def ops_memory() -> Dict[str, Any]:
    """
    E.3 memory report: current and peak usage against the binding limit,
    the knobs it was measured under, and a verdict on plan.md §8.7
    (Render free vs. Starter).

    Drive load with tools/profile_memory.py and read this afterwards.
    """
    return memory.report()


def _engine_probe() -> Dict[str, Any]:
    """Confirm the Stockfish binary is present and executable. No UCI handshake."""
    import shutil

    path = shutil.which(settings.stockfish_path) or (
        settings.stockfish_path if os.path.isfile(settings.stockfish_path) else None
    )
    if path is None:
        return {"ok": False, "detail": f"not found: {settings.stockfish_path}"}
    return {"ok": os.access(path, os.X_OK), "path": path}


@app.get("/config", tags=["ops"])
async def public_config() -> Dict[str, Any]:
    """Client-safe config. Never add a service key or the LLM key here."""
    return settings.public()


# --------------------------------------------------------------------------
# E.2  Analysis endpoints
# --------------------------------------------------------------------------
class AnalyseRequest(BaseModel):
    """
    Exactly one source. Mirrors the four A.1 adapters.

    chesscom_username is required alongside chesscom_url: the Chess.com API
    has no get-game-by-url endpoint, so the game has to be found by scanning
    that player's monthly archives.
    """

    lichess_url: Optional[str] = Field(default=None, examples=["https://lichess.org/abc123xy"])
    chesscom_url: Optional[str] = None
    chesscom_username: Optional[str] = None
    # Optional "YYYY/MM". Narrows the archive scan to one request; without
    # it the adapter walks back through recent months.
    chesscom_month: Optional[str] = Field(default=None, pattern=r"^\d{4}/\d{2}$")
    pgn: Optional[str] = None
    fen: Optional[str] = None

    depth: int = Field(default=ANALYSE_DEPTH_DEFAULT,
                       ge=ANALYSE_DEPTH_MIN, le=ANALYSE_DEPTH_MAX)
    multipv: int = Field(default=ANALYSE_MULTIPV_DEFAULT,
                         ge=1, le=ANALYSE_MULTIPV_MAX)
    estimate_rating: bool = True
    # Part B. B.1 facts come with it; prose needs LLM_API_KEY.
    commentary: bool = True

    def sources(self) -> List[str]:
        present = []
        for name in ("lichess_url", "chesscom_url", "pgn", "fen"):
            if getattr(self, name):
                present.append(name)
        return present


class AnalyseAccepted(BaseModel):
    job_id: str
    state: str
    poll: str
    events: str


@app.post(
    "/analyse",
    response_model=AnalyseAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["analysis"],
)
async def post_analyse(
    req: AnalyseRequest,
    request: Request,
    caller: Caller = Depends(current_caller),
) -> AnalyseAccepted:
    """Accept an analysis job and return immediately with its id."""
    present = req.sources()
    if len(present) != 1:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Provide exactly one of lichess_url / chesscom_url / pgn / fen (got {present or 'none'})",
        )
    if req.chesscom_url and not req.chesscom_username:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "chesscom_username is required with chesscom_url (no get-by-url endpoint)",
        )

    # Availability is checked after shape validation: a malformed request
    # should get a 422 whether or not the analysis core is loaded.
    core = _load_analysis_core()
    if core is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Analysis core unavailable ({_part_a_error})",
        )

    # E.3: refuse before issuing a job id. An OOM kill would take every
    # in-flight job with it and cost the next visitor a ~60 s cold start,
    # so a 503 now is the cheaper failure.
    admitted, why = memory.admit()
    if not admitted:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Server is at its memory limit, try again shortly ({why})",
            headers={"Retry-After": "30"},
        )

    identity = caller.user_id or (request.client.host if request.client else "anon")
    allowed, remaining = limiter.check(identity)
    if not allowed:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Rate limit reached ({settings.rate_limit_per_hour}/hour)",
        )
    log.info("analyse accepted identity=%s remaining=%d", identity, remaining)

    job = jobs.create(caller.user_id)
    asyncio.create_task(_run_analysis(job, req, core))
    return AnalyseAccepted(
        job_id=job.id,
        state=job.state.value,
        poll=f"/jobs/{job.id}",
        events=f"/jobs/{job.id}/events",
    )


async def _run_analysis(job: Job, req: AnalyseRequest, core: Any) -> None:
    """
    Background pass. Part A is synchronous and CPU-bound (Stockfish), so it
    runs in a worker thread -- otherwise it would block the event loop and
    /health would start timing out mid-analysis.
    """
    async with analysis_semaphore:
        job.state = JobState.RUNNING
        job.stage = "fetching"
        jobs.publish(job, "state", {"state": job.state.value, "stage": job.stage})

        loop = asyncio.get_running_loop()

        def on_progress(stage: str, fraction: float, partial: Optional[Dict] = None) -> None:
            # Called from the worker thread; hop back onto the loop so the
            # queues are only ever touched from one thread.
            def apply() -> None:
                job.stage = stage
                job.progress = fraction
                jobs.publish(
                    job,
                    "progress",
                    {"stage": stage, "progress": round(fraction, 3), "partial": partial},
                )

            loop.call_soon_threadsafe(apply)

        try:
            game_input = await loop.run_in_executor(
                None, lambda: _resolve_source(core, req)
            )
            job.stage = "analysing"
            report = await loop.run_in_executor(
                None,
                lambda: core.analyse(
                    game_input,
                    stockfish_path=settings.stockfish_path,
                    depth=req.depth,
                    multipv=req.multipv,
                    hash_mb=settings.stockfish_hash_mb,
                    threads=settings.stockfish_threads,
                    maia2_path=None,
                    # E.3: one shared session for the whole process rather
                    # than 93 MB of weights per job.
                    maia2_session=get_maia2_session() if req.estimate_rating else None,
                    progress=on_progress,
                ),
            )
            job.result = report.to_dict()

            # Part A's report is useful on its own, so publish it before
            # commentary runs -- the client can render the board, the
            # timeline and the accuracy figures while the LLM works.
            jobs.publish(job, "analysis", job.result)

            if req.commentary and report.kind == "game":
                await _run_commentary(job, report.to_dict(), on_progress)

            job.state = JobState.DONE
            job.stage = "done"
            job.progress = 1.0
            jobs.publish(job, "result", job.result)

            if job.user_id:
                await _persist(job, game_input, req)

        except Exception as exc:
            log.exception("analysis job %s failed", job.id)
            job.state = JobState.FAILED
            job.error = f"{type(exc).__name__}: {exc}"
            jobs.publish(job, "error", {"error": job.error})
        finally:
            job.finished_at = time.time()
            jobs.publish(job, "state", {"state": job.state.value, "stage": job.stage})


async def _run_commentary(job: Job, report: Dict[str, Any], on_progress: Any) -> None:
    """
    B.1 -> B.2 -> B.3 for a finished game, in a worker thread.

    Failure here never fails the job: the analysis already succeeded and is
    already published. Commentary attaches itself to the result if it works
    and reports a reason if it does not.
    """
    core = _load_commentary_core()
    if core is None:
        report["commentary"] = {
            "available": False,
            "reason": f"Commentary core unavailable ({_part_b_error})",
        }
        return

    commentator, cache = get_commentator()
    job.stage = "commentary"
    jobs.publish(job, "state", {"state": job.state.value, "stage": job.stage})

    loop = asyncio.get_running_loop()

    def commentary_progress(stage: str, frac: float, partial: Optional[Dict] = None) -> None:
        # B.4: each batch of comments reaches the client the moment it
        # lands, rather than after the whole pass. Namespaced so the client
        # can tell commentary progress from analysis progress.
        on_progress(f"commentary:{stage}", frac, partial)

    try:
        commentary = await loop.run_in_executor(
            None,
            lambda: core.generate_commentary(
                report,
                commentator=commentator,
                cache=cache,
                max_comments=settings.max_comments_per_game,
                batch_size=settings.comment_batch_size,
                progress=commentary_progress,
            ),
        )
        report["commentary"] = commentary.to_dict()
        job.result = report
        if commentary.available:
            log.info("B: %d comments, usage=%s",
                     len(commentary.comments), commentary.usage.get("tokens"))
        else:
            log.info("B: commentary unavailable (%s)", commentary.reason)
    except Exception as exc:
        log.exception("commentary failed for job %s", job.id)
        report["commentary"] = {
            "available": False,
            "reason": f"{type(exc).__name__}: {exc}",
        }
        job.result = report


def _resolve_source(core: Any, req: AnalyseRequest) -> Any:
    """Map the request onto one of the four A.1 adapters."""
    if req.lichess_url:
        return core.load_lichess(req.lichess_url)
    if req.chesscom_url:
        return core.load_chesscom(
            req.chesscom_url, req.chesscom_username, month=req.chesscom_month
        )
    if req.pgn:
        return core.load_pgn(req.pgn)
    return core.load_fen(req.fen)


async def _persist(job: Job, game_input: Any, req: Any = None) -> None:
    """
    Save the finished analysis, so the user can come back to it.

    Best effort in both backends: a failed write must not turn a completed
    analysis into a failed job. The user still has the result in front of
    them; they just will not find it in Study later.
    """
    store = get_accounts() if _local_accounts_active() else None
    if store is not None:
        try:
            core_b = _load_commentary_core()
            store.save_analysis(
                job.user_id,
                source_type=getattr(game_input, "source_type", "unknown"),
                # The dedup key. Re-analysing the same game updates that row
                # rather than evicting the oldest of thirty. A pasted PGN has
                # no URL, so its own text is hashed to give it one.
                source_ref=_analysis_ref(game_input),
                report=job.result or {},
                depth=getattr(req, "depth", None),
                prompt_version=getattr(core_b, "PROMPT_VERSION", None),
            )
        except Exception as exc:
            log.warning("local save failed for job %s: %s", job.id, exc)
        return

    if not supabase.configured:
        return
    try:
        await supabase.insert(
            "analyses",
            {
                "user_id": job.user_id,
                "source_type": getattr(game_input, "source_type", "unknown"),
                "source_ref": getattr(game_input, "source_ref", None),
                "pgn": getattr(game_input, "pgn", None),
                "result_json": job.result,
            },
        )
        await supabase.rpc("bump_games_analysed", {"p_user_id": job.user_id})
    except Exception as exc:
        # A failed write must not turn a completed analysis into a failure.
        log.warning("persist failed for job %s: %s", job.id, exc)


def _analyses_cap() -> int:
    """How many analyses a user keeps, from the store that enforces it."""
    try:
        import accounts as accounts_mod            # noqa: PLC0415
        return int(accounts_mod.MAX_ANALYSES)
    except Exception:
        return 30


def _analysis_ref(game_input: Any) -> Optional[str]:
    """
    A stable identity for the game, for deduplication.

    A URL when there is one. A pasted PGN has none, so its moves are hashed
    -- without that, pasting the same game twice would store it twice and
    push out an older analysis for no reason.
    """
    ref = getattr(game_input, "source_ref", None)
    if ref:
        return str(ref)
    pgn = getattr(game_input, "pgn", None)
    if pgn:
        return "pgn:" + hashlib.sha256(
            " ".join(str(pgn).split()).encode("utf-8")).hexdigest()[:32]
    return None


@app.get("/jobs/{job_id}", tags=["analysis"])
async def get_job(job_id: str, caller: Caller = Depends(current_caller)) -> Dict[str, Any]:
    job = _owned_job(job_id, caller)
    return job.public()


@app.get("/jobs/{job_id}/events", tags=["analysis"])
async def job_events(job_id: str, caller: Caller = Depends(current_caller)):
    """
    Server-Sent Events: partial results stream in as they are produced
    (E.2). Past events are replayed first, so attaching late is safe.

    Readable with the job id alone -- see `_owned_job`. EventSource cannot
    carry a bearer token, and without this a signed-in user got no live
    progress on their own analysis.
    """
    job = _owned_job(job_id, caller, allow_anonymous=True)

    async def stream() -> AsyncIterator[bytes]:
        q = jobs.subscribe(job)
        try:
            while True:
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield b": keep-alive\n\n"  # Render idles out silent streams
                    continue
                yield f"event: {payload['event']}\ndata: {json.dumps(payload['data'])}\n\n".encode()
                # "analysis" is a partial -- the stream continues through
                # commentary and ends on "result".
                if payload["event"] in ("result", "error"):
                    break
        finally:
            jobs.unsubscribe(job, q)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _owned_job(job_id: str, caller: Caller, *, allow_anonymous: bool = False) -> Job:
    """
    A job the caller may read.

    Anonymous jobs are readable by anyone holding the opaque id. A job
    created while signed in is normally readable only by that user --
    EXCEPT on the SSE stream, where `allow_anonymous` relaxes it to the
    same id-as-capability rule.

    That exception is not laziness, it is forced: EventSource cannot send
    an Authorization header, so once accounts existed every signed-in
    user's own progress stream started 404ing and the UI silently fell back
    to 3-second polling. The alternatives were a token in the query string
    (logged by every proxy in between) or a fetch-based reader, which is
    the right long-term answer but not a small change.

    The id is 32 hex characters from `secrets`, so it is a capability in
    its own right -- the same thing already relied on for anonymous jobs.
    What it protects is one analysis of a public game, for an hour.
    """
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown or expired job")
    if (job.user_id is not None and job.user_id != caller.user_id
            and not allow_anonymous):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown or expired job")
    return job


# --------------------------------------------------------------------------
# Dev inspection harness -- NOT Part E.4
#
# harness/index.html is a hand-driven page for exercising the API and seeing
# what the models produce: no framework, no build step, no design system.
# The real frontend (frontend/, built to static/ and served by spa.py) does
# not replace it -- the two answer different questions, and this one keeps
# working when the SPA will not build.
#
# It moved from static/ to harness/ and from / to /harness when the SPA
# arrived, because Vite owns static/ and empties it on every build.
#
# Served only when the directory exists, so a deployment without it is
# unaffected, and skipped entirely in production unless SERVE_HARNESS=1 --
# a debug UI on a public URL is not something to ship by accident.
# --------------------------------------------------------------------------
_HARNESS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "harness")


def _mount_harness() -> None:
    if not os.path.isdir(_HARNESS_DIR):
        return
    if settings.env == "production" and os.getenv("SERVE_HARNESS") != "1":
        log.info("Dev harness not served in production (set SERVE_HARNESS=1 to override)")
        return

    from fastapi.responses import FileResponse  # noqa: PLC0415

    index = os.path.join(_HARNESS_DIR, "index.html")

    @app.get("/harness", include_in_schema=False)
    async def harness() -> Any:
        # no-store, not just an ETag. A harness is edited constantly, and a
        # browser holding yesterday's copy looks exactly like a broken
        # feature -- it was already mistaken for one once: an old cached page
        # drew a board that could not be clicked because that version of the
        # JS had no concept of clicking.
        return FileResponse(index, headers={"Cache-Control": "no-store"})

    log.info("Dev harness at /harness (harness/index.html)")


_mount_harness()


# --------------------------------------------------------------------------
# Shared board endpoint
#
# Every tab needs an interactive board, and an interactive board needs to
# know what is legal. Rather than ship a chess rules engine to the client and
# have two implementations that can disagree, the backend answers the
# question -- it already has python-chess, and it is the only thing that gets
# to decide legality anyway (a client's opinion is never trusted: puzzle
# attempts are re-validated server-side regardless).
#
# One call per position gives the board everything it needs to be playable:
# the legal moves, their SAN, and the FEN each one leads to. So the client
# applies a move by swapping in `fen_after` and never computes a position.
#
# Used by Analyse (explore a line), Puzzles (play the solution) and, when
# Part D is built, playing against a bot. plan.md E.4's "one shared board
# component" needs exactly one shared endpoint behind it.
# --------------------------------------------------------------------------
@app.get("/board/moves", tags=["board"])
async def board_moves(fen: str) -> Dict[str, Any]:
    """
    Every legal move from a position, with the position each one reaches.

    Promotions appear as four separate entries sharing a from/to square, so
    a client can detect that case and offer a picker without knowing the
    promotion rules itself.
    """
    try:
        board = chess.Board(fen)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"Invalid FEN: {exc}") from exc
    if not board.is_valid():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "FEN describes an illegal position")

    legal = []
    for mv in board.legal_moves:
        san = board.san(mv)
        board.push(mv)
        legal.append({
            "uci": mv.uci(),
            "san": san,
            "from": chess.square_name(mv.from_square),
            "to": chess.square_name(mv.to_square),
            "promotion": chess.piece_name(mv.promotion) if mv.promotion else None,
            "fen_after": board.fen(),
            "is_check": board.is_check(),
            "is_checkmate": board.is_checkmate(),
        })
        board.pop()

    outcome = board.outcome(claim_draw=False)
    return {
        "fen": board.fen(),
        "turn": "white" if board.turn == chess.WHITE else "black",
        "in_check": board.is_check(),
        "is_game_over": outcome is not None,
        "outcome": outcome.result() if outcome else None,
        "legal": legal,
    }


# --------------------------------------------------------------------------
# Part C.1  Accounts
#
# There is no sign-up, sign-in, password-reset or email-verification
# endpoint here, and that is the point (plan.md C.1: "Supabase Auth. Do not
# write your own."). The client talks to Supabase Auth directly with the
# anon key and sends the resulting JWT here as a bearer token; this service
# only ever verifies it. Nothing below stores or sees a password.
# --------------------------------------------------------------------------
@app.get("/me", tags=["accounts"])
async def me(caller: Caller = Depends(require_user)) -> Dict[str, Any]:
    """The signed-in user and their stats, creating the stats row if absent."""
    stats: Dict[str, Any] = {}
    if _dev_mode():
        return {"user_id": caller.user_id, "email": caller.email,
                "stats": _stats_for(caller.user_id or "dev")}
    if supabase.configured:
        try:
            rows = await supabase.select(
                "user_stats", filters={"user_id": f"eq.{caller.user_id}"}, limit=1)
            if rows:
                stats = rows[0]
            else:
                # The signup trigger should have made this row; a user who
                # predates the migration would not have one.
                await supabase.rpc("bump_games_analysed", {"p_user_id": caller.user_id})
                rows = await supabase.select(
                    "user_stats", filters={"user_id": f"eq.{caller.user_id}"}, limit=1)
                stats = rows[0] if rows else {}
        except Exception as exc:
            log.warning("could not read user_stats: %s", exc)
    core = _load_puzzle_core()
    return {
        "user_id": caller.user_id,
        "email": caller.email,
        "stats": stats or {
            "puzzle_rating": getattr(core, "DEFAULT_RATING", 1200),
            "puzzle_attempts": 0, "puzzles_solved": 0, "games_analysed": 0,
        },
    }


@app.get("/me/analyses", tags=["accounts"])
async def my_analyses(
    caller: Caller = Depends(require_user),
    limit: int = 20,
    offset: int = 0,
) -> Dict[str, Any]:
    """
    This user's past analyses, newest first.

    Summary only. `result_json` holds a whole Part A report plus Part B's
    commentary, and returning twenty of those would be megabytes.
    """
    store = get_accounts() if _local_accounts_active() else None
    if store is not None:
        rows = store.list_analyses(caller.user_id)
        return {"analyses": rows[offset:offset + min(max(limit, 1), 100)],
                "limit": limit, "offset": offset, "total": len(rows),
                "cap": _analyses_cap()}

    if not supabase.configured:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "Persistence is not configured")
    rows = await supabase.select(
        "analyses",
        columns="id,source_type,source_ref,created_at",
        filters={"user_id": f"eq.{caller.user_id}"},
        order="created_at.desc",
        limit=min(max(limit, 1), 100),
        offset=max(offset, 0),
    )
    return {"analyses": rows, "limit": limit, "offset": offset}


@app.get("/me/analyses/{analysis_id}", tags=["accounts"])
async def my_analysis(
    analysis_id: str, caller: Caller = Depends(require_user)
) -> Dict[str, Any]:
    """One full analysis. Filtered by user_id as well as id -- see select()."""
    store = get_accounts() if _local_accounts_active() else None
    if store is not None:
        row = store.get_analysis(caller.user_id, analysis_id)
        if row is None:
            # Not-found rather than forbidden: this must not reveal that an
            # analysis with this id exists under another account.
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Analysis not found")
        return row

    if not supabase.configured:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "Persistence is not configured")
    rows = await supabase.select(
        "analyses",
        filters={"id": f"eq.{analysis_id}", "user_id": f"eq.{caller.user_id}"},
        limit=1,
    )
    if not rows:
        # Not-found rather than forbidden: this must not reveal that an
        # analysis with this id exists under another account.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Analysis not found")
    return rows[0]


# --------------------------------------------------------------------------
# Part C.3  Puzzles
# --------------------------------------------------------------------------
class AttemptRequest(BaseModel):
    """The moves the player made, in UCI or SAN."""

    moves: List[str] = Field(default_factory=list, max_length=PUZZLE_MAX_MOVES)
    ms_taken: Optional[int] = Field(default=None, ge=0)


# --------------------------------------------------------------------------
# D.1 -- Play against a bot
#
# Under /api/ because GET /play/{id} is ambiguous: it is both a page in the
# SPA and a game state. spa.py explains the convention.
#
# The bots are Maia-2 at four rating bands plus Stockfish, and they cost no
# extra memory: the ONNX session is already loaded once per process for the
# A.5 rating estimate, and bots.pick_move borrows it.
# --------------------------------------------------------------------------
_games: Optional[Any] = None
_bots_error: Optional[str] = None


def _load_bots() -> Optional[Any]:
    global _games, _bots_error
    if _bots_error is not None:
        return None
    try:
        import bots as bots_mod                     # noqa: PLC0415
        if _games is None:
            _games = bots_mod.GameStore()
        return bots_mod
    except Exception as exc:
        _bots_error = f"{type(exc).__name__}: {exc}"
        log.warning("Bots unavailable: %s", _bots_error)
        return None


def _bots_available() -> bool:
    return _load_bots() is not None and os.path.exists(settings.maia2_onnx_path)


class NewGameRequest(BaseModel):
    bot: str = Field(min_length=1, max_length=32)
    color: str = Field(default="white", pattern="^(white|black|random)$")


class PlayMoveRequest(BaseModel):
    uci: str = Field(min_length=4, max_length=5)


def _bot_reply(bots_mod: Any, game: Any) -> None:
    """Play the bot's move, in place. Caller has already checked it is due."""
    import chess                                    # noqa: PLC0415
    if game.over or game.user_to_move:
        return
    bot = game.bot
    if bot.is_engine:
        core = _load_analysis_core()
        if core is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                                "The engine bot needs the analysis core")
        with core.Engine(settings.stockfish_path, hash_mb=settings.stockfish_hash_mb,
                         threads=settings.stockfish_threads) as eng:
            ev = eng.evaluate(game.board, depth=bot.engine_depth, multipv=1)
        move = chess.Move.from_uci(ev.best.move) if ev.best else None
        if move is None or move not in game.board.legal_moves:
            move = next(iter(game.board.legal_moves))
    else:
        core = _load_analysis_core()
        # A.6's opening book, already a process-wide singleton. Maia-2 is
        # trained with the opening plies skipped, so it has barely seen the
        # starting position and plays nonsense there without this.
        book = core.get_opening_book() if core else None
        move = bots_mod.pick_move(get_maia2_session(), game.board, bot, book=book)
    game.moves.append(game.board.san(move))
    game.board.push(move)
    game.updated_at = time.time()


@app.get("/api/play/bots", tags=["play"])
async def list_bots() -> Dict[str, Any]:
    """The opponents on offer. No auth: this is a menu."""
    bots_mod = _load_bots()
    if bots_mod is None or not _bots_available():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Bots unavailable ({_bots_error or 'Maia-2 weights not present'})")
    return {"bots": [b.to_dict() for b in bots_mod.BOTS]}


@app.post("/api/play/new", tags=["play"])
async def new_game(req: NewGameRequest,
                   caller: Caller = Depends(current_caller)) -> Dict[str, Any]:
    """
    Start a game. Anonymous is allowed -- playing a bot needs no account,
    and requiring one to try the feature would be a pointless gate.
    """
    import chess                                    # noqa: PLC0415
    bots_mod = _load_bots()
    if bots_mod is None or not _bots_available():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Bots unavailable ({_bots_error or 'Maia-2 weights not present'})")
    if req.bot not in bots_mod.BOTS_BY_ID:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No bot '{req.bot}'")

    colour = req.color
    if colour == "random":
        colour = secrets.choice(("white", "black"))
    game = bots_mod.Game(
        id=secrets.token_hex(8), bot_id=req.bot,
        user_color=chess.WHITE if colour == "white" else chess.BLACK,
        created_at=time.time(), updated_at=time.time())
    # Playing Black means the bot opens, so its first move happens here
    # rather than leaving the user looking at a board that will not respond.
    if not game.user_to_move:
        _bot_reply(bots_mod, game)
    _games.put(game)
    return game.to_dict()


@app.get("/api/play/{game_id}", tags=["play"])
async def get_game(game_id: str) -> Dict[str, Any]:
    bots_mod = _load_bots()
    game = _games.get(game_id) if (bots_mod and _games) else None
    if game is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "That game has expired or does not exist")
    return game.to_dict()


@app.post("/api/play/{game_id}/move", tags=["play"])
async def play_move(game_id: str, req: PlayMoveRequest) -> Dict[str, Any]:
    """
    Play one move and get the bot's reply in the same response.

    One round trip rather than two: the client has nothing useful to show
    between the two halves, and a second request only adds a window in which
    the board is in a state neither player chose.
    """
    import chess                                    # noqa: PLC0415
    bots_mod = _load_bots()
    game = _games.get(game_id) if (bots_mod and _games) else None
    if game is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "That game has expired or does not exist")
    if game.over:
        raise HTTPException(status.HTTP_409_CONFLICT, "That game is over")
    if not game.user_to_move:
        raise HTTPException(status.HTTP_409_CONFLICT, "It is not your move")

    try:
        move = chess.Move.from_uci(req.uci)
    except ValueError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"'{req.uci}' is not a move") from None
    if move not in game.board.legal_moves:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"'{req.uci}' is not legal in this position")

    game.moves.append(game.board.san(move))
    game.board.push(move)
    game.updated_at = time.time()
    _bot_reply(bots_mod, game)
    _games.put(game)
    return game.to_dict()


@app.post("/api/play/{game_id}/resign", tags=["play"])
async def resign_game(game_id: str) -> Dict[str, Any]:
    bots_mod = _load_bots()
    game = _games.get(game_id) if (bots_mod and _games) else None
    if game is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "That game has expired or does not exist")
    game.resigned = True
    game.updated_at = time.time()
    return game.to_dict()


def _client_key(request: Request) -> str:
    """Rate-limit key for an unauthenticated caller."""
    return request.client.host if request.client else "anon"


class CredentialsRequest(BaseModel):
    """Username and password. Never logged, never echoed back."""

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


def _require_local_accounts() -> Any:
    store = get_accounts() if _local_accounts_active() else None
    if store is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Supabase is configured, so sign-in goes through it rather than "
            "through this endpoint."
            if supabase.configured else
            f"Local accounts unavailable ({_accounts_error})",
        )
    return store


@app.post("/api/auth/signup", tags=["auth"])
async def signup(req: CredentialsRequest, request: Request) -> Dict[str, Any]:
    """
    Create an account and sign in.

    Rate limited on the caller's address: account creation is the one
    unauthenticated write in the app, and without a limit it is a free way
    to fill the disk.
    """
    import accounts as accounts_mod                 # noqa: PLC0415
    store = _require_local_accounts()
    ok, retry = limiter.check(f"signup:{_client_key(request)}")
    if not ok:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                            "Too many sign-ups from here. Try later.",
                            headers={"Retry-After": str(retry)})
    try:
        user, token = store.create_user(req.username, req.password)
    except accounts_mod.AccountError as exc:
        # The user's own mistake, and safe to repeat back verbatim -- these
        # messages name the rule that was broken.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return {"token": token, "user": {"id": user.id, "username": user.username},
            "stats": store.stats(user.id)}


@app.post("/api/auth/login", tags=["auth"])
async def login(req: CredentialsRequest, request: Request) -> Dict[str, Any]:
    """Sign in. The failure message never says WHICH half was wrong."""
    import accounts as accounts_mod                 # noqa: PLC0415
    store = _require_local_accounts()
    ok, retry = limiter.check(f"login:{_client_key(request)}")
    if not ok:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                            "Too many attempts from here. Try later.",
                            headers={"Retry-After": str(retry)})
    try:
        user, token = store.login(req.username, req.password)
    except accounts_mod.AccountError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    return {"token": token, "user": {"id": user.id, "username": user.username},
            "stats": store.stats(user.id)}


@app.post("/api/auth/logout", tags=["auth"])
async def logout(authorization: Optional[str] = Header(default=None)
                 ) -> Dict[str, Any]:
    """Drop the session server-side, so the token stops working."""
    store = get_accounts()
    if store and authorization and authorization.lower().startswith("bearer "):
        store.logout(authorization.split(" ", 1)[1].strip())
    return {"ok": True}


@app.delete("/me/analyses/{analysis_id}", tags=["accounts"])
async def delete_analysis(analysis_id: str,
                          caller: Caller = Depends(require_user)
                          ) -> Dict[str, Any]:
    """
    Remove one saved analysis.

    Worth having rather than relying on the 30-cap to evict: the cap drops
    the OLDEST, which is not necessarily the one the user wants gone.
    """
    store = get_accounts() if _local_accounts_active() else None
    if store is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "Persistence is not configured")
    if not store.delete_analysis(caller.user_id, analysis_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Analysis not found")
    return {"deleted": analysis_id}


@app.get("/puzzles/next", tags=["puzzles"])
async def next_puzzle(caller: Caller = Depends(require_user)) -> Dict[str, Any]:
    """
    A puzzle near the user's rating that they have not already attempted.

    The rating window widens on each retry, because a 50,000-puzzle subset
    has thin spots and an empty band must degrade into "slightly off-rating"
    rather than into an error.

    The solution is NOT in the response. It stays server-side and the
    attempt is validated here -- see partC.Puzzle.to_dict.
    """
    core = _load_puzzle_core()
    if core is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Puzzles unavailable ({_part_c_error})",
        )

    if _dev_mode():
        import random as _random

        stat = _stats_for(caller.user_id or "dev")
        rating = stat["puzzle_rating"]
        store = get_accounts() if _local_accounts_active() else None
        seen = (set(store.attempted_ids(caller.user_id))
                if store and caller.user_id
                else _dev_attempts.setdefault(caller.user_id or "dev", set()))
        for attempt in range(6):
            low, high = core.rating_window(rating, attempt)
            pool = [p for p in _dev_puzzle_list()
                    if low <= p.rating <= high and p.id not in seen]
            if pool:
                p = _random.choice(pool)
                return {"puzzle": p.to_dict(), "your_rating": rating,
                        "rating_window": [low, high], "dev_mode": True}
        remaining = [p for p in _dev_puzzle_list() if p.id not in seen]
        if not remaining:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                f"The dev sample only has {len(_dev_puzzle_list())} puzzles and "
                "you have attempted them all. Seed the real set to continue.")
        p = _random.choice(remaining)
        return {"puzzle": p.to_dict(), "your_rating": rating,
                "rating_window": [p.rating, p.rating], "dev_mode": True,
                "note": "off-rating: the dev sample is small"}

    if not supabase.configured:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "Puzzles unavailable (Supabase not configured)")

    stats = await supabase.select(
        "user_stats", filters={"user_id": f"eq.{caller.user_id}"}, limit=1)
    rating = (stats[0]["puzzle_rating"] if stats else core.DEFAULT_RATING)

    seen = {
        row["puzzle_id"] for row in await supabase.select(
            "puzzle_attempts", columns="puzzle_id",
            filters={"user_id": f"eq.{caller.user_id}"}, limit=2000)
    }

    for attempt in range(5):
        low, high = core.rating_window(rating, attempt)
        rows = await supabase.select(
            "puzzles",
            filters={"rating": [f"gte.{low}", f"lte.{high}"]},
            limit=60,
        )
        candidates = [r for r in rows if r["id"] not in seen]
        if candidates:
            import random as _random

            row = _random.choice(candidates)
            puzzle = _puzzle_from_db(core, row)
            return {
                "puzzle": puzzle.to_dict(),
                "your_rating": rating,
                "rating_window": [low, high],
            }

    raise HTTPException(
        status.HTTP_404_NOT_FOUND,
        "No unattempted puzzle found near your rating -- the subset may need "
        "rebuilding with a wider rating spread",
    )


@app.post("/puzzles/{puzzle_id}/step", tags=["puzzles"])
async def step_attempt(
    puzzle_id: str,
    req: AttemptRequest,
    caller: Caller = Depends(require_user),
) -> Dict[str, Any]:
    """
    Validate the line so far and, when it is right, play the scripted reply.

    An interactive board needs this: the player clicks one move and the
    opponent has to answer before they can click the next one, but the
    opponent's replies live inside the hidden solution, so only the server
    can supply them. This endpoint does NOT touch the rating -- the client
    submits the finished line to /attempt for that.

    Deliberately it does not say what the right move was on a wrong one.
    The client auto-submits as soon as a step comes back wrong, and /attempt
    reveals the expected move there, so the answer still reaches the player
    through the path that also records the loss.

    Known limitation: a hand-written client could call this repeatedly to
    find the solution for free and then submit a clean line to /attempt.
    Closing that needs per-attempt server state (a row written when the
    puzzle is served, so the rating is already committed before the first
    step). Noted in decisions.md rather than papered over.
    """
    core = _load_puzzle_core()
    if core is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Puzzles unavailable ({_part_c_error})",
        )
    if not req.moves:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "No moves submitted")

    puzzle = await _fetch_puzzle(core, puzzle_id)
    result = core.check_attempt(puzzle, req.moves)

    n = len(req.moves)
    ok = result.moves_correct >= n
    last = result.checks[n - 1] if ok and len(result.checks) >= n else None
    return {
        "puzzle_id": puzzle_id,
        "index": n - 1,
        "correct": ok,
        "complete": bool(result.solved),
        "moves_required": puzzle.moves_required,
        "opponent_reply": last.opponent_reply if last else None,
        "fen_after": last.fen_after if last else None,
        "accepted_alternative": bool(last.accepted_alternative) if last else False,
    }


async def _fetch_puzzle(core: Any, puzzle_id: str) -> Any:
    """One puzzle by id, from the dev sample or the database."""
    if _dev_mode():
        puzzle = next((p for p in _dev_puzzle_list() if p.id == puzzle_id), None)
        if puzzle is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown puzzle")
        return puzzle
    if not supabase.configured:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "Puzzles unavailable (Supabase not configured)")
    rows = await supabase.select("puzzles", filters={"id": f"eq.{puzzle_id}"}, limit=1)
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown puzzle")
    return _puzzle_from_db(core, rows[0])


@app.post("/puzzles/{puzzle_id}/attempt", tags=["puzzles"])
async def submit_attempt(
    puzzle_id: str,
    req: AttemptRequest,
    caller: Caller = Depends(require_user),
) -> Dict[str, Any]:
    """
    Validate an attempt, record it, and update the rating.

    Validation happens here, never in the client: a client that decided
    whether it was right could award itself any rating. The attempt and the
    stats update go through one Postgres function so a second tab cannot
    lose an update.
    """
    core = _load_puzzle_core()
    if core is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Puzzles unavailable ({_part_c_error})",
        )

    puzzle = await _fetch_puzzle(core, puzzle_id)

    if _dev_mode():
        # The same validation and Elo code the database path uses.
        result = core.check_attempt(puzzle, req.moves)
        user = caller.user_id or "dev"
        stat = _stats_for(user)
        change = core.update_rating(stat["puzzle_rating"], puzzle.rating,
                                    result.solved, attempts=stat["puzzle_attempts"])

        store = get_accounts() if _local_accounts_active() else None
        if store and caller.user_id:
            # Attempt and stats move together, in one transaction -- the
            # same guarantee C.2 gets from a Postgres function. A rating
            # that does not match its own history is worse than no rating.
            stat = store.record_attempt(caller.user_id, puzzle_id,
                                        result.solved, change.after,
                                        req.ms_taken)
        else:
            stat["puzzle_rating"] = change.after
            stat["puzzle_attempts"] += 1
            stat["puzzles_solved"] += 1 if result.solved else 0
            stat["current_streak"] = stat["current_streak"] + 1 if result.solved else 0
            stat["best_streak"] = max(stat["best_streak"], stat["current_streak"])
            _dev_attempts.setdefault(user, set()).add(puzzle_id)

        return {"result": result.to_dict(), "rating": change.to_dict(),
                "stats": dict(stat), "dev_mode": True,
                "solution": puzzle.solution if result.solved else None}

    result = core.check_attempt(puzzle, req.moves)

    stats = await supabase.select(
        "user_stats", filters={"user_id": f"eq.{caller.user_id}"}, limit=1)
    rating_before = stats[0]["puzzle_rating"] if stats else core.DEFAULT_RATING
    attempts_before = stats[0]["puzzle_attempts"] if stats else 0
    change = core.update_rating(
        rating_before, puzzle.rating, result.solved, attempts=attempts_before)

    updated = None
    try:
        updated = await supabase.rpc("record_puzzle_attempt", {
            "p_user_id": caller.user_id,
            "p_puzzle_id": puzzle_id,
            "p_solved": result.solved,
            "p_ms_taken": req.ms_taken,
            "p_moves": req.moves,
            "p_rating_before": change.before,
            "p_rating_after": change.after,
        })
    except Exception as exc:
        # The verdict is still correct and worth returning; only the
        # bookkeeping failed.
        log.warning("could not record attempt for %s: %s", puzzle_id, exc)

    return {
        "result": result.to_dict(),
        "rating": change.to_dict(),
        "stats": (updated[0] if isinstance(updated, list) and updated else updated),
        # Only once it is over -- returning it earlier would hand over the
        # answer to anyone submitting a deliberate wrong move.
        "solution": puzzle.solution if result.solved else None,
    }


def _puzzle_from_db(core: Any, row: Dict[str, Any]) -> Any:
    """A DB row -> partC.Puzzle. The row already holds the player's position."""
    return core.Puzzle(
        id=row["id"],
        fen=row["fen"],
        solution=list(row.get("solution") or []),
        opponent_replies=list(row.get("opponent_replies") or []),
        rating=row.get("rating", 1500),
        rating_deviation=row.get("rating_deviation") or 100,
        popularity=row.get("popularity") or 0,
        nb_plays=row.get("nb_plays") or 0,
        themes=list(row.get("themes") or []),
        game_url=row.get("game_url"),
        opening_tags=list(row.get("opening_tags") or []),
    )


# --------------------------------------------------------------------------
# Part E.4 frontend -- LAST, after every route above is registered.
#
# install_spa adds a GET /{full_path:path} catch-all, which claims any path
# not already matched. Anything registered after this line is unreachable, so
# new routes go above it.
#
# It is a no-op until `frontend/` has been built (npm run build writes
# static/), so this is safe with no build present: the API and /harness carry
# on as before.
# --------------------------------------------------------------------------
try:
    from spa import install_spa  # noqa: PLC0415
except ImportError as _spa_exc:        # pragma: no cover - spa.py is optional
    log.warning("SPA not mounted (%s)", _spa_exc)
else:
    if install_spa(app):
        log.info("Frontend mounted from static/ (SPA routes -> index.html)")
    else:
        log.info(
            "No frontend build in static/ -- API only. "
            "Build it with: cd frontend && npm ci && npm run build"
        )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("partE:app", host="0.0.0.0", port=settings.port, reload=True)
