"""
Part B -- Comments / agent system.
plan.md: "This is the differentiating feature. Everything else on this list
exists on other sites." The reason chess.com's comments feel cheap is that
they are templates with no grounding in what actually happened. The fix is
grounding, in three layers:

  B.1  Fact extraction   -- deterministic, no LLM. Material, hanging pieces,
                            tactical motifs, king safety, from python-chess
                            plus Part A's engine output.
  B.2  Game-level pass   -- one LLM call per game: narrative arc, critical
                            moments, each player's recurring weakness.
  B.3  Move-level pass   -- batched: move facts + game narrative -> comment.
  B.4  Cost and latency  -- selection, batching, caching, streaming.

B.1 is the real work and it carries no API dependency, so the facts are
useful on their own: they are returned even when commentary is unavailable,
and the engine is never re-run to produce them (everything comes from Part
A's report plus the position).

Target quality (plan.md B.3):

    "This hangs the knight on f6 -- after Nxf6 you lose a piece for nothing,
     and it's the third time this game a piece was left undefended on an
     open file."
Not: "Good move!"
"""
from __future__ import annotations
import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple
import chess

import batch_budget
import claim_guard
log = logging.getLogger("chessapp.commentary")
ProgressFn = Callable[[str, float, Optional[Dict[str, Any]]], None]
# Standard relative values, used for "is this capture profitable" reasoning
# rather than for evaluation -- Part A's Stockfish owns evaluation.
PIECE_VALUE: Dict[int, int] = {
    chess.PAWN: 1,
    chess.KNIGHT: 3,
    chess.BISHOP: 3,
    chess.ROOK: 5,
    chess.QUEEN: 9,
    chess.KING: 0,          # never captured; excluded from material counts
}


PIECE_NAME: Dict[int, str] = {
    chess.PAWN: "pawn",
    chess.KNIGHT: "knight",
    chess.BISHOP: "bishop",
    chess.ROOK: "rook",
    chess.QUEEN: "queen",
    chess.KING: "king",
}
# Sliding directions, for the ray walks that relative pins, skewers and
# discovered attacks all need.
_ROOK_DIRS = ((1, 0), (-1, 0), (0, 1), (0, -1))
_BISHOP_DIRS = ((1, 1), (1, -1), (-1, 1), (-1, -1))
# For pin/skewer geometry the king must outrank everything: the motif is
# defined by which end of the ray is worth more, and PIECE_VALUE puts the
# king at 0 (it is never captured, so it must not count toward material).
# Using PIECE_VALUE here reports every skewer-through-a-king as a pin.
_KING_RANK_VALUE = 100


def _pin_value(piece_type: int) -> int:
    return _KING_RANK_VALUE if piece_type == chess.KING else PIECE_VALUE[piece_type]


# ==========================================================================
# B.1  Layer 1 -- Fact extraction (deterministic, no LLM)
#
# Every function here is pure and testable. Motif detection is approximate
# by nature; each one documents what it does and does not catch, because a
# comment grounded in a *wrong* fact is worse than one with fewer facts.


# ==========================================================================
def material(board: chess.Board, color: chess.Color) -> int:
    """Total material for `color`, kings excluded."""
    return sum(
        PIECE_VALUE[p.piece_type]
        for p in board.piece_map().values()
        if p.color == color and p.piece_type != chess.KING
    )


def _square_name(sq: int) -> str:
    return chess.square_name(sq)


def _colour(piece: chess.Piece) -> str:
    """
    "white" / "black" for a piece.

    Every fact that names a piece carries this. Without it the commentary
    layer has to infer ownership and gets it wrong often enough to matter --
    it produced "your c4 pawn" for a white pawn while addressing Black, and
    "the white bishop on e6" for a black knight.
    """
    return "white" if piece.color == chess.WHITE else "black"


def _describe(piece: chess.Piece, square: int) -> str:
    """
    "white bishop on c4" -- colour, piece and square in one unambiguous noun.

    Motifs name their participants by ROLE using these strings rather than by
    geometry (`by` / `near` / `far`). Geometry labels left the direction of a
    motif to be inferred, and it was inferred wrongly about a third of the
    time: a bishop that pinned an enemy pawn was described as being itself
    pinned, and a discovered attack by Black was reported as helping White.
    A role name plus `benefits` removes the guess.
    """
    return f"{_colour(piece)} {PIECE_NAME[piece.piece_type]} on {_square_name(square)}"


def _side_name(color: chess.Color) -> str:
    return "white" if color == chess.WHITE else "black"


def _cheapest_attacker_value(board: chess.Board, by: chess.Color, sq: int) -> Optional[int]:
    attackers = board.attackers(by, sq)
    if not attackers:
        return None
    return min(PIECE_VALUE[board.piece_at(a).piece_type] for a in attackers)


def hanging_squares(board: chess.Board, color: chess.Color) -> Set[int]:
    """
    Squares holding a piece of `color` the opponent can profitably take.
    "Profitable" means undefended, or defended but attacked by something
    cheaper -- a rook defended only by a king but attacked by a pawn is
    still losing material.

    Approximation, deliberately: this is an attacker/defender count, not a
    full static exchange evaluation. It does not resolve long capture
    sequences, and it ignores whether a defender is pinned. Part A's engine
    eval is the authority on whether material is actually lost; these facts
    exist to tell the commentary *which piece* and *on what square*.
    """
    out: Set[int] = set()
    for sq, piece in board.piece_map().items():
        if piece.color != color or piece.piece_type == chess.KING:
            continue
        cheapest = _cheapest_attacker_value(board, not color, sq)
        if cheapest is None:
            continue
        defenders = board.attackers(color, sq)
        if not defenders or cheapest < PIECE_VALUE[piece.piece_type]:
            out.add(sq)
    return out


def king_zone(board: chess.Board, color: chess.Color) -> Set[int]:
    """The king's square plus its adjacent squares."""
    ksq = board.king(color)
    if ksq is None:
        return set()
    zone = {ksq}
    f, r = chess.square_file(ksq), chess.square_rank(ksq)
    for df in (-1, 0, 1):
        for dr in (-1, 0, 1):
            nf, nr = f + df, r + dr
            if 0 <= nf < 8 and 0 <= nr < 8:
                zone.add(chess.square(nf, nr))
    return zone


def king_pressure(board: chess.Board, color: chess.Color) -> int:
    """
    How many enemy pieces bear on `color`'s king zone.

    Counts distinct attacking pieces, not attacked squares, so a queen
    covering four squares of the zone counts once. A crude but monotone
    proxy for king safety; its value is in the *change* across a move.
    """
    zone = king_zone(board, color)
    attackers: Set[int] = set()
    for sq in zone:
        attackers |= set(board.attackers(not color, sq))
    return len(attackers)


def find_forks(board: chess.Board, move: chess.Move) -> List[Dict[str, Any]]:
    """
    Did the piece that just moved attack two or more worthwhile targets?

    A target counts when it is the enemy king, a piece worth more than the
    forking piece, or an undefended piece. That excludes the common false
    positive of a queen "forking" two defended pawns.
    """
    piece = board.piece_at(move.to_square)
    if piece is None:
        return []
    mover = piece.color
    my_value = PIECE_VALUE[piece.piece_type]
    targets = []
    for sq in board.attacks(move.to_square):
        victim = board.piece_at(sq)
        if victim is None or victim.color == mover:
            continue
        if victim.piece_type == chess.KING:
            targets.append(_describe(victim, sq))
            continue
        undefended = not board.attackers(victim.color, sq)
        if PIECE_VALUE[victim.piece_type] > my_value or undefended:
            label = _describe(victim, sq)
            targets.append(label + (" (undefended)" if undefended else ""))
    if len(targets) < 2:
        return []
    return [{
        "motif": "fork",
        "forker": _describe(piece, move.to_square),
        "targets": targets,
        "benefits": _side_name(mover),
    }]


def _ray(frm: int, df: int, dr: int) -> Iterable[int]:
    """Squares from `frm` outward along (df, dr), exclusive of `frm`."""
    f, r = chess.square_file(frm) + df, chess.square_rank(frm) + dr
    while 0 <= f < 8 and 0 <= r < 8:
        yield chess.square(f, r)
        f += df
        r += dr


def _slider_dirs(piece_type: int) -> Tuple[Tuple[int, int], ...]:
    if piece_type == chess.ROOK:
        return _ROOK_DIRS
    if piece_type == chess.BISHOP:
        return _BISHOP_DIRS
    if piece_type == chess.QUEEN:
        return _ROOK_DIRS + _BISHOP_DIRS
    return ()


# The labels that mean the move lost something. Shared so the fact layer and
# the selection logic cannot drift apart on what counts as a mistake.
FAULTED_LABELS = frozenset({"blunder", "mistake", "inaccuracy"})

# How many plies of a line reach the prompt. The material phrase MUST be
# computed over exactly these moves: computing it over the full PV while
# showing four plies described a capture the model could not see, and it
# duly attributed the rook to the wrong side. One constant, used by both.
SHOWN_PV_PLIES = 4

# A motif has to be worth mentioning, not merely present. The piece behind
# must be worth at least a minor piece: lining something up against a pawn is
# geometry, not a tactic, and the comment layer treated the two identically.
MOTIF_MIN_BEHIND = PIECE_VALUE[chess.KNIGHT]
# And the two ends must differ by enough to be worth winning. Pinning a
# knight against a bishop gains nothing if it is ever resolved.
MOTIF_MIN_GAP = 2


def _motif_is_material(far: chess.Piece, near: chess.Piece) -> bool:
    """
    Is there enough material at stake for this to be a motif at all?

    An absolute pin always qualifies: the king cannot be traded off, so the
    pin binds whatever the pinned piece is worth. Otherwise the PRIZE -- the
    piece behind, which the motif exists to win -- must be worth taking, and
    the two ends must differ enough that resolving it gains something.

    The pawn rule is the one that matters, and it is what the original bad
    comment hinged on: a piece lined up in front of a PAWN wins a pawn, and
    "skewers the knight on g6, winning the pawn on h7" was the result.
    Measured over 60 games with tools/audit_motifs.py, it removes 86% of
    skewers.

    It deliberately does NOT test the near end. An earlier version did, on
    the theory that a pinned pawn was never going anywhere, and it removed
    270 of 346 relative pins -- but "your bishop on d5 pins the b7 pawn to
    the a8 rook" is a true and entirely standard annotation, and there is no
    labelled data here saying those are unwanted. Filtering them would be a
    guess dressed as a rule. Pin volume is recorded as an open question in
    decisions.md rather than silently decided.
    """
    if far.piece_type == chess.KING:
        return True             # absolute pin: always binding
    # `_pin_value` on both ends, never PIECE_VALUE: PIECE_VALUE[KING] is 0,
    # so a rook skewering a KING to win a rook would score the king as
    # worthless and suppress the most valid skewer on the board.
    far_v = _pin_value(far.piece_type)
    near_v = _pin_value(near.piece_type)
    if far_v < MOTIF_MIN_BEHIND:
        return False
    return abs(far_v - near_v) >= MOTIF_MIN_GAP


def _skewer_threat_is_credible(board: chess.Board, attacker: chess.Piece,
                               target_sq: int, target: chess.Piece,
                               by: chess.Color) -> bool:
    """
    Would the attack on the front piece actually force it to move?

    This is a SKEWER-only test, and the distinction matters. A skewer works
    by threatening the front piece so that it moves and exposes the one
    behind; if taking the front piece just loses material, nothing is forced
    and there is no skewer. That is the case that produced the original bad
    comment: a queen "attacking" a knight defended by a pawn is the queen's
    problem, not the knight's.

    A PIN is not subject to this. Its value is the bind -- the front piece
    cannot move without losing what is behind it -- and that holds whether
    or not capturing the front piece is profitable. Applying this test to
    pins would have rejected a rook pinning a knight against a queen, which
    is a textbook relative pin.

    Same attacker/defender approximation as `hanging_squares`, with the same
    caveat: not a full static exchange evaluation.
    """
    if target.piece_type == chess.KING:
        return True             # a king must always move when attacked
    if PIECE_VALUE[attacker.piece_type] <= PIECE_VALUE[target.piece_type]:
        return True             # an even or favourable capture is a real threat
    # Our attacker is the more valuable piece, so this threatens the front
    # piece only if nothing is defending it.
    return not board.attackers(not by, target_sq)


