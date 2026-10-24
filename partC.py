"""
Part C -- Accounts and puzzles.

C.1  Auth      -- Supabase Auth, not ours. plan.md: "Password hashing,
                  sessions, reset flows, and email verification are solved
                  problems and a reliable way to lose two days." The token
                  verification already lives in partE.SupabaseClient; this
                  module adds nothing to it on purpose.
C.2  Schema    -- migrations/001_init.sql, with Row Level Security so a user
                  can only ever read or write their own rows.
C.3  Puzzles   -- the Lichess CC0 puzzle database, subset to fit the free
                  tier, served near the user's rating, validated against the
                  solution line, scored with an Elo-style update.

Everything here is deterministic and offline: no engine, no LLM, no network.
Rating maths is arithmetic, per plan.md C.3 ("not ML").
"""

from __future__ import annotations

import csv
import io
import logging
import random
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

import chess

log = logging.getLogger("chessapp.puzzles")


# ==========================================================================
# C.3  The puzzle model
#
# The Lichess CSV layout, which is easy to get wrong:
#
#   PuzzleId,FEN,Moves,Rating,RatingDeviation,Popularity,NbPlays,Themes,
#   GameUrl,OpeningTags,DailyDate
#
# `FEN` is the position BEFORE the opponent's move, and `Moves[0]` is that
# opponent move. The position shown to the player is FEN with Moves[0]
# already applied. Moves[1] begins the solution, and from there the player's
# moves are the odd indices and the opponent's replies the even ones.
#
# Treating Moves[0] as the player's first move -- the obvious reading -- makes
# every puzzle unsolvable and the bug looks like bad data rather than bad
# parsing, so it is worth the comment.
# ==========================================================================
PUZZLE_COLUMNS = (
    "PuzzleId", "FEN", "Moves", "Rating", "RatingDeviation",
    "Popularity", "NbPlays", "Themes", "GameUrl", "OpeningTags",
)


class PuzzleError(ValueError):
    """A puzzle row could not be parsed, or an attempt was malformed."""


