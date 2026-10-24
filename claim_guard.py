"""
B.1b -- Material arithmetic along a line, and a guard on what the LLM may claim.

THE BUG THIS FIXES
------------------
The comment on 14...Ng6 read:

    "White's refutation Ng5 forces Black to answer f4, after which White
     captures on d5 and Black recaptures, leaving material even. Engine
     analysis shows that Black's best continuation was f4, leading to
     Bxf4 Bxf4 gxf4 Rxf4, which would have won the exchange."

Both material claims are false, and they are false in the same way:

    f4 Bxf4 Bxf4 gxf4 Rxf4   ->  Black gives pawn+bishop, takes bishop+pawn
                                 = DEAD LEVEL, not "the exchange" (no rook
                                 was captured by anyone)
    Ng5 f4 Nxd5 cxd5 Bxd5+   ->  White gives a knight, takes two pawns
                                 = White is a pawn down, not "even"
                                 (and -1 is exactly the engine's -0.9)

The model was handed a PV -- a list of moves -- and asked why one line was
better than another. Material outcome is not derivable from a move list
without playing it out, which the model cannot do reliably, so it filled the
gap with a chess cliche that sounded right. The engine's own eval already
contradicted it.

THE RULE
--------
The model never does material arithmetic. It is computed here, handed over as
a finished phrase, and any comment asserting a different material outcome is
rejected before it reaches the user.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import chess

PIECE_VALUE = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3,
               chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 0}
MINORS = {chess.KNIGHT, chess.BISHOP}
PIECE_NAME = {chess.PAWN: "pawn", chess.KNIGHT: "knight", chess.BISHOP: "bishop",
              chess.ROOK: "rook", chess.QUEEN: "queen", chess.KING: "king"}


# ==========================================================================
# Walking a line
# ==========================================================================
@dataclass
class LineMaterial:
    """What a principal variation actually does to the material count."""

    pv_san: List[str] = field(default_factory=list)
    gained: List[int] = field(default_factory=list)    # piece types mover won
    lost: List[int] = field(default_factory=list)      # piece types mover lost
    delta: int = 0                                     # pawns, mover's POV
    description: str = ""                              # the phrase the LLM must use
    ends_in_mate: bool = False
    gives_check: bool = False
    truncated_at: Optional[int] = None                 # PV went illegal here

    @property
    def is_quiet(self) -> bool:
        """No captures at all -- the line is positional, say so."""
        return not self.gained and not self.lost


def describe_material(delta: int, gained: Sequence[int], lost: Sequence[int]) -> str:
    """
    The phrase the comment is allowed to use.

    "The exchange" is detected STRUCTURALLY -- a rook won for a minor given --
    not from the number 2. Two pawns is also +2 and is not the exchange, and
    that conflation is how the Ng6 comment went wrong in the first place.
    """
    if delta == 0:
        if not gained and not lost:
            return "no material changes hands"
        return "material is level at the end of the line"

    if delta == 2 and chess.ROOK in gained and any(p in MINORS for p in lost):
        return "wins the exchange"
    if delta == -2 and chess.ROOK in lost and any(p in MINORS for p in gained):
        return "loses the exchange"

    side = "wins" if delta > 0 else "loses"
    n = abs(delta)
    won, given = (gained, lost) if delta > 0 else (lost, gained)

    # Name a piece ONLY when the capture list proves it, never from the
    # number. This is the same conflation the exchange test above exists to
    # avoid, and it bites just as hard elsewhere: in the Fried Liver, Black
    # wins a knight for a pawn, and a net of +2 rendered as "wins two pawns"
    # tells the reader something that did not happen. A net figure is always
    # safe to state; a piece identity is not.
    if not given and len(won) == 1 and PIECE_VALUE[won[0]] == n:
        return f"{side} a {PIECE_NAME[won[0]]}"
    if not given and n == 1 and all(p == chess.PAWN for p in won) and len(won) == 1:
        return f"{side} a pawn"
    if not given and n == 2 and len(won) == 2 and all(p == chess.PAWN for p in won):
        return f"{side} two pawns"

    ahead = "ahead" if delta > 0 else "behind"
    if n == 1:
        return f"ends up a pawn {ahead}"
    return f"ends up {n} pawns {ahead}"


def walk_line(board: chess.Board, pv: Sequence) -> LineMaterial:
    """
    Play a PV out and account for every capture and promotion.

    Accepts UCI strings, SAN strings or chess.Move. Stops cleanly at the first
    illegal move and records where -- a truncated PV means the line came from
    a different position, which is a bug worth surfacing rather than hiding.
    """
    b = board.copy(stack=False)
    mover = b.turn
    out = LineMaterial()
    promo_delta = 0

    for i, raw in enumerate(pv):
        try:
            if isinstance(raw, chess.Move):
                mv = raw
            else:
                text = str(raw).strip()
                try:
                    mv = chess.Move.from_uci(text)
                    if mv not in b.legal_moves:
                        mv = b.parse_san(text)
                except ValueError:
                    mv = b.parse_san(text)
        except ValueError:
            out.truncated_at = i
            break
        if mv not in b.legal_moves:
            out.truncated_at = i
            break

        out.pv_san.append(b.san(mv))
        if b.is_capture(mv):
            victim = (chess.PAWN if b.is_en_passant(mv)
                      else b.piece_at(mv.to_square).piece_type)
            (out.gained if b.turn == mover else out.lost).append(victim)
        if mv.promotion:
            bonus = PIECE_VALUE[mv.promotion] - PIECE_VALUE[chess.PAWN]
            promo_delta += bonus if b.turn == mover else -bonus
        b.push(mv)

    out.delta = (sum(PIECE_VALUE[p] for p in out.gained)
                 - sum(PIECE_VALUE[p] for p in out.lost) + promo_delta)
    out.description = describe_material(out.delta, out.gained, out.lost)
    out.ends_in_mate = b.is_checkmate()
    out.gives_check = b.is_check()
    return out


# ==========================================================================
# Positional delta -- why a LEVEL line can still be better
#
# When material is equal at the end of the PV, the eval difference is
# positional, and a comment that cannot name the positional reason has
# nothing true to say. These are the cheap features worth diffing; they turn
# "would have won the exchange" (false) into "material is level, but the rook
# lands on the open f-file and White's king cover is gone" (true).
# ==========================================================================
@dataclass
class PositionalDelta:
    mobility: int = 0                 # mover's legal-move count, end minus start
    opp_king_attackers: int = 0       # mover's pieces bearing on squares near enemy king
    own_king_attackers: int = 0
    open_files_seized: List[str] = field(default_factory=list)
    pieces_improved: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


def _king_zone(board: chess.Board, color: chess.Color) -> List[int]:
    ksq = board.king(color)
    if ksq is None:
        return []
    return [s for s in chess.SQUARES if chess.square_distance(s, ksq) <= 1]


def _attackers_on_zone(board: chess.Board, by: chess.Color, zone: Sequence[int]) -> int:
    return sum(len(board.attackers(by, sq)) for sq in zone)


def positional_delta(board: chess.Board, pv: Sequence) -> PositionalDelta:
    """Diff a handful of cheap positional features across the line."""
    start = board.copy(stack=False)
    mover = start.turn
    end = board.copy(stack=False)
    for raw in pv:
        try:
            mv = raw if isinstance(raw, chess.Move) else (
                chess.Move.from_uci(str(raw)) if len(str(raw)) in (4, 5)
                and chess.Move.from_uci(str(raw)) in end.legal_moves
                else end.parse_san(str(raw)))
        except ValueError:
            break
        if mv not in end.legal_moves:
            break
        end.push(mv)

    d = PositionalDelta()
    s_mob = sum(1 for _ in start.legal_moves) if start.turn == mover else 0
    e = end.copy(stack=False)
    e.turn = mover
    d.mobility = sum(1 for _ in e.legal_moves) - (s_mob or 0)

    opp = not mover
    d.opp_king_attackers = (_attackers_on_zone(end, mover, _king_zone(end, opp))
                            - _attackers_on_zone(start, mover, _king_zone(start, opp)))
    d.own_king_attackers = (_attackers_on_zone(end, opp, _king_zone(end, mover))
                            - _attackers_on_zone(start, opp, _king_zone(start, mover)))

    for f in range(8):
        name = chess.FILE_NAMES[f]
        squares = [chess.square(f, r) for r in range(8)]
        start_pawns = any(start.piece_at(s) and start.piece_at(s).piece_type == chess.PAWN
                          for s in squares)
        end_pawns = any(end.piece_at(s) and end.piece_at(s).piece_type == chess.PAWN
                        for s in squares)
        if start_pawns and not end_pawns:
            d.open_files_seized.append(name)

    # King shelter: pawns of a colour on the king's file and its neighbours,
    # within three ranks of the king. A FULLY open file is rare and misses the
    # case that actually matters -- the Ng6 line strips White's g3 pawn while
    # Black's g7 pawn keeps the g-file nominally closed, so a fully-open test
    # reports nothing where a human would say the king got drafty.
    for colour, tag in ((mover, "the mover's"), (opp, "the opponent's")):
        before = _king_shelter(start, colour)
        after = _king_shelter(end, colour)
        if after < before:
            d.notes.append(
                f"{before - after} pawn(s) leave {tag} king shelter "
                f"({before} -> {after})")

    if d.opp_king_attackers > 0:
        d.notes.append(f"{d.opp_king_attackers} more attacker(s) near the enemy king")
    if d.own_king_attackers > 0:
        d.notes.append(f"{d.own_king_attackers} more attacker(s) near the mover's own king")
    for f in d.open_files_seized:
        d.notes.append(f"the {f}-file opens")
    return d


def _king_shelter(board: chess.Board, colour: chess.Color) -> int:
    """Friendly pawns on the king's file and the two adjacent files, nearby."""
    ksq = board.king(colour)
    if ksq is None:
        return 0
    kf, kr = chess.square_file(ksq), chess.square_rank(ksq)
    n = 0
    for f in range(max(kf - 1, 0), min(kf + 2, 8)):
        for r in range(8):
            if abs(r - kr) > 3:
                continue
            p = board.piece_at(chess.square(f, r))
            if p and p.piece_type == chess.PAWN and p.color == colour:
                n += 1
    return n