def find_pins_and_skewers(board: chess.Board, by: chess.Color) -> List[Dict[str, Any]]:
    """
    Sliders of `by` that line up two enemy pieces on one ray.

    Walks each slider's rays for the first two occupied squares. If both
    hold enemy pieces, the pair is a pin when the far piece is worth more
    (or is the king) and a skewer when the near piece is worth more.

    Covers relative pins, which `chess.Board.is_pinned` does not -- that
    reports absolute (king) pins only. Reported separately below via
    `absolute_pins`, since those are the ones that are legally binding.
    """
    found: List[Dict[str, Any]] = []
    for sq, piece in board.piece_map().items():
        if piece.color != by:
            continue
        for df, dr in _slider_dirs(piece.piece_type):
            first = second = None
            for target in _ray(sq, df, dr):
                occupant = board.piece_at(target)
                if occupant is None:
                    continue
                if first is None:
                    first = (target, occupant)
                    continue
                second = (target, occupant)
                break
            if first is None or second is None:
                continue
            (near_sq, near), (far_sq, far) = first, second
            if near.color == by or far.color == by:
                continue
            near_v = _pin_value(near.piece_type)
            far_v = _pin_value(far.piece_type)
            if far_v > near_v:
                motif = "pin"
            elif near_v > far_v:
                motif = "skewer"
            else:
                continue        # equal value on both ends: neither

            # Geometry alone is not a tactic. Without these two tests the
            # detector reported a knight "skewered" against a PAWN defended
            # by its own king -- true as geometry, worthless as chess, and
            # taking the front piece would simply have lost the queen.
            if not _motif_is_material(far, near):
                continue
            if motif == "skewer" and not _skewer_threat_is_credible(
                    board, piece, near_sq, near, by):
                continue
            entry: Dict[str, Any] = {
                "motif": motif,
                # `benefits` is the side the motif helps -- always the side
                # whose piece is doing the attacking, never the side pinned.
                "benefits": _side_name(by),
            }
            if motif == "pin":
                entry["pinner"] = _describe(piece, sq)
                entry["pinned"] = _describe(near, near_sq)
                entry["pinned_against"] = _describe(far, far_sq)
                # A pin against the king is legally binding; a relative pin
                # only costs material. Worth distinguishing in a comment.
                entry["absolute"] = far.piece_type == chess.KING
            else:
                entry["attacker"] = _describe(piece, sq)
                entry["front_piece"] = _describe(near, near_sq)
                entry["piece_won_behind"] = _describe(far, far_sq)
            found.append(entry)
    return found


def own_pinned_pieces(board: chess.Board, color: chess.Color) -> List[Dict[str, Any]]:
    """
    Pieces of `color` that are absolutely pinned, via python-chess.

    A distinct fact from `find_pins_and_skewers`, which reports pins the
    mover *created*. This reports the mover's own pieces that cannot legally
    move -- a liability rather than an achievement -- so the two never
    describe the same thing twice.
    """
    out = []
    for sq, piece in board.piece_map().items():
        if piece.color != color or piece.piece_type == chess.KING:
            continue
        if board.is_pinned(color, sq):
            out.append({
                "motif": "own_piece_pinned",
                "pinned": _describe(piece, sq),
                "benefits": _side_name(not color),
            })
    return out


def find_discovered_attacks(
    before: chess.Board, after: chess.Board, move: chess.Move
) -> List[Dict[str, Any]]:
    """
    Did vacating the from-square open a friendly slider's line?

    Detected by comparing, for each friendly slider, which enemy pieces it
    attacked before and after. A newly attacked piece whose ray passes
    through the vacated square is a discovery. The ray check is what keeps
    this from firing on attacks the moved piece created itself.
    """
    piece = after.piece_at(move.to_square)
    if piece is None:
        return []
    mover = piece.color
    found: List[Dict[str, Any]] = []
    for sq, slider in after.piece_map().items():
        if slider.color != mover or sq == move.to_square:
            continue
        dirs = _slider_dirs(slider.piece_type)
        if not dirs:
            continue
        gained = set(after.attacks(sq)) - set(before.attacks(sq)) \
            if before.piece_at(sq) is not None else set()
        for target in gained:
            victim = after.piece_at(target)
            if victim is None or victim.color == mover:
                continue
            if not _passes_through(sq, target, move.from_square):
                continue
            found.append({
                "motif": "discovered_attack",
                "attacker": _describe(slider, sq),
                "victim": _describe(victim, target),
                "opened_by_vacating": _square_name(move.from_square),
                "benefits": _side_name(mover),
            })
    return found


def _passes_through(frm: int, to: int, through: int) -> bool:
    """Is `through` strictly between `frm` and `to` on a shared line?"""
    if frm == to or through in (frm, to):
        return False
    try:
        between = chess.SquareSet.between(frm, to)
    except (ValueError, AttributeError):
        return False
    return through in between


def back_rank_weakness(board: chess.Board, color: chess.Color) -> Optional[Dict[str, Any]]:
    """
    Is `color`'s king stuck on its back rank behind its own pawns, with an
    enemy rook or queen able to reach that rank?

    Requires all three: the king on its own back rank, every escape square
    on the next rank occupied by a friendly pawn, and a heavy enemy piece
    with a line to the rank. Without the third condition this fires on
    almost every castled position and the fact becomes noise.
    """
    ksq = board.king(color)
    if ksq is None:
        return None
    back_rank = 0 if color == chess.WHITE else 7
    if chess.square_rank(ksq) != back_rank:
        return None
    escape_rank = 1 if color == chess.WHITE else 6
    kf = chess.square_file(ksq)
    escapes = [chess.square(f, escape_rank) for f in (kf - 1, kf, kf + 1) if 0 <= f < 8]
    for sq in escapes:
        occupant = board.piece_at(sq)
        if not (occupant and occupant.color == color and occupant.piece_type == chess.PAWN):
            return None     # the king has a way out
    heavy = [
        sq for sq, p in board.piece_map().items()
        if p.color != color and p.piece_type in (chess.ROOK, chess.QUEEN)
        and any(chess.square_rank(t) == back_rank for t in board.attacks(sq))
    ]
    if not heavy:
        return None
    return {
        "motif": "back_rank_weakness",
        "vulnerable_king": f"{_side_name(color)} king on {_square_name(ksq)}",
        "threatened_by": [_describe(board.piece_at(sq), sq) for sq in heavy],
        "benefits": _side_name(not color),
    }


@dataclass
class MoveFacts:
    """
    Everything concrete that is true about one move.

    This is the entire input the LLM gets about a move, plus Part A's
    classification. No prose, no interpretation -- the whole point is that
    the comment is grounded in facts that were computed, not recalled.
    """
    ply: int
    move_number: int
    color: str
    san: str
    uci: str
    fen_before: str
    phase: str
    # From Part A (A.2/A.3/A.4/A.6)
    label: str
    cp_before: int
    cp_after: int
    win_drop: float
    accuracy: float
    best_move: Optional[str]
    best_pv: List[str]
    # What the opponent does about it. Set only on a faulted move, where it
    # IS the refutation -- on a good move the same field would be an
    # ordinary reply, and a comment would dress it up as a punishment.
    # This is the concrete reason a bad move was bad, and it was being
    # computed by Part A and discarded.
    refutation_move: Optional[str]
    refutation_pv: List[str]
    # Whose move the refutation is. Every other fact names its colour and the
    # prompt tells the model to check it; shipping these two without one
    # produced "Black's refutation Nxf7" on a move where Nxf7 was White's.
    # The refutation is always the OPPONENT's reply, never the mover's.
    refutation_by: Optional[str]
    # What each line does to the material count, computed by playing it out.
    # Material outcome is NOT derivable from a list of SAN moves without
    # playing it, which a language model cannot do reliably -- asked to
    # explain it anyway, one produced "would have won the exchange" for a
    # line in which no rook was ever captured. These are finished phrases:
    # the prompt requires them verbatim and forbids any other material claim.
    best_line: Dict[str, Any]
    refutation_line: Dict[str, Any]
    played_best: bool
    only_move: bool
    multipv_spread_cp: int
    complexity_band: str
    mate_status_change: Optional[str]
    time_spent: Optional[float]
    time_flag: Optional[str]
    in_book: bool
    # B.1 deterministic extras
    is_capture: bool
    is_check: bool
    captured_piece: Optional[str]
    moved_piece: Optional[str]
    material_delta: int                       # mover's material minus opponent's, change
    material_after: Dict[str, int]
    newly_hanging: List[Dict[str, str]]       # mover's pieces now loose
    still_hanging: List[Dict[str, str]]
    opponent_hanging: List[Dict[str, str]]    # what the mover could have taken
    motifs: List[Dict[str, Any]]              # fork/pin/skewer/discovery/back-rank
    king_pressure_mover: int
    king_pressure_opponent: int
    king_pressure_change: int

    # Who is being addressed. Stated outright rather than left to be inferred
    # from `color`, because inferring it was the single largest source of
    # wrong comments -- pieces were attributed to the wrong player.
    you_are: str = "white"
    your_pieces_are: str = "white"
    opponent_pieces_are: str = "black"
    # Every square any of this move's facts mentions. The prompt is told it
    # may not name a square outside this list, which is what stops invented
    # geometry ("a discovered attack on d2" when d2 appears nowhere).
    squares_in_facts: List[str] = field(default_factory=list)
    # What actually stands on each of those squares AFTER the move, e.g.
    # {"e4": "black king"}. Without it the model knew which squares mattered
    # but not what was on them, and wrote "a hanging knight on e4" when the
    # black king was standing there. Empty value means the square is empty.
    pieces_on_those_squares: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def for_prompt(self) -> Dict[str, Any]:
        """
        The view handed to B.3. Narrower than `to_dict` on purpose.

        Each omission below made comments worse in live testing:

        `fen_before` -- given the position, the model reasons about the board
            instead of the facts and invents tactics that are not there ("a
            discovered queen attack on d2", a "passed pawn" that is a check).
            B.1 exists so nothing has to be inferred from the board; handing
            over the board defeats that.

        `cp_before` / `cp_after` -- raw centipawns leaked into prose as
            "+0.68" and were read as a verdict. `win_drop` already carries
            the magnitude on a scale the prompt can reason about.

        `time_flag` / `time_spent` -- read as a quality judgement, so a move
            labelled `best` was described as "a wasted move". Clock pressure
            is a fact about the clock, and it belongs in A.4's report.
        """
        d = asdict(self)
        for name in ("fen_before", "cp_before", "cp_after", "time_flag",
                     "time_spent", "accuracy", "material_after",
                     "still_hanging", "multipv_spread_cp", "color"):
            d.pop(name, None)

        # The line facts are trimmed to what the model must restate. Sending
        # them whole re-sent each PV (already present as `best_pv` /
        # `refutation_pv`) plus fields it has no use for, and that pushed a
        # move batch past Groq's request ceiling -- a 413 that silently cost
        # six of seven comments. Validation still sees the full dicts; only
        # the prompt is trimmed.
        for name in ("best_line", "refutation_line"):
            line = d.get(name)
            if not line:
                d.pop(name, None)
                continue
            slim = {"material_phrase": line.get("material_phrase")}
            if line.get("ends_in_mate"):
                slim["ends_in_mate"] = True
            if line.get("positional_notes"):
                slim["positional_notes"] = line["positional_notes"][:3]
            slim["has_concrete_reason"] = line.get("has_concrete_reason", True)
            if "material_explains" in line:
                slim["material_explains"] = line["material_explains"]
            d[name] = slim
        return d


