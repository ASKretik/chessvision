# The truncation error, and how to fix it

## What the message actually is

It's **your own error text**, not Groq's. Three separate batch failures, joined
into one string by `_unavailable_reason`:

```python
if comments:
    return "; ".join(errors) or None        # partB.py:1974
```

Each failure came from this, in the batch loop:

```python
except CommentaryError as exc:
    log.warning("B.3 batch %d/%d failed: %s", i + 1, len(batches), exc)
    errors.append(str(exc))
    continue                                # ~5 moves of commentary, gone
```

So three batches × ~5 moves = roughly 15 moves with labels and no comments.
That matches the screenshot exactly: 22–39 annotated, nothing written.

The first two come from `AnthropicCommentator._request` (`stop_reason ==
"max_tokens"`, partB.py:1598); the third from `GroqCommentator._request`
(`json_validate_failed`, partB.py:1747). Note the Groq one says **1800**
tokens while the file I have says `move_pass_tokens = 3000` — so the running
config differs from the uploaded source. Worth checking which is live before
you tune anything.

## The mechanism

Both providers use **strict JSON-schema decoding**. The model emits one JSON
object, hits `max_completion_tokens` mid-object, and strict decoding can't
produce a valid object — so the request is rejected outright.

You are billed for the full output and get **nothing back**, including the
comments that had already finished generating. JSON is the worst possible wire
format for an output-capped generation: an object truncated at 95% is worth
exactly as much as one truncated at 5%.

## Why it's intermittent — the actual root cause

The budget isn't wildly wrong. It's wrong by a thin margin.

Measured against the comments actually on screen (~55 tokens of prose, ~67 with
the JSON wrapper) and partB's own reasoning measurements for `gpt-oss-120b`
(`low 647 | medium 1,402 | high >4,800`):

```
batch  effort  reasoning  answer  total   vs 1800
    5  medium       1170     345   1515   fits -- 16% headroom
    8  medium       1575     546   2121   truncates
```

**Reasoning tokens are billed as output and are spent before any JSON is
emitted.** At `reasoning_effort: medium` they're the majority of the budget,
and they are not deterministic — they scale with how much there is to think
about.

So: 16% headroom against a variable cost. Some batches clear, some don't. And
since reasoning grows with the eventfulness of the moves, **the batches that
fail are the ones with the most to say.** Moves 25–39 of your game contain
twelve annotated moves including four blunders — that's precisely the stretch
where the budget blows, and precisely where you wanted the commentary.

Running the budget model against your configured values:

```
largest safe batch at 1800 tokens:   low effort -> 8 moves
                                     medium     -> 4 moves
```

`default_batch_size = 5`. At medium effort the safe number is 4. **Off by one**
— which is the exact signature of intermittent rather than total failure.

---

## Fixes, in order of value per minute of work

### 1. Drop the move pass to `effort="low"` — one line, biggest win

```python
# partB.py, BaseCommentator.move_pass
effort="medium",   ->   effort="low",
```

B.2 (the game narrative) genuinely needs reasoning — it's synthesising an arc
across 40 moves. B.3 does not. Its job is to restate facts you already computed
into one English sentence. It is the one pass where reasoning buys nothing.

Per your own measured numbers this frees ~700 tokens per call, taking a batch of
5 from 16% headroom to comfortable, and raising the safe batch size at 1800
from 4 to 8.

### 2. Cap comment length in the schema

```python
MOVE_SCHEMA["properties"]["comments"]["items"]["properties"]["comment"]["maxLength"] = 240
```

Right now nothing bounds how long a comment can be, which makes the budget
uncomputable. Strict decoding enforces `maxLength`, so the answer half becomes
a known quantity (~57 tokens/move instead of an unbounded 67+).

240 characters is about 40 words — enough for *"this hangs the knight on f6;
after Nxf6 Black is a piece up"*, not enough for the three-clause essays
currently shipping. This is the same change as Fix 5 in the commentary doc:
the verbosity causing the fabricated material claims is the verbosity blowing
the token budget. One change fixes both.

### 3. Scale the budget with batch size instead of using a constant

`move_pass_tokens` is a fixed number but the output scales with `len(batch)`.
`batch_budget.move_pass_budget()` does this:

```python
budget = (reasoning_reserve(effort, n_moves) + n_moves * per_comment + 20) * 1.35
```

The 1.35 is the headroom you don't currently have. Pass `ceiling=` to clamp it
under Groq's 8,000/minute window, which is charged as
`input + max_completion_tokens`.

### 4. Split and retry instead of dropping the batch

A truncated batch isn't a permanent failure — it's a batch that was too big
*for this budget*. `run_with_split_retry()` halves it and recurses:

```
batch of 9, model can only handle 3 at a time
  produced 9/9 comments in 7 calls (3 splits), 0 lost
  current code would have produced 0/9
```

It deliberately does **not** split non-truncation errors. Auth failures and
daily-quota exhaustion propagate immediately — retrying those just burns the
quota you've already run out of (verified: 1 call, not 7).

Wire it into the batch loop:

```python
res = run_with_split_retry(batch, lambda b: commentator.move_pass(b, narrative, players),
                           error_type=CommentaryError)
produced = res.produced
if res.failed:
    gaps.extend(gap_placeholders([f.ply for f in res.failed], res.errors[-1]))
```

### 5. Make the gap visible per move

A dropped batch currently shows as silence in the move list plus one long string
at the bottom. `gap_placeholders()` emits one entry per affected ply:

```json
{"ply": 54, "comment": null, "unavailable": "the batch exceeded the model's output budget"}
```

Render that as a muted line in the move list. A visible gap is debuggable; an
invisible one looks like the feature doesn't work.

### 6. Dedupe the error string

```
before: Response was truncated...; Response was truncated...; Groq could not...
after : Response was truncated... (x2); Groq could not complete the JSON...
```

Three batches failing identically currently read as three different problems.

---

## The structural option, if you want to stop fighting this

Everything above keeps strict JSON. The alternative is to change the wire
format so truncation degrades instead of failing:

**NDJSON** — one JSON object per line, parse line by line, keep every complete
line. A truncated response loses only the final partial line. The cost is that
Groq's `json_schema` strict mode wants a single object, so you'd drop to
`json_object` mode and lose the schema guarantee — which you'd then need to
validate yourself.

Given you already run `validate_comment()` on every comment, that guarantee may
be worth less to you than it looks. But it's a real trade, and fixes 1–4 are
enough to make the problem go away without taking it.

---

## Suggested order

1. `effort="low"` on the move pass — one line, fixes most of it
2. `maxLength: 240` in the schema — bounds the budget, improves quality
3. Split-retry — turns remaining failures into partial success instead of zero
4. Gap placeholders + dedupe — makes what's left legible

Steps 1 and 2 are about five minutes and should eliminate the error on normal
games. 3 and 4 are what keep a bad game from losing fifteen comments silently.

## Worth checking first

Your Groq error reports **1800** tokens; the `partB.py` I have says
`move_pass_tokens = 3000`. Find which is actually live before tuning — if
something is overriding it to 1800, that override is doing more damage than the
batch size, and raising it to 3000 alone would have made the symptom disappear
without fixing the missing headroom underneath.