# ==========================================================================
# The guard
#
# A lexicon of material claims and the delta each one asserts. Run over the
# generated comment; any claim that contradicts the computed delta fails.
# This is the net that catches the CLASS of error, not just this instance --
# it fires whether the fabrication came from a hallucination, a stale fact,
# or a line attributed to the wrong position.
# ==========================================================================
# Every verb needs all its inflections. The shipped Ng6 comment said "would
# have WON the exchange", and a lexicon that only matched "wins/win" let it
# through -- which is how a guard gives false assurance. Tense is the most
# likely gap in any hand-written lexicon, so the verb groups are built once
# and reused rather than spelled out per claim.
_WIN = r"(?:win|wins|won|winning|gain|gains|gained|gaining)"
_LOSE = r"(?:lose|loses|lost|losing|drop|drops|dropped|dropping|hang|hangs|hung|hanging|give[sn]?\s+up|giving\s+up)"
_UP = r"(?:up|ahead\s+by)"
_DOWN = r"(?:down|behind\s+by)"

# (pattern, predicate on the computed delta, human description of the rule).
# Predicates are callables, not strings fed to eval(): a validator that
# eval()s its own rule table is one bad merge away from being an execution
# sink, and the table is static anyway so there is nothing to gain.
MATERIAL_CLAIMS: List[Tuple[str, Callable[[int], bool], str]] = [
    (rf"\b{_WIN}\s+(?:the\s+)?exchange\b",  lambda d: d == 2,  "delta == +2"),
    (rf"\b{_UP}\s+the\s+exchange\b",        lambda d: d >= 2,  "delta >= +2"),
    (rf"\b{_LOSE}\s+(?:the\s+)?exchange\b", lambda d: d == -2, "delta == -2"),
    (rf"\b{_DOWN}\s+the\s+exchange\b",      lambda d: d <= -2, "delta <= -2"),
    (rf"\b{_WIN}\s+a\s+pawn\b",             lambda d: d == 1,  "delta == +1"),
    (rf"\b{_WIN}\s+(?:two|2)\s+pawns?\b",   lambda d: d == 2,  "delta == +2"),
    (rf"\b{_LOSE}\s+a\s+pawn\b",            lambda d: d == -1, "delta == -1"),
    (rf"\b(?:{_WIN}|{_UP})\s+a\s+(?:piece|knight|bishop|minor)\b",
     lambda d: d >= 3,  "delta >= +3"),
    (rf"\b(?:{_LOSE}|{_DOWN})\s+a\s+(?:piece|knight|bishop|minor)\b",
     lambda d: d <= -3, "delta <= -3"),
    (rf"\b{_WIN}\s+a\s+rook\b",             lambda d: d >= 5,  "delta >= +5"),
    (rf"\b{_LOSE}\s+a\s+rook\b",            lambda d: d <= -5, "delta <= -5"),
    (rf"\b{_WIN}\s+(?:the\s+)?queen\b",     lambda d: d >= 8,  "delta >= +8"),
    (rf"\b{_LOSE}\s+(?:the\s+)?queen\b",    lambda d: d <= -8, "delta <= -8"),
    (r"\bmaterial\s+(?:is\s+|remains\s+|stays\s+)?(?:even|level|equal|unchanged)\b",
     lambda d: d == 0, "delta == 0"),
    (r"\bleav(?:es|ing)\s+material\s+(?:even|level|equal)\b",
     lambda d: d == 0, "delta == 0"),
    (r"\b(?:even|level|equal)\s+material\b", lambda d: d == 0, "delta == 0"),
    (r"\bdecisive\s+material\b",            lambda d: abs(d) >= 3, "|delta| >= 3"),
    (rf"\b{_WIN}\s+material\b",             lambda d: d >= 1,  "delta >= +1"),
    (rf"\b{_LOSE}\s+material\b",            lambda d: d <= -1, "delta <= -1"),
    (r"\bmaterial\s+advantage\b",           lambda d: abs(d) >= 1, "|delta| >= 1"),
    # The neutral net phrasing describe_material now emits when the capture
    # list cannot prove a piece identity. Without these the guard would not
    # check its own preferred wording.
    (r"\b(?:a|1)\s+pawn\s+ahead\b",        lambda d: d == 1,  "delta == +1"),
    (r"\b(?:a|1)\s+pawn\s+behind\b",       lambda d: d == -1, "delta == -1"),
    (r"\b(\d+)\s+pawns\s+ahead\b",         lambda d: d >= 1,  "delta >= +1"),
    (r"\b(\d+)\s+pawns\s+behind\b",        lambda d: d <= -1, "delta <= -1"),
    (rf"\b{_WIN}\s+a\s+knight\b",           lambda d: d >= 3,  "delta >= +3"),
    (rf"\b{_LOSE}\s+a\s+knight\b",          lambda d: d <= -3, "delta <= -3"),
    (rf"\b{_WIN}\s+a\s+bishop\b",           lambda d: d >= 3,  "delta >= +3"),
    (rf"\b{_LOSE}\s+a\s+bishop\b",          lambda d: d <= -3, "delta <= -3"),
]