def _piece_list(board: chess.Board, squares: Iterable[int]) -> List[Dict[str, str]]:
    out = []
    for sq in sorted(squares):
        piece = board.piece_at(sq)
        if piece is not None:
            out.append({
                "piece": PIECE_NAME[piece.piece_type],
                "square": _square_name(sq),
                "color": _colour(piece),
            })
    return out


def _line_facts(board: chess.Board, lead: Optional[str], pv: Sequence[str],
                label: str, *, flip: bool = False) -> Dict[str, Any]:
    """
    One engine line reduced to facts the model can restate but not invent.

    `pv[0]` is normally the lead move itself, so the two are merged rather
    than concatenated -- passing both would play it twice, and the second
    parse fails, which claim_guard would then (correctly but uselessly)
    report as "the line does not belong to this position".

    `flip` normalises the point of view. `walk_line` measures material from
    the side to move at the START of the line, which for a refutation is the
    OPPONENT -- so an unflipped refutation phrase said "loses a rook" about
    the opponent while the comment addressed the player, and a live run duly
    produced "you lose a rook" for a line in which the player WON one. Every
    phrase reaching the prompt is from the player's point of view, the same
    as `material_delta`, so the two can never disagree about a sign.
    """
    if not lead and not pv:
        return {}
    moves = list(pv)
    if lead and (not moves or moves[0] != lead):
        moves.insert(0, lead)
    facts = claim_guard.line_facts(board, moves, label=label)
    if not flip or not facts:
        return facts

    delta = -int(facts.get("material_delta_pawns", 0))
    mat = claim_guard.walk_line(board, moves)
    # gained/lost swap with the point of view, which matters: "the exchange"
    # is detected structurally as a rook won for a minor given, so feeding it
    # the unswapped lists would mislabel the motif as well as the sign.
    facts["material_delta_pawns"] = delta
    facts["material_phrase"] = claim_guard.describe_material(
        delta, mat.lost, mat.gained)
    return facts


def _refutation_line_facts(after: chess.Board,
                           record: Dict[str, Any]) -> Dict[str, Any]:
    """
    The refutation line, with a flag saying whether material explains the
    fault at all.

    A move can be a blunder while the engine's best line still ends with the
    PLAYER a rook up -- the loss is positional, or further down the line
    than the PV reaches. Handing "wins a rook" to the generator under a
    blunder invites exactly what it produced: "Rd1 and Bg4 win a rook",
    attributed to the wrong side, as the reason the move failed.

    So when the material outcome does not point the same way as the verdict,
    `material_explains` is false and the prompt is told not to lead with it.
    """
    if record["label"] not in FAULTED_LABELS:
        return {}
    facts = _line_facts(after, record.get("refutation_move"),
                        (record.get("refutation_pv") or [])[:SHOWN_PV_PLIES],
                        "refutation", flip=True)
    if not facts:
        return facts
    delta = int(facts.get("material_delta_pawns", 0))
    # The move lost ground, so material explains it only if the player ends
    # up worse off materially. Level is allowed: a level line with a
    # positional note still explains something.
    facts["material_explains"] = bool(
        delta < 0 or (delta == 0 and facts.get("has_concrete_reason")))
    return facts


def _geometry_key(entry: Dict[str, Any]) -> Tuple[str, ...]:
    """
    Identity of a pin or skewer: its type plus the squares of the three
    pieces involved, in role order.

    Keyed on squares rather than on the whole description so that the same
    geometry is recognised across the move, while a motif whose pieces have
    actually changed counts as new.
    """
    parts: List[str] = [str(entry.get("motif", ""))]
    for role in ("pinner", "pinned", "pinned_against",
                 "attacker", "front_piece", "piece_won_behind"):
        described = entry.get(role)
        if not described:
            continue
        squares = _SQUARE_RE.findall(str(described))
        parts.append(squares[-1] if squares else str(described))
    return tuple(parts)


def extract_move_facts(record: Dict[str, Any], *, in_book: bool) -> MoveFacts:
    """
    Facts for one move, from Part A's MoveRecord dict.

    Rebuilds the position from `fen_before` and pushes the move, so this
    never re-runs the engine -- Part A already paid for that.
    """
    before = chess.Board(record["fen_before"])
    move = chess.Move.from_uci(record["uci"])
    mover = before.turn
    moved = before.piece_at(move.from_square)
    captured = before.piece_at(move.to_square)
    if captured is None and before.is_en_passant(move):
        captured = chess.Piece(chess.PAWN, not mover)
    after = before.copy(stack=False)
    after.push(move)
    mat_before = material(before, mover) - material(before, not mover)
    mat_after = material(after, mover) - material(after, not mover)
    hang_before = hanging_squares(before, mover)
    hang_after = hanging_squares(after, mover)
    # Compare by (piece, square) so a piece that simply moved is not reported
    # as newly loose on its old square.
    before_ids = {(before.piece_at(s).piece_type, s) for s in hang_before}
    after_ids = {(after.piece_at(s).piece_type, s) for s in hang_after}
    newly = {s for (_, s) in (after_ids - before_ids)}
    still = {s for (_, s) in (after_ids & before_ids)}
    motifs: List[Dict[str, Any]] = []
    motifs += find_forks(after, move)
    # Diffed, not listed. Reporting every pin and skewer standing after the
    # move attributed pre-existing geometry to whatever move happened next:
    # a queen that had been aiming down a diagonal for ten moves was
    # reported as "now skewers", under a knight move on the other wing. The
    # facts are handed to the model under a move, so they are implicitly
    # that move's doing -- which means anything not caused by the move must
    # not be in the list.
    was = {_geometry_key(m) for m in find_pins_and_skewers(before, mover)}
    motifs += [m for m in find_pins_and_skewers(after, mover)
               if _geometry_key(m) not in was]
    motifs += find_discovered_attacks(before, after, move)
    motifs += own_pinned_pieces(after, mover)
    br = back_rank_weakness(after, not mover)
    if br:
        motifs.append(br)
    kp_mover_before = king_pressure(before, mover)
    kp_mover_after = king_pressure(after, mover)
    facts = MoveFacts(
        ply=record["ply"],
        move_number=record["move_number"],
        color=record["color"],
        san=record["san"],
        uci=record["uci"],
        fen_before=record["fen_before"],
        phase=record["phase"],
        label=record["label"],
        cp_before=record["cp_before"],
        cp_after=record["cp_after"],
        win_drop=record["win_drop"],
        accuracy=record["accuracy"],
        best_move=record["best_move"],
        best_pv=record["best_pv"][:SHOWN_PV_PLIES],
        refutation_move=(record.get("refutation_move")
                         if record["label"] in FAULTED_LABELS else None),
        # Four plies is enough to show why a move fails and short enough to
        # leave little room to narrate. A longer line invited the model to
        # describe positions it cannot see.
        refutation_pv=(list(record.get("refutation_pv") or [])[:SHOWN_PV_PLIES]
                       if record["label"] in FAULTED_LABELS else []),
        refutation_by=(("black" if record["color"] == "white" else "white")
                       if record["label"] in FAULTED_LABELS
                       and record.get("refutation_move") else None),
        # The engine's line runs from the position BEFORE the move (it is
        # what should have been played); the refutation runs from AFTER it.
        # Walking either from the wrong frame makes every move illegal at
        # index 0, which claim_guard reports rather than silently zeroing.
        best_line=_line_facts(before, record["best_move"],
                              (record.get("best_pv") or [])[:SHOWN_PV_PLIES],
                              "best"),
        refutation_line=_refutation_line_facts(after, record),
        played_best=record["played_best"],
        only_move=record["only_move"],
        multipv_spread_cp=record["multipv_spread_cp"],
        complexity_band=record["complexity_band"],
        mate_status_change=record.get("mate_status_change"),
        time_spent=record.get("time_spent"),
        time_flag=record.get("time_flag"),
        in_book=in_book,
        is_capture=captured is not None,
        is_check=after.is_check(),
        captured_piece=PIECE_NAME[captured.piece_type] if captured else None,
        moved_piece=PIECE_NAME[moved.piece_type] if moved else None,
        material_delta=mat_after - mat_before,
        material_after={
            "white": material(after, chess.WHITE),
            "black": material(after, chess.BLACK),
        },
        newly_hanging=_piece_list(after, newly),
        still_hanging=_piece_list(after, still),
        opponent_hanging=_piece_list(before, hanging_squares(before, not mover)),
        motifs=motifs,
        king_pressure_mover=kp_mover_after,
        king_pressure_opponent=king_pressure(after, not mover),
        king_pressure_change=kp_mover_after - kp_mover_before,
        you_are=record["color"],
        your_pieces_are=record["color"],
        opponent_pieces_are="black" if record["color"] == "white" else "white",
    )
    facts.squares_in_facts = _squares_mentioned(facts)
    facts.pieces_on_those_squares = {
        name: (
            f"{_colour(p)} {PIECE_NAME[p.piece_type]}"
            if (p := after.piece_at(chess.parse_square(name))) is not None
            else "empty"
        )
        for name in facts.squares_in_facts
    }
    return facts


_SQUARE_RE = re.compile(r"\b([a-h][1-8])\b")


def _line_squares(board: chess.Board, lead: Optional[str],
                  pv: Sequence[str]) -> Set[str]:
    """
    Every square a SAN line touches, walked from `board`.

    `lead` is the headline move and `pv[0]` is normally the same move, so
    passing both would push it twice -- the second parse fails and the walk
    stops. That is exactly what used to happen: `[best_move] + best_pv` added
    the first move's two squares and then broke, so no square past the first
    move of any engine line was ever mentionable, while the comment above it
    claimed the whole PV was. Comments citing a real continuation were being
    rejected for naming squares the allowlist had silently dropped.
    """
    moves = list(pv)
    if lead and (not moves or moves[0] != lead):
        moves.insert(0, lead)

    out: Set[str] = set()
    walk = board.copy(stack=False)
    for san in moves:
        if not san:
            continue
        try:
            mv = walk.parse_san(san)
        except ValueError:
            break
        out.add(chess.square_name(mv.from_square))
        out.add(chess.square_name(mv.to_square))
        walk.push(mv)
    return out


def _squares_mentioned(facts: "MoveFacts") -> List[str]:
    """
    Every board square this move's facts refer to.

    Walks the fact structures rather than listing fields by hand, so a new
    detector's squares are picked up automatically. Also includes the move's
    own from- and to-squares, which are always fair to mention.
    """
    found: Set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, str):
            # Squares now appear inside role descriptions such as
            # "white bishop on c4", so scan rather than match whole strings.
            found.update(_SQUARE_RE.findall(value))
        elif isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, (list, tuple)):
            for v in value:
                walk(v)

    for group in (facts.newly_hanging, facts.still_hanging,
                  facts.opponent_hanging, facts.motifs):
        walk(group)

    # The move's own squares.
    found.add(facts.uci[0:2])
    found.add(facts.uci[2:4])

    # The engine's recommended continuation. The prompt asks the model to
    # cite `best_move` and `best_pv`, so their squares must be mentionable --
    # otherwise the validator would reject exactly the advice it asked for.
    found |= _line_squares(chess.Board(facts.fen_before),
                           facts.best_move, facts.best_pv or [])

    # Both kings: king safety is relevant to almost any comment, and a true
    # statement about where a king stands should never be rejected.
    after = chess.Board(facts.fen_before)
    try:
        after.push(chess.Move.from_uci(facts.uci))
    except (ValueError, AssertionError):
        after = chess.Board(facts.fen_before)

    # The refutation, walked from the position AFTER the move -- that is
    # where the opponent's reply begins, and parsing it from `fen_before`
    # would fail on the first move and drop the whole line. The prompt
    # requires a faulted comment to name this, so its squares must be
    # sayable or the validator would reject what it asked for.
    found |= _line_squares(after, facts.refutation_move,
                           facts.refutation_pv or [])
    for colour in (chess.WHITE, chess.BLACK):
        ksq = after.king(colour)
        if ksq is not None:
            found.add(chess.square_name(ksq))

    # Pieces that actually bear on a relevant square. A comment about a loose
    # knight on e6 will rightly name the queen that is about to take it, and
    # rejecting that would have been a false positive -- the piece and the
    # relationship are both real. Widens the allowlist only to pieces with a
    # genuine tactical relationship to a square already in it.
    for name in list(found):
        try:
            sq = chess.parse_square(name)
        except ValueError:
            continue
        for colour in (chess.WHITE, chess.BLACK):
            for attacker in after.attackers(colour, sq):
                found.add(chess.square_name(attacker))

    return sorted(found)


