"""
A.5b -- the error profile.

Blunders, mistakes, inaccuracies, centipawn loss and engine agreement, all
measured PER DECISION rather than per move.

THE DENOMINATOR IS THE WHOLE POINT
----------------------------------
Error counts per MOVE are dominated by game length and by how forcing the
game was: a 70-move endgame grind and a 25-move miniature are not comparable
at the same strength. Per DECISION they are. A move is a decision only when
the player could actually have gone wrong:

  - more than one legal move
  - not a forced recapture or an only-move (finding the only move is weak
    evidence of strength, and missing it is weak evidence of weakness --
    both are mostly tactics luck)
  - not still in book: that is preparation, not strength
  - not in a position already decided, where a 40cp loss means nothing

Measured on a real game that gives 24-28 decisions out of 43 moves; on a
theory-heavy miniature it reports 1 of 11, which is the correct answer
rather than a bug.

WHAT THIS DELIBERATELY NO LONGER DOES
-------------------------------------
It used to score the profile as a likelihood over rating bands and report an
Elo. That half is gone. The calibration table it needed was never fitted --
its placeholder values were not even mutually consistent (an ACPL of 110
implied 16 blunders per 100 against a table value of 9) -- so the estimator
refused to report anything and the user saw a refusal where a number should
have been.

What is left is a MEASUREMENT, and that is why it survived the cut.
"4.2 blunders per 100 real decisions, ACPL 62, engine-best agreement 34%" is
a fact about the game that needs no calibration to be true. An Elo derived
from it needed one, did not have it, and was removed along with
`performance_rating` -- see decisions.md 3a2.8.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional, Sequence

# ==========================================================================
# What counts as a decision
#
# The denominator is the single most important choice in this module. Error
# counts per MOVE are dominated by game length and by how forcing the game
# was: a 70-move endgame grind and a 25-move miniature are not comparable at
# the same strength. Error counts per DECISION are.
#
# A move is a decision when the player could actually have gone wrong:
#   - more than one legal move
#   - not a forced recapture or an only-move the engine scores far above all
#     alternatives (finding the only move is weak evidence of strength, and
#     missing it is weak evidence of weakness -- both are mostly tactics luck)
#   - not still in book (that is preparation, not strength)
#   - not in a position already decided, where a 40cp loss means nothing
# ==========================================================================
DECIDED_CP = 600          # |eval| beyond which accuracy stops being informative
MIN_DECISIONS = 8         # below this, report the profile but refuse a rating


@dataclass
class MoveSignal:
    """The per-move inputs this module needs. All already on partA.MoveRecord."""

    cp_loss: int
    win_drop: float
    label: str
    complexity: float
    only_move: bool
    played_best: bool
    legal_moves: int
    cp_before: int            # White POV
    phase: str
    in_book: bool = False
    rank_of_played: Optional[int] = None   # 1 = engine's first choice

    def is_decision(self) -> bool:
        if self.legal_moves <= 1 or self.in_book or self.only_move:
            return False
        return abs(self.cp_before) <= DECIDED_CP


# ==========================================================================
# The profile
# ==========================================================================
PHASE_WEIGHT = {"opening": 0.6, "middlegame": 1.0, "endgame": 0.9}
CPL_CAP = 300             # one catastrophe should not define the game


@dataclass
class ErrorProfile:
    """Everything the rating estimate is built from, in reportable form."""

    moves: int
    decisions: int

    # Per 100 decisions, so the numbers are comparable across game lengths.
    blunder_rate: float
    mistake_rate: float
    inaccuracy_rate: float

    acpl: float               # winsorized mean over decisions
    cpl_median: float
    cpl_p90: float            # how bad the bad moves are
    cpl_volatility: float

    top1_match: float         # fraction of decisions matching engine's first choice
    top3_match: float

    # Severity-weighted, continuous. Counts bucket a 29-point drop and a
    # 31-point drop into different labels though they are near-identical;
    # this keeps the information the labels throw away.
    weighted_error: float

    phase_acpl: Dict[str, Optional[float]] = field(default_factory=dict)
    phase_weighted_acpl: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def feature_vector(self) -> Dict[str, float]:
        """The subset the likelihood actually scores."""
        return {
            "acpl": self.acpl,
            "blunder_rate": self.blunder_rate,
            "mistake_rate": self.mistake_rate,
            "inaccuracy_rate": self.inaccuracy_rate,
            "cpl_p90": self.cpl_p90,
            "top1_match": self.top1_match,
            "weighted_error": self.weighted_error,
        }


def _percentile(xs: Sequence[float], q: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    i = min(int(q * (len(s) - 1) + 0.5), len(s) - 1)
    return float(s[i])


def _stdev(xs: Sequence[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def build_profile(signals: Sequence[MoveSignal]) -> ErrorProfile:
    """Reduce one player's moves to the profile the estimator scores."""
    decisions = [s for s in signals if s.is_decision()]
    n = len(decisions)
    if n == 0:
        return ErrorProfile(
            moves=len(signals), decisions=0,
            blunder_rate=0.0, mistake_rate=0.0, inaccuracy_rate=0.0,
            acpl=0.0, cpl_median=0.0, cpl_p90=0.0, cpl_volatility=0.0,
            top1_match=0.0, top3_match=0.0, weighted_error=0.0,
        )

    losses = [min(s.cp_loss, CPL_CAP) for s in decisions]
    per100 = 100.0 / n

    blunders = sum(1 for s in decisions if s.label == "blunder")
    mistakes = sum(1 for s in decisions if s.label == "mistake")
    inacc = sum(1 for s in decisions if s.label == "inaccuracy")

    # Continuous severity: each move contributes its win% drop above the
    # inaccuracy floor, so near-threshold moves are not rounded away.
    weighted = sum(max(s.win_drop - 10.0, 0.0) for s in decisions) / n

    phase_acpl: Dict[str, Optional[float]] = {}
    weighted_num = weighted_den = 0.0
    for phase in ("opening", "middlegame", "endgame"):
        rs = [min(s.cp_loss, CPL_CAP) for s in decisions if s.phase == phase]
        phase_acpl[phase] = round(sum(rs) / len(rs), 1) if rs else None
        if rs:
            w = PHASE_WEIGHT[phase]
            weighted_num += w * sum(rs) / len(rs) * len(rs)
            weighted_den += w * len(rs)

    ranked = [s for s in decisions if s.rank_of_played is not None]
    return ErrorProfile(
        moves=len(signals),
        decisions=n,
        blunder_rate=round(blunders * per100, 2),
        mistake_rate=round(mistakes * per100, 2),
        inaccuracy_rate=round(inacc * per100, 2),
        acpl=round(sum(losses) / n, 1),
        cpl_median=round(_percentile(losses, 0.5), 1),
        cpl_p90=round(_percentile(losses, 0.9), 1),
        cpl_volatility=round(_stdev(losses), 1),
        top1_match=round(sum(1 for s in decisions if s.played_best) / n, 3),
        top3_match=(round(sum(1 for s in ranked if (s.rank_of_played or 9) <= 3)
                          / len(ranked), 3) if ranked else 0.0),
        weighted_error=round(weighted, 2),
        phase_acpl=phase_acpl,
        phase_weighted_acpl=(round(weighted_num / weighted_den, 1)
                             if weighted_den else None),
    )
