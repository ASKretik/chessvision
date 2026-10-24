"""
Part B live verification -- makes REAL API calls and costs real money.

tests/test_commentary.py covers everything up to the API boundary with a
fake client. This covers the three things only a real call can:

  1. the request is actually accepted (no 400 on shape, model id, schema)
  2. prompt caching really engages -- cache_read_input_tokens > 0 on the
     second batch proves the breakpoint placement works
  3. the comments are worth reading, which is the entire point of Part B
     and which no assertion can judge for you

Cost: one B.2 call plus 2-4 B.3 calls for one game. A few cents.
A --probe run makes one tiny call instead, to check auth and request shape
for well under a cent.

Setup -- put the key in .env (gitignored), not in your shell history:

    printf 'LLM_PROVIDER=groq\nLLM_API_KEY=gsk_...\n' >> .env

Then:

    .venv/bin/python tests/test_commentary_live.py --probe   # cheap check
    .venv/bin/python tests/test_commentary_live.py           # full game
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load .env before anything reads the environment, so a key placed there
# reaches os.environ. Calling the loader directly rather than relying on an
# import side effect keeps the dependency explicit.
from partE import load_dotenv        # noqa: E402

load_dotenv()

import partA                         # noqa: E402
import partB                         # noqa: E402

# $/1M tokens, for the cost estimate only. Groq's free tier bills nothing;
# the figure is still printed so the Anthropic path can be compared.
PRICING = {
    "anthropic": (5.00, 25.00),     # Opus 5
    "groq": (0.0, 0.0),             # free tier
}

PGN = """[Event "Rated blitz"]
[White "alice"]
[Black "bob"]
[Result "1-0"]
[WhiteElo "1520"]
[BlackElo "1495"]
[TimeControl "300+3"]

