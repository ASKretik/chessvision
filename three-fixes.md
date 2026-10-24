# Rating, time, and the Ng6 comment

Three problems, three different kinds of fix. The rating one needs a new metric.
The time one is mostly already built and not shown. The comment one is the
interesting one, and it has a clean mechanical answer.

---

## 1. The rating projection

### What it does now

`partA.performance_rating()` computes the FIDE performance rating with N = 1:

```
Rp = opponent + 400 * (W - L) / N
```

With one game that has exactly three possible outputs: `opponent + 400` for a
win, `opponent` for a draw, `opponent - 400` for a loss. The docstring admits
this ("a single game can only ever say +400, 0 or -400").

The deeper problem isn't granularity, it's that **it measures the result, not
the play**. A 1200 who wins because their opponent hung a queen scores
opponent+400. A 2000 who outplays everyone and flags scores opponent-400. It
also needs the opponent's rating as input, so it says nothing about an unrated
game and mostly reports back what you already told it.

### Why the first replacement attempt underperformed

`decisions.md 3a2.6` records that regressing rating on move quality beat
predicting the mean by only 4%. That's a real result, and it's not a reason to
abandon the approach — it's a reason to change **what gets reported**.

A single game carries roughly ±200–300 Elo of genuine information, because a
player's own performance varies that much game to game. A point estimate
therefore *cannot* be accurate, and R² against a noisy per-game label will look
poor no matter how good the features are. Chasing a better point estimate is
chasing noise.

### The replacement: a posterior over rating bands

`rating_profile.py` (attached) builds an error profile and scores it as a
likelihood:

```
for band r:  score[r] = Σ_features log P(feature | r)
posterior    = softmax(score)
estimate     = Σ posterior[r] · midpoint(r)
interval     = central 80% of posterior mass, widened by 260/√decisions
```

Three things this buys you:

- **The interval is derived, not fixed.** It narrows as evidence accumulates
  instead of sitting at ±400 forever.
- **Every number can explain itself.** The module emits per-feature sentences
  ("blunders per 100 decisions 4.2 — typical of 1400–1600, band mean 4.2").
- **It's the same shape as `estimate_rating()` for Maia-2**, so log-likelihoods
  from the policy model and the error profile *add*. `refine_rating` and
  `likelihood_interval` consume the result unchanged. You get an ensemble for
  free rather than having to choose.

### The features, and the one that matters most

| Feature | Why |
|---|---|
| `blunder_rate`, `mistake_rate`, `inaccuracy_rate` | **per 100 decisions**, not per move |
| `acpl` (winsorized at 300cp) | one catastrophe shouldn't define a game |
| `cpl_p90` | how bad your *bad* moves are |
| `top1_match` | engine-best agreement, strongly rating-correlated |
| `weighted_error` | continuous severity — a 29-point drop and a 31-point drop are near-identical but land in different labels |

The denominator is the single most important choice. A move is a **decision**
only when going wrong was actually possible:

- more than one legal move
- not a forced recapture, not an only-move
- not still in book (that's preparation, not strength)
- `|eval| ≤ 600cp` — accuracy in a decided position means nothing

Error counts per *move* are dominated by game length and how forcing the game
was. Per *decision* they're comparable. You already compute everything needed
for this gate in `Complexity`.

### Test results

Sampling feature vectors from each band's own distribution, the estimator
recovers truth:

```
       band  true mid  mean est    bias     sd
   800-1000       900      1012    +112    107
  1200-1400      1300      1295      -5    154
  1600-1800      1700      1682     -18    151
  2000-2200      2100      2087     -13    126
  2200-2500      2350      2268     -82     77

  mean |bias|: 37 Elo
```

Edge-band bias is shrinkage toward the centre, which is correct behaviour for a
posterior mean. And the interval does what ±400 never could:

```
 games  decisions    est       interval  width
     1         40   1497 1259-1741         482
     4        160   1477 1279-1721         442
     8        320   1603 1485-1715         230
    16        640   1634 1490-1710         220
```

### The catch, and it's a real one

**The placeholder calibration table is internally inconsistent.** Checking
whether its own columns can co-occur:

```
       band   acpl  implied blu/100  table blu/100
   800-1000    110             16.2            9.0   <- contradicts
  1600-1800     50              1.8            3.0
  1800-2000     41              0.8            2.1
```

Values picked feature-by-feature by hand don't describe any real player. Fit
the table **jointly from labelled games** — that makes consistency automatic —
and the module refuses to report a rating while `fitted: false`. That refusal
is deliberate; don't remove it.

`bootstrap_interval()` is the check: resample decisions with replacement,
re-estimate, compare spread to the posterior interval. Large disagreement means
the calibration is wrong. Run it in tests, not at request time.

### What to put on screen

Lead with the error profile, not the Elo. "4.2 blunders per 100 real decisions,
ACPL 62, engine-best agreement 34%" is defensible and useful. "Estimated
1480–1720" belongs underneath it, with the width visible rather than hidden.
Keep `performance_rating` if you like, but label it *"what this result was
worth"*, not *"your rating"* — it's answering a different question.

---

## 2. The time analysis is computed but unreachable

Two separate problems, and one of them is a genuine bug.

### 2a. The aggregate is computed and never rendered

`_time_summary()` already produces everything you asked for:

```python
"seconds_wasted", "wasted_count", "rushed_count",
"top_wasted": [...],   # top 5, sorted by seconds
"top_rushed": [...],   # top 5, sorted by complexity
```

It lands in `PlayerSummary.time` and ships in the report JSON. The move list in
your screenshot shows only the per-move half (`5s spent · low complexity`) — the
aggregate panel just isn't drawn. That's a frontend gap, not a missing feature.

### 2b. The `rushed` flag is structurally unreachable — this is the bug

```python
only_move := near_best == 1 and spread >= 100
rushed    := ratio <= 0.4 and complexity > 0.66
```

Running the complexity formula across the range:

```
  spread  legal  complexity  band     only_move  can 'rushed' fire?
     100     32       0.570  medium   yes        NO
     125     32       0.620  medium   yes        NO
     140     32       0.650  medium   yes        NO
     150     32       0.670  high     yes        yes
```

**Every only-move position with top-3 spread between 100 and ~150cp is scored
"medium", so `rushed` can never fire there** — and those are exactly the
critical positions you want flagged. You can see the contradiction in your own
screenshot: move 15 Qd3 is tagged `only move · medium complexity`, which is
self-contradictory on its face.

The cause is scale compression. With `multipv=3`, `spread_term` saturates at
300cp and the other two terms contribute at most 0.40 combined, so almost
nothing reaches 0.66.

Three fixes, in order of effort:

1. **Make `only_move` force high complexity.** One line, removes the
   contradiction immediately:
   ```python
   score = max(score, 0.70) if only_move else score
   ```
2. **Raise `multipv` to 5** for the complexity read. A 3-line spread
   systematically understates how many ways there are to go wrong.
3. **Recalibrate the thresholds against the actual distribution.** Dump
   complexity across ~50 games, look at the histogram, and set the bands at
   real percentiles instead of round numbers. `HIGH_COMPLEXITY = 0.66` was
   chosen before anyone knew the scale only reaches ~0.70 in practice.

### 2c. The conjunction you actually asked for

"A critical position where one hasn't used much time and ended up missing the
advantage" is a three-way conjunction that isn't currently expressed:

```python
critical_rush = (
    complexity_band == "high"
    and time_ratio <= 0.5
    and win_drop >= MISTAKE_DROP        # it actually cost something
)
```

The `win_drop` term is what makes it worth reporting. Rushing a hard position
and finding the move anyway isn't a mistake — it's the rush *plus* the cost that
makes a teachable moment. Same for the other direction:

```python
wasted_think = (
    complexity_band == "low"
    and time_ratio >= 2.5
    and win_drop < INACCURACY_DROP      # nothing was at stake
)
```

Surface both as their own list, separate from `top_wasted` / `top_rushed`, with
a jump-to-position link. That's the panel that's missing.

---

## 3. The Ng6 comment

### The arithmetic

> *"White's refutation Ng5 forces Black to answer f4, after which White captures
> on d5 and Black recaptures, **leaving material even**. Engine analysis shows
> that Black's best continuation was f4, leading to Bxf4 Bxf4 gxf4 Rxf4, which
> **would have won the exchange**."*

Both material claims are false:

```
f4 Bxf4 Bxf4 gxf4 Rxf4
  Black gives pawn + bishop, takes bishop + pawn   ->  delta  0   DEAD LEVEL
  "the exchange" means rook-for-minor. No rook was captured by anyone.

Ng5 f4 Nxd5 cxd5 Bxd5+ Kh8
  White gives knight, takes two pawns              ->  delta -1   White a pawn down
  and -1 is exactly the engine's -0.9
```

Note the second one: **the engine's own eval already contradicted the comment.**
The data to catch this was sitting right next to the claim.

### Why it happened

The model was handed a PV — a list of moves — and asked why one line was better
than another. Material outcome is not derivable from a move list without playing
it out, which a model cannot do reliably. So it filled the gap with a chess
cliché that pattern-matched the shape of the line: long forcing sequence ending
in a rook capture → "wins the exchange."

**This is the general failure: asking the model to explain a quantity using data
from which that quantity cannot be derived.** It will always produce something
fluent. The fix isn't a better prompt or a better model — it's supplying the
quantity.

### Fix 1 — compute material along every PV (highest value, cheapest)

`claim_guard.py` → `walk_line()`. Plays the PV out, counts every capture and
promotion, returns a `delta` and a **finished phrase**:

```python
LineMaterial(delta=0,  description="material is level at the end of the line")
LineMaterial(delta=-1, description="loses a pawn")
```

`describe_material()` detects "the exchange" **structurally** — a rook won for a
minor given — not from the number 2. Two pawns is also +2 and is not the
exchange, and that conflation is precisely how this comment went wrong.

The prompt rule becomes: **use `material_phrase` verbatim; make no other claim
about material.** The model never does arithmetic.

### Fix 2 — a guard that rejects contradicting claims

`check_comment()` runs a lexicon of material claims over the generated text and
fails any that contradicts the computed delta. On your actual comment:

```
REJECT  comment asserts "won the exchange" (requires delta == +2)
        but the line computes delta=+0 ("material is level at the end of the line")
REJECT  comment asserts "leaving material even" (requires delta == 0)
        but the line computes delta=-1 ("loses a pawn")
```

One warning from building it: my first lexicon matched `wins/win` but not
**`won`**, and the comment says *"would have won the exchange"* — so it passed.
A guard with a tense gap gives false assurance, which is worse than no guard.
Every verb needs all inflections; they're built once as `_WIN` / `_LOSE` groups
rather than spelled out per rule.

### Fix 3 — answer "why is it better" with the right data

This is the one that turns *almost correct* into *useful*.

When material is level at the end of the line, the eval difference is
**positional**, and a comment that can't name the positional reason has nothing
true to say. So compute that too — `positional_delta()` diffs cheap features
across the line:

- mobility delta
- attackers near each king
- files that open
- **king shelter**: friendly pawns on the king's file and neighbours

King shelter matters more than open files here. In the f4 line, White's g3 pawn
disappears but Black's g7 pawn keeps the g-file nominally closed — a fully-open-
file test reports nothing where a human would say the king got drafty.

Running it on the f4 line surfaces something honest and slightly awkward:

```json
{
  "material_phrase": "material is level at the end of the line",
  "positional_notes": ["1 pawn(s) leave the mover's king shelter (3 -> 2)"],
  "mobility_delta": 3
}
```

That note cuts *against* f4 — it's Black's own shelter thinning. Which is useful
information: it means the real reason the engine likes f4 isn't in this feature
set yet, and the comment shouldn't pretend otherwise. Worth adding a sign to
each note (favours mover / against mover) so the generator can tell.

### Fix 4 — ban ungrounded evaluation

Move 15's comment in the same screenshot says *"gaining a decisive material
advantage"* and *"kept pressure on Black's position"*. Both are the same failure
one level up: evaluative phrases with no computable referent.

`UNGROUNDED` in the guard flags these as warnings. The prompt rule: **every
comment ends with a consequence stated as either a computed material fact or a
named positional feature** — never as a bare evaluative phrase.

### Fix 5 — be terse when the facts are thin

`line_facts()` returns `has_concrete_reason`. When it's false — no material
change, no surviving motif, no king-safety delta — the honest output is:

> *Inaccuracy. f4 was better, keeping Black's advantage at −1.9.*

**A thin true comment beats a rich false one.** Most of the damage in your
current output comes from the generator being obliged to produce a paragraph
when it has one sentence of real content.

### What the Ng6 comment should have said

> *f4 was the move: after Bxf4 Bxf4 gxf4 Rxf4 material is level, but Black's
> rook lands actively on f4. Ng6 instead allows Ng5, and after f4 Nxd5 cxd5
> Bxd5+ White has two pawns for the knight — Black is still better, but the
> advantage has shrunk from −1.9 to −0.9.*

Every number in that is computed. The model's only job is word order.

---

## Suggested order

1. **`only_move` → high complexity** (one line, un-breaks `rushed`)
2. **Wire the time aggregate into the UI** (already computed, just not drawn)
3. **`walk_line` + `check_comment`** into the B.3 pipeline — biggest quality
   jump per hour, and it fixes a whole class of error rather than one comment
4. **Fit the rating calibration** on labelled games, then swap the headline
5. `positional_delta` and the terse-fallback path

Steps 1–3 are a few hours and fix what's visibly wrong in the screenshot.

## Open calls

- **Keep Maia-2 alongside the error profile, or drop it?** They sum cleanly, so
  keeping both costs little. But if the 512 MB budget is tight, the error
  profile alone is ~100 bytes of coefficients versus Maia-2's weights, and it
  removes the §8.1 opponent-Elo question entirely.
- **Calibration source** — the Lichess HF dataset from `plan_v1`, or games you
  analyse yourself? The former is far cheaper; the latter guarantees your
  features match your pipeline exactly.
- **Comment on every move, or only faulted ones?** Fix 5 makes the quiet-move
  case cheap and honest, which changes the answer to §8.3.
