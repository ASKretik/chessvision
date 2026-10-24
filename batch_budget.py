"""
B.4b -- Token budgeting and salvage for the move pass.

THE FAILURE
-----------
    "Response was truncated; lower the batch size; Response was truncated;
     lower the batch size; Groq could not complete the JSON within 1800
     output tokens -- raise the budget for this pass, or shrink the batch"

That string is three separate batch failures joined by `_unavailable_reason`.
Each one silently dropped its whole batch:

    except CommentaryError as exc:
        errors.append(str(exc))
        continue                      # <- ~5 moves of commentary, gone

which is why moves 22-39 carry labels but no comments.

WHY IT HAPPENS
--------------
Both providers use strict JSON-schema decoding. The model emits one JSON
object; it hits max_completion_tokens mid-object; strict decoding cannot
produce a valid object, so the request is rejected outright. You are billed
for the full output and receive nothing -- not even the comments that had
already completed.

The budget is not wildly wrong, it is wrong by a thin margin. Measured
against the comment lengths actually on screen (~67 tokens per move including
the JSON wrapper) and partB's own reasoning measurements for gpt-oss-120b
(low 647 / medium 1,402 / high >4,800 on the B.2 pass):

    batch  effort  reasoning  answer  total   vs 1800
        5  medium       1170     345   1515   fits, 16% headroom
        8  medium       1575     546   2121   truncates

16% headroom against a *non-deterministic* reasoning budget is not a margin.
And reasoning scales with how much there is to think about, so the batches
that fail are the ones covering the most eventful moves -- which is exactly
where the commentary was worth having. Moves 25-39 of the failing game hold
twelve annotated moves including four blunders.

THE FIX, IN FOUR PARTS
----------------------
1. Budget tokens as a function of batch size and effort, not a constant.
2. Cap comment length in the schema, so the answer half is bounded.
3. Drop the move pass to low effort -- it restates supplied facts, it does
   not need to reason. This is the single biggest win and it is one line.
4. On truncation, split the batch and retry instead of dropping it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, TypeVar

log = logging.getLogger("chessapp.commentary.budget")

T = TypeVar("T")


# ==========================================================================
# 1. A budget that scales
#
# Reasoning tokens are billed as output and are spent BEFORE any JSON is
# emitted, so they must be reserved explicitly rather than hoped for. These
# numbers come from partB's own measurement note on gpt-oss-120b, scaled for
# the smaller B.3 prompt and for the fact that reasoning grows with how many
# moves are in the batch.
# ==========================================================================
REASONING_BASE = {"low": 350, "medium": 900, "high": 2600}

# Tokens one comment costs, including its {"ply": N, "comment": "..."} wrapper.
# 67 is measured from the comments currently shipping (~55 tokens of prose).
# With MAX_COMMENT_CHARS enforced in the schema this drops to ~57.
TOKENS_PER_COMMENT = 67
TOKENS_PER_COMMENT_CAPPED = 57

# Characters, enforced by the JSON schema so strict decoding guarantees it.
# 240 is about 40 words -- enough for "this hangs the knight on f6; after Nxf6
# Black is a piece up", which is the target quality, and not enough for the
# three-clause essays that are blowing the budget.
MAX_COMMENT_CHARS = 240

# Multiplicative safety margin. 1.0 is what is shipping now and it is why
# failures are intermittent rather than absent.
HEADROOM = 1.35


def reasoning_reserve(effort: str, n_moves: int) -> int:
    """Reasoning tokens to set aside. Grows sublinearly with batch size."""
    base = REASONING_BASE.get(effort, REASONING_BASE["medium"])
    return int(base * (0.55 + 0.15 * n_moves))


def move_pass_budget(
    n_moves: int,
    effort: str = "low",
    *,
    capped: bool = True,
    ceiling: Optional[int] = None,
) -> int:
    """
    max_completion_tokens for a batch of `n_moves`.

    `ceiling` is the provider's hard limit -- on Groq's free tier the
    8,000/minute window is charged as input + max_completion_tokens, so this
    must stay small enough that input + budget clears it. Pass it and the
    result is clamped rather than silently exceeding the window.
    """
    per = TOKENS_PER_COMMENT_CAPPED if capped else TOKENS_PER_COMMENT
    answer = n_moves * per + 20                      # + object scaffolding
    total = int((reasoning_reserve(effort, n_moves) + answer) * HEADROOM)
    if ceiling is not None:
        total = min(total, ceiling)
    return total


def max_batch_for_budget(
    budget: int,
    effort: str = "low",
    *,
    capped: bool = True,
    hard_max: int = 8,
) -> int:
    """
    Largest batch that fits a fixed budget. The inverse of the above, for when
    the provider's window pins the budget and the batch has to give instead.
    """
    for n in range(hard_max, 0, -1):
        if move_pass_budget(n, effort, capped=capped) <= budget:
            return n
    return 1


# ==========================================================================
# 2. Schema patch -- bound the answer half
#
# Without maxLength the model decides how long a comment is, which makes the
# budget uncomputable. With it, strict decoding enforces the bound, so the
# answer half of the budget becomes a known quantity.
# ==========================================================================
def cap_comment_length(schema: Dict[str, Any],
                       max_chars: int = MAX_COMMENT_CHARS) -> Dict[str, Any]:
    """Add maxLength to the comment field of MOVE_SCHEMA. Returns a copy."""
    import copy

    out = copy.deepcopy(schema)
    try:
        item = out["properties"]["comments"]["items"]["properties"]
        item["comment"]["maxLength"] = max_chars
    except (KeyError, TypeError):
        log.warning("cap_comment_length: unexpected schema shape, left unchanged")
    return out


# ==========================================================================
# 3. Salvage -- split and retry instead of dropping
#
# A truncated batch is not a permanent failure, it is a batch that was too
# big for this budget. Halving it almost always clears, and a single move on
# its own essentially always does. The current code drops the batch on the
# first failure, so one unlucky batch costs five comments.
# ==========================================================================
TRUNCATION_MARKERS = (
    "truncated", "could not complete the json", "json_validate_failed",
    "max_tokens", "larger than the rate",
)


def is_truncation(exc: Exception) -> bool:
    """Distinguish 'too big' from 'broken'. Only the former is worth splitting."""
    text = str(exc).lower()
    return any(m in text for m in TRUNCATION_MARKERS)


@dataclass
class SalvageResult:
    produced: List[Any]
    failed: List[Any]            # the items no sub-batch could complete
    errors: List[str]
    calls: int = 0
    splits: int = 0


def run_with_split_retry(
    batch: Sequence[T],
    call: Callable[[Sequence[T]], List[Any]],
    *,
    min_size: int = 1,
    max_depth: int = 3,
    error_type: type = Exception,
) -> SalvageResult:
    """
    Call `call(batch)`; on a truncation failure, split in half and recurse.

    A 5-move batch that truncates becomes 3 + 2. If the 3 truncates it becomes
    2 + 1. Everything that can complete, does -- instead of losing all five.

    Non-truncation errors (auth, daily quota, a dead provider) are NOT split:
    retrying those just burns quota, so they propagate as a single failure for
    the whole batch.
    """
    res = SalvageResult(produced=[], failed=[], errors=[])

    def attempt(items: Sequence[T], depth: int) -> None:
        if not items:
            return
        try:
            res.calls += 1
            res.produced.extend(call(items))
            return
        except error_type as exc:          # noqa: BLE001 -- caller names the type
            if not is_truncation(exc) or len(items) <= min_size or depth >= max_depth:
                res.failed.extend(items)
                res.errors.append(str(exc))
                return
            log.info("B.3 batch of %d truncated; splitting (depth %d)",
                     len(items), depth)
            res.splits += 1
            mid = len(items) // 2
            attempt(items[:mid], depth + 1)
            attempt(items[mid:], depth + 1)

    attempt(batch, 0)
    return res


# ==========================================================================
# 4. Reporting -- make the gap visible per move, not as one string at the end
#
# A dropped batch currently shows as nothing in the move list and one long
# error at the bottom of the report. Whoever reads it cannot tell which moves
# are missing commentary or why.
# ==========================================================================
def gap_placeholders(failed_plies: Sequence[int], reason: str) -> List[Dict[str, Any]]:
    """One entry per move the pipeline could not comment on."""
    short = ("the batch exceeded the model's output budget"
             if any(m in reason.lower() for m in TRUNCATION_MARKERS)
             else reason[:120])
    return [{"ply": p, "comment": None, "unavailable": short} for p in failed_plies]


def dedupe_errors(errors: Sequence[str]) -> List[str]:
    """
    Collapse repeats, keeping order.

    Three batches failing the same way currently render as the same sentence
    three times, which reads like three different problems.
    """
    seen: Dict[str, int] = {}
    for e in errors:
        seen[e] = seen.get(e, 0) + 1
    return [f"{e} (x{n})" if n > 1 else e for e, n in seen.items()]