1. e4 { [%clk 0:05:00] } e5 { [%clk 0:04:58] } 2. Nf3 { [%clk 0:05:01] } Nc6 { [%clk 0:04:55] }
3. Bc4 { [%clk 0:05:00] } Nf6 { [%clk 0:04:40] } 4. Ng5 { [%clk 0:04:35] } d5 { [%clk 0:04:20] }
5. exd5 { [%clk 0:04:30] } Nxd5 { [%clk 0:04:00] } 6. Nxf7 { [%clk 0:04:10] } Kxf7 { [%clk 0:03:55] }
7. Qf3+ { [%clk 0:04:05] } Ke6 { [%clk 0:03:10] } 8. Nc3 { [%clk 0:03:50] } Nd4 { [%clk 0:02:40] }
9. Bxd5+ { [%clk 0:03:30] } Kd6 { [%clk 0:02:20] } 10. Qf7 { [%clk 0:03:00] } Be6 { [%clk 0:01:50] }
11. Bxe6 { [%clk 0:02:55] } Nxe6 { [%clk 0:01:45] } 12. Ne4+ { [%clk 0:02:40] } Kd5 { [%clk 0:01:00] }
13. c4+ { [%clk 0:02:35] } Kxe4 { [%clk 0:00:50] } 14. Qxe6 1-0 { [%clk 0:02:30] }
"""


def cost(tokens: dict, provider: str) -> float:
    price_in, price_out = PRICING.get(provider, (0.0, 0.0))
    inp = tokens.get("input_tokens", 0)
    out = tokens.get("output_tokens", 0)
    writes = tokens.get("cache_creation_input_tokens", 0)
    reads = tokens.get("cache_read_input_tokens", 0)
    return ((inp + writes * 1.25 + reads * 0.1) * price_in
            + out * price_out) / 1_000_000


def key_present() -> str | None:
    return (os.getenv("LLM_API_KEY") or os.getenv("GROQ_API_KEY")
            or os.getenv("ANTHROPIC_API_KEY")) or None


def probe(commentator) -> int:
    """
    One minimal call through the real provider class, for about a cent at
    most. Catches the cheap failures -- bad key, wrong model id, a rejected
    request shape -- before a full game's tokens are spent on discovering
    them.

    Deliberately routed through `_request` rather than a hand-built SDK call,
    so what is verified is the code the pipeline actually uses.
    """
    print(f"probe: one tiny structured-output call via {commentator.provider} "
          f"({commentator.model})...")
    t0 = time.perf_counter()
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }
    try:
        data = commentator._request(
            system="Reply with the requested JSON only.",
            stable=[], volatile=["Say ok."],
            schema=schema, schema_name="probe", effort="low", max_tokens=256,
        )
    except partB.CommentaryError as exc:
        print(f"  FAIL: {exc}")
        print("\n  Check LLM_PROVIDER and LLM_API_KEY in .env.")
        return 1

    print(f"  response     {data}")
    print(f"  tokens       {dict(commentator.usage)}")
    print(f"  latency      {time.perf_counter() - t0:.1f}s")
    print("\n  PASS: auth works and the request shape is accepted.")
    print("  Now run without --probe for the full pipeline.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--probe", action="store_true",
                    help="one tiny call to check auth and request shape")
    ap.add_argument("--depth", type=int, default=12)
    args = ap.parse_args()

    if not key_present():
        print("No API key found.\n")
        print("Put one in .env (which is gitignored) so it stays out of your")
        print("shell history and out of this transcript:\n")
        print("    printf 'LLM_PROVIDER=groq\\nLLM_API_KEY=gsk_...\\n' >> .env\n")
        print("Free Groq keys:  https://console.groq.com/keys")
        print("Anthropic keys:  https://console.anthropic.com/settings/keys")
        return 2

    commentator = partB.make_commentator(
        os.getenv("LLM_PROVIDER") or None,
        api_key=key_present(),
        model=os.getenv("LLM_MODEL") or None,
    )
    print(f"provider {commentator.provider} | model {commentator.model}")
    if args.probe:
        return probe(commentator)

    print("running Part A...")
    report = partA.analyse(
        partA.load_pgn(PGN), stockfish_path=os.getenv("STOCKFISH_PATH", "stockfish"),
        depth=args.depth, multipv=3,
        maia2_path="models/maia2.onnx" if os.path.exists("models/maia2.onnx") else None,
    ).to_dict()

    facts = partB.extract_game_facts(report)
    selected = partB.select_commentable(facts.moves)
    print(f"  {len(facts.moves)} plies, {len(selected)} selected for comment\n")

    cache = partB.CommentCache()

    order: list[str] = []
    t0 = time.perf_counter()
    result = partB.generate_commentary(
        report, commentator=commentator, cache=cache, facts=facts,
        progress=lambda s, f, p: order.append(s),
    )
    elapsed = time.perf_counter() - t0

    failures = []

    if not result.available:
        print(f"FAIL: commentary unavailable -- {result.reason}")
        return 1

    tokens = result.usage["tokens"]
    print("=" * 72)
    print(f"GAME NARRATIVE (B.2)\n\n{result.narrative['arc']}\n")
    print(f"  white's recurring weakness: {result.narrative['white_weakness']}")
    print(f"  black's recurring weakness: {result.narrative['black_weakness']}")
    print("\n  critical moments:")
    for m in result.narrative["critical_moments"]:
        print(f"    ply {m['ply']}: {m['why']}")

    by_ply = {m.ply: m for m in facts.moves}
    print("\n" + "=" * 72)
    print("MOVE COMMENTS (B.3)\n")
    for c in result.comments:
        f = by_ply.get(c["ply"])
        head = (f"  {f.move_number}{'.' if f.color == 'white' else '...'} {f.san}"
                f"  [{f.label}]" if f else f"  ply {c['ply']}")
        tag = " (cached)" if c.get("cached") else ""
        print(f"{head}{tag}")
        print(f"      {c['comment']}\n")

    # Comments the validator refused, and batches that failed. Both leave
    # gaps in the timeline, and a harness that hides them is worse than no
    # harness -- a silently short timeline looks like a model that had
    # nothing to say.
    rejected = result.usage.get("rejected") or []
    if rejected:
        print("=" * 72)
        print(f"WITHHELD BY THE VALIDATOR ({len(rejected)})\n")
        for r in rejected:
            f = by_ply.get(r["ply"])
            label = (f"{f.move_number}{'.' if f.color == 'white' else '...'} {f.san}"
                     if f else f"ply {r['ply']}")
            print(f"  {label}")
            print(f"      would have said: {r['text'][:110]}")
            for reason in r["reasons"]:
                print(f"      rejected: {reason}")
            print()

    if result.usage.get("errors"):
        print("=" * 72)
        print("FAILED BATCHES\n")
        for e in result.usage["errors"]:
            print(f"  {e}")
        print()

    print("=" * 72)
    print("MECHANICS\n")
    print(f"  latency          {elapsed:.1f}s for {result.usage['batches']} batch(es)")
    print(f"  comments         {len(result.comments)} of "
          f"{result.usage['selected_moves']} selected"
          + (f"  ({len(rejected)} withheld)" if rejected else ""))
    print(f"  LLM calls        {tokens.get('calls', 0)}")
    print(f"  input tokens     {tokens.get('input_tokens', 0):,}")
    print(f"  output tokens    {tokens.get('output_tokens', 0):,}")
    print(f"  cache writes     {tokens.get('cache_creation_input_tokens', 0):,}")
    print(f"  cache reads      {tokens.get('cache_read_input_tokens', 0):,}")
    est = cost(tokens, commentator.provider)
    print("  est. cost        "
          + (f"${est:.4f} for this game" if est else "$0.00 (free tier)"))

    # 2. Prompt caching -- the thing the fake client could not verify.
    #    Groq has no prompt caching, so this check only applies to Anthropic.
    reads = tokens.get("cache_read_input_tokens", 0)
    if commentator.provider != "anthropic":
        print(f"\n  SKIP: {commentator.provider} has no prompt caching")
    elif tokens.get("calls", 0) >= 3 and reads == 0:
        failures.append(
            "prompt caching never engaged (cache_read_input_tokens == 0 across "
            f"{tokens['calls']} calls) -- a breakpoint is misplaced or the "
            "prefix is below the model's minimum cacheable length")
    elif reads:
        print(f"\n  PASS: prompt caching engaged ({reads:,} tokens read from cache)")
    else:
        print("\n  SKIP: too few calls to demonstrate cache reads")

    # Grounding spot-check: a comment on a move that hung a piece should name
    # the square. Not a quality judgement, just a floor.
    grounded = 0
    checked = 0
    for c in result.comments:
        f = by_ply.get(c["ply"])
        if f and f.newly_hanging:
            checked += 1
            if any(h["square"] in c["comment"] for h in f.newly_hanging):
                grounded += 1
    if checked:
        print(f"  grounding        {grounded}/{checked} comments on a loose piece "
              f"name the square")
        if grounded == 0:
            failures.append("no comment on a loose piece named its square -- "
                            "the facts are not reaching the prose")

    lost = result.usage["selected_moves"] - len(result.comments)
    if lost:
        print(f"  NOTE: {lost} selected move(s) have no comment "
              f"({len(rejected)} withheld by the validator, "
              f"{len(result.usage.get('errors') or [])} batch failure(s))")

    print()
    if failures:
        for f in failures:
            print(f"  [FAIL] {f}")
        return 1
    print("  [PASS] live pipeline works end to end")
    print("\nRead the comments above. If they sound like a coach, Part B is done;")
    print("if they sound like a template, the prompts in partB.py need work.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
