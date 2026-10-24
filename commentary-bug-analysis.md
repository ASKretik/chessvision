# Why the Na4 comment is wrong, and how to fix it

Your comment engine isn't hallucinating. It faithfully reported a fact that `partB.py`
genuinely produced — which is worse news, but much easier to fix.

## Why that exact sentence appeared

Trace `find_pins_and_skewers` on the position after Na4. White queen on d3, direction
(+1,+1): e4 empty, f5 empty, **g6 black knight** → first occupied. Continue the ray:
**h7 black pawn** → second occupied. Both enemy pieces, so:

```python
near_v = PIECE_VALUE[KNIGHT] = 3
far_v  = PIECE_VALUE[PAWN]   = 1
near_v > far_v  →  motif = "skewer"
```

The detector emitted `{attacker: white queen on d3, front_piece: black knight on g6,
piece_won_behind: black pawn on h7}`. The LLM rendered that into English correctly.
Your validator passed it because the queen really is on d3 and the knight really is
on g6 — both square claims are true.

So three separate bugs, all in B.1, none in the model.

## Bug 1 — motifs aren't diffed, so old ones are reported as new

`find_discovered_attacks` compares `before` vs `after`. `find_pins_and_skewers` does not:

```python
motifs += [m for m in find_pins_and_skewers(after, mover)]
```

That queen was on d3 before Na4 and after it, aiming down the same diagonal.
**Na4 created nothing.** The move was a knight on the opposite wing, and your fact
extractor attributed a pre-existing geometry to it. Then the model wrote "now skewers,"
because facts handed to it under a move are implicitly that move's doing.

This also explains move 20's comment — same queen, same diagonal, reported again as new.

**Fix:** run the detector on `before` and `after`, emit only the set difference. Key
motifs on their participant squares so a motif that merely shifted isn't counted as
created.

## Bug 2 — no materiality filter

A knight "skewered" against a pawn is not a tactic. Worse, h7 is defended by the king
on g8, so the threatened capture wins nothing at all, and Qxg6 just loses a queen to
hxg6.

**Fix,** two conditions before emitting a skewer:
- the piece behind is worth meaningfully more than a pawn (or the exchange nets material)
- the front piece is actually capturable at a profit — you already have the
  attacker/defender logic in `hanging_squares`, so reuse it

Same treatment for relative pins. Your own comment in the code notes one game produced
seven pins in fourteen moves; that's the same noise problem.

## Bug 3 — the real reason the move is bad is never extracted

This is the big one, and the data is already sitting in memory.

In `analyse_game`, `best` comes from `evals[i]` — the position *before* the move. So
`best_move` means "what you should have played instead." You never pass **what the
opponent does to you now**, which is the actual explanation for almost every bad move.

`evals[i+1].best.pv` is Bxg3, hxg3, Nxg3 — the line that wins the exchange, and the
reason Na4 drops 1.4 pawns. It's computed, stored, and thrown away.

**Fix:** add `refutation_move` and `refutation_pv` to `MoveRecord` from `evals[i+1]`,
carry them into `MoveFacts`, add their squares to `squares_in_facts`, and make the
prompt rule unconditional: *for any faulted move, the comment must lead with the
refutation.* The target output becomes "This loses the exchange to Bxg3 — hxg3 Nxg3
forks the rook on f1."

That single change costs no extra engine time and would have fixed this comment on its
own.

## Two cheap guards worth adding

**Valence check.** A move labelled mistake/blunder whose comment describes a threat
*for* the player is incoherent on its face. Suppress mover-benefiting motifs from the
prompt entirely when `win_drop` is large, and reject any comment on a faulted move that
doesn't name a square from the refutation PV.

**Engine cross-check.** If a motif claims a threat that never appears in the engine's PV
from that position, it's decorative. You already have the PV — filter on it.

## On training a model

Don't. The failure mode here is the clearest possible evidence against it: the model
was handed `piece_won_behind: black pawn on h7` and wrote a sentence saying exactly
that. A fine-tuned model receiving the same input produces the same wrong claim, more
fluently.

Fine-tuning teaches *style*. Your defect is *factual*, and the fact was supplied. You'd
also need labelled "which motifs matter" data that doesn't exist — building it is
strictly more work than fixing the three detectors, and it breaks the no-training
constraint your professor set.

## How to know it's actually fixed

Dump every motif your extractor emits across ~50 games to a CSV with the FEN, and
hand-check a sample of 40 for *real* and *relevant*. You'll get a precision number per
detector, and you'll very likely find one or two should be deleted rather than fixed.
That's a couple of hours and it's the kind of measured result that defends well in a
viva — much stronger than "I improved the prompt."