_CLAIM_RE = re.compile(
    r"\b(white|black)?\s*(pawn|knight|bishop|rook|queen|king)s?\s+(?:on|at)\s+([a-h][1-8])\b",
    re.IGNORECASE,
)


def _material_problems(text: str, facts: "MoveFacts") -> List[str]:
    """
    Reject a material claim that no computed line supports.

    A comment may legitimately talk about the material of the move itself,
    of the engine's line, or of the refutation, so a claim is accepted when
    it is consistent with ANY of them and rejected only when it matches
    none. Checking against a single line would reject true sentences; this
    still catches the case it was built for, because "won the exchange"
    asserts +2 and that matched nothing on the board.

    A line that went illegal is NOT used as a candidate: its delta is zero
    because nothing was played, and admitting it would let "material is
    level" pass on the strength of a broken line.
    """
    deltas: List[int] = [facts.material_delta]
    for line in (facts.best_line, facts.refutation_line):
        if line and line.get("truncated_at") is None:
            deltas.append(int(line.get("material_delta_pawns", 0)))
    if not deltas:
        return []

    lowered = " " + re.sub(r"\s+", " ", text).lower() + " "
    out: List[str] = []

    # "The exchange" is a rook won for a minor, not any +2. The claim lexicon
    # can only see the net delta, so it would pass the phrase on a line that
    # won two pawns -- which is the exact conflation that produced the
    # comment this guard was built for. Tested structurally here instead.
    lines = [l for l in (facts.best_line, facts.refutation_line)
             if l and l.get("truncated_at") is None]
    if re.search(r"\b(?:win|wins|won|winning|up)\s+(?:the\s+)?exchange\b", lowered):
        if not any(l.get("exchange_won") for l in lines):
            out.append('claims "the exchange" (a rook won for a minor piece), '
                       "but no computed line wins one")
    if re.search(r"\b(?:lose|loses|lost|losing|down)\s+(?:the\s+)?exchange\b", lowered):
        if not any(l.get("exchange_lost") for l in lines):
            out.append('claims losing "the exchange" (a rook given for a minor '
                       "piece), but no computed line loses one")

    for pattern, predicate, rule in claim_guard.MATERIAL_CLAIMS:
        if "exchange" in pattern:
            continue            # handled structurally above
        m = re.search(pattern, lowered)
        if m and not any(predicate(d) for d in deltas):
            out.append(
                f'claims "{m.group(0).strip()}" ({rule}), but nothing computed '
                f"for this move supports it (deltas: "
                f"{', '.join(f'{d:+d}' for d in sorted(set(deltas)))})"
            )
    return out


def validate_comment(text: str, facts: "MoveFacts") -> List[str]:
    """
    Check a generated comment against the facts. Returns the problems found.

    Two things are checkable without re-deriving chess:

      1. **Squares.** A square outside `squares_in_facts` is one the model
         introduced on its own.
      2. **Piece-on-square claims.** "knight on e4" is false if no knight
         stands on e4 in either the position before the move or the one
         after. Both frames are allowed because a comment legitimately talks
         about what was just captured as well as what now stands there.

    Deliberately NOT checked, because it cannot be done reliably:

      * claims about lines rather than squares ("clearing the d-file")
      * a stale present-tense reference to a piece that was true before the
        move and is not now -- it matches the before-frame, so it passes
      * anything semantic: a wrong plan, a wrong assessment

    So this narrows the error surface; it does not close it. Prevention via
    `pieces_on_those_squares` does more of the work than this does.
    """
    problems: List[str] = []
    allowed = set(facts.squares_in_facts)

    for square in set(_SQUARE_RE.findall(text)):
        if square not in allowed:
            problems.append(f"names square {square}, which is not in this move's facts")

    problems += _material_problems(text, facts)

    before = chess.Board(facts.fen_before)
    after = before.copy(stack=False)
    try:
        after.push(chess.Move.from_uci(facts.uci))
    except (ValueError, AssertionError):
        return problems

    for colour, piece_name, square in _CLAIM_RE.findall(text):
        sq = chess.parse_square(square.lower())
        wanted = piece_name.lower()
        ok = False
        for board in (after, before):
            piece = board.piece_at(sq)
            if piece is None:
                continue
            if PIECE_NAME[piece.piece_type] != wanted:
                continue
            if colour and _colour(piece) != colour.lower():
                continue
            ok = True
            break
        if not ok:
            actual = after.piece_at(sq)
            found = (f"{_colour(actual)} {PIECE_NAME[actual.piece_type]}"
                     if actual else "nothing")
            problems.append(
                f"claims a {colour.lower() + ' ' if colour else ''}{wanted} on "
                f"{square}, but {square} holds {found}"
            )
    return problems


@dataclass
class GameFacts:
    """The whole fact timeline, plus the patterns that only show up in aggregate."""
    source_type: str
    headers: Dict[str, str]
    opening: Dict[str, Any]
    result: Optional[str]
    players: Dict[str, Dict[str, Any]]
    moves: List[MoveFacts]
    recurring: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_type": self.source_type,
            "headers": self.headers,
            "opening": self.opening,
            "result": self.result,
            "players": self.players,
            "moves": [m.to_dict() for m in self.moves],
            "recurring": self.recurring,
        }


def _recurring_patterns(moves: Sequence[MoveFacts]) -> Dict[str, Any]:
    """
    Patterns across the game, per player.

    This is what makes "it's the third time this game a piece was left
    undefended" possible: the count is computed here, so the comment can
    state it instead of the model guessing at it.
    """
    out: Dict[str, Any] = {}
    for colour in ("white", "black"):
        mine = [m for m in moves if m.color == colour]
        motif_counts: Counter = Counter()
        for m in mine:
            for motif in m.motifs:
                motif_counts[motif["motif"]] += 1
        # Only count a loose piece as a habit when the engine did NOT endorse
        # the move. Without this, the Fried Liver's knight sacrifice and every
        # check in the game were reported as "tends to leave pieces en prise",
        # and B.2 faithfully repeated it as the player's recurring weakness --
        # a confident, wrong diagnosis built from a true-but-misleading count.
        hung = [m for m in mine if m.newly_hanging and not m.played_best]
        sacrificed = [m for m in mine if m.newly_hanging and m.played_best]
        out[colour] = {
            "moves": len(mine),
            "times_left_a_piece_loose": len(hung),
            "loose_piece_plies": [m.ply for m in hung][:10],
            # Engine-approved loose pieces: sacrifices and checks, not errors.
            # Reported separately so B.2 can tell the difference.
            "deliberate_sacrifices": len(sacrificed),
            "motif_counts": dict(motif_counts),
            "blunders_by_phase": dict(Counter(
                m.phase for m in mine if m.label == "blunder")),
            "mistakes_by_phase": dict(Counter(
                m.phase for m in mine if m.label == "mistake")),
            "rushed_moves": [m.ply for m in mine if m.time_flag == "rushed"][:10],
            "wasted_moves": [m.ply for m in mine if m.time_flag == "wasted"][:10],
            "material_low_point": min((m.material_delta for m in mine), default=0),
        }
    return out


def extract_game_facts(report: Dict[str, Any]) -> GameFacts:
    """
    B.1 over a whole game, from Part A's report dict.

    No engine, no network, no API key -- so the facts are available even
    when commentary is not.
    """
    started = time.perf_counter()
    left_book = (report.get("opening") or {}).get("left_book_ply")
    moves = [
        extract_move_facts(
            rec, in_book=(left_book is None or rec["ply"] < left_book)
        )
        for rec in report["moves"]
    ]
    players = {}
    for colour in ("white", "black"):
        p = report[colour]
        players[colour] = {
            "name": p["name"],
            "rating_header": p["rating_header"],
            "accuracy": p["accuracy"],
            "acpl": p["acpl"],
            "blunders": p["blunders"],
            "mistakes": p["mistakes"],
            "inaccuracies": p["inaccuracies"],
            "weakest_phase": p["weakest_phase"],
            "rating_estimate_band": (p.get("rating_estimate") or {}).get("band"),
        }
    log.info("B.1 extracted facts for %d moves in %.2fs",
             len(moves), time.perf_counter() - started)
    return GameFacts(
        source_type=report["source_type"],
        headers=report["headers"],
        opening=report["opening"],
        result=report["headers"].get("Result"),
        players=players,
        moves=moves,
        recurring=_recurring_patterns(moves),
    )


# ==========================================================================
# B.4  Cost and latency control -- designed up front, per plan.md
#
# ~40 commentable moves per game, called naively, is minutes of latency and
# unbounded cost on a public app. Five levers, all applied here:
#
#   1. Comment only on moves that earn it, plus a few representative quiet
#      ones so the timeline is not all disasters (select_commentable).
#   2. Batch several moves per LLM call (MOVES_PER_BATCH).
#   3. Cache by (FEN, move_played) (CommentCache).
#   4. Rate limit per authenticated account -- in partE, at the edge.
#   5. Stream comments to the UI as they arrive -- the progress callback
#      publishes each batch the moment it lands.


# ==========================================================================
# Bumped whenever a prompt or schema changes, because it is part of every
# cache key: an old comment generated by a different prompt must not be
# served as if the new prompt had produced it.
PROMPT_VERSION = "b-2026-10-14.14"
# Each provider carries its own default_model; LLM_PROVIDER picks the class.
DEFAULT_PROVIDER = "groq"
# Selection thresholds. Win% drop, matching Part A's corrected scale.
COMMENT_WIN_DROP = 5.0          # anything Lichess would label at all
MAX_COMMENTS_PER_GAME = 18      # hard cap on the LLM bill per analysis
# ...and a proportional cap, so a 24-move game is not commented end to end.
# Without this the fixed cap alone selects most of a short game, which is
# both expensive and a worse read than a few chosen moments.
MAX_COMMENT_FRACTION = 0.35
QUIET_MOVE_SAMPLE = 4           # representative good moves, evenly spread
MOVES_PER_BATCH = 6
MAX_BATCHES_PER_GAME = 4

# Notability gate for moves that do NOT clear the win-drop threshold. Higher
# than COMMENT_WIN_DROP on purpose: reusing the drop threshold here let any
# capture through, because material and motif bonuses alone exceeded 5.
NOTABILITY_GATE = 14.0

# Motifs worth a comment on their own. The rest -- ordinary relative pins,
# a piece of your own being pinned -- occur in most positions; one game here
# produced seven pins in fourteen moves, so weighting them equally with a
# fork drowns out the moves that matter.
SHARP_MOTIFS = frozenset({"fork", "skewer", "discovered_attack", "back_rank_weakness"})


class CommentaryError(RuntimeError):
    """Commentary could not be produced. Safe to show to a user."""