@dataclass
class Puzzle:
    """One puzzle, already advanced past the opponent's setup move."""

    id: str
    fen: str                      # the position the PLAYER sees
    solution: List[str]           # the player's moves only, in UCI
    opponent_replies: List[str]   # interleaved replies, in UCI
    rating: int
    rating_deviation: int
    popularity: int
    nb_plays: int
    themes: List[str]
    game_url: Optional[str] = None
    opening_tags: List[str] = field(default_factory=list)

    @property
    def side_to_move(self) -> str:
        return "white" if chess.Board(self.fen).turn == chess.WHITE else "black"

    @property
    def moves_required(self) -> int:
        return len(self.solution)

    def to_dict(self, *, include_solution: bool = False) -> Dict[str, Any]:
        """
        Client-safe by default.

        The solution is withheld unless explicitly asked for: serving it
        alongside the puzzle would let anyone read the answer out of the
        network tab, which makes the whole feature pointless.
        """
        data = {
            "id": self.id,
            "fen": self.fen,
            "side_to_move": self.side_to_move,
            "rating": self.rating,
            "rating_deviation": self.rating_deviation,
            "popularity": self.popularity,
            "nb_plays": self.nb_plays,
            "themes": self.themes,
            "moves_required": self.moves_required,
            "game_url": self.game_url,
            "opening_tags": self.opening_tags,
        }
        if include_solution:
            data["solution"] = self.solution
            data["opponent_replies"] = self.opponent_replies
        return data

    @classmethod
    def from_subset_row(cls, row: Dict[str, str]) -> "Puzzle":
        """
        Parse a row of a SUBSET csv, as tools/build_puzzle_subset.py writes.

        Different from `from_row`, which reads the raw Lichess dump. The
        subset is already parsed: its FEN is the position after the
        opponent's setup move and the line is split into the player's moves
        and the replies. Re-running the setup logic over it would push a
        move that has already been played.

        Lists are Postgres array literals ("{a,b}") so the same file can be
        loaded with \\copy; that is the only reason for the braces.
        """
        def unpack(text: str) -> List[str]:
            text = (text or "").strip()
            if text.startswith("{") and text.endswith("}"):
                text = text[1:-1]
            return [part.strip().strip('"') for part in text.split(",") if part.strip()]

        try:
            return cls(
                id=row["id"].strip(),
                fen=row["fen"].strip(),
                solution=unpack(row["solution"]),
                opponent_replies=unpack(row.get("opponent_replies", "")),
                rating=int(row["rating"]),
                rating_deviation=int(row.get("rating_deviation") or 0),
                popularity=int(row.get("popularity") or 0),
                nb_plays=int(row.get("nb_plays") or 0),
                themes=unpack(row.get("themes", "")),
                game_url=(row.get("game_url") or "").strip() or None,
                opening_tags=unpack(row.get("opening_tags", "")),
            )
        except (KeyError, ValueError) as exc:
            raise PuzzleError(f"Malformed subset row: {exc}") from exc

    @classmethod
    def from_row(cls, row: Dict[str, str]) -> "Puzzle":
        """Parse one CSV row, applying the opponent's setup move."""
        try:
            fen = row["FEN"].strip()
            raw_moves = row["Moves"].split()
            puzzle_id = row["PuzzleId"].strip()
        except KeyError as exc:
            raise PuzzleError(f"Puzzle row missing column {exc}") from exc
        if len(raw_moves) < 2:
            raise PuzzleError(f"Puzzle {puzzle_id} has fewer than two moves")

        board = chess.Board(fen)
        setup = chess.Move.from_uci(raw_moves[0])
        if setup not in board.legal_moves:
            raise PuzzleError(
                f"Puzzle {puzzle_id}: setup move {raw_moves[0]} is illegal in {fen}"
            )
        board.push(setup)

        rest = raw_moves[1:]
        return cls(
            id=puzzle_id,
            fen=board.fen(),
            solution=rest[0::2],            # the player's moves
            opponent_replies=rest[1::2],    # the replies between them
            rating=_as_int(row.get("Rating"), 1500),
            rating_deviation=_as_int(row.get("RatingDeviation"), 100),
            popularity=_as_int(row.get("Popularity"), 0),
            nb_plays=_as_int(row.get("NbPlays"), 0),
            themes=(row.get("Themes") or "").split(),
            game_url=(row.get("GameUrl") or "").strip() or None,
            opening_tags=(row.get("OpeningTags") or "").split(),
        )


def _as_int(value: Optional[str], default: int) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


# ==========================================================================
# C.3  Solution validation
# ==========================================================================
@dataclass
class MoveCheck:
    """The verdict on one submitted move."""

    index: int
    submitted: str
    expected: str
    correct: bool
    opponent_reply: Optional[str] = None
    fen_after: Optional[str] = None
    accepted_alternative: bool = False


@dataclass
class AttemptResult:
    puzzle_id: str
    solved: bool
    moves_correct: int
    moves_required: int
    checks: List[MoveCheck]
    first_wrong_move: Optional[str] = None
    expected_instead: Optional[str] = None
    reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["checks"] = [asdict(c) for c in self.checks]
        return d


