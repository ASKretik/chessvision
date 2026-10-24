"""
Part A -- Analysis core.

A.1  Input adapters       -- Lichess / Chess.com / PGN / FEN -> one GameInput
A.2  Engine service       -- Stockfish over UCI, MultiPV, FEN-keyed cache
A.3  Move classification  -- Lichess Win% + Accuracy% formulas
A.4  Time analysis        -- MultiPV complexity vs. %clk deltas, heuristic
A.5  Rating estimate      -- Maia-2 ONNX, max-likelihood over rating bands
A.6  Phase + opening      -- phase verdict, ECO code, move left book

Entry points:
    load_lichess / load_chesscom / load_pgn / load_fen  -> GameInput
    analyse(game_input, ...)                            -> GameReport | PositionReport

Imports onnxruntime lazily and never imports torch (E.3). The .onnx file is
produced offline by tools/export_maia2_onnx.py.

Open decisions from plan.md §8 resolved here; each is marked DECISION with
its reasoning and is overridable through the function arguments.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import math
import os
import re
import threading
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import chess
import chess.engine

import rating_profile
import chess.pgn

log = logging.getLogger("chessapp.analysis")

ProgressFn = Callable[[str, float, Optional[Dict[str, Any]]], None]

# Centipawn cap. Engine evals beyond this say "winning", not "more winning",
# and letting +4000 into an average would make ACPL meaningless.
CP_CAP = 1000
MATE_CP = 10_000


# ==========================================================================
# A.1  Input adapters
#
# Four sources, one normalised representation: PGN + metadata. Everything
# downstream reads GameInput and nothing else.
# ==========================================================================
class SourceType(str, Enum):
    LICHESS = "lichess"
    CHESSCOM = "chesscom"
    PGN = "pgn"
    FEN = "fen"


class InputError(ValueError):
    """Source could not be fetched or parsed. Safe to show to a user."""


@dataclass
class GameInput:
    source_type: str
    source_ref: Optional[str]
    pgn: Optional[str]
    fen: Optional[str] = None
    headers: Dict[str, str] = field(default_factory=dict)
    # A.1: detected, not assumed. False disables A.4 entirely rather than
    # emitting zeroed-out time stats.
    has_clock: bool = False

    @property
    def is_position_only(self) -> bool:
        return self.source_type == SourceType.FEN.value

    def game(self) -> chess.pgn.Game:
        if self.pgn is None:
            raise InputError("No PGN on this input (FEN mode)")
        game = chess.pgn.read_game(io.StringIO(self.pgn))
        if game is None or game.next() is None:
            raise InputError("PGN contains no moves")
        return game


_USER_AGENT = "chess-analysis-app/0.1 (UMC301 student project)"
_LICHESS_ID = re.compile(r"lichess\.org/(?:game/export/)?([a-zA-Z0-9]{8})")


def _http_get(url: str, *, accept: str, timeout: float = 20.0, what: str = "Game") -> str:
    """
    Single blocking HTTP GET.

    httpx is used synchronously on purpose: Part A is called from a worker
    thread (see partE._run_analysis), so an async client here would mean two
    event loops for no gain.
    """
    import httpx

    try:
        r = httpx.get(
            url,
            timeout=timeout,
            follow_redirects=True,
            headers={"Accept": accept, "User-Agent": _USER_AGENT},
        )
    except httpx.HTTPError as exc:
        raise InputError(f"Could not reach {url.split('?')[0]}: {type(exc).__name__}") from exc
    if r.status_code == 404:
        raise InputError(f"{what} not found")
    if r.status_code == 429:
        raise InputError("Upstream rate limit hit -- try again in a minute")
    if r.status_code >= 400:
        raise InputError(f"Upstream returned {r.status_code}")
    return r.text


def load_lichess(url_or_id: str) -> GameInput:
    """
    Lichess: GET /game/export/{id}. The easy path -- clocks and often
    %eval come back in the PGN directly.
    """
    m = _LICHESS_ID.search(url_or_id)
    game_id = m.group(1) if m else url_or_id.strip()
    if not re.fullmatch(r"[a-zA-Z0-9]{8,12}", game_id):
        raise InputError(f"Not a recognisable Lichess game id or URL: {url_or_id!r}")
    game_id = game_id[:8]  # 12-char ids are player-scoped; first 8 is the game

    pgn = _http_get(
        f"https://lichess.org/game/export/{game_id}"
        "?clocks=true&evals=true&literate=false&tags=true",
        accept="application/x-chess-pgn",
        what=f"Lichess game {game_id!r}",
    )
    return _from_pgn_text(pgn, SourceType.LICHESS, f"https://lichess.org/{game_id}")


CHESSCOM_MAX_MONTHS = 24


def load_chesscom(
    url: str,
    username: Optional[str] = None,
    *,
    month: Optional[str] = None,
    max_months: int = CHESSCOM_MAX_MONTHS,
) -> GameInput:
    """
    Chess.com: there is no get-game-by-URL endpoint, so the game has to be
    found by scanning that player's monthly archives (newest first) and
    filtering on the `url` field. Awkward, and the fragile path per plan.md F.

    `username` is required because a Chess.com game URL
    (/game/live/123456789) carries no player or date.

    `month` ("YYYY/MM") skips straight to one archive -- one request instead
    of up to `max_months`. Worth surfacing in the UI as an optional field.

    The scan is bounded: a decade-old account has ~120 archives of several MB
    each, and walking all of them would take minutes and megabytes to report
    a URL typo.
    """
    url = url.strip().rstrip("/")
    if not username:
        raise InputError(
            "Chess.com needs the username as well as the URL -- the API has "
            "no get-by-url endpoint, so the monthly archive must be scanned."
        )
    user = username.strip().lower()
    target = _normalise_chesscom_url(url)

    if month:
        if not re.fullmatch(r"\d{4}/\d{2}", month):
            raise InputError(f"month must look like 'YYYY/MM', got {month!r}")
        candidates = [f"https://api.chess.com/pub/player/{user}/games/{month}"]
        searched = month
    else:
        archives_raw = _http_get(
            f"https://api.chess.com/pub/player/{user}/games/archives",
            accept="application/json",
            what=f"Chess.com user {username!r}",
        )
        try:
            archives: List[str] = json.loads(archives_raw).get("archives", [])
        except json.JSONDecodeError as exc:
            raise InputError("Chess.com archive list was not valid JSON") from exc
        if not archives:
            raise InputError(f"No public games found for Chess.com user {username!r}")
        # Newest month first: people analyse recent games.
        candidates = list(reversed(archives))[:max_months]
        searched = f"the last {len(candidates)} month(s)"

    for month_url in candidates:
        month_raw = _http_get(
            month_url, accept="application/json", what="Chess.com archive"
        )
        try:
            games = json.loads(month_raw).get("games", [])
        except json.JSONDecodeError:
            continue
        for g in games:
            if _normalise_chesscom_url(g.get("url", "")) == target:
                pgn = g.get("pgn")
                if not pgn:
                    raise InputError("Chess.com returned the game without a PGN")
                return _from_pgn_text(pgn, SourceType.CHESSCOM, url)

    raise InputError(
        f"Game not found in {username}'s archives ({searched}). Check the URL "
        "belongs to that player and that their games are public; for an older "
        "game, pass the month as YYYY/MM."
    )


def _normalise_chesscom_url(url: str) -> str:
    """Strip scheme, www, query and trailing slash so URL forms compare equal."""
    u = url.strip().lower().split("?")[0].rstrip("/")
    u = re.sub(r"^https?://", "", u)
    return re.sub(r"^www\.", "", u)


def load_pgn(pgn_text: str) -> GameInput:
    """Pasted or uploaded PGN. Trivial, and the fallback when an API fails."""
    if not pgn_text or not pgn_text.strip():
        raise InputError("Empty PGN")
    return _from_pgn_text(pgn_text, SourceType.PGN, None)


def load_fen(fen: str) -> GameInput:
    """
    Single-position mode.

    DECISION (§8.2): a FEN has no clock data, no move history and no rating
    signal, so this path deliberately produces a different output shape --
    engine lines and a complexity read on one position. No time analysis, no
    accuracy, no rating estimate. See analyse_position / PositionReport.
    """
    if not fen or not fen.strip():
        raise InputError("Empty FEN")
    fen = fen.strip()
    try:
        board = chess.Board(fen)
    except ValueError as exc:
        raise InputError(f"Invalid FEN: {exc}") from exc
    if not board.is_valid():
        raise InputError("FEN describes an illegal position")
    return GameInput(
        source_type=SourceType.FEN.value,
        source_ref=None,
        pgn=None,
        fen=board.fen(),
        headers={},
        has_clock=False,
    )


def _from_pgn_text(pgn_text: str, source: SourceType, ref: Optional[str]) -> GameInput:
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    if game is None:
        raise InputError("Could not parse that PGN")
    if game.next() is None:
        raise InputError("PGN contains no moves")

    # A.1: detect %clk rather than trusting the TimeControl header -- daily
    # games and imported PGNs often have one without the other.
    has_clock = any(node.clock() is not None for node in game.mainline())

    return GameInput(
        source_type=source.value,
        source_ref=ref,
        pgn=pgn_text,
        headers=dict(game.headers),
        has_clock=has_clock,
    )


def load(source: str, chesscom_username: Optional[str] = None) -> GameInput:
    """Best-effort dispatch for a single pasted string."""
    s = source.strip()
    if "lichess.org" in s:
        return load_lichess(s)
    if "chess.com" in s:
        return load_chesscom(s, chesscom_username)
    if "[Event" in s or re.match(r"^\s*1\.", s):
        return load_pgn(s)
    if re.match(r"^[rnbqkpRNBQKP1-8/]+\s+[wb]\s", s):
        return load_fen(s)
    raise InputError("Could not tell what that is: expected a URL, PGN or FEN")


# ==========================================================================
# A.2  Engine service
#
# Stockfish via python-chess UCI. MultiPV is always on: A.4 needs the
# top-k spread for complexity, and Part B needs "was this the only good
# move". Evaluations are cached by position -- opening positions repeat
# heavily across users, and within one game every position is scored twice
# (as "after move i" and "before move i+1").
# ==========================================================================
@dataclass
class EngineLine:
    """One MultiPV line. Scores are White-POV so they compose across plies."""

    rank: int
    move: str                   # UCI
    move_san: str
    cp_white: int               # mate folded to +-MATE_CP
    mate_in: Optional[int]
    pv: List[str] = field(default_factory=list)   # SAN, truncated

    def cp_for(self, color: chess.Color) -> int:
        return self.cp_white if color == chess.WHITE else -self.cp_white


@dataclass
class PositionEval:
    fen: str
    turn: bool
    lines: List[EngineLine]
    depth: int
    legal_moves: int
    terminal: bool = False
    outcome: Optional[str] = None

    @property
    def best(self) -> Optional[EngineLine]:
        return self.lines[0] if self.lines else None

    def is_mate(self) -> bool:
        """True if this position has a forced mate (either way), or IS mate."""
        if self.terminal:
            return self.outcome == "checkmate"
        return bool(self.best and self.best.mate_in is not None)

    def cp_white(self) -> int:
        """Position value from White's POV, i.e. the best line's score."""
        if self.terminal:
            if self.outcome == "checkmate":
                # Side to move is mated.
                return -MATE_CP if self.turn == chess.WHITE else MATE_CP
            return 0  # stalemate / insufficient material / repetition
        return self.best.cp_white if self.best else 0


class EngineService:
    """
    Owns one Stockfish process. Use as a context manager -- an orphaned
    engine process is a 40 MB leak inside a 512 MB budget (E.3).
    """

    def __init__(
        self,
        path: str = "stockfish",
        *,
        depth: int = 14,
        multipv: int = 3,
        hash_mb: int = 16,
        threads: int = 1,
        cache_size: int = 20_000,
        pv_length: int = 6,
    ) -> None:
        self.path = path
        self.depth = depth
        self.multipv = multipv
        self.hash_mb = hash_mb
        self.threads = threads
        self.pv_length = pv_length
        self._engine: Optional[chess.engine.SimpleEngine] = None
        self._cache: Dict[Tuple[str, int, int], PositionEval] = {}
        self._cache_size = cache_size
        self.cache_hits = 0
        self.cache_misses = 0

    def __enter__(self) -> "EngineService":
        self.open()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def open(self) -> None:
        if self._engine is not None:
            return
        try:
            self._engine = chess.engine.SimpleEngine.popen_uci(self.path)
        except FileNotFoundError as exc:
            raise InputError(
                f"Stockfish not found at {self.path!r}. Set STOCKFISH_PATH."
            ) from exc
        # E.3: the hash table is the single biggest tunable here. 16 MB is
        # plenty at depth ~14 and leaves room for onnxruntime.
        opts = {"Hash": self.hash_mb, "Threads": self.threads}
        available = self._engine.options
        self._engine.configure({k: v for k, v in opts.items() if k in available})

    def close(self) -> None:
        if self._engine is not None:
            try:
                self._engine.quit()
            except Exception:
                pass
            self._engine = None

    def analyse(
        self, board: chess.Board, *, multipv: Optional[int] = None, depth: Optional[int] = None
    ) -> PositionEval:
        k = multipv or self.multipv
        d = depth or self.depth

        if board.is_game_over(claim_draw=False):
            return PositionEval(
                fen=board.fen(),
                turn=board.turn,
                lines=[],
                depth=0,
                legal_moves=0,
                terminal=True,
                outcome="checkmate" if board.is_checkmate() else "draw",
            )

        # EPD omits the halfmove/fullmove counters, which do not change the
        # evaluation, so transpositions and repeated openings share entries.
        key = (board.epd(), k, d)
        cached = self._cache.get(key)
        if cached is not None:
            self.cache_hits += 1
            return cached
        self.cache_misses += 1

        if self._engine is None:
            self.open()
        assert self._engine is not None

        infos = self._engine.analyse(board, chess.engine.Limit(depth=d), multipv=k)
        if isinstance(infos, dict):  # multipv=1 returns a bare InfoDict
            infos = [infos]

        lines: List[EngineLine] = []
        for i, info in enumerate(infos):
            pv_moves = info.get("pv") or []
            if not pv_moves:
                continue
            score = info["score"].white()
            lines.append(
                EngineLine(
                    rank=i + 1,
                    move=pv_moves[0].uci(),
                    move_san=board.san(pv_moves[0]),
                    cp_white=score.score(mate_score=MATE_CP),
                    mate_in=score.mate(),
                    pv=_pv_to_san(board, pv_moves[: self.pv_length]),
                )
            )

        result = PositionEval(
            fen=board.fen(),
            turn=board.turn,
            lines=lines,
            depth=d,
            legal_moves=board.legal_moves.count(),
        )
        if len(self._cache) >= self._cache_size:
            self._cache.clear()  # crude, but bounded and never evicts mid-game
        self._cache[key] = result
        return result

    def stats(self) -> Dict[str, Any]:
        total = self.cache_hits + self.cache_misses
        return {
            "cache_entries": len(self._cache),
            "hits": self.cache_hits,
            "misses": self.cache_misses,
            "hit_rate": round(self.cache_hits / total, 3) if total else 0.0,
        }


def _pv_to_san(board: chess.Board, moves: Sequence[chess.Move]) -> List[str]:
    out: List[str] = []
    b = board.copy(stack=False)
    for mv in moves:
        if mv not in b.legal_moves:
            break
        out.append(b.san(mv))
        b.push(mv)
    return out


# ==========================================================================
# A.3  Move classification
#
# Lichess's published formulas, ported verbatim. Accuracy% is shown in the
# UI because it is a legitimate, well-known metric; it is NOT used to derive
# a rating -- that is A.5's job, for the reasons in plan.md A.5.
# ==========================================================================
def win_percent(cp: float) -> float:
    """Centipawns -> winning chances 0..100, from the POV of the cp sign."""
    cp = max(-CP_CAP, min(CP_CAP, cp))
    return 50 + 50 * (2 / (1 + math.exp(-0.00368208 * cp)) - 1)


def accuracy_percent(win_before: float, win_after: float) -> float:
    """
    Lichess accuracy for one move, from the mover's win% before and after.

    A move that improves the mover's position gives >100 before clamping,
    hence the clamp rather than an assertion.
    """
    drop = max(0.0, win_before - win_after)
    return max(0.0, min(100.0, 103.1668 * math.exp(-0.04354 * drop) - 3.1669))


class MoveLabel(str, Enum):
    BEST = "best"
    EXCELLENT = "excellent"
    GOOD = "good"
    INACCURACY = "inaccuracy"
    MISTAKE = "mistake"
    BLUNDER = "blunder"


# Win% drop thresholds.
#
# plan.md A.3 says 30/20/10, which is WRONG BY 2x and was corrected here with
# the user's agreement. lila computes "winning chances" on a [-1, 1] scale and
# judges on deltas of 0.3/0.2/0.1; Win% is 50 + 50*wc, so the same constants
# are 15/10/5 in Win% points. The plan carried the numbers across without
# rescaling.
#
# Measured over 9,524 judged plies in 120 Lichess-analysed games
# (tests/test_classification_vs_lichess.py):
#     30/20/10 -> 86.4% agreement with Lichess's own labels
#     15/10/5  -> 99.8%
# The residual 0.2% are moves Lichess declines to label at all.
BLUNDER_DROP = 15.0
MISTAKE_DROP = 10.0
INACCURACY_DROP = 5.0

# Mate-status transitions are judged on a separate scale (see
# classify_mate_transition). Boundaries measured, not guessed.
MATE_TRANSITION_BLUNDER_CP = 700
MATE_TRANSITION_MISTAKE_CP = 1000

# Values for MoveRecord.mate_status_change.
MATE_NOW_FORCED = "mate_now_forced"      # no forced mate -> opponent mates
FORCED_MATE_LOST = "forced_mate_lost"    # had a forced mate -> no longer


def classify_mate_transition(nonmate_cp_white: int) -> MoveLabel:
    """
    Label a move that created or threw away a forced mate.

    Win% drop does not work here. Once a mate is forced the Win% is already
    pinned near 0 or 100, so walking into mate from a position that was
    already lost by 9 pawns is a ~3-point drop -- yet Lichess calls it a
    mistake, and calls the same transition from a 6-pawn deficit a blunder.
    The severity tracks how decided the position already was, not the swing.

    Measured on 162 mate-status transitions across 120 Lichess-analysed
    games: the label is set by the magnitude of the non-mate side's
    evaluation, with boundaries that fall cleanly at 700 and 1000 cp
    (observed Blunder up to |cp| 699 and Mistake from |cp| 702; Mistake up to
    997 and Inaccuracy from 1009).

    Inaccuracy-vs-unlabelled is NOT reproducible from the API: in the
    |cp| >= 1000 bucket Lichess labels some and silently skips others, which
    is presumably its advice de-duplication. Those are reported as
    inaccuracies here.
    """
    magnitude = abs(nonmate_cp_white)
    if magnitude < MATE_TRANSITION_BLUNDER_CP:
        return MoveLabel.BLUNDER
    if magnitude < MATE_TRANSITION_MISTAKE_CP:
        return MoveLabel.MISTAKE
    return MoveLabel.INACCURACY


def classify(
    win_drop: float,
    *,
    played_best: bool,
    mate_status_change: Optional[str] = None,
    nonmate_cp: Optional[int] = None,
) -> MoveLabel:
    """
    Win% drop -> label.

    played_best short-circuits everything: the engine's top choice is the
    measuring stick, so it cannot be a mistake against itself. Without this,
    a forced sequence or depth instability can show a drop on the very move
    the engine recommended and get labelled a blunder. Lichess agrees --
    it only ever advises on a move that differs from its best.

    A mate-status change is then judged on its own scale, because the Win%
    drop is uninformative once mate is forced.
    """
    if played_best:
        return MoveLabel.BEST
    if mate_status_change is not None and nonmate_cp is not None:
        return classify_mate_transition(nonmate_cp)
    if win_drop >= BLUNDER_DROP:
        return MoveLabel.BLUNDER
    if win_drop >= MISTAKE_DROP:
        return MoveLabel.MISTAKE
    if win_drop >= INACCURACY_DROP:
        return MoveLabel.INACCURACY
    if win_drop < 2.0:
        return MoveLabel.EXCELLENT
    return MoveLabel.GOOD


def game_accuracy(move_accuracies: Sequence[float]) -> Optional[float]:
    """
    Mean of per-move accuracies.

    Lichess blends a volatility-weighted mean with a harmonic mean; that
    weighting is not published as a formula, so the plain mean is used and
    labelled as such rather than approximating something unstated.
    """
    if not move_accuracies:
        return None
    return round(sum(move_accuracies) / len(move_accuracies), 1)


# ==========================================================================
# A.4  Time analysis -- heuristic, no model
# ==========================================================================
@dataclass
class Complexity:
    """How hard the position was to play, from the MultiPV spread."""

    score: float                # 0..1
    near_best: int              # lines within NEAR_BEST_CP of the best
    spread_cp: int              # best minus worst among the top-k
    legal_moves: int
    only_move: bool

    def band(self) -> str:
        if self.score >= 0.66:
            return "high"
        if self.score >= 0.33:
            return "medium"
        return "low"


NEAR_BEST_CP = 50
# An "only move": one line near best and everything else far worse. The
# threshold was an unnamed literal 100 inline; naming it keeps the definition
# used by the flag and by the complexity floor from drifting apart.
ONLY_MOVE_SPREAD_CP = 100
# Floor for an only-move position. Above HIGH_COMPLEXITY (0.66) by design:
# the point is that these positions ARE hard, and the raw scale cannot say so.
ONLY_MOVE_FLOOR = 0.70


def complexity_of(pe: PositionEval) -> Complexity:
    """
    Heuristic complexity. Documented so it can be tuned against real games
    rather than argued about:

      - a wide top-k spread means the choice matters -> harder
      - many moves clustered near the best means it is hard to go wrong -> easier
      - more legal moves means more to look at -> slightly harder

    The degenerate case the weights are chosen for is the "only move"
    position: one line near best, everything else far worse. That is the
    hardest kind, and it scores highest.
    """
    if pe.terminal or not pe.lines:
        return Complexity(0.0, 0, 0, pe.legal_moves, False)

    mover = pe.turn
    cps = [ln.cp_for(mover) for ln in pe.lines]
    best = cps[0]
    near_best = sum(1 for c in cps if best - c <= NEAR_BEST_CP)
    spread = best - cps[-1] if len(cps) > 1 else 0

    spread_term = min(max(spread, 0) / 300.0, 1.0)
    # (near_best - 1) / (k - 1): 0 when only one move is good, 1 when all are.
    choice_term = (near_best - 1) / (len(cps) - 1) if len(cps) > 1 else 0.0
    mobility_term = min(pe.legal_moves / 40.0, 1.0)

    score = 0.60 * spread_term + 0.25 * (1.0 - choice_term) + 0.15 * mobility_term

    # An only-move position IS the hard case -- the weights above were chosen
    # for it -- but the scale never got there. With multipv=3 the spread term
    # saturates at 300cp and the other two contribute at most 0.40 combined,
    # so an only-move with a 100-150cp spread scored "medium". That is
    # self-contradictory on its face ("only move - medium complexity"), and
    # it made the `rushed` time flag unreachable in exactly the positions
    # worth flagging: rushed needs complexity > HIGH_COMPLEXITY, which those
    # positions could not reach.
    only_move = near_best == 1 and spread >= ONLY_MOVE_SPREAD_CP
    if only_move:
        score = max(score, ONLY_MOVE_FLOOR)

    return Complexity(
        score=round(min(max(score, 0.0), 1.0), 3),
        near_best=near_best,
        spread_cp=int(spread),
        legal_moves=pe.legal_moves,
        only_move=only_move,
    )


@dataclass
class TimeControl:
    base_seconds: Optional[int]
    increment_seconds: int

    @property
    def known(self) -> bool:
        return self.base_seconds is not None

    def expected_per_move(self) -> Optional[float]:
        """
        Seconds a player can afford per move: base spread over an assumed
        40-move game, plus the increment. Crude, but it is the normaliser
        that makes "slow" comparable between bullet and rapid.
        """
        if self.base_seconds is None:
            return None
        return max(self.base_seconds / 40.0 + self.increment_seconds, 0.5)


def parse_time_control(header: Optional[str]) -> TimeControl:
    """Parse PGN TimeControl: '300+3', '600', '40/5400+30', '-' or missing."""
    if not header or header in ("-", "?"):
        return TimeControl(None, 0)
    spec = header.split(":")[0].strip()
    if "/" in spec:                      # moves/seconds+inc
        spec = spec.split("/", 1)[1]
    m = re.match(r"^(\d+)(?:\+(\d+))?$", spec)
    if not m:
        return TimeControl(None, 0)
    return TimeControl(int(m.group(1)), int(m.group(2) or 0))


# Flag thresholds. A move must be both out of proportion AND on the wrong
# side of the complexity read before it is called out.
WASTED_RATIO = 2.0
WASTED_FLOOR_S = 3.0
RUSHED_RATIO = 0.4
# The reportable conjunctions (A.4). Looser on time than the raw flags
# because the consequence is doing the work: a move only qualifies if it
# actually cost something (critical_rush) or cost nothing (wasted_think).
CRITICAL_RUSH_RATIO = 0.5
WASTED_THINK_RATIO = 2.5
LOW_COMPLEXITY = 0.33
HIGH_COMPLEXITY = 0.66


@dataclass
class TimeVerdict:
    spent_seconds: Optional[float]
    clock_after: Optional[float]
    ratio: Optional[float]          # spent / expected_per_move
    flag: Optional[str]             # "wasted" | "rushed" | None


def _time_verdict(
    spent: Optional[float],
    clock_after: Optional[float],
    tc: TimeControl,
    cx: Complexity,
) -> TimeVerdict:
    expected = tc.expected_per_move()
    if spent is None or expected is None:
        return TimeVerdict(spent, clock_after, None, None)
    ratio = spent / expected
    flag: Optional[str] = None
    if ratio >= WASTED_RATIO and spent >= WASTED_FLOOR_S and cx.score < LOW_COMPLEXITY:
        flag = "wasted"
    elif ratio <= RUSHED_RATIO and cx.score > HIGH_COMPLEXITY:
        flag = "rushed"
    return TimeVerdict(round(spent, 1), clock_after, round(ratio, 2), flag)


# ==========================================================================
# A.5  Rating estimate -- Maia-2 via ONNX
#
# No published accuracy->rating mapping exists, so none is invented here.
# Maia-2 conditions a move-prediction policy on both players' ratings, which
# turns rating estimation into maximum likelihood over rating buckets:
#
#     for b in buckets: score[b] = sum_moves log P(move_played | position, b)
#     estimate = argmax_b score[b]
#
# The deployed container ships onnxruntime + maia2.onnx and NEVER installs
# torch (E.3). Conversion is offline: tools/export_maia2_onnx.py.
#
# Everything model-shaped -- the move vocabulary, the Elo buckets, the plane
# layout, the output order -- is read from maia2_meta.json, which the export
# writes off the real package. The runtime refuses to guess any of it: the
# move vocabulary's ordering is part of the trained weights, so a wrong order
# maps logits to the wrong moves and yields confident nonsense rather than an
# error.
#
# VERIFIED against maia2 0.9.1 (maia2/utils.py, main.py, inference.py).
# tools/verify_maia2_onnx.py re-checks encoding, conversion and end-to-end
# agreement; run it after any re-export.
# ==========================================================================


class OpponentEloMode(str, Enum):
    """
    DECISION (plan.md §8.1): Maia-2 conditions on both players' ratings, so
    estimating player X needs a value for the opponent.

    HEADER  -- use the opponent's actual rating from the PGN. Default: it is
               the true conditioning value whenever it is known, which for
               Lichess and Chess.com games is almost always.
    TIED    -- opponent rating = the bucket being tested. Fallback when the
               header is missing; assumes a roughly matched game, which is
               what both sites' pairing produces.
    FIXED   -- a constant, for reproducible comparisons across games.

    Joint iteration over both players' buckets is deliberately not offered:
    it is buckets^2 inference passes for a number the plan asks to report as
    a point estimate with no interval.
    """

    HEADER = "header"
    TIED = "tied"
    FIXED = "fixed"


@dataclass
class RatingEstimate:
    """
    One player's estimate.

    The MLE searches all of the model's buckets, including the two
    open-ended ones, so a player well outside 1100-2000 is not pinned at an
    artificial edge. `band` is always the model's own label.

    `elo` is a 100-point point estimate derived from the curve's shape, so it
    is populated even for ">=2000" -- but read `elo_method` with it:
    "interpolated" is a real sub-bucket maximum, while "floor" and "ceiling"
    mean the curve never turned over, so the number is a bound in that
    direction and not a measurement.
    """

    available: bool
    band: Optional[str] = None          # e.g. "1500-1599", "<1100", ">=2000"
    # A point estimate on a 100-point grid, from the shape of the whole
    # likelihood curve rather than just its argmax (see refine_rating). Set
    # for every band, including ">=2000" -- but `elo_method` says whether it
    # is an interior maximum or a bound, so a UI can hedge a bound.
    elo: Optional[int] = None
    elo_method: Optional[str] = None
    elo_detail: Dict[str, Any] = field(default_factory=dict)
    # How much the moves actually pin down: the range of buckets the
    # likelihood does not rule out, plus a coarse confidence. Read this
    # BEFORE `elo` -- on a flat curve the point estimate means very little,
    # and presenting it alone overstates what one game can tell you.
    interval: Dict[str, Any] = field(default_factory=dict)
    bucket_index: Optional[int] = None
    moves_used: int = 0
    moves_skipped: int = 0
    log_likelihood: Optional[float] = None
    per_band: Dict[str, float] = field(default_factory=dict)
    opponent_elo_used: Optional[int] = None
    opponent_elo_mode: Optional[str] = None
    model_type: Optional[str] = None
    reason: Optional[str] = None        # why unavailable


def elo_to_bucket(elo: int, bucketing: Dict[str, int], n_buckets: int) -> int:
    """
    Raw Elo -> bucket index, reproducing maia2.utils.map_to_category exactly.

    Bucket 0 is "< start", buckets 1..n-2 are the closed `interval`-wide
    bands, and bucket n-1 is ">= end". Parameters come from the metadata
    rather than being hardcoded, so a re-export with different bucketing
    cannot silently disagree with the runtime.

    Deliberately NOT nearest-value matching: the real mapping is a
    half-open range lookup, and the two differ at every boundary.
    """
    start, end = bucketing["start"], bucketing["end"]
    interval = bucketing["interval"]
    if elo < start:
        return 0
    if elo >= end:
        return n_buckets - 1
    return 1 + (elo - start) // interval


# --------------------------------------------------------------------------
# A.5a  Turning 11 buckets into a 100-point estimate
#
# Maia-2's embedding table has exactly 11 rows, the top one being ">=2000".
# The model was never trained to tell 2100 from 2500, so that resolution is
# not in the weights and no amount of code can conjure it.
#
# What the weights DO carry is the *shape* of the log-likelihood curve across
# the buckets, and argmax throws that away. Two observations from real games:
#
#   a 1400-rated player   curve peaks in the interior and turns over
#   a 2400-rated player   curve is still climbing at >=2000 (slope +0.30)
#
# So:
#   interior peak -> fit a parabola through the peak and its neighbours and
#                    take the vertex. A genuine sub-bucket maximum-likelihood
#                    estimate, no new assumptions.
#   peak at an end -> the true maximum is outside the model's range. Report
#                    the edge and say so: `elo_method` is "floor" (>=2000) or
#                    "ceiling" (<1100), meaning the number is a bound.
#
# The obvious alternative -- projecting past the last bucket from the final
# slope -- was tried, measured at R^2 = 0.0007 against 186 players of known
# rating, and removed. The calibration file and its loader went with it: a
# table of zeros that nothing reads is not a record of the experiment, it is
# a thing the next reader has to work out is dead. The result is in
# decisions.md 3a2.2, which is where a negative result belongs.
ELO_GRID = 100                 # report on a 100-point grid, as requested


# How much worse than the peak a bucket may be and still count as "the moves
# do not rule this out". 1.92 is the standard likelihood-ratio cutoff for one
# parameter (half of chi-square(1) at 95%), which is the same rule a
# statistician would use to read this curve by eye.
LR_CUTOFF = 1.92


def likelihood_interval(
    bands: Sequence[str],
    per_band: Dict[str, float],
    bucketing: Dict[str, int],
) -> Dict[str, Any]:
    """
    How much the moves actually pin down, from the shape of the curve.

    The argmax alone hides the thing that matters most: whether the peak is
    sharp or the curve is nearly flat. A real example from this codebase --
    eight moves, and the span from 1400 to ">=2000" covered 0.11 log-
    likelihood, a likelihood ratio of 1.12. Reporting "2000+" from that is
    not a measurement, it is noise with a number printed on it.

    Returns the contiguous run of buckets within LR_CUTOFF of the peak, as
    an Elo range, plus the posterior mass that run carries under a uniform
    prior over buckets, and a coarse confidence label.

    The uniform prior is an assumption and a wrong one -- most players are
    not above 2000 -- so `mass` is a description of this curve, not a
    calibrated probability. It is reported to show flatness, not to claim
    coverage.
    """
    if not bands or not per_band:
        return {}

    lls = [per_band[b] for b in bands]
    peak = max(range(len(lls)), key=lambda i: lls[i])
    cutoff = lls[peak] - LR_CUTOFF

    # Walk out from the peak while the curve stays within the cutoff. A
    # contiguous run, because a disconnected bucket on the far side is an
    # artefact of a noisy curve rather than a second real explanation.
    low = peak
    while low - 1 >= 0 and lls[low - 1] >= cutoff:
        low -= 1
    high = peak
    while high + 1 < len(lls) and lls[high + 1] >= cutoff:
        high += 1

    # Posterior over buckets, uniform prior. Subtract the max before the
    # exponential or a long game underflows to zero everywhere.
    top = lls[peak]
    weights = [math.exp(v - top) for v in lls]
    total = sum(weights)
    mass = sum(weights[low:high + 1]) / total if total else 0.0

    start, interval = bucketing["start"], bucketing["interval"]
    # Band edges in Elo. The open-ended buckets have no outer edge, which is
    # reported as None rather than invented.
    low_elo = None if low == 0 else start + (low - 1) * interval
    high_elo = None if high == len(bands) - 1 else start + high * interval - 1

    # Only meaningful when both ends are closed; an open end has no width.
    width = (None if low_elo is None or high_elo is None
             else high_elo - low_elo)
    if high - low <= 1 and low_elo is not None and high_elo is not None:
        confidence = "high"
    elif high - low <= 3:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "low_band": bands[low],
        "high_band": bands[high],
        "low_elo": low_elo,
        "high_elo": high_elo,
        "buckets": high - low + 1,
        "mass": round(mass, 3),
        "confidence": confidence,
        "spread_log_likelihood": round(lls[peak] - min(lls), 3),
        "width_elo": width,
    }


def bucket_rating(label: str, index: int, n_buckets: int,
                  bucketing: Dict[str, int]) -> float:
    """
    A representative rating for a bucket, for curve fitting.

    Closed bands use their midpoint. The two open bands get the edge plus
    half an interval, which is a placeholder for curve geometry only -- it is
    never reported as an estimate on its own.
    """
    mid = band_midpoint(label)
    if mid is not None:
        return float(mid)
    half = bucketing["interval"] / 2
    return (bucketing["start"] - half if index == 0
            else bucketing["end"] + half)


def refine_rating(
    labels: Sequence[str],
    per_band: Dict[str, float],
    bucketing: Dict[str, int],
) -> Tuple[Optional[int], str, Dict[str, Any]]:
    """
    A point estimate on a 100-point grid, from the shape of the curve.

    Returns (elo, method, detail). `method` is one of:
        "interpolated"  parabolic vertex between buckets -- a real sub-bucket
                        maximum, and the case that genuinely delivers a
                        100-point estimate (1100-2000)
        "floor"         peak is the top bucket: `elo` is a LOWER BOUND, not a
                        point estimate. Render as "2000+".
        "ceiling"       peak is the bottom bucket: `elo` is an UPPER bound
        "degenerate"    too few buckets to fit anything
    """
    n = len(labels)
    if n < 3:
        return None, "degenerate", {}

    lls = [per_band[label] for label in labels]
    ratings = [bucket_rating(label, i, n, bucketing)
               for i, label in enumerate(labels)]
    peak = max(range(n), key=lambda i: lls[i])

    # ---- interior peak: parabolic vertex ----
    if 0 < peak < n - 1:
        y0, y1, y2 = lls[peak - 1], lls[peak], lls[peak + 1]
        denom = y0 - 2 * y1 + y2
        # denom >= 0 means the three points are not concave, so there is no
        # usable vertex; fall back to the peak bucket itself.
        offset = 0.0 if denom == 0 else 0.5 * (y0 - y2) / denom
        offset = max(-1.0, min(1.0, offset))        # stay within a bucket
        step = bucketing["interval"]
        elo = ratings[peak] + offset * step
        return (_to_grid(elo), "interpolated",
                {"bucket": labels[peak], "offset_buckets": round(offset, 3)})

    # ---- peak at the top ----
    #
    # This is where a finer number was wanted, and it is not available.
    # Extrapolating from the curve's final slope was tried and MEASURED
    # against 186 players of known rating (the tool has since been removed
    # with the rest of the rating calibration; the result is in
    # decisions.md 3a2.2):
    #
    #     R^2 = 0.0007,  mean abs error 273 Elo,  fitted coefficient negative
    #
    # The slope carries essentially no information about rating above the
    # top bucket, which is unsurprising -- the model was never trained to
    # distinguish those players, so nothing in its output separates them.
    # Reporting a projected 2300 on that basis would be exactly the invented
    # mapping plan.md A.5 rules out.
    #
    # So the honest answer is a floor: ">=2000" as the number 2000, flagged
    # so a UI can render it as "2000+" rather than a point estimate.
    if peak == n - 1:
        slope = lls[-1] - lls[-2]
        return (bucketing["end"], "floor",
                {"bucket": labels[peak], "final_slope": round(slope, 4),
                 "note": ("the model has no bucket above this; the slope was "
                          "tested as a predictor and explained nothing "
                          "(R^2=0.0007, n=186)")})

    # ---- peak at the bottom: a ceiling, for the same reason ----
    slope = lls[1] - lls[0]
    return (bucketing["start"], "ceiling",
            {"bucket": labels[peak], "final_slope": round(slope, 4),
             "note": "the model has no bucket below this"})


MIN_REPORTABLE_ELO = 600


def _to_grid(elo: float) -> int:
    return int(round(elo / ELO_GRID) * ELO_GRID)


def band_midpoint(label: str) -> Optional[int]:
    """
    Midpoint of a closed band label like '1500-1599'. None if open-ended.

    The band is inclusive on both ends, so its true centre is x.5; rounded
    up to give the round number people expect (1500-1599 -> 1550).
    """
    m = re.fullmatch(r"(\d+)-(\d+)", label.strip())
    if not m:
        return None
    return (int(m.group(1)) + int(m.group(2)) + 1) // 2


class Maia2Session:
    """
    onnxruntime wrapper around the offline-exported Maia-2 policy.

    Lazy: constructing this does not load the model, so a container without
    the weights still starts and simply reports the estimate unavailable.
    """

    # Must match tools/export_maia2_onnx.py ENCODING_ID. Describes the real
    # 18-plane board_to_tensor layout documented in encode_board().
    ENCODING = "maia2-board-tensor-18ch"

    def __init__(self, onnx_path: str) -> None:
        self.onnx_path = onnx_path
        self.meta_path = os.path.join(os.path.dirname(onnx_path) or ".", "maia2_meta.json")
        self._session: Any = None
        self._meta: Optional[Dict[str, Any]] = None
        self._move_index: Dict[str, int] = {}

    # -- loading ---------------------------------------------------------
    def load(self) -> None:
        if self._session is not None:
            return
        if not os.path.exists(self.onnx_path):
            raise FileNotFoundError(
                f"Maia-2 ONNX not found at {self.onnx_path}. It is fetched at "
                "Docker build time from MAIA2_ONNX_URL; see models/README.md."
            )
        if not os.path.exists(self.meta_path):
            raise FileNotFoundError(
                f"Maia-2 metadata not found at {self.meta_path}. It is written by "
                "tools/export_maia2_onnx.py and must ship with the .onnx."
            )
        with open(self.meta_path) as fh:
            meta = json.load(fh)

        if meta.get("encoding") != self.ENCODING:
            raise ValueError(
                f"Encoding mismatch: model says {meta.get('encoding')!r}, runtime "
                f"implements {self.ENCODING!r}. Re-export, or update encode_board() "
                "against maia2/utils.py board_to_tensor."
            )
        for required in ("move_vocab", "elo_dict", "elo_bucketing", "input_names"):
            if not meta.get(required):
                raise ValueError(f"Metadata is missing {required!r}")

        planes = meta.get("input_channels")
        if planes is not None and planes != 18:
            raise ValueError(
                f"Model expects {planes} planes but encode_board() produces 18"
            )

        import onnxruntime as ort  # lazy: keeps import cost off the startup path

        self._session = ort.InferenceSession(
            self.onnx_path,
            providers=["CPUExecutionProvider"],
            sess_options=_single_threaded_options(ort),
        )
        self._meta = meta
        self._move_index = {uci: i for i, uci in enumerate(meta["move_vocab"])}
        log.info(
            "Maia-2 loaded: type=%s, %d moves, %d elo buckets",
            meta.get("model_type"), len(self._move_index), len(meta["elo_dict"]),
        )

    def unload(self) -> None:
        """
        Drop the onnxruntime session and metadata.

        Exists for E.3: it lets a profiler or a memory-pressure handler
        release ~93 MB of weights plus the arena without restarting the
        process. load() rebuilds on next use.
        """
        self._session = None
        self._meta = None
        self._move_index = {}

    @property
    def loaded(self) -> bool:
        return self._session is not None

    # -- metadata accessors ----------------------------------------------
    @property
    def meta(self) -> Dict[str, Any]:
        self.load()
        assert self._meta is not None
        return self._meta

    @property
    def model_type(self) -> Optional[str]:
        return self.meta.get("model_type")

    @property
    def bands(self) -> List[str]:
        """Bucket labels in index order, e.g. ['<1100', '1100-1199', ...]."""
        d = self.meta["elo_dict"]
        return [label for label, _ in sorted(d.items(), key=lambda kv: kv[1])]

    def elo_bucket_index(self, elo: int) -> int:
        return elo_to_bucket(elo, self.meta["elo_bucketing"], len(self.meta["elo_dict"]))

    # -- move/vocabulary mapping -----------------------------------------
    def move_column(self, uci: str) -> Optional[int]:
        """Vocabulary column for an already-mirrored UCI move."""
        return self._move_index.get(uci)

    def move_column_for(self, board: chess.Board, move: chess.Move) -> Optional[int]:
        """
        Vocabulary column for a move on a real board, handling mirroring.

        The vocabulary holds White-perspective moves only (its promotions are
        all rank7->rank8), matching the mirror-iff-Black convention.
        """
        uci = move.uci() if board.turn == chess.WHITE else mirror_uci(move.uci())
        return self._move_index.get(uci)

    def legal_columns(self, board: chess.Board) -> List[int]:
        """
        Vocabulary columns of every legal move, in the model's frame.

        Mirrors maia2.inference.preprocessing, which enumerates the legal
        moves of the already-mirrored board.
        """
        pov = board if board.turn == chess.WHITE else board.mirror()
        cols = []
        for mv in pov.legal_moves:
            col = self._move_index.get(mv.uci())
            if col is not None:
                cols.append(col)
        return cols

    # -- inference -------------------------------------------------------
    def log_probs(
        self, boards: Sequence[chess.Board], elo_self: int, elo_oppo: int
    ) -> "Any":
        """
        Batched log-probabilities over the move vocabulary, with illegal
        moves masked OUT of the softmax.

        Returns an (n_boards, vocab) array whose legal entries sum to 1 per
        row; illegal entries are -inf.

        Masking matters: normalising across all 1880 moves when only ~30 are
        legal spreads probability mass onto moves that were never available,
        so the likelihood of the move actually played is understated by a
        factor that varies with position -- which biases the argmax over
        buckets. Masking first makes every row a distribution over the real
        choice set. This is also what maia2 0.9.1 does
        (inference._masked_softmax); 0.9 multiplied logits by the mask and
        softmaxed over the full vocabulary, which is the behaviour this
        deliberately does not reproduce.

        One ONNX run per call, not per move -- the difference between
        seconds and minutes.
        """
        import numpy as np

        self.load()
        assert self._session is not None and self._meta is not None

        x = np.stack([encode_board(b) for b in boards]).astype(np.float32)
        n = len(boards)
        si = np.full((n,), self.elo_bucket_index(elo_self), dtype=np.int64)
        oi = np.full((n,), self.elo_bucket_index(elo_oppo), dtype=np.int64)

        names = self._meta["input_names"]
        outputs = self._session.run(None, {names[0]: x, names[1]: si, names[2]: oi})
        logits = outputs[int(self._meta.get("policy_output_index", 0))].reshape(n, -1)

        # Mask illegal moves to -inf before the softmax.
        masked = np.full_like(logits, -np.inf)
        for row, board in enumerate(boards):
            cols = self.legal_columns(board)
            if not cols:
                # Terminal position: no legal moves, so no distribution
                # exists. Callers never pass these (a move was played from
                # every position they use), but fail loudly rather than
                # returning an all -inf row that silently becomes NaN.
                raise ValueError(
                    f"Position has no legal moves, cannot score: {board.fen()}"
                )
            masked[row, cols] = logits[row, cols]

        # Stable log-softmax over the legal entries only.
        shifted = masked - np.nanmax(np.where(np.isfinite(masked), masked, -np.inf),
                                     axis=1, keepdims=True)
        with np.errstate(invalid="ignore"):
            exp = np.where(np.isfinite(shifted), np.exp(shifted), 0.0)
        return shifted - np.log(exp.sum(axis=1, keepdims=True))


def _single_threaded_options(ort: Any) -> Any:
    """
    One thread. E.3: onnxruntime defaults to a thread per core, and each
    thread brings its own arena -- the fastest way to blow a 512 MB budget.
    """
    so = ort.SessionOptions()
    so.intra_op_num_threads = 1
    so.inter_op_num_threads = 1
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    return so


def encode_board(board: chess.Board) -> "Any":
    """
    Board -> (18, 8, 8) float planes.

    A faithful port of maia2.utils.board_to_tensor composed with
    maia2.inference.preprocessing. partA cannot import torch (E.3), so this
    is reimplemented -- and therefore able to drift from the real source.
    tools/verify_maia2_onnx.py asserts equality against the real function on
    a fixed batch; run it after upgrading maia2.

    Orientation: the board is mirrored with chess.Board.mirror() iff Black is
    to move. mirror() flips ranks AND swaps colours, so planes 0-5 are always
    the side to move, and plane 12 is consequently always 1.0 at inference.
    Played and legal moves are mirrored to match (see mirror_uci).

    Planes, matching board_to_tensor exactly:
       0-5    white pawn, knight, bishop, rook, queen, king
       6-11   black pawn, knight, bishop, rook, queen, king
       12     side to move is white (whole plane)
       13     castling: white kingside        (whole plane)
       14     castling: white queenside       (whole plane)
       15     castling: black kingside        (whole plane)
       16     castling: black queenside       (whole plane)
       17     en-passant target square (single cell)

    Indexing is [plane, rank, file], from divmod(square, 8).
    """
    import numpy as np

    pov = board if board.turn == chess.WHITE else board.mirror()

    planes = np.zeros((18, 8, 8), dtype=np.float32)

    # 0-11: piece planes, white first. board_to_tensor orders piece types
    # P,N,B,R,Q,K, which is chess.PAWN..chess.KING == 1..6.
    for square, piece in pov.piece_map().items():
        plane = (piece.piece_type - 1) + (0 if piece.color == chess.WHITE else 6)
        rank, file = divmod(square, 8)
        planes[plane, rank, file] = 1.0

    # 12: side to move. Always 1.0 after the mirror, but encoded from the
    # board rather than hardcoded, so the function is correct for any input.
    if pov.turn == chess.WHITE:
        planes[12, :, :] = 1.0

    # 13-16: castling rights, white kingside/queenside then black.
    for offset, has_right in enumerate((
        pov.has_kingside_castling_rights(chess.WHITE),
        pov.has_queenside_castling_rights(chess.WHITE),
        pov.has_kingside_castling_rights(chess.BLACK),
        pov.has_queenside_castling_rights(chess.BLACK),
    )):
        if has_right:
            planes[13 + offset, :, :] = 1.0

    # 17: en-passant target square.
    if pov.ep_square is not None:
        rank, file = divmod(pov.ep_square, 8)
        planes[17, rank, file] = 1.0

    return planes


def mirror_uci(uci: str) -> str:
    """
    Flip a UCI move vertically, matching maia2.utils.mirror_move.

    Squares are mirrored and any promotion suffix is preserved.
    """
    def flip(sq: str) -> str:
        return sq[0] + str(9 - int(sq[1]))

    out = flip(uci[0:2]) + flip(uci[2:4])
    return out + uci[4:] if len(uci) > 4 else out


def estimate_rating(
    session: Maia2Session,
    boards: Sequence[chess.Board],
    moves: Sequence[chess.Move],
    *,
    opponent_elo: Optional[int] = None,
    mode: OpponentEloMode = OpponentEloMode.HEADER,
    fixed_opponent_elo: int = 1500,
) -> RatingEstimate:
    """
    Maximum likelihood over the model's Elo buckets, for one player.

    `boards` are the positions where it was this player's turn, `moves` the
    moves they actually played. Searches every bucket the model has,
    including the open-ended ones.
    """
    if not boards:
        return RatingEstimate(False, reason="No positions for this player")

    try:
        session.load()
    except Exception as exc:
        return RatingEstimate(False, reason=f"{type(exc).__name__}: {exc}")

    # Keep only positions whose played move is in the vocabulary and which
    # have legal moves to normalise over. A vocabulary miss would mean a
    # mismatched export, so count and report them rather than ignoring them.
    kept_boards: List[chess.Board] = []
    kept_cols: List[int] = []
    skipped = 0
    for board, move in zip(boards, moves):
        col = session.move_column_for(board, move)
        if col is None or board.is_game_over():
            skipped += 1
            continue
        kept_boards.append(board)
        kept_cols.append(col)

    if not kept_boards:
        return RatingEstimate(
            False, moves_skipped=skipped,
            reason="No played move matched the model's move vocabulary",
        )

    import numpy as np

    col_idx = np.array(kept_cols)
    row_idx = np.arange(len(kept_cols))
    bands = session.bands

    per_band: Dict[str, float] = {}
    oppo_used: Optional[int] = None
    for index, label in enumerate(bands):
        # The self-Elo is conveyed by bucket index, so feed a representative
        # raw Elo that lands in this bucket. For the open-ended buckets the
        # bucketing maps anything below/above to the right index anyway.
        self_elo = band_midpoint(label)
        if self_elo is None:
            b = session.meta["elo_bucketing"]
            self_elo = b["start"] - 50 if index == 0 else b["end"] + 50

        if mode == OpponentEloMode.HEADER and opponent_elo is not None:
            oppo = opponent_elo
        elif mode == OpponentEloMode.FIXED:
            oppo = fixed_opponent_elo
        else:
            oppo = self_elo          # TIED, and the HEADER fallback
        oppo_used = oppo

        lp = session.log_probs(kept_boards, elo_self=self_elo, elo_oppo=oppo)
        per_band[label] = float(lp[row_idx, col_idx].sum())

    best_label = max(per_band, key=per_band.__getitem__)
    elo, method, detail = refine_rating(
        bands, per_band, session.meta["elo_bucketing"])
    return RatingEstimate(
        available=True,
        band=best_label,
        elo=elo,
        elo_method=method,
        elo_detail=detail,
        interval=likelihood_interval(
            bands, per_band, session.meta["elo_bucketing"]),
        bucket_index=bands.index(best_label),
        moves_used=len(kept_cols),
        moves_skipped=skipped,
        log_likelihood=round(per_band[best_label], 3),
        per_band={k: round(v, 3) for k, v in per_band.items()},
        opponent_elo_used=oppo_used,
        opponent_elo_mode=mode.value,
        model_type=session.model_type,
    )


def _header_rating(headers: Dict[str, str], key: str) -> Optional[int]:
    raw = headers.get(key, "")
    m = re.match(r"^(\d{3,4})", raw.strip())
    return int(m.group(1)) if m else None


# ==========================================================================
# A.6  Phase + opening insights
# ==========================================================================
class Phase(str, Enum):
    OPENING = "opening"
    MIDDLEGAME = "middlegame"
    ENDGAME = "endgame"


# DECISION (§8.6): both rules, each governing the boundary it describes well.
#
#   opening -> middlegame : the ply the player left book, capped at ply 24
#                           (move 12). Book exit is the meaningful signal;
#                           the cap stops a deeply-theoretical game being
#                           "all opening".
#   middlegame -> endgame : non-pawn material (Q=9 R=5 B=N=3, both sides)
#                           drops to <= 13, or <= 6 non-king pieces remain.
#                           Material, not move number -- a queenless game on
#                           move 15 is an endgame.
#
# The endgame transition is monotonic: once in, never back out (promotions
# would otherwise flip the phase back and forth).
OPENING_PLY_CAP = 24
ENDGAME_NONPAWN_MATERIAL = 13
ENDGAME_PIECE_COUNT = 6
_PIECE_VALUES = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3,
                 chess.ROOK: 5, chess.QUEEN: 9}


def non_pawn_material(board: chess.Board) -> int:
    return sum(
        _PIECE_VALUES[p.piece_type]
        for p in board.piece_map().values()
        if p.piece_type not in (chess.PAWN, chess.KING)
    )


def _phase_sequence(boards: Sequence[chess.Board], left_book_ply: Optional[int]) -> List[Phase]:
    """Phase for each position, applying the rule above."""
    opening_until = min(left_book_ply if left_book_ply is not None else OPENING_PLY_CAP,
                        OPENING_PLY_CAP)
    phases: List[Phase] = []
    in_endgame = False
    for ply, board in enumerate(boards):
        pieces = len(board.piece_map()) - 2  # minus the two kings
        if not in_endgame and (
            non_pawn_material(board) <= ENDGAME_NONPAWN_MATERIAL
            or pieces <= ENDGAME_PIECE_COUNT
        ):
            in_endgame = True
        if in_endgame:
            phases.append(Phase.ENDGAME)
        elif ply < opening_until:
            phases.append(Phase.OPENING)
        else:
            phases.append(Phase.MIDDLEGAME)
    return phases


@dataclass
class OpeningInfo:
    eco: Optional[str] = None
    name: Optional[str] = None
    left_book_ply: Optional[int] = None
    left_book_move: Optional[str] = None       # SAN of the first out-of-book move
    left_book_move_number: Optional[int] = None
    source: str = "unavailable"


# Process-wide opening-book cache, keyed by data directory.
#
# E.3: OpeningBook.load() parses five TSVs into ~8000 positions. Rebuilding
# it per analysis burned CPU and churned the heap on every job; the table is
# read-only once built, so one instance per directory is shared by all jobs.
_OPENING_BOOKS: Dict[str, "OpeningBook"] = {}
_OPENING_BOOK_LOCK = threading.Lock()


def get_opening_book(data_dir: str = "data/openings") -> "OpeningBook":
    """
    The shared, already-loaded book for `data_dir`.

    Locked, not just cached: two jobs starting together would otherwise both
    parse the TSVs and briefly hold two copies of the table.
    """
    book = _OPENING_BOOKS.get(data_dir)
    if book is not None:
        return book
    with _OPENING_BOOK_LOCK:
        book = _OPENING_BOOKS.get(data_dir)
        if book is None:
            book = OpeningBook(data_dir)
            book.load()
            _OPENING_BOOKS[data_dir] = book
    return book


class OpeningBook:
    """
    ECO lookup from lichess-org/chess-openings (CC0).

    Keyed on EPD, not move text, so transpositions resolve to the same
    opening -- which is the whole point of keying on position.

    Two tiers, because they answer different questions:

      _named    EPD -> (eco, name) for the position each TSV row *names*.
                Source of the opening label.
      _in_book  every EPD any line passes through, including prefixes.
                Source of the book-exit ply.

    Indexing only named positions would report "left book" at 1.e4, since
    no row is named for that position alone.

    Expects a.tsv .. e.tsv in `data_dir`; falls back to a small built-in
    table so the module never hard-depends on a downloaded file. The report
    says which source was used.
    """

    _FALLBACK = [
        ("C50", "Italian Game", "e4 e5 Nf3 Nc6 Bc4"),
        ("C60", "Ruy Lopez", "e4 e5 Nf3 Nc6 Bb5"),
        ("B20", "Sicilian Defense", "e4 c5"),
        ("C00", "French Defense", "e4 e6"),
        ("B10", "Caro-Kann Defense", "e4 c6"),
        ("D02", "Queen's Pawn Game", "d4 d5 Nf3"),
        ("D06", "Queen's Gambit", "d4 d5 c4"),
        ("E60", "King's Indian Defense", "d4 Nf6 c4 g6"),
        ("A04", "Zukertort Opening", "Nf3"),
        ("A10", "English Opening", "c4"),
    ]

    def __init__(self, data_dir: str = "data/openings") -> None:
        self.data_dir = data_dir
        self._named: Dict[str, Tuple[str, str]] = {}
        self._in_book: set[str] = set()
        self.source = "unavailable"
        self._loaded = False

    def load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        rows = list(self._read_tsvs())
        if rows:
            self.source = "lichess-chess-openings"
        else:
            rows = [(eco, name, pgn) for eco, name, pgn in self._FALLBACK]
            self.source = "builtin-fallback"
            log.warning(
                "No opening TSVs in %s -- falling back to %d built-in lines, so "
                "ECO names and book-exit plies will be poor. The TSVs are a "
                "committed asset; if this fires in production the image is "
                "missing data/openings/. Refresh with OpeningBook.download().",
                self.data_dir, len(rows),
            )
        for eco, name, pgn in rows:
            epds = self._epds_along(pgn)
            if not epds:
                continue
            self._in_book.update(epds)
            # Shorter lines win a contested name: the first row to reach a
            # position describes it more generally than a deep transposition.
            self._named.setdefault(epds[-1], (eco, name))
        log.info(
            "Opening book: %d named / %d book positions from %s",
            len(self._named), len(self._in_book), self.source,
        )

    def _read_tsvs(self) -> Iterable[Tuple[str, str, str]]:
        """Yield (eco, name, pgn) from a.tsv .. e.tsv. Missing files are fine."""
        if not os.path.isdir(self.data_dir):
            return
        for letter in "abcde":
            path = os.path.join(self.data_dir, f"{letter}.tsv")
            if not os.path.exists(path):
                continue
            with open(path, newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh, delimiter="\t"):
                    eco, name, pgn = row.get("eco"), row.get("name"), row.get("pgn")
                    if eco and name and pgn:
                        yield eco, name, pgn

    @staticmethod
    def _epds_along(pgn_moves: str) -> List[str]:
        """Every position a line passes through, in order. [] if unplayable."""
        board = chess.Board()
        out: List[str] = []
        try:
            for token in pgn_moves.split():
                # Skip move numbers: "1." and "1..." both appear in the TSVs.
                if re.match(r"^\d+\.*$", token):
                    continue
                board.push_san(token)
                out.append(board.epd())
        except ValueError:
            return []
        return out

    def lookup(self, board: chess.Board) -> Optional[Tuple[str, str]]:
        """(eco, name) if this exact position is a named opening."""
        self.load()
        return self._named.get(board.epd())

    def contains(self, board: chess.Board) -> bool:
        """
        Is this position still inside known opening theory?

        `_in_book` holds every position any catalogued line passes through,
        prefixes included, which is exactly the question a bot needs: not
        "is this a named opening" but "are we still in theory".
        """
        return board.epd() in self._in_book

    def identify(self, boards: Sequence[chess.Board], nodes: Sequence[Any]) -> OpeningInfo:
        """
        Walk the game keeping the longest matching prefix. The first ply
        whose position is not in the book is where the player left theory.
        """
        self.load()
        info = OpeningInfo(source=self.source)
        for ply in range(1, len(boards)):
            epd = boards[ply].epd()
            named = self._named.get(epd)
            if named is not None:
                # Keep the deepest named position reached: that is the most
                # specific correct label for what was actually played.
                info.eco, info.name = named
            if epd in self._in_book:
                continue
            info.left_book_ply = ply
            info.left_book_move = boards[ply - 1].san(nodes[ply - 1].move)
            info.left_book_move_number = (ply + 1) // 2
            break
        return info

    def download(self) -> None:
        """
        Fetch the CC0 TSVs. Run once at build time, not per request --
        pulling them on a cold start would add seconds to the first visitor's
        wait (plan.md F: cold-start behaviour).
        """
        import httpx

        os.makedirs(self.data_dir, exist_ok=True)
        base = ("https://raw.githubusercontent.com/lichess-org/chess-openings/master/")
        for letter in "abcde":
            r = httpx.get(f"{base}{letter}.tsv", timeout=30.0, follow_redirects=True)
            r.raise_for_status()
            with open(os.path.join(self.data_dir, f"{letter}.tsv"), "w", encoding="utf-8") as fh:
                fh.write(r.text)
        self._loaded = False
        self._named.clear()
        self._in_book.clear()
        log.info("Downloaded opening TSVs to %s", self.data_dir)


@dataclass
class PhaseStats:
    phase: str
    moves: int
    acpl: Optional[float]
    accuracy: Optional[float]
    blunders: int
    mistakes: int
    inaccuracies: int


def _weakest_phase(stats: Sequence[PhaseStats]) -> Optional[str]:
    """
    The phase verdict. Ranked by ACPL, ignoring phases with too few moves
    to mean anything -- three endgame moves is not evidence.
    """
    candidates = [s for s in stats if s.moves >= 4 and s.acpl is not None]
    if len(candidates) < 2:
        return None
    worst = max(candidates, key=lambda s: s.acpl or 0.0)
    return worst.phase


# ==========================================================================
# Report shapes
#
# Two different shapes, deliberately (§8.2): a game produces a timeline plus
# per-player summaries; a FEN produces one position's engine read. The
# frontend branches on `kind`.
# ==========================================================================
@dataclass
class MoveRecord:
    ply: int
    move_number: int
    color: str                       # "white" | "black"
    san: str
    uci: str
    fen_before: str
    # The resulting position. Carried so a client can show the final
    # position of the game and step a board forward without a rules
    # engine of its own.
    fen_after: str
    phase: str

    # A.3
    cp_before: int                   # White POV, position before the move
    cp_after: int                    # White POV, position after the move
    win_before: float                # mover's POV
    win_after: float
    win_drop: float
    cp_loss: int                     # mover's POV, capped
    accuracy: float
    label: str
    # B.1 fact layer: "mate_now_forced" / "forced_mate_lost" / None. Recorded
    # separately from `label` because it is a different kind of statement --
    # the grounding a comment needs ("checkmate is now unavoidable") rather
    # than a severity.
    mate_status_change: Optional[str]
    played_best: bool
    best_move: Optional[str]
    best_pv: List[str]

    # The opponent's best reply, from the engine's read of the position
    # AFTER this move. On a faulted move this is the refutation -- the
    # concrete reason the move was bad, and the single most useful thing a
    # comment can lead with. It was already being computed for `cp_after`
    # and then discarded.
    #
    # Named for the use rather than the generality: on a good move it is
    # just the normal continuation, and Part B withholds it there so that no
    # comment can present a routine reply as a punishment.
    refutation_move: Optional[str]
    refutation_pv: List[str]

    # A.4
    complexity: float
    complexity_band: str
    only_move: bool
    # Legal moves in the position this was played from. Needed by A.5b to
    # decide whether a move was a DECISION at all -- with one legal move the
    # player could not have gone wrong, so it carries no information about
    # their strength and must not count in the denominator.
    legal_moves: int
    multipv_spread_cp: int
    time_spent: Optional[float]
    clock_after: Optional[float]
    time_ratio: Optional[float]
    time_flag: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PlayerSummary:
    color: str
    name: Optional[str]
    rating_header: Optional[int]
    moves: int
    accuracy: Optional[float]
    acpl: Optional[float]
    blunders: int
    mistakes: int
    inaccuracies: int
    best_moves: int
    phases: List[PhaseStats]
    weakest_phase: Optional[str]
    rating_estimate: Dict[str, Any]
    # A.5b. Blunders, centipawn loss and engine agreement measured PER
    # DECISION rather than per move, so the numbers are comparable across
    # games of different length and sharpness.
    #
    # This is a MEASUREMENT, not an estimate, which is why it survived the
    # cut that removed `performance` and `profile_rating`: "4.2 blunders per
    # 100 real decisions, ACPL 62, engine-best agreement 34%" is a fact
    # about the game. An Elo derived from it was not.
    error_profile: Dict[str, Any]
    time: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["phases"] = [asdict(p) for p in self.phases]
        return d


@dataclass
class GameReport:
    kind: str
    source_type: str
    source_ref: Optional[str]
    headers: Dict[str, str]
    has_clock: bool
    time_control: Dict[str, Any]
    opening: Dict[str, Any]
    moves: List[MoveRecord]
    white: PlayerSummary
    black: PlayerSummary
    meta: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "source_type": self.source_type,
            "source_ref": self.source_ref,
            "headers": self.headers,
            "has_clock": self.has_clock,
            "time_control": self.time_control,
            "opening": self.opening,
            "moves": [m.to_dict() for m in self.moves],
            "white": self.white.to_dict(),
            "black": self.black.to_dict(),
            "meta": self.meta,
        }


@dataclass
class PositionReport:
    """
    FEN mode (§8.2). No clock data, no history, no rating signal, so this
    carries engine lines, a complexity read and the phase -- and says
    explicitly which analyses do not apply, so the UI can hide them rather
    than render empty panels.
    """

    kind: str
    fen: str
    turn: str
    legal_moves: int
    terminal: bool
    outcome: Optional[str]
    eval_cp_white: int
    win_percent_white: float
    lines: List[Dict[str, Any]]
    complexity: Dict[str, Any]
    phase: str
    opening: Dict[str, Any]
    unavailable: List[str]
    meta: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ==========================================================================
# Orchestration
# ==========================================================================
def analyse(
    game_input: GameInput,
    *,
    stockfish_path: str = "stockfish",
    depth: int = 14,
    multipv: int = 3,
    hash_mb: int = 16,
    threads: int = 1,
    maia2_path: Optional[str] = "models/maia2.onnx",
    maia2_session: Optional["Maia2Session"] = None,
    openings_dir: str = "data/openings",
    opponent_elo_mode: OpponentEloMode = OpponentEloMode.HEADER,
    progress: Optional[ProgressFn] = None,
) -> Any:
    """
    Single entry point used by partE. Dispatches on the input shape and
    owns the engine's lifetime so the process is always cleaned up.

    `maia2_session` injects an already-loaded session. Pass it in any
    long-lived process: the weights are 93 MB, so building one per call
    reloads them from disk every time and, under concurrency, holds several
    copies at once (E.3). partE owns a singleton for exactly this reason.
    `maia2_path` is the standalone fallback used by scripts and tests.
    """
    with EngineService(
        stockfish_path, depth=depth, multipv=multipv, hash_mb=hash_mb, threads=threads
    ) as engine:
        if game_input.is_position_only:
            return analyse_position(
                game_input, engine, openings_dir=openings_dir, progress=progress
            )
        return analyse_game(
            game_input,
            engine,
            maia2_path=maia2_path,
            maia2_session=maia2_session,
            openings_dir=openings_dir,
            opponent_elo_mode=opponent_elo_mode,
            progress=progress,
        )


def analyse_game(
    game_input: GameInput,
    engine: EngineService,
    *,
    maia2_path: Optional[str] = "models/maia2.onnx",
    maia2_session: Optional["Maia2Session"] = None,
    openings_dir: str = "data/openings",
    opponent_elo_mode: OpponentEloMode = OpponentEloMode.HEADER,
    progress: Optional[ProgressFn] = None,
) -> GameReport:
    started = time.perf_counter()
    game = game_input.game()
    nodes = list(game.mainline())
    if not nodes:
        raise InputError("PGN contains no moves")

    def report_progress(stage: str, frac: float, partial: Optional[Dict] = None) -> None:
        if progress is not None:
            progress(stage, frac, partial)

    # Positions 0..N: boards[i] is the position before move i+1.
    boards: List[chess.Board] = [game.board()]
    for node in nodes:
        nxt = boards[-1].copy(stack=False)
        nxt.push(node.move)
        boards.append(nxt)

    # ---- A.6 opening + phase -------------------------------------------
    book = get_opening_book(openings_dir)
    opening = book.identify(boards, nodes)
    phases = _phase_sequence(boards, opening.left_book_ply)
    report_progress("opening", 0.05, {"opening": asdict(opening)})

    # ---- A.2 evaluate every position once ------------------------------
    #
    # N+1 positions, not 2N: the position after move i is the position
    # before move i+1, so one sequential pass gives both sides of every
    # classification. This is the single biggest cost saving in Part A.
    evals: List[PositionEval] = []
    for i, board in enumerate(boards):
        evals.append(engine.analyse(board))
        if i % 5 == 0 or i == len(boards) - 1:
            # 0.05..0.85 of the bar is the engine pass; A.5 gets the rest.
            report_progress("analysing", 0.05 + 0.80 * (i + 1) / len(boards))

    # ---- A.4 clocks ----------------------------------------------------
    tc = parse_time_control(game_input.headers.get("TimeControl"))
    time_enabled = game_input.has_clock and tc.known
    if game_input.has_clock and not tc.known:
        log.info("Clocks present but TimeControl unparseable -- time analysis disabled")
    clocks: List[Optional[float]] = [node.clock() for node in nodes]
    spent = _time_spent_per_move(clocks, tc) if time_enabled else [None] * len(nodes)

    # ---- A.3 classification per move -----------------------------------
    records: List[MoveRecord] = []
    for i, node in enumerate(nodes):
        before, after = evals[i], evals[i + 1]
        mover = boards[i].turn

        cp_before_w, cp_after_w = before.cp_white(), after.cp_white()
        wb = win_percent(cp_before_w if mover == chess.WHITE else -cp_before_w)
        wa = win_percent(cp_after_w if mover == chess.WHITE else -cp_after_w)
        drop = max(0.0, wb - wa)

        best = before.best
        played_best = bool(best and best.move == node.move.uci())
        cx = complexity_of(before)
        tv = _time_verdict(spent[i], clocks[i], tc, cx)

        # Mate-status transition. The non-mate side's evaluation is what sets
        # the severity (see classify_mate_transition), so capture whichever
        # of the two positions is not a mate score.
        mate_before, mate_after = before.is_mate(), after.is_mate()
        mate_change: Optional[str] = None
        nonmate_cp: Optional[int] = None
        if mate_before != mate_after:
            if mate_after:
                # Walked into a forced mate -- but only report it as such if
                # the mate is against the mover; finding a mate FOR yourself
                # is not a transition worth flagging.
                if (cp_after_w < 0) == (mover == chess.WHITE):
                    mate_change = MATE_NOW_FORCED
                    nonmate_cp = cp_before_w
            else:
                # Had a forced mate and no longer does.
                if (cp_before_w > 0) == (mover == chess.WHITE):
                    mate_change = FORCED_MATE_LOST
                    nonmate_cp = cp_after_w

        # ACPL uses capped evals: the difference between +2000 and +2600 is
        # not 600 centipawns of mistake.
        cap_b = max(-CP_CAP, min(CP_CAP, cp_before_w if mover == chess.WHITE else -cp_before_w))
        cap_a = max(-CP_CAP, min(CP_CAP, cp_after_w if mover == chess.WHITE else -cp_after_w))

        records.append(
            MoveRecord(
                ply=i + 1,
                move_number=i // 2 + 1,
                color="white" if mover == chess.WHITE else "black",
                san=boards[i].san(node.move),
                uci=node.move.uci(),
                fen_before=boards[i].fen(),
                fen_after=boards[i + 1].fen(),
                phase=phases[i].value,
                cp_before=cp_before_w,
                cp_after=cp_after_w,
                win_before=round(wb, 2),
                win_after=round(wa, 2),
                win_drop=round(drop, 2),
                cp_loss=max(0, cap_b - cap_a),
                accuracy=round(accuracy_percent(wb, wa), 1),
                label=classify(
                    drop,
                    played_best=played_best,
                    mate_status_change=mate_change,
                    nonmate_cp=nonmate_cp,
                ).value,
                mate_status_change=mate_change,
                played_best=played_best,
                refutation_move=after.best.move_san if after.best else None,
                refutation_pv=list(after.best.pv[:6]) if after.best else [],
                best_move=best.move_san if best else None,
                best_pv=best.pv if best else [],
                legal_moves=before.legal_moves,
                complexity=cx.score,
                complexity_band=cx.band(),
                only_move=cx.only_move,
                multipv_spread_cp=cx.spread_cp,
                time_spent=tv.spent_seconds,
                clock_after=tv.clock_after,
                time_ratio=tv.ratio,
                time_flag=tv.flag,
            )
        )
    report_progress("classified", 0.88, {"moves": [r.to_dict() for r in records]})

    # ---- A.5 rating estimate -------------------------------------------
    white_rating = _header_rating(game_input.headers, "WhiteElo")
    black_rating = _header_rating(game_input.headers, "BlackElo")
    if maia2_session is not None or maia2_path:
        report_progress("rating", 0.90)
        rating_w, rating_b = _estimate_both(
            maia2_session or Maia2Session(maia2_path),
            boards, nodes, white_rating, black_rating, opponent_elo_mode,
        )
    else:
        rating_w = rating_b = RatingEstimate(False, reason="Rating estimate not requested")

    # ---- assemble ------------------------------------------------------
    white = _player_summary(
        chess.WHITE, records, game_input.headers, white_rating, rating_w, tc,
        time_enabled, opening.left_book_ply
    )
    black = _player_summary(
        chess.BLACK, records, game_input.headers, black_rating, rating_b, tc,
        time_enabled, opening.left_book_ply
    )
    report_progress("done", 1.0)

    return GameReport(
        kind="game",
        source_type=game_input.source_type,
        source_ref=game_input.source_ref,
        headers=game_input.headers,
        has_clock=game_input.has_clock,
        time_control={
            "raw": game_input.headers.get("TimeControl"),
            "base_seconds": tc.base_seconds,
            "increment_seconds": tc.increment_seconds,
            "expected_per_move": tc.expected_per_move(),
            "time_analysis_enabled": time_enabled,
        },
        opening=asdict(opening),
        moves=records,
        white=white,
        black=black,
        meta={
            "plies": len(records),
            "depth": engine.depth,
            "multipv": engine.multipv,
            "engine_cache": engine.stats(),
            "elapsed_seconds": round(time.perf_counter() - started, 2),
            "accuracy_method": "mean of per-move Lichess accuracy",
            "phase_rule": (
                f"opening until book exit (cap ply {OPENING_PLY_CAP}); "
                f"endgame at non-pawn material <= {ENDGAME_NONPAWN_MATERIAL} "
                f"or <= {ENDGAME_PIECE_COUNT} non-king pieces, monotonic"
            ),
        },
    )


def _estimate_both(
    session: Maia2Session,
    boards: Sequence[chess.Board],
    nodes: Sequence[Any],
    white_rating: Optional[int],
    black_rating: Optional[int],
    mode: OpponentEloMode,
) -> Tuple[RatingEstimate, RatingEstimate]:
    """A.5 for both players, sharing one loaded ONNX session."""
    try:
        session.load()
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        log.info("Rating estimate unavailable: %s", reason)
        return RatingEstimate(False, reason=reason), RatingEstimate(False, reason=reason)

    out: List[RatingEstimate] = []
    for color, oppo_rating in (
        (chess.WHITE, black_rating),
        (chess.BLACK, white_rating),
    ):
        sel_boards = [boards[i] for i, n in enumerate(nodes) if boards[i].turn == color]
        sel_moves = [n.move for i, n in enumerate(nodes) if boards[i].turn == color]
        effective_mode = (
            mode
            if not (mode == OpponentEloMode.HEADER and oppo_rating is None)
            else OpponentEloMode.TIED      # documented fallback, §8.1
        )
        out.append(
            estimate_rating(
                session,
                sel_boards,
                sel_moves,
                opponent_elo=oppo_rating,
                mode=effective_mode,
            )
        )
    return out[0], out[1]


def _time_spent_per_move(clocks: Sequence[Optional[float]], tc: TimeControl) -> List[Optional[float]]:
    """
    Seconds spent per move from %clk deltas.

    %clk is the clock *after* the move, so a move's cost is the previous
    reading for that same player, minus this one, plus the increment that
    was just added. Each side's first move measures against the base time.
    """
    spent: List[Optional[float]] = []
    prev = {chess.WHITE: float(tc.base_seconds or 0), chess.BLACK: float(tc.base_seconds or 0)}
    for i, clk in enumerate(clocks):
        color = chess.WHITE if i % 2 == 0 else chess.BLACK
        if clk is None:
            spent.append(None)
            continue
        delta = prev[color] - clk + tc.increment_seconds
        # Negative deltas mean added time, a berserk, or a mid-game clock
        # adjustment -- unusable rather than zero.
        spent.append(delta if delta >= 0 else None)
        prev[color] = clk
    return spent


def _player_summary(
    color: chess.Color,
    records: Sequence[MoveRecord],
    headers: Dict[str, str],
    rating_header: Optional[int],
    rating_estimate: RatingEstimate,
    tc: TimeControl,
    time_enabled: bool,
    left_book_ply: Optional[int] = None,
) -> PlayerSummary:
    tag = "white" if color == chess.WHITE else "black"
    mine = [r for r in records if r.color == tag]

    def counts(rs: Sequence[MoveRecord]) -> Tuple[int, int, int, int]:
        return (
            sum(1 for r in rs if r.label == MoveLabel.BLUNDER.value),
            sum(1 for r in rs if r.label == MoveLabel.MISTAKE.value),
            sum(1 for r in rs if r.label == MoveLabel.INACCURACY.value),
            sum(1 for r in rs if r.played_best),
        )

    blunders, mistakes, inaccuracies, bests = counts(mine)

    phase_stats: List[PhaseStats] = []
    for phase in (Phase.OPENING, Phase.MIDDLEGAME, Phase.ENDGAME):
        rs = [r for r in mine if r.phase == phase.value]
        b, m, ina, _ = counts(rs)
        phase_stats.append(
            PhaseStats(
                phase=phase.value,
                moves=len(rs),
                acpl=round(sum(r.cp_loss for r in rs) / len(rs), 1) if rs else None,
                accuracy=game_accuracy([r.accuracy for r in rs]),
                blunders=b,
                mistakes=m,
                inaccuracies=ina,
            )
        )

    # A.5b. `in_book` is per ply, not per player: the book exit is a
    # property of the game, and both sides are in it until then.
    profile = rating_profile.build_profile([
        rating_profile.MoveSignal(
            cp_loss=r.cp_loss,
            win_drop=r.win_drop,
            label=r.label,
            complexity=r.complexity,
            only_move=r.only_move,
            played_best=r.played_best,
            legal_moves=r.legal_moves,
            cp_before=r.cp_before,
            phase=r.phase,
            in_book=left_book_ply is not None and r.ply <= left_book_ply,
        )
        for r in mine
    ])

    return PlayerSummary(
        color=tag,
        name=headers.get("White" if color == chess.WHITE else "Black"),
        rating_header=rating_header,
        moves=len(mine),
        accuracy=game_accuracy([r.accuracy for r in mine]),
        acpl=round(sum(r.cp_loss for r in mine) / len(mine), 1) if mine else None,
        blunders=blunders,
        mistakes=mistakes,
        inaccuracies=inaccuracies,
        best_moves=bests,
        phases=phase_stats,
        weakest_phase=_weakest_phase(phase_stats),
        rating_estimate=asdict(rating_estimate),
        error_profile=profile.to_dict(),
        time=_time_summary(mine, tc) if time_enabled else {"enabled": False},
    )


def _time_summary(records: Sequence[MoveRecord], tc: TimeControl) -> Dict[str, Any]:
    """A.4 output: total seconds wasted, plus the top moments each way."""
    expected = tc.expected_per_move() or 0.0
    wasted = [r for r in records if r.time_flag == "wasted"]
    rushed = [r for r in records if r.time_flag == "rushed"]
    timed = [r for r in records if r.time_spent is not None]

    def moment(r: MoveRecord) -> Dict[str, Any]:
        return {
            "ply": r.ply,
            "move_number": r.move_number,
            "san": r.san,
            "phase": r.phase,
            "seconds": r.time_spent,
            "ratio": r.time_ratio,
            "complexity": r.complexity,
            "complexity_band": r.complexity_band,
            "label": r.label,
            "only_move": r.only_move,
        }

    return {
        "enabled": True,
        "expected_per_move": round(expected, 1),
        "total_seconds_used": round(sum(r.time_spent or 0 for r in timed), 1),
        "median_seconds": _median([r.time_spent or 0 for r in timed]),
        # Seconds above what the position warranted, on the moves flagged
        # wasted. This is the "you spent 94 s thinking in simple positions"
        # number, not total time spent.
        "seconds_wasted": round(sum((r.time_spent or 0) - expected for r in wasted), 1),
        "wasted_count": len(wasted),
        "rushed_count": len(rushed),
        # The two conjunctions that are actually teachable, kept separate
        # from the raw flags above. `win_drop` is what makes each one worth
        # reporting: rushing a hard position and finding the move anyway is
        # not a mistake, and a long think that cost nothing is not waste. It
        # is the rush (or the dawdle) PLUS the consequence that is a lesson.
        "critical_rush": [
            moment(r) for r in sorted(
                (r for r in records
                 if r.complexity_band == "high"
                 and r.time_ratio is not None and r.time_ratio <= CRITICAL_RUSH_RATIO
                 and r.win_drop >= MISTAKE_DROP),
                key=lambda r: -r.win_drop)[:5]
        ],
        "wasted_think": [
            moment(r) for r in sorted(
                (r for r in records
                 if r.complexity_band == "low"
                 and r.time_ratio is not None and r.time_ratio >= WASTED_THINK_RATIO
                 and r.win_drop < INACCURACY_DROP),
                key=lambda r: -(r.time_spent or 0))[:5]
        ],
        "top_wasted": [moment(r) for r in sorted(wasted, key=lambda r: -(r.time_spent or 0))[:5]],
        "top_rushed": [moment(r) for r in sorted(rushed, key=lambda r: -r.complexity)[:5]],
    }


def _median(xs: Sequence[float]) -> Optional[float]:
    if not xs:
        return None
    s = sorted(xs)
    mid = len(s) // 2
    return round(s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2, 1)


def analyse_position(
    game_input: GameInput,
    engine: EngineService,
    *,
    openings_dir: str = "data/openings",
    progress: Optional[ProgressFn] = None,
) -> PositionReport:
    """FEN mode. See the DECISION note on load_fen (§8.2)."""
    board = chess.Board(game_input.fen or chess.STARTING_FEN)
    if progress:
        progress("analysing", 0.3, None)

    pe = engine.analyse(board, multipv=max(engine.multipv, 3))
    cx = complexity_of(pe)
    phases = _phase_sequence([board], None)

    book = get_opening_book(openings_dir)
    hit = book.lookup(board)

    if progress:
        progress("done", 1.0, None)

    return PositionReport(
        kind="position",
        fen=board.fen(),
        turn="white" if board.turn == chess.WHITE else "black",
        legal_moves=pe.legal_moves,
        terminal=pe.terminal,
        outcome=pe.outcome,
        eval_cp_white=pe.cp_white(),
        win_percent_white=round(win_percent(pe.cp_white()), 2),
        lines=[
            {
                "rank": ln.rank,
                "move": ln.move_san,
                "uci": ln.move,
                "cp_white": ln.cp_white,
                "mate_in": ln.mate_in,
                "win_percent_mover": round(win_percent(ln.cp_for(board.turn)), 2),
                "pv": ln.pv,
            }
            for ln in pe.lines
        ],
        complexity=asdict(cx) | {"band": cx.band()},
        phase=phases[0].value,
        opening={"eco": hit[0], "name": hit[1]} if hit else {"eco": None, "name": None},
        unavailable=[
            "time_analysis: a FEN has no clock data",
            "accuracy: no move was played from this position",
            "rating_estimate: one position carries no rating signal",
            "phase_aggregates: no game history",
        ],
        meta={"depth": pe.depth, "engine_cache": engine.stats()},
    )