def _notability(f: MoveFacts) -> float:
    """
    How much a move deserves a comment.

    Deliberately not just the eval swing: a move can be the only move in a
    sharp position, create a fork, walk into a mate, or hang a piece the
    engine does not punish immediately, and all of those are worth saying
    something about.
    """
    # A drop on the engine's own top move is not the player's error -- it is
    # depth instability or a forced sequence (Part A's classify() short-
    # circuits played_best for the same reason). Counting it in full spent
    # comments on strong moves that merely looked bad.
    score = f.win_drop if not f.played_best else f.win_drop * 0.25
    if f.mate_status_change:
        score += 40
    if f.newly_hanging:
        # The signature teachable error: a piece left where it can be taken.
        # Unless the engine endorsed the move -- a loose piece inside a
        # sacrifice or a check is tactically paid for, not an oversight.
        score += (3 if f.played_best else 8) * len(f.newly_hanging)
    if f.material_delta <= -2:
        score += 5 * min(abs(f.material_delta), 4)
    elif f.material_delta >= 2:
        # Winning material is worth a mention but is usually just a
        # recapture, so it gets a flat nudge rather than scaling. Scaling it
        # ranked routine recaptures above the game's actual blunder.
        score += 3
    if f.only_move and f.played_best:
        score += 12                     # found the single move that held
    sharp = sum(1 for m in f.motifs if m["motif"] in SHARP_MOTIFS)
    score += 4 * min(sharp, 3) + min(len(f.motifs) - sharp, 2)
    if f.time_flag == "rushed":
        score += 4
    if f.in_book and f.win_drop < COMMENT_WIN_DROP:
        # Playing theory well is not the player's doing. But a book move the
        # engine dislikes is still worth saying something about -- discounting
        # those dropped the Fried Liver's key mistake (...Nxd5, a 15-point
        # drop that is also a known line) out of the selection entirely.
        score *= 0.4
    return score


def select_commentable(
    moves: Sequence[MoveFacts],
    *,
    max_comments: int = MAX_COMMENTS_PER_GAME,
    win_drop_threshold: float = COMMENT_WIN_DROP,
    quiet_sample: int = QUIET_MOVE_SAMPLE,
) -> List[int]:
    """
    Which plies to comment on. Returns plies in game order.

    Three tiers, in strict priority order. The tiering is structural rather
    than a product of the weights, because weight-tuning alone kept ranking
    impressive-but-correct moves above the game's actual errors -- and a
    review exists to show someone their mistakes first.

      1. flagged  -- every move the engine faulted, or that changed mate
                     status. A move Part A called a mistake is always
                     commented before any move it approved.
      2. notable  -- strong or sharp moves clearing NOTABILITY_GATE: found
                     the only move, won material, created a fork.
      3. quiet    -- a few unremarkable good moves, evenly spaced. A review
                     of nothing but blunders reads as a scolding and teaches
                     less than one that also says why the good moves worked.

    `_notability` orders moves *within* a tier; it no longer decides which
    tier they land in.
    """
    if not moves:
        return []

    budget = min(max_comments, max(4, int(len(moves) * MAX_COMMENT_FRACTION)))
    quiet_sample = min(quiet_sample, max(budget // 3, 1))

    # Tier 1: everything the engine faulted.
    faulted = set(FAULTED_LABELS)
    flagged = sorted(
        (f for f in moves
         if f.label in faulted or f.mate_status_change
         or f.win_drop >= win_drop_threshold),
        key=_notability, reverse=True,
    )
    chosen = {f.ply for f in flagged[:budget]}

    # Tier 2: fill what is left, keeping room for the quiet sample.
    room = max(budget - quiet_sample - len(chosen), 0)
    if room:
        notable = [
            f for f in sorted(moves, key=_notability, reverse=True)
            if f.ply not in chosen and _notability(f) >= NOTABILITY_GATE
        ]
        chosen |= {f.ply for f in notable[:room]}

    # Tier 3: representative quiet moves, strong, unremarkable, evenly spaced.
    quiet = [
        f for f in moves
        if f.ply not in chosen and f.label in ("best", "excellent") and not f.in_book
    ]
    if quiet and quiet_sample > 0:
        step = max(len(quiet) // quiet_sample, 1)
        for f in quiet[::step][:quiet_sample]:
            if len(chosen) >= budget:
                break
            chosen.add(f.ply)

    return sorted(chosen)

class CommentCache:
    """
    In-process comment cache, keyed per plan.md B.4 on (FEN, move_played).

    The key also carries the label, prompt version and model, because a
    comment is a function of all four -- serving a comment written by an
    older prompt would silently misrepresent what the current pipeline
    produces.

    A cached comment is deliberately *position-local*: it cannot reference
    this particular game's narrative, since the same position recurs across
    games. That is the accepted trade for B.4's cost control, and it is why
    critical moments identified by B.2 bypass the cache entirely and always
    get a fresh, narrative-aware comment.

    In-process for the same reason JobStore is (E.2): Render free runs a
    single instance. Move to the `analyses` table or Redis alongside it.
    """

    def __init__(self, max_entries: int = 5000) -> None:
        self._data: Dict[str, str] = {}
        self._max = max_entries
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    @staticmethod
    def key(facts: MoveFacts, model: str) -> str:
        raw = "|".join([
            facts.fen_before, facts.uci, facts.label, PROMPT_VERSION, model,
        ])
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    def get(self, facts: MoveFacts, model: str) -> Optional[str]:
        k = self.key(facts, model)
        with self._lock:
            hit = self._data.get(k)
        if hit is None:
            self.misses += 1
        else:
            self.hits += 1
        return hit

    def put(self, facts: MoveFacts, model: str, comment: str) -> None:
        with self._lock:
            if len(self._data) >= self._max:
                self._data.clear()      # bounded; see EngineService.analyse
            self._data[self.key(facts, model)] = comment

    def stats(self) -> Dict[str, Any]:
        total = self.hits + self.misses
        return {
            "entries": len(self._data),
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total, 3) if total else 0.0,
        }


# ==========================================================================
# B.2 / B.3  Prompts and schemas
#
# The prompts are the product here, so they are explicit about the one thing
# that makes this different from template commentary: every claim must trace
# to a supplied fact. The facts are computed in B.1 precisely so the model
# never has to recall or infer them.


# ==========================================================================
SYSTEM_GAME = """\
You are a chess coach reviewing one game for the players who played it.

You receive a deterministic fact timeline: every evaluation, classification,
material count, hanging piece, tactical motif and clock reading in it was
computed by an engine and a rules library. It is ground truth.

Your job is the shape of the game, not individual moves. Produce:

- arc: how the game actually went, in 3-5 sentences. Name the moments where
  the result was decided. Refer to real moves and squares.

- critical_moments: the plies where the game's outcome genuinely turned, with
  one clause each on why. Three to six of them. Use the `ply` numbers given.

- white_weakness / black_weakness: the one recurring habit each player should
  work on, stated concretely and drawn from the aggregate counts supplied
  (phase blunder counts, loose-piece counts, time flags). If the facts do not
  support a pattern, say that there is no clear pattern in this single game
  rather than inventing one.

  `times_left_a_piece_loose` counts genuine oversights only.
  `deliberate_sacrifices` counts pieces left loose on moves the engine
  endorsed - sacrifices and checks, never a weakness. Do not describe a
  player who sacrificed soundly as careless with their pieces.

Rules:

- Never state a fact that is not in the input. Do not infer piece placement
  you were not given.

- Do not quote raw numbers. `win_drop` is a swing in winning chances measured
  in percentage points, NOT centipawns - reporting it as "14.93 cp" is simply
  wrong. Say "decisive", "serious" or "slight" instead.

- Every fact that names a piece carries a `color`. Check it before writing
  "White's" or "Black's".

- `ply` is a position index, for the `critical_moments` field only. In prose
  always use the `move` string supplied (e.g. "10.Qf7"), never the ply
  number - writing "19.Qf7" for ply 19 invents a move that does not exist.

- Do not moralise and do not pad. A coach is specific.

- One game is a small sample. Do not generalise about a player's overall
  strength beyond what the game shows."""


SYSTEM_MOVE = """\
You are a chess coach writing per-move comments for a game review.

For each move you receive its computed facts and the game narrative you were
given. Write one comment per move, keyed by ply.

THE ONE RULE THAT MATTERS MOST: never describe the move that was played.
The reader can see the notation. A comment opening with "You moved...",
"You played...", "You captured...", "You delivered..." has spent its first
sentence on what the reader already knew. Open with the consequence.

What a good comment does:

- Names the concrete consequence, with squares and pieces:

  "This hangs the knight on f6 - after Nxf6 you lose a piece for nothing."
- Connects to the game when the facts support it:

  "...and it's the third time this game a piece was left undefended."
- For a strong move, says what it achieved, not that it was strong.

- For a losing move, LEADS WITH THE REFUTATION. `refutation_move` and
  `refutation_pv` are the opponent's best reply - what actually happens to
  you now - and that is the reason the move was bad. State it first, as a
  concrete line, then give what held instead from `best_move` / `best_pv`.

    GOOD  "This loses the exchange to Bxg3 - hxg3 Nxg3 forks the rook on f1.
           Re1 held everything together."

  When `refutation_move` is present it is the single most important fact
  about that move. A comment on a faulted move that does not say what the
  opponent does about it has not explained anything. These fields appear
  only on faulted moves: on a good move there is no refutation to give.

  THE REFUTATION IS THE OPPONENT'S MOVE, never yours. `refutation_by` says
  whose it is, and it is always the opposite side to `you_are`. Writing
  "Black's refutation Nxf7" when White plays Nxf7 reverses the whole point
  of the sentence. The moves in `refutation_pv` then alternate from there.

  MATERIAL IS COMPUTED FOR YOU, by playing each line out and counting:
  `best_line.material_phrase` and `refutation_line.material_phrase`. Make no
  other claim about material. Never add up captures yourself, and never
  infer an outcome from the shape of a line - a comment that tried said
  "would have won the exchange" about a line in which no rook was captured.
  ("The exchange" is a rook won for a minor, never just two pawns.)

  Each phrase is a complete clause, already written from the point of view
  of `you_are`. Use it as its own clause, or drop it - saying nothing about
  material is always allowed; stating a DIFFERENT outcome is not.
    BAD   "Na5 would have kept material is level at the end of the line."
    GOOD  "After Na5, material is level at the end of the line."
  "wins a rook" under a move by Black means BLACK wins it, never White.

  If `material_explains` is false, material is NOT why the move was bad -
  the cost is positional or beyond the line. Do not lead with it there.
  If `has_concrete_reason` is false, there is nothing concrete to say: be
  brief ("Inaccuracy. f4 was better."). A short true comment beats a padded
  one.

  GIVE THE LINE, DO NOT ANALYSE IT. You may name the moves and say what is
  won or lost at the end of it, using the supplied phrase for anything
  material. You may NOT claim a tactic inside it - no
  pin, fork, skewer or mate threat that happens partway down the line. Every
  motif you were given describes the position after the move that was
  PLAYED, not positions several plies into a variation, and you cannot see
  those positions. "Rd1 pins the queen against the king" three plies deep is
  an invention even when every square in it is real.
    BAD   "Qf7 Kd8 Rd1 pins the black queen on b6, winning it."
    GOOD  "Qf7 is close to winning - after Kd8 Rd1 there is no good defence."

What it never does:

- "Good move!", "Interesting choice", "This is a mistake." - empty on its own.

- Narrate the move. Rewrite every one of these:
    BAD   "You delivered a check with Ne4+, forcing the black king to move."
    GOOD  "The king has no safe square left - d5 walks into c4+."
    BAD   "You responded with Kxe4, regaining the piece."
    GOOD  "Forced, but it leaves the knight on e6 undefended."
    BAD   "You played Be6, which hangs the b7 pawn."
    GOOD  "This drops the bishop outright - Bxe6 and there is no recapture."
    BAD   "You captured the bishop on e6, winning a piece."
    GOOD  "That is a clean piece, and the king on d6 still has no shelter."
- Invent anything. You do not have the board, only the facts listed. If a
  tactic is not in `motifs`, it did not happen.
- Name a square that is not in that move's `squares_in_facts`. That list is
  exhaustive; anything outside it is something you imagined.
- Describe a piece that is not actually there. `pieces_on_those_squares`
  gives you exactly what stands on each of those squares after the move
  ("e4": "black king"). Check it before naming a piece on a square. A square
  marked "empty" holds nothing.
- Mention a file or rank ("the d-file", "the seventh rank") unless the facts
  support it. You are given squares, not lines.

Comments are machine-checked against the facts after you write them, and any
comment naming a square or a piece that is not there is discarded entirely -
the reader then gets nothing for that move. A shorter comment that is certainly
true is always better than a fuller one that risks being dropped.
- Get ownership wrong. Every piece is named in full ("white bishop on c4"),
  and each move says `you_are`, `your_pieces_are` and `opponent_pieces_are`.
  A piece whose colour differs from `your_pieces_are` is THEIRS, never yours.
- Get a tactic backwards. Motifs name their participants by role, and every
  one carries `benefits` -- the side it helps. Read those fields; never infer
  direction from the squares:
    pin        pinner / pinned / pinned_against
               The `pinner` is attacking. The `pinned` piece is the one that
               cannot move. A bishop that pins an enemy pawn is NOT itself
               pinned.
    skewer     attacker / front_piece / piece_won_behind
    fork       forker / targets
    discovered_attack
               attacker / victim / opened_by_vacating
               If `benefits` is your colour this is YOUR threat, not a
               danger to you.
    own_piece_pinned
               `pinned` is your own piece and it cannot move; `benefits` is
               the opponent.
  If `benefits` is not your colour, the motif is working against you. If it
  is, it is working for you. Say so accordingly.
- Quote evaluation numbers. Say "winning" or "lost", never "+0.68".
- Treat a loose piece on a move with `played_best: true` as an oversight --
  that is a sacrifice or a check the engine endorsed.
- Describe a move that lost ground as if it achieved something. If the label
  is `blunder`, `mistake` or `inaccuracy`, the move made the position worse,
  full stop. Any motif still listed is secondary to that - never open a
  comment on a faulted move with a threat it created.

- Claim a tactic, threat or plan that is not in the facts. If the facts are
  thin, comment on what they do support, briefly.

- Exceed two sentences.
Write in second person to the player who moved. Plain prose, no markdown."""
GAME_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "arc": {"type": "string"},
        "critical_moments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ply": {"type": "integer"},
                    "why": {"type": "string"},
                },
                "required": ["ply", "why"],
                "additionalProperties": False,
            },
        },
        "white_weakness": {"type": "string"},
        "black_weakness": {"type": "string"},
    },
    "required": ["arc", "critical_moments", "white_weakness", "black_weakness"],
    "additionalProperties": False,
}