def check_attempt(puzzle: Puzzle, submitted: Sequence[str]) -> AttemptResult:
    """
    Validate the player's moves against the solution line.

    Strict line matching, with one standard exception: when the expected move
    delivers mate, ANY mating move is accepted. Lichess does the same, and
    without it a puzzle with two mates in one marks a correct answer wrong.

    Stops at the first wrong move and reports what was expected there, which
    is what a review UI needs to show.
    """
    board = chess.Board(puzzle.fen)
    checks: List[MoveCheck] = []

    for i, expected_uci in enumerate(puzzle.solution):
        if i >= len(submitted):
            return AttemptResult(
                puzzle_id=puzzle.id, solved=False, moves_correct=i,
                moves_required=puzzle.moves_required, checks=checks,
                expected_instead=board.san(chess.Move.from_uci(expected_uci)),
                reason="Incomplete: the line is not finished",
            )

        expected = chess.Move.from_uci(expected_uci)
        expected_san = board.san(expected)

        raw = submitted[i]
        try:
            played = _parse_move(board, raw)
        except PuzzleError as exc:
            # An illegal or unreadable move is still a wrong answer, and the
            # player should be told what the line wanted either way --
            # reporting the failure without the answer makes the puzzle
            # unlearnable.
            return AttemptResult(
                puzzle_id=puzzle.id, solved=False, moves_correct=i,
                moves_required=puzzle.moves_required, checks=checks,
                first_wrong_move=str(raw), expected_instead=expected_san,
                reason=str(exc),
            )

        correct = played == expected
        alternative = False
        if not correct and _is_mate(board, expected) and _is_mate(board, played):
            # Both mate: a different mate in one is still a solution.
            correct = alternative = True

        if not correct:
            checks.append(MoveCheck(index=i, submitted=played.uci(),
                                    expected=expected.uci(), correct=False))
            return AttemptResult(
                puzzle_id=puzzle.id, solved=False, moves_correct=i,
                moves_required=puzzle.moves_required, checks=checks,
                first_wrong_move=board.san(played),
                expected_instead=expected_san,
                reason="Wrong move",
            )

        # Accepted. Advance through the player's move and the scripted reply.
        board.push(played)
        reply_uci = (puzzle.opponent_replies[i]
                     if i < len(puzzle.opponent_replies) else None)
        if reply_uci:
            reply = chess.Move.from_uci(reply_uci)
            if reply in board.legal_moves:
                board.push(reply)
            else:
                reply_uci = None
        checks.append(MoveCheck(
            index=i, submitted=played.uci(), expected=expected.uci(),
            correct=True, opponent_reply=reply_uci, fen_after=board.fen(),
            accepted_alternative=alternative,
        ))

    return AttemptResult(
        puzzle_id=puzzle.id, solved=True,
        moves_correct=puzzle.moves_required,
        moves_required=puzzle.moves_required, checks=checks,
    )


def _parse_move(board: chess.Board, raw: str) -> chess.Move:
    """Accept UCI or SAN, so the frontend can send either."""
    text = str(raw).strip()
    if not text:
        raise PuzzleError("Empty move")
    try:
        move = chess.Move.from_uci(text)
        if move in board.legal_moves:
            return move
        # A UCI-shaped string that is not legal here.
        if re.fullmatch(r"[a-h][1-8][a-h][1-8][qrbn]?", text):
            raise PuzzleError(f"{text} is not legal in this position")
    except ValueError:
        pass
    try:
        return board.parse_san(text)
    except ValueError as exc:
        raise PuzzleError(f"Could not read move {text!r}") from exc


def _is_mate(board: chess.Board, move: chess.Move) -> bool:
    if move not in board.legal_moves:
        return False
    board.push(move)
    try:
        return board.is_checkmate()
    finally:
        board.pop()


# ==========================================================================
# C.3  Puzzle rating -- Elo-style, arithmetic only
#
# plan.md: "update the user's puzzle rating with a simple Elo-style update
# (arithmetic, not ML)". The puzzle's own rating is treated as fixed: it
# comes from Lichess, derived from far more attempts than this app will ever
# see, so updating it from one user's result would only add noise.
# ==========================================================================
DEFAULT_RATING = 1200          # where a new account starts
MIN_RATING = 400
MAX_RATING = 3000

# K-factor, the amount a single result can move the rating. High while the
# estimate is new and the true rating is unknown, settling once there is
# enough history for a stable number. Standard provisional-rating practice,
# and it is why a new user converges in a handful of puzzles instead of
# fifty.
K_PROVISIONAL = 64
K_SETTLING = 32
K_ESTABLISHED = 16
PROVISIONAL_ATTEMPTS = 10
SETTLING_ATTEMPTS = 40


def k_factor(attempts: int) -> int:
    """How much one result may move the rating, given history length."""
    if attempts < PROVISIONAL_ATTEMPTS:
        return K_PROVISIONAL
    if attempts < SETTLING_ATTEMPTS:
        return K_SETTLING
    return K_ESTABLISHED