# Evaluative phrases with no computable referent. Allowed only when the fact
# set supplies something concrete to attach them to.
UNGROUNDED = [
    r"\bdecisive\s+advantage\b", r"\bkeeps?\s+(?:up\s+)?the\s+pressure\b",
    r"\bmaintain(?:s|ing)?\s+(?:the\s+)?initiative\b",
    r"\bstrong\s+position\b", r"\bclearly\s+better\b", r"\bwinning\s+position\b",
]


@dataclass
class GuardResult:
    ok: bool
    violations: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def check_comment(
    comment: str,
    material: LineMaterial,
    *,
    strict_ungrounded: bool = True,
) -> GuardResult:
    """
    Reject a comment whose material claims contradict the computed line.

    Returns violations (hard fail -- regenerate or fall back to the terse
    template) and warnings (ungrounded evaluative language).
    """
    text = " " + re.sub(r"\s+", " ", comment).lower() + " "
    delta = material.delta
    res = GuardResult(ok=True)

    for pattern, predicate, rule in MATERIAL_CLAIMS:
        m = re.search(pattern, text)
        if m and not predicate(delta):
            res.ok = False
            res.violations.append(
                f'comment asserts "{m.group(0).strip()}" (requires {rule}) '
                f'but the line computes delta={delta:+d} '
                f'("{material.description}")'
            )
    if strict_ungrounded:
        for pattern in UNGROUNDED:
            m = re.search(pattern, text)
            if m:
                res.warnings.append(
                    f'ungrounded phrase "{m.group(0).strip()}" -- '
                    f"attach it to a computed fact or drop it"
                )
    if material.truncated_at is not None:
        res.ok = False
        res.violations.append(
            f"PV went illegal at index {material.truncated_at} -- "
            f"the line does not belong to this position"
        )
    return res