MOVE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "comments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ply": {"type": "integer"},
                    # Bounded, and enforced by strict decoding rather than
                    # asked for in the prompt. Without a cap the model
                    # decides how long a comment is, which makes the output
                    # budget uncomputable -- and an over-long answer does
                    # not get trimmed, it blows max_completion_tokens and
                    # the WHOLE batch is rejected mid-object.
                    #
                    # 240 characters is about 40 words: enough for "this
                    # hangs the knight on f6; after Nxf6 Black is a piece
                    # up", not enough for the three-clause essays that were
                    # both blowing the budget and carrying the fabricated
                    # material claims (3b.4).
                    "comment": {
                        "type": "string",
                        "maxLength": batch_budget.MAX_COMMENT_CHARS,
                    },
                },
                "required": ["ply", "comment"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["comments"],
    "additionalProperties": False,
}


@dataclass
class GameNarrative:
    arc: str
    critical_moments: List[Dict[str, Any]]
    white_weakness: str
    black_weakness: str

    def critical_plies(self) -> Set[int]:
        return {int(m["ply"]) for m in self.critical_moments if "ply" in m}

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class MoveComment:
    ply: int
    comment: str
    cached: bool = False
    # Populated only on a rejected comment, for diagnosis. A rejected comment
    # is never returned to the caller.
    rejected_because: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Commentary:
    available: bool
    narrative: Optional[Dict[str, Any]] = None
    comments: List[Dict[str, Any]] = field(default_factory=list)
    facts: Optional[Dict[str, Any]] = None
    usage: Dict[str, Any] = field(default_factory=dict)
    reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ==========================================================================