def expected_score(player_rating: int, puzzle_rating: int) -> float:
    """Standard Elo expectation that the player solves this puzzle."""
    return 1.0 / (1.0 + 10 ** ((puzzle_rating - player_rating) / 400.0))


@dataclass
class RatingChange:
    before: int
    after: int
    delta: int
    expected: float
    k: int
    attempts_before: int

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def update_rating(
    player_rating: int,
    puzzle_rating: int,
    solved: bool,
    *,
    attempts: int = 0,
) -> RatingChange:
    """
    New rating after one attempt.

    Only the player's rating moves (see the section note). Clamped to
    MIN/MAX so a long unlucky streak cannot drive the rating somewhere that
    no puzzle exists to match.
    """
    expected = expected_score(player_rating, puzzle_rating)
    k = k_factor(attempts)
    raw = player_rating + k * ((1.0 if solved else 0.0) - expected)
    after = int(round(min(max(raw, MIN_RATING), MAX_RATING)))
    return RatingChange(
        before=player_rating, after=after, delta=after - player_rating,
        expected=round(expected, 4), k=k, attempts_before=attempts,
    )


# ==========================================================================
# C.3  Serving a puzzle
# ==========================================================================
# How far from the player's rating a puzzle may be. Widened on retry when
# the band holds nothing, since a subset will always have thin spots.
TARGET_BAND = 100
MAX_BAND = 600
BAND_STEP = 150


def rating_window(player_rating: int, attempt: int = 0) -> Tuple[int, int]:
    """
    The rating range to draw a puzzle from, widening with each retry.

    Slightly above the player's rating on purpose: a puzzle you solve ~60%
    of the time teaches more than one you solve every time, and the Elo
    update means a diet of too-easy puzzles stops moving the rating at all.
    """
    band = min(TARGET_BAND + attempt * BAND_STEP, MAX_BAND)
    centre = player_rating + 25
    return max(centre - band, MIN_RATING), min(centre + band, MAX_RATING)


# ==========================================================================
# C.3  Building the subset -- §8.4
#
# The full database is several million puzzles, which does not fit the free
# tier's 500 MB alongside the analyses table (plan.md C.3: "Do not load the
# full set").
#
# DECISION (§8.4), and the reasoning rather than just the number:
#
#   size      50,000 puzzles, roughly 12 MB. Comfortably inside the budget
#             with room for analyses, and large enough that a user cannot
#             exhaust a rating band.
#   selection stratified by rating band, then by theme within each band.
#             A naive random sample mirrors the database's own distribution,
#             which is heavily weighted to the middle -- so a 2200-rated
#             user runs out of puzzles while a 1200 one sees the same themes
#             forever. Even coverage matters more than matching the source
#             distribution.
#   quality   `popularity >= 70` and `nb_plays >= 50` only. Lichess's
#             popularity score is a community up/down vote; low scores are
#             usually puzzles with a dual solution or a dubious premise, and
#             one of those is far more damaging than a missing puzzle.
#
# All three are CLI flags on tools/build_puzzle_subset.py, so this is a
# starting point rather than a commitment.
# ==========================================================================
SUBSET_SIZE = 50_000
RATING_BANDS: Tuple[Tuple[int, int], ...] = (
    (0, 1000), (1000, 1200), (1200, 1400), (1400, 1600), (1600, 1800),
    (1800, 2000), (2000, 2200), (2200, 2500), (2500, 4000),
)
MIN_POPULARITY = 70
MIN_PLAYS = 50


def band_for(rating: int) -> Tuple[int, int]:
    for low, high in RATING_BANDS:
        if low <= rating < high:
            return low, high
    return RATING_BANDS[-1]


def iter_puzzle_rows(path: str) -> Iterator[Dict[str, str]]:
    """
    Stream rows from the Lichess dump, plain `.csv` or `.csv.zst`.

    Streamed, never loaded: the decompressed file is several GB and this has
    to run on a laptop.
    """
    if path.endswith(".zst"):
        try:
            import zstandard  # noqa: PLC0415 -- only needed for the dump
        except ImportError as exc:
            raise PuzzleError(
                "Reading .csv.zst needs the `zstandard` package (in requirements.txt)"
            ) from exc
        with open(path, "rb") as raw:
            reader = zstandard.ZstdDecompressor().stream_reader(raw)
            text = io.TextIOWrapper(reader, encoding="utf-8", newline="")
            yield from csv.DictReader(text)
    else:
        with open(path, newline="", encoding="utf-8") as fh:
            yield from csv.DictReader(fh)