# ==========================================================================
# The facts the prompt should carry
# ==========================================================================
def line_facts(board: chess.Board, pv: Sequence, *, label: str) -> Dict[str, object]:
    """
    One line reduced to facts a model can only restate, not invent.

    `material_phrase` is the ONLY material language the comment may use. The
    prompt rule is: use it verbatim, and make no other claim about material.
    """
    mat = walk_line(board, pv)
    out: Dict[str, object] = {
        "label": label,
        "pv_san": mat.pv_san,
        "material_delta_pawns": mat.delta,
        "material_phrase": mat.description,
        "ends_in_mate": mat.ends_in_mate,
        "is_quiet_line": mat.is_quiet,
        # Whether the line is STRUCTURALLY an exchange -- a rook won for a
        # minor given. The claim lexicon can only test the net delta, and a
        # net of +2 is equally "two pawns" and "a knight for a pawn", so a
        # delta-only guard passes "wins the exchange" on any +2 line. That
        # is the same conflation describe_material exists to avoid, and a
        # guard that misses the very phrase it was built for is worse than
        # no guard. Callers check this flag for exchange claims.
        "exchange_won": bool(mat.delta == 2 and chess.ROOK in mat.gained
                             and any(p in MINORS for p in mat.lost)),
        "exchange_lost": bool(mat.delta == -2 and chess.ROOK in mat.lost
                              and any(p in MINORS for p in mat.gained)),
        # Carried out of walk_line deliberately. A truncated line has a delta
        # of 0 because nothing was played, and a caller that cannot see the
        # truncation would read that as "material is level" -- turning a
        # broken line into a confident false statement. Callers must treat a
        # non-None value as "this line is unusable", not as a level one.
        "truncated_at": mat.truncated_at,
    }
    if mat.delta == 0 and not mat.ends_in_mate:
        # Level material: the reason must be positional or there is no reason,
        # and a comment with no reason should be terse rather than invented.
        pd = positional_delta(board, pv)
        out["positional_notes"] = pd.notes
        out["mobility_delta"] = pd.mobility
        out["has_concrete_reason"] = bool(pd.notes)
    else:
        out["has_concrete_reason"] = True
    return out