# B.2 / B.3  Providers
#
# Everything above this line is provider-neutral: B.1's facts, B.4's
# selection, batching and cache. Only the `_request` method differs per
# provider, so adding one is a subclass, not a rewrite.
#
# Two are shipped:
#   ClaudeCommentator -- Anthropic. Highest quality, metered (~$0.14/game
#                        on Opus 5). Supports prompt caching, which is where
#                        the stable-prefix layout below earns its keep.
#   GroqCommentator   -- Groq. Free tier, very fast, open models. No prompt
#                        caching, so the same layout is simply concatenated.
#
# The prompt text and the JSON schemas are shared, so a comment's quality is
# the only thing that varies between providers -- not its shape.
# ==========================================================================
class BaseCommentator:
    """
    Provider-neutral B.2/B.3 logic.

    Subclasses implement `_request`, which takes the prompt already split
    into a system message, stable user blocks and volatile user blocks, and
    returns parsed JSON matching `schema`.

    The split is not cosmetic: on a provider with prompt caching the stable
    blocks are identical across every batch of a game, so they sit behind a
    cache breakpoint and are not re-read at full price. A provider without
    caching ignores the distinction and concatenates.
    """

    provider = "base"
    default_model = ""
    # Per-request output ceilings, per pass. Provider-specific because they
    # must fit the provider's rate-limit window, not just the model's
    # capability -- and the two passes need very different room: B.2 writes a
    # narrative plus critical moments, B.3 writes a few short comments.
    #
    # Too small is not a soft failure: with strict schema decoding the model
    # is cut off mid-JSON and the request is rejected outright.
    max_output_tokens = 16000
    game_pass_tokens = 16000
    move_pass_tokens = 16000
    # B.4 batch size. Smaller batches mean more requests but each one fits a
    # tighter token budget.
    default_batch_size = MOVES_PER_BATCH
    # Highest reasoning effort this provider can afford. None means no cap.
    max_effort: Optional[str] = None

    _EFFORT_ORDER = ("low", "medium", "high")

    def clamp_effort(self, effort: str) -> str:
        """
        Lower a requested effort to what the provider can actually pay for.

        Reasoning tokens are billed as output and count against rate limits,
        so an effort the window cannot fund does not degrade gracefully -- it
        truncates the response mid-JSON and the request is rejected.
        """
        if self.max_effort is None or effort not in self._EFFORT_ORDER:
            return effort
        if self._EFFORT_ORDER.index(effort) <= self._EFFORT_ORDER.index(self.max_effort):
            return effort
        return self.max_effort

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        model: Optional[str] = None,
        max_batches: int = MAX_BATCHES_PER_GAME,
    ) -> None:
        self.model = model or self.default_model
        self.max_batches = max_batches
        self._api_key = api_key or self._key_from_env()
        self._client: Any = None
        self.usage: Counter = Counter()

    def _key_from_env(self) -> Optional[str]:
        return os.getenv("LLM_API_KEY") or None

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    # -- subclass contract ------------------------------------------------
    def _request(
        self,
        *,
        system: str,
        stable: List[str],
        volatile: List[str],
        schema: Dict[str, Any],
        schema_name: str,
        effort: str,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        raise NotImplementedError

    # -- B.2 ---------------------------------------------------------------
    def game_pass(self, facts: GameFacts) -> GameNarrative:
        """
        One call for the whole game: arc, critical moments, weaknesses.

        This is what lets the move comments reference the game instead of
        floating free, so it runs first and its output feeds every batch.
        """
        payload = {
            "players": facts.players,
            "opening": facts.opening,
            "result": facts.result,
            "recurring_patterns": facts.recurring,
            "timeline": [_timeline_entry(m) for m in facts.moves],
        }
        data = self._request(
            system=SYSTEM_GAME,
            stable=[],
            volatile=["Fact timeline:\n" + json.dumps(payload, indent=1)],
            schema=GAME_SCHEMA,
            schema_name="game_narrative",
            effort="high",
            max_tokens=self.game_pass_tokens,
        )
        return GameNarrative(
            arc=data["arc"],
            critical_moments=data.get("critical_moments", []),
            white_weakness=data["white_weakness"],
            black_weakness=data["black_weakness"],
        )

    # -- B.3 ---------------------------------------------------------------
    def move_pass(
        self, batch: Sequence[MoveFacts], narrative: GameNarrative,
        players: Dict[str, Dict[str, Any]],
    ) -> List[MoveComment]:
        """One call for several moves, grounded in the game narrative."""
        context = {
            "game_arc": narrative.arc,
            "white_weakness": narrative.white_weakness,
            "black_weakness": narrative.black_weakness,
            "players": players,
        }
        data = self._request(
            system=SYSTEM_MOVE,
            # Identical across every batch of this game -> cacheable.
            stable=["Game context:\n" + json.dumps(context, indent=1)],
            volatile=["Comment on these moves:\n"
                      + json.dumps([m.for_prompt() for m in batch], indent=1)],
            schema=MOVE_SCHEMA,
            schema_name="move_comments",
            # LOW, not medium. B.2 synthesises an arc across forty moves and
            # genuinely needs to reason; B.3 restates facts that have already
            # been computed into one English sentence. It is the one pass
            # where reasoning buys nothing -- and reasoning tokens are billed
            # as output and spent BEFORE any JSON is emitted, so at medium
            # they were the majority of the budget. Measured on gpt-oss-120b:
            # low 647, medium 1,402.
            effort="low",
            # Scaled to the batch rather than a constant. The old fixed value
            # left ~16% headroom against a reasoning cost that is not
            # deterministic and that GROWS with how much there is to say --
            # so the batches that failed were the eventful ones, which is
            # exactly where the commentary was worth having.
            max_tokens=batch_budget.move_pass_budget(
                len(batch), "low", ceiling=self.move_pass_tokens),
        )
        by_ply = {m.ply: m for m in batch}
        out: List[MoveComment] = []
        for entry in data.get("comments", []):
            try:
                ply = int(entry["ply"])
            except (KeyError, TypeError, ValueError):
                continue
            # Drop comments for plies we did not ask about, which would
            # otherwise attach to the wrong move.
            if ply not in by_ply:
                continue
            text = str(entry.get("comment", "")).strip()
            if not text:
                continue

            problems = validate_comment(text, by_ply[ply])
            if problems:
                # No comment beats a wrong one. The reader cannot tell a
                # confident falsehood from a fact, so a gap in the timeline
                # is the safer failure -- and `rejected` in the usage block
                # makes the gap visible rather than silent.
                log.info("B.3 rejected the comment on ply %d: %s",
                         ply, "; ".join(problems))
                self.usage["rejected_comments"] += 1
                out.append(MoveComment(ply=ply, comment=text,
                                       rejected_because=problems))
                continue
            out.append(MoveComment(ply=ply, comment=text))
        return out

    # -- shared helpers ----------------------------------------------------
    def _parse_json(self, text: str) -> Dict[str, Any]:
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise CommentaryError(
                f"{self.provider} returned text that was not valid JSON"
            ) from exc

    def _note_usage(self, **counts: int) -> None:
        self.usage["calls"] += 1
        for name, value in counts.items():
            self.usage[name] += value or 0


class ClaudeCommentator(BaseCommentator):
    """Anthropic. Metered; supports prompt caching."""

    provider = "anthropic"
    default_model = "claude-opus-5"

    def _key_from_env(self) -> Optional[str]:
        return os.getenv("LLM_API_KEY") or os.getenv("ANTHROPIC_API_KEY") or None

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not self._api_key:
            raise CommentaryError(
                "No Anthropic API key configured; set LLM_API_KEY to enable commentary"
            )
        try:
            import anthropic  # noqa: PLC0415 -- lazy, so partB imports without it
        except ImportError as exc:
            raise CommentaryError(
                "The `anthropic` package is not installed; add it to requirements.txt"
            ) from exc
        self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    def _request(
        self, *, system: str, stable: List[str], volatile: List[str],
        schema: Dict[str, Any], schema_name: str, effort: str,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        One structured-output call.

        Non-streaming on purpose: structured output via `output_config.format`
        is the documented path and these payloads are small (a fact timeline
        is a few thousand tokens, a batch of comments a few hundred). B.4's
        UI-streaming requirement is met a level up, by publishing each batch
        as it completes.

        Cache breakpoints go on the system prompt and on the last stable
        block; the volatile blocks follow, so they never invalidate the
        cached prefix. The SDK renders tools -> system -> messages.
        """
        import anthropic

        client = self._ensure_client()
        blocks: List[Dict[str, Any]] = []
        for i, text in enumerate(stable):
            block: Dict[str, Any] = {"type": "text", "text": text}
            if i == len(stable) - 1:
                block["cache_control"] = {"type": "ephemeral"}
            blocks.append(block)
        blocks += [{"type": "text", "text": t} for t in volatile]

        try:
            response = client.messages.create(
                model=self.model,
                max_tokens=max_tokens or self.max_output_tokens,
                system=[{"type": "text", "text": system,
                         "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": blocks}],
                thinking={"type": "adaptive"},
                output_config={
                    "format": {"type": "json_schema", "schema": schema},
                    "effort": self.clamp_effort(effort),
                },
            )
        except anthropic.AuthenticationError as exc:
            raise CommentaryError("Anthropic API key was rejected") from exc
        except anthropic.RateLimitError as exc:
            raise CommentaryError("Anthropic rate limit reached; try again shortly") from exc
        except anthropic.APIStatusError as exc:
            raise CommentaryError(
                f"Anthropic returned {exc.status_code}"
                + (" (server-side, retryable)" if exc.status_code >= 500 else "")
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise CommentaryError("Could not reach the Anthropic API") from exc

        u = getattr(response, "usage", None)
        if u is not None:
            self._note_usage(
                input_tokens=getattr(u, "input_tokens", 0),
                output_tokens=getattr(u, "output_tokens", 0),
                cache_creation_input_tokens=getattr(u, "cache_creation_input_tokens", 0),
                cache_read_input_tokens=getattr(u, "cache_read_input_tokens", 0),
            )

        if response.stop_reason == "refusal":
            raise CommentaryError("The model declined to comment on this game")
        if response.stop_reason == "max_tokens":
            raise CommentaryError("Response was truncated; lower the batch size")

        return self._parse_json(
            "".join(b.text for b in response.content if b.type == "text")
        )


class GroqCommentator(BaseCommentator):
    """
    Groq. Free tier, open models, no prompt caching.

    Default model is `openai/gpt-oss-120b`: a production model (not
    preview, so it will not vanish at short notice), 131k context, and one
    of the few on Groq that supports **strict** JSON-schema decoding --
    which matters here because both passes depend on schema-shaped output.
    `llama-3.3-70b-versatile` is comparable in size but only offers
    json_object mode, i.e. "valid JSON" with no schema guarantee.

    Groq is OpenAI-compatible but the first-party `groq` SDK is used rather
    than pointing the `openai` client at their base URL: same result, one
    less layer to reason about.

    No prompt caching exists here, so the stable/volatile split collapses
    into one concatenated message. On a free tier the saving would be moot
    anyway; the binding constraint is requests per minute, which B.4's
    batching already minimises.
    """

    provider = "groq"
    default_model = "openai/gpt-oss-120b"

    # The free tier allows 8,000 tokens per minute, and Groq counts
    # input + max_completion_tokens against that window when it admits a
    # request. Asking for 16,000 therefore exceeds the whole budget on its
    # own and is rejected with 413 as soon as any of the window is spent --
    # which is exactly what killed the first batch of a two-batch game.
    # 3,000 leaves room for ~2,000 tokens of input plus reasoning.
    # All three numbers below were measured against the free tier's 8,000
    # tokens/minute window, which Groq charges as input + max_completion_tokens.
    #
    # Reasoning effort is the binding constraint, not the answer length.
    # Measured output for the B.2 game pass on this model:
    #     low 647 | medium 1,402 | high >4,800 (truncated, request rejected)
    # With ~3,300 tokens of input there is no room to fund high reasoning, so
    # effort is capped at medium. That is a real quality difference from the
    # Anthropic path, and it is the trade for running free.
    max_effort = "medium"

    # Groq reserves `max_completion_tokens` against the per-minute window
    # whether or not they are used, so an over-generous reservation is paid
    # for twice: once on this request and once on the next one in the same
    # minute. Measured actual output, 7 comments over 2 calls: 2,346 tokens
    # total, ~1,170 per call. The old 3,000 reserved more than twice what
    # the pass has ever needed, and once the fact layer grew (refutation
    # lines, a wider square allowlist) the pair of calls stopped fitting in
    # the 8,000/minute window at all -- a 413 that silently cost six of
    # seven comments.
    #
    # 1,800 is ~1.5x the measured peak: enough headroom for a long batch,
    # small enough that two calls fit. Reasoning effort is medium, whose
    # measured need is ~1,400, so this still funds it.
    max_output_tokens = 1800
    game_pass_tokens = 2200          # the narrative is one longer answer
    move_pass_tokens = 1800

    # Five, not three. Without prompt caching SYSTEM_MOVE (~1,000 tokens) is
    # resent on every batch, so batching is the only way to stop paying for
    # it repeatedly. Measured for a 9-comment game:
    #     batch 3 -> 3 calls, ~10,450 tokens
    #     batch 5 -> 2 calls, ~9,170 tokens
    # Fewer, larger requests cost less in total while the per-request peak
    # (~5,600) still clears the 8,000/minute window. Note that a full game is
    # close to that whole window, so expect ~60-90s per game on the free tier
    # and do not run games back to back.
    default_batch_size = 5

    # 413 here means "larger than your rate limit allows", not "malformed",
    # so it is worth waiting out rather than failing the batch.
    RATE_LIMIT_RETRIES = 3

    def _key_from_env(self) -> Optional[str]:
        return os.getenv("LLM_API_KEY") or os.getenv("GROQ_API_KEY") or None

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not self._api_key:
            raise CommentaryError(
                "No Groq API key configured; set LLM_API_KEY to enable commentary"
            )
        try:
            from groq import Groq  # noqa: PLC0415 -- lazy
        except ImportError as exc:
            raise CommentaryError(
                "The `groq` package is not installed; pip install groq"
            ) from exc
        # max_retries=0: this class owns retrying, because it reads Groq's
        # rate-limit headers and waits the right amount. Leaving the SDK's
        # default of 2 in place meant every one of our attempts became up to
        # three HTTP requests, each *reserving* input + max_completion_tokens
        # against the 8,000/minute window -- so a single game pass could fire
        # twelve requests reserving ~6,300 each and starve the window it was
        # waiting on. The retries were causing the rate limiting.
        self._client = Groq(api_key=self._api_key, max_retries=0)
        return self._client

    def _request(
        self, *, system: str, stable: List[str], volatile: List[str],
        schema: Dict[str, Any], schema_name: str, effort: str,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        One strict-schema chat completion.

        `strict: True` gives constrained decoding, so the response is
        guaranteed to match the schema rather than merely attempting to --
        the same guarantee the Anthropic path gets from
        `output_config.format`, which is why both passes can assume shape.

        `reasoning_effort` takes low/medium/high on gpt-oss, mapping
        directly onto the effort levels the two passes already use.
        """
        import groq

        client = self._ensure_client()
        user = "\n\n".join([*stable, *volatile])

        kwargs: Dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_completion_tokens": max_tokens or self.max_output_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                },
            },
        }
        effort = self.clamp_effort(effort)
        if effort in self._EFFORT_ORDER:
            kwargs["reasoning_effort"] = effort

        response = None
        for attempt in range(self.RATE_LIMIT_RETRIES + 1):
            try:
                response = client.chat.completions.create(**kwargs)
                break
            except groq.AuthenticationError as exc:
                raise CommentaryError("Groq API key was rejected") from exc
            except groq.BadRequestError as exc:
                detail = str(exc)
                if "json_validate_failed" in detail:
                    # Strict decoding could not complete the object. Almost
                    # always the output cap, not the schema: the model is cut
                    # off mid-JSON and the whole request is rejected.
                    raise CommentaryError(
                        f"Groq could not complete the JSON within "
                        f"{max_tokens or self.max_output_tokens} output tokens "
                        "-- raise the budget for this pass, or shrink the batch"
                    ) from exc
                # Otherwise most likely a model without strict-schema support.
                raise CommentaryError(
                    f"Groq rejected the request: {detail[:200]}"
                ) from exc
            except (groq.RateLimitError, groq.APIStatusError) as exc:
                status = getattr(exc, "status_code", None)
                detail = str(exc)

                # A per-DAY cap will not clear in seconds, so retrying it just
                # burns time and more quota. Groq names the limit it hit in the
                # message ("tokens per day (TPD): Limit 200000, Used 197094")
                # along with how long to wait, so surface that verbatim rather
                # than guessing at a per-minute explanation.
                if _DAILY_LIMIT_RE.search(detail):
                    raise CommentaryError(
                        "Groq daily free quota exhausted. "
                        + _groq_limit_summary(detail)
                    ) from exc

                retryable = isinstance(exc, groq.RateLimitError) or status in (413, 429)
                if not retryable or attempt == self.RATE_LIMIT_RETRIES:
                    if status == 413:
                        raise CommentaryError(
                            "Groq rejected the request as larger than the rate "
                            "limit allows. Lower COMMENT_BATCH_SIZE or the "
                            "per-pass token budgets. " + _groq_limit_summary(detail)
                        ) from exc
                    raise CommentaryError(
                        f"Groq rate limit reached after {attempt + 1} attempt(s). "
                        + _groq_limit_summary(detail)
                    ) from exc
                wait = self._retry_after(exc, attempt, status=status)
                log.info("Groq %s; waiting %.1fs before retry %d/%d (%s)",
                         status or "rate limit", wait,
                         attempt + 1, self.RATE_LIMIT_RETRIES,
                         _groq_limit_summary(detail))
                time.sleep(wait)
            except groq.APIConnectionError as exc:
                raise CommentaryError("Could not reach the Groq API") from exc

        if response is None:
            raise CommentaryError("Groq request did not complete")

        u = getattr(response, "usage", None)
        if u is not None:
            self._note_usage(
                input_tokens=getattr(u, "prompt_tokens", 0),
                output_tokens=getattr(u, "completion_tokens", 0),
            )

        choice = response.choices[0]
        if choice.finish_reason == "length":
            raise CommentaryError("Response was truncated; lower the batch size")
        return self._parse_json(choice.message.content or "")


    # Floor and ceiling on a rate-limit wait. The floor matters: the
    # reset-tokens header reports when the *continuously* refilling window
    # will admit a small request, which can read as a fraction of a second
    # even when the minute's budget is spent. Trusting it directly burned
    # all three retries inside two seconds and then gave up.
    MIN_RETRY_WAIT = 2.0
    MAX_RETRY_WAIT = 65.0
    # A 413 is not "wait a moment", it is "this request does not fit in what
    # is left of the window". The reset-tokens header answers a different
    # question -- when a SMALL request would fit -- and on a 413 it reads as
    # a second or two, so the retries were spent inside ten seconds and the
    # batch was lost. Measured: three retries at 2.5s, 3.5s, 3.5s, all 413.
    # Only a real slice of the minute helps, so a 413 gets its own floor.
    MIN_413_WAIT = 22.0

    @classmethod
    def _retry_after(cls, exc: Any, attempt: int,
                     *, status: Optional[int] = None) -> float:
        """
        Seconds to wait before retrying a rate-limited request.

        `retry-after` is checked first and is authoritative on a 429;
        `x-ratelimit-reset-tokens` is a fallback for a 413, where Groq
        reports when the window will have room rather than when to retry.
        Anything shorter than MIN_RETRY_WAIT is raised to it, and the final
        fallback backs off linearly toward a full window.
        """
        headers = getattr(getattr(exc, "response", None), "headers", None) or {}
        for name in ("retry-after", "x-ratelimit-reset-tokens"):
            raw = headers.get(name)
            if not raw:
                continue
            seconds = cls._parse_duration(str(raw))
            if seconds is not None:
                floor = cls.MIN_413_WAIT if status == 413 else cls.MIN_RETRY_WAIT
                return min(max(seconds + 0.5, floor), cls.MAX_RETRY_WAIT)
        # No usable header: step toward a full window rather than hammering.
        floor = cls.MIN_413_WAIT if status == 413 else cls.MIN_RETRY_WAIT
        return min(max(20.0 * (attempt + 1), floor), cls.MAX_RETRY_WAIT)

    @staticmethod
    def _parse_duration(text: str) -> Optional[float]:
        """Groq durations come as "1.2s", "500ms", "2m30s" or bare seconds."""
        text = text.strip()
        if not text:
            return None
        if text.endswith("ms"):
            try:
                return float(text[:-2]) / 1000
            except ValueError:
                return None
        compound = re.fullmatch(r"(?:(\d+)m)?([\d.]+)s", text)
        if compound:
            minutes = float(compound.group(1) or 0)
            return minutes * 60 + float(compound.group(2))
        try:
            return float(text)
        except ValueError:
            return None