def select_subset(
    rows: Iterable[Dict[str, str]],
    *,
    size: int = SUBSET_SIZE,
    min_popularity: int = MIN_POPULARITY,
    min_plays: int = MIN_PLAYS,
    seed: int = 20261007,
) -> List[Puzzle]:
    """
    Stratified subset: even across rating bands, then even across themes.

    Reservoir sampling per (band, theme) bucket, so this needs one pass and
    memory proportional to the subset rather than to the source. A puzzle's
    first theme is used as its bucket key -- Lichess lists themes roughly
    most-specific first, and bucketing on all of them would multiply the
    buckets without improving coverage.

    Deterministic for a given seed, so a rebuild produces the same subset
    and a reseed is a deliberate act.
    """
    rng = random.Random(seed)
    per_band = max(size // len(RATING_BANDS), 1)
    buckets: Dict[Tuple[Tuple[int, int], str], List[Puzzle]] = {}
    seen = kept = skipped_quality = skipped_bad = 0

    for row in rows:
        seen += 1
        if _as_int(row.get("Popularity"), 0) < min_popularity:
            skipped_quality += 1
            continue
        if _as_int(row.get("NbPlays"), 0) < min_plays:
            skipped_quality += 1
            continue
        try:
            puzzle = Puzzle.from_row(row)
        except (PuzzleError, ValueError):
            skipped_bad += 1
            continue

        key = (band_for(puzzle.rating), puzzle.themes[0] if puzzle.themes else "untagged")
        bucket = buckets.setdefault(key, [])
        # Reservoir sampling: every qualifying puzzle has an equal chance of
        # being in the final bucket, without holding the whole band in memory.
        cap = max(per_band // 8, 4)
        if len(bucket) < cap:
            bucket.append(puzzle)
            kept += 1
        else:
            j = rng.randint(0, seen)
            if j < cap:
                bucket[j] = puzzle

    # Draw evenly from the buckets, round-robin, until the target is met.
    ordered = [buckets[k] for k in sorted(buckets)]
    for bucket in ordered:
        rng.shuffle(bucket)
    out: List[Puzzle] = []
    index = 0
    while len(out) < size and any(len(b) > index for b in ordered):
        for bucket in ordered:
            if index < len(bucket):
                out.append(bucket[index])
                if len(out) >= size:
                    break
        index += 1

    log.info(
        "Puzzle subset: %d of %d rows (%d below quality floor, %d unparseable), "
        "%d buckets",
        len(out), seen, skipped_quality, skipped_bad, len(buckets),
    )
    return out


def subset_report(puzzles: Sequence[Puzzle]) -> Dict[str, Any]:
    """Coverage summary, so a built subset can be sanity-checked."""
    from collections import Counter

    by_band: Counter = Counter()
    by_theme: Counter = Counter()
    for p in puzzles:
        by_band[f"{band_for(p.rating)[0]}-{band_for(p.rating)[1]}"] += 1
        for theme in p.themes:
            by_theme[theme] += 1
    ratings = sorted(p.rating for p in puzzles)
    return {
        "count": len(puzzles),
        "rating_min": ratings[0] if ratings else None,
        "rating_max": ratings[-1] if ratings else None,
        "rating_median": ratings[len(ratings) // 2] if ratings else None,
        "by_band": dict(sorted(by_band.items())),
        "distinct_themes": len(by_theme),
        "top_themes": dict(by_theme.most_common(12)),
        "approx_bytes": sum(
            len(p.id) + len(p.fen) + sum(len(m) for m in p.solution)
            + sum(len(m) for m in p.opponent_replies)
            + sum(len(t) for t in p.themes) + 40
            for p in puzzles
        ),
    }