# "tokens per day (TPD)" / "requests per day (RPD)" in a Groq 429 body.
_DAILY_LIMIT_RE = re.compile(r"per day \((?:TPD|RPD)\)", re.IGNORECASE)
# The useful clauses of a Groq rate-limit message, matched separately. A
# single pattern with an optional leading group matched too early -- the
# optional part was skipped and the limit's name was lost.
_GROQ_KIND_RE = re.compile(r"on ([a-z ]+\((?:TPD|RPD|TPM|RPM)\))", re.IGNORECASE)
_GROQ_NUMBERS_RE = re.compile(
    r"Limit (?P<limit>\d+), Used (?P<used>\d+)(?:, Requested (?P<requested>\d+))?",
    re.IGNORECASE,
)
_GROQ_RETRY_RE = re.compile(r"try again in ([0-9hms.]+?)\.?(?:\s|$|'|\")", re.IGNORECASE)


def _groq_limit_summary(detail: str) -> str:
    """
    Groq's own numbers, in one readable clause.

    Its 429 body says exactly which limit was hit, how much is used and when
    to retry. Replacing that with a generic message threw away the only
    information that makes the error actionable.
    """
    bits = []
    kind = _GROQ_KIND_RE.search(detail)
    nums = _GROQ_NUMBERS_RE.search(detail)
    if nums:
        label = kind.group(1).strip() if kind else "limit"
        bits.append(f"{label}: used {nums.group('used')} of {nums.group('limit')}")
        if nums.group("requested"):
            bits.append(f"this request needed {nums.group('requested')}")
    retry = _GROQ_RETRY_RE.search(detail)
    if retry:
        bits.append(f"retry in {retry.group(1).rstrip('.')}")
    return "; ".join(bits) if bits else detail[:160]


PROVIDERS: Dict[str, Any] = {
    "anthropic": ClaudeCommentator,
    "groq": GroqCommentator,
}


def make_commentator(
    provider: Optional[str] = None,
    *,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    max_batches: int = MAX_BATCHES_PER_GAME,
) -> BaseCommentator:
    """
    Build the configured commentator.

    `provider` defaults to LLM_PROVIDER, then to groq when only GROQ_API_KEY
    is set, then anthropic. Resolving it here keeps the choice out of partE
    and out of every call site.
    """
    name = (provider or os.getenv("LLM_PROVIDER") or "").strip().lower()
    if not name:
        name = "anthropic" if os.getenv("ANTHROPIC_API_KEY") else DEFAULT_PROVIDER
    if name not in PROVIDERS:
        raise CommentaryError(
            f"Unknown LLM provider {name!r}; expected one of {sorted(PROVIDERS)}"
        )
    return PROVIDERS[name](api_key=api_key, model=model, max_batches=max_batches)



def _timeline_entry(f: MoveFacts) -> Dict[str, Any]:
    """
    The compact per-move view handed to B.2.

    Trimmed deliberately: the game pass needs shape, not every square. The
    full facts go to B.3 for the moves that actually get a comment, which
    keeps the one large call small.
    """
    entry: Dict[str, Any] = {
        "ply": f.ply,
        "move": f"{f.move_number}{'.' if f.color == 'white' else '...'} {f.san}",
        "color": f.color,
        "label": f.label,
        "phase": f.phase,
        "win_drop": f.win_drop,
        "cp_after": f.cp_after,
    }
    if f.material_delta:
        entry["material_delta"] = f.material_delta
    if f.newly_hanging:
        entry["left_loose"] = [h["piece"] + "@" + h["square"] for h in f.newly_hanging]
    if f.motifs:
        entry["motifs"] = sorted({m["motif"] for m in f.motifs})
    if f.mate_status_change:
        entry["mate"] = f.mate_status_change
    # time_flag is deliberately NOT sent. B.2 read it as a quality judgement
    # and described 12...Kd5 -- labelled `best` -- as "a wasted move". Clock
    # pressure belongs in A.4's report, not in a verdict on a move.
    if f.only_move and f.played_best:
        entry["found_only_move"] = True
    return entry


# ==========================================================================
# Orchestration


# ==========================================================================
def _unavailable_reason(
    comments: Sequence[Any], errors: Sequence[str], rejected: Sequence[Dict[str, Any]]
) -> Optional[str]:
    """
    Why there is no commentary, or None when there is some.

    A bare `available: False` with no reason is useless to whoever has to
    debug it -- and "every comment was rejected by the validator" is a very
    different situation from "the provider was down".
    """
    # Deduped. Three batches failing the same way rendered the identical
    # sentence three times, which reads as three different problems.
    unique = batch_budget.dedupe_errors(errors)
    if comments:
        return "; ".join(unique) or None
    if errors:
        return "; ".join(unique)
    if rejected:
        return (f"all {len(rejected)} generated comment(s) failed fact "
                f"validation and were withheld")
    return "no moves were selected for comment"


def generate_commentary(
    report: Dict[str, Any],
    *,
    commentator: Optional[ClaudeCommentator] = None,
    cache: Optional[CommentCache] = None,
    max_comments: int = MAX_COMMENTS_PER_GAME,
    batch_size: Optional[int] = None,
    progress: Optional[ProgressFn] = None,
    facts: Optional[GameFacts] = None,
) -> Commentary:
    """
    B.1 -> B.2 -> B.3 over one Part A report.

    The B.1 facts are returned whether or not commentary succeeds: they are
    deterministic and useful on their own, and this is the same degradation
    contract A.5 uses for missing weights.
    """

    def emit(stage: str, frac: float, partial: Optional[Dict] = None) -> None:
        if progress is not None:
            progress(stage, frac, partial)
    game_facts = facts or extract_game_facts(report)
    facts_dict = game_facts.to_dict()
    emit("facts", 0.1, {"facts": facts_dict})
    if commentator is None:
        commentator = make_commentator()
    if not commentator.configured:
        return Commentary(
            available=False, facts=facts_dict,
            reason="No LLM API key configured; B.1 facts only",
        )
    cache = cache if cache is not None else CommentCache()
    try:
        narrative = commentator.game_pass(game_facts)
    except CommentaryError as exc:
        log.warning("B.2 game pass failed: %s", exc)
        return Commentary(available=False, facts=facts_dict, reason=str(exc))
    emit("narrative", 0.3, {"narrative": narrative.to_dict()})
    plies = select_commentable(game_facts.moves, max_comments=max_comments)
    by_ply = {m.ply: m for m in game_facts.moves}
    selected = [by_ply[p] for p in plies if p in by_ply]
    critical = narrative.critical_plies()
    # Cache lookup first. Critical moments bypass it: those are exactly the
    # comments that should reference this game, and a position-local cached
    # comment cannot (see CommentCache).
    comments: Dict[int, MoveComment] = {}
    to_generate: List[MoveFacts] = []
    for f in selected:
        if f.ply in critical:
            to_generate.append(f)
            continue
        hit = cache.get(f, commentator.model)
        if hit is not None:
            comments[f.ply] = MoveComment(ply=f.ply, comment=hit, cached=True)
        else:
            to_generate.append(f)
    if comments:
        emit("comments", 0.35,
             {"comments": [c.to_dict() for c in comments.values()]})
    # The provider's own default when the caller did not choose: Groq's
    # per-minute token window wants smaller batches than Anthropic's.
    size = batch_size or commentator.default_batch_size
    batches = [to_generate[i:i + size] for i in range(0, len(to_generate), size)]
    if len(batches) > commentator.max_batches:
        log.info("B.4: %d batches needed, capping at %d",
                 len(batches), commentator.max_batches)
        batches = batches[: commentator.max_batches]
    errors: List[str] = []
    rejected: List[Dict[str, Any]] = []
    gaps: List[Dict[str, Any]] = []
    for i, batch in enumerate(batches):
        # Split and retry rather than drop. A truncated batch is not a
        # permanent failure, it is a batch that was too big for this budget,
        # and halving it almost always clears. The old code dropped the whole
        # batch on the first failure, so one unlucky batch cost five
        # comments -- three of them in a row cost fifteen, which is what the
        # user saw as "moves 22-39 annotated but never commented on".
        #
        # Non-truncation errors are NOT split: retrying a dead key or an
        # exhausted daily quota just burns what is left of it.
        salvage = batch_budget.run_with_split_retry(
            batch,
            lambda b: commentator.move_pass(b, narrative, game_facts.players),
            error_type=CommentaryError,
        )
        produced = salvage.produced
        if salvage.splits:
            log.info("B.3 batch %d/%d: split %d time(s), %d/%d comments saved",
                     i + 1, len(batches), salvage.splits,
                     len(produced), len(batch))
        if salvage.failed:
            reason = salvage.errors[-1] if salvage.errors else "unknown"
            log.warning("B.3 batch %d/%d: %d move(s) lost: %s",
                        i + 1, len(batches), len(salvage.failed), reason)
            errors.extend(salvage.errors)
            # One placeholder per move, not one string at the end of the
            # report. A visible gap is debuggable; silence in the move list
            # looks like the feature does not work.
            gaps.extend(batch_budget.gap_placeholders(
                [f.ply for f in salvage.failed], reason))
        for c in produced:
            if c.rejected_because:
                rejected.append({"ply": c.ply, "reasons": c.rejected_because,
                                 "text": c.comment})
                continue
            comments[c.ply] = c
            # Only cache a comment that passed validation.
            if c.ply not in critical:
                cache.put(by_ply[c.ply], commentator.model, c.comment)
        # B.4: stream to the UI as each batch lands.
        emit("comments", 0.35 + 0.6 * (i + 1) / len(batches),
             {"comments": [c.to_dict() for c in produced if not c.rejected_because]})
    emit("done", 1.0, None)
    ordered = [comments[p].to_dict() for p in sorted(comments)]
    return Commentary(
        available=bool(ordered),
        narrative=narrative.to_dict(),
        comments=ordered,
        facts=facts_dict,
        usage={
            "provider": commentator.provider,
            "model": commentator.model,
            "prompt_version": PROMPT_VERSION,
            "selected_moves": len(selected),
            "generated": len(to_generate),
            "batches": len(batches),
            "cache": cache.stats(),
            "tokens": dict(commentator.usage),
            "errors": batch_budget.dedupe_errors(errors),
            # Comments the validator refused. A non-empty list means the
            # timeline has gaps on purpose; the reasons say why.
            "rejected": rejected,
            # Moves the pipeline could not comment on at all, one entry each
            # with the reason. Distinct from `rejected`: those were written
            # and refused, these were never produced.
            "gaps": gaps,
        },
        reason=_unavailable_reason(ordered, errors, rejected),
    )
