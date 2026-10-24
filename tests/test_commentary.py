"""
Part B tests: B.1 facts, B.4 selection and caching, B.2/B.3 request assembly.

The LLM calls are exercised against an injected fake client, not the real
API. That is a deliberate boundary, not a shortcut: it covers prompt
assembly, cache-breakpoint placement, the output schema, batching, the
cache-bypass rule for critical moments, batch-failure isolation and
streaming order -- everything except whether Claude writes a good sentence,
which no unit test can assert anyway.

There are no Anthropic credentials on the machine this was written on, so
the live path is unverified. See decisions.md 5.1.

    .venv/bin/python tests/test_commentary.py
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import chess
import partB

_results: List[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))


# --------------------------------------------------------------------------
# Fake clients, one per provider. Each records every request so the tests can
# assert on prompt structure, and returns schema-valid JSON.
#
# Both providers are exercised with the same assertions, because the whole
# point of the provider split is that only `_request` differs.
# --------------------------------------------------------------------------
NARRATIVE_PAYLOAD = {
    "arc": "White sacrificed a knight on f7 and converted.",
    "critical_moments": [{"ply": 20, "why": "the bishop drop lost the game"}],
    "white_weakness": "slow to punish loose pieces",
    "black_weakness": "leaves pieces undefended under pressure",
}


MOVES_MARKER = "Comment on these moves:\n"


def _reply_for(schema: Dict[str, Any], asked_text: str) -> Dict[str, Any]:
    """
    Schema-appropriate fake response, shared by both fake clients.

    The move payload is located by its marker rather than by position:
    Anthropic receives it as its own content block, while Groq gets the
    stable and volatile blocks concatenated into one message.
    """
    if "arc" in schema["properties"]:
        return NARRATIVE_PAYLOAD
    tail = asked_text.rsplit(MOVES_MARKER, 1)[-1]
    asked = json.loads(tail)
    return {"comments": [{"ply": m["ply"], "comment": f"fake comment for ply {m['ply']}"}
                         for m in asked]}


class FakeUsage:
    input_tokens = 1000
    output_tokens = 200
    cache_creation_input_tokens = 500
    cache_read_input_tokens = 400
    prompt_tokens = 1000
    completion_tokens = 200


class FakeBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class FakeAnthropicResponse:
    def __init__(self, payload: Dict[str, Any]) -> None:
        self.content = [FakeBlock(json.dumps(payload))]
        self.stop_reason = "end_turn"
        self.usage = FakeUsage()


class FakeChoiceMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = FakeChoiceMessage(content)
        self.finish_reason = "stop"


class FakeGroqResponse:
    def __init__(self, payload: Dict[str, Any]) -> None:
        self.choices = [FakeChoice(json.dumps(payload))]
        self.usage = FakeUsage()


class FakeAnthropicClient:
    """Shape of anthropic.Anthropic: .messages.create(...)."""

    def __init__(self, owner: "Recorder") -> None:
        self.messages = self
        self.owner = owner

    def create(self, **kwargs: Any) -> FakeAnthropicResponse:
        self.owner.record(kwargs)
        schema = kwargs["output_config"]["format"]["schema"]
        asked = kwargs["messages"][0]["content"][-1]["text"]
        return FakeAnthropicResponse(_reply_for(schema, asked))


class FakeGroqClient:
    """Shape of groq.Groq: .chat.completions.create(...)."""

    def __init__(self, owner: "Recorder") -> None:
        self.chat = self
        self.completions = self
        self.owner = owner

    def create(self, **kwargs: Any) -> FakeGroqResponse:
        self.owner.record(kwargs)
        schema = kwargs["response_format"]["json_schema"]["schema"]
        asked = kwargs["messages"][-1]["content"]
        return FakeGroqResponse(_reply_for(schema, asked))


class Recorder:
    def __init__(self, raise_on: set[int] | None = None, provider: str = "anthropic") -> None:
        self.requests: List[Dict[str, Any]] = []
        self.raise_on = raise_on or set()
        self.provider = provider

    def record(self, kwargs: Dict[str, Any]) -> None:
        self.requests.append(kwargs)
        if len(self.requests) in self.raise_on:
            if self.provider == "groq":
                import groq

                raise groq.APIConnectionError(request=None)  # type: ignore[arg-type]
            import anthropic

            raise anthropic.APIConnectionError(request=None)  # type: ignore[arg-type]


def make_commentator(raise_on: set[int] | None = None, provider: str = "anthropic"):
    """A commentator with its SDK client replaced by a fake."""
    c = partB.make_commentator(provider, api_key="fake-key")
    rec = Recorder(raise_on, provider)
    c._client = (FakeGroqClient(rec) if provider == "groq"
                 else FakeAnthropicClient(rec))
    return c, rec


# --------------------------------------------------------------------------
class GameNarrativeStub(partB.GameNarrative):
    """Minimal narrative for tests that only exercise the move pass."""

    def __init__(self) -> None:
        super().__init__(arc="arc", critical_moments=[],
                         white_weakness="w", black_weakness="b")


def fact(**over: Any) -> partB.MoveFacts:
    """A MoveFacts with sane defaults, overridable per test."""
    base: Dict[str, Any] = dict(
        ply=1, move_number=1, color="white", san="e4", uci="e2e4",
        fen_before=chess.STARTING_FEN, phase="opening", label="best",
        cp_before=20, cp_after=20, win_drop=0.0, accuracy=100.0,
        best_move="e4", best_pv=["e4", "e5"], played_best=True, only_move=False,
        multipv_spread_cp=10, complexity_band="low", mate_status_change=None,
        time_spent=3.0, time_flag=None, in_book=True, is_capture=False,
        is_check=False, captured_piece=None, moved_piece="pawn",
        refutation_move=None, refutation_pv=[], refutation_by=None,
        best_line={}, refutation_line={},
        material_delta=0, material_after={"white": 39, "black": 39},
        newly_hanging=[], still_hanging=[], opponent_hanging=[], motifs=[],
        king_pressure_mover=0, king_pressure_opponent=0, king_pressure_change=0,
    )
    base.update(over)
    return partB.MoveFacts(**base)


def main() -> int:
    # ---------------- B.1 motifs ----------------
    print("B.1 motif detection")
    pin = chess.Board("k7/8/8/r7/8/8/8/R3K3 w - - 0 1")
    sk = chess.Board("r7/8/8/k7/8/8/8/R3K3 w - - 0 1")
    pm = partB.find_pins_and_skewers(pin, chess.WHITE)
    sm = partB.find_pins_and_skewers(sk, chess.WHITE)
    check("pin: rook shielding king", len(pm) == 1 and pm[0]["motif"] == "pin",
          str(pm))
    check("pin against a king is flagged absolute", pm and pm[0]["absolute"] is True)
    check("skewer: king in front of rook", len(sm) == 1 and sm[0]["motif"] == "skewer",
          str(sm))
    # `absolute` is pin-only now: a skewer cannot be absolute, so carrying
    # the key at all would invite the model to reason about it.
    check("skewer carries no absolute flag", "absolute" not in sm[0], str(sorted(sm[0])))
    # --- materiality: geometry is not a tactic -------------------------
    # The reported bug: white queen d3, black knight g6, black pawn h7 behind
    # it. Both ends are real, so every square claim was true and the
    # validator passed it -- but the "win" is a pawn defended by the king,
    # and Qxg6 just loses the queen to hxg6.
    noise = chess.Board("6k1/5ppp/6n1/8/8/3Q4/8/6K1 w - - 0 1")
    check("a piece lined up against a PAWN is not a skewer",
          partB.find_pins_and_skewers(noise, chess.WHITE) == [],
          str(partB.find_pins_and_skewers(noise, chess.WHITE)))
    check("...because the piece behind is below knight value",
          partB._motif_is_material(noise.piece_at(chess.H7),
                                   noise.piece_at(chess.G6)) is False)
    check("...and because the queen cannot profitably take the knight",
          partB._skewer_threat_is_credible(
              noise, noise.piece_at(chess.D3), chess.G6,
              noise.piece_at(chess.G6), chess.WHITE) is False)
    check("a rook skewering a king to win a rook still counts",
          [m["motif"] for m in partB.find_pins_and_skewers(sk, chess.WHITE)]
          == ["skewer"])
    pin_pawn = chess.Board("6k1/8/8/3p4/3n4/8/8/3R2K1 w - - 0 1")
    check("pinning a knight against a PAWN is suppressed too",
          partB.find_pins_and_skewers(pin_pawn, chess.WHITE) == [],
          str(partB.find_pins_and_skewers(pin_pawn, chess.WHITE)))

    # The credibility test is skewer-only on purpose. A pin's value is the
    # bind, not the capture: a rook pinning a knight against a queen is a
    # textbook relative pin even though Rxd5 would lose material.
    bind = chess.Board("3q2k1/8/8/3n4/8/8/8/3R2K1 w - - 0 1")
    check("a relative pin survives even when the capture would lose material",
          [m["motif"] for m in partB.find_pins_and_skewers(bind, chess.WHITE)]
          == ["pin"],
          str(partB.find_pins_and_skewers(bind, chess.WHITE)))

    rel = chess.Board("3q2k1/8/8/3n4/8/8/8/3R2K1 w - - 0 1")
    rm = partB.find_pins_and_skewers(rel, chess.WHITE)
    check("relative pin (no king) found",
          len(rm) == 1 and rm[0]["motif"] == "pin" and not rm[0]["absolute"], str(rm))

    # Roles and direction, named rather than implied by geometry. This is the
    # contract that stopped "the pinner is pinned" comments.
    check("pin names the pinner, not just the geometry",
          pm[0]["pinner"] == "white rook on a1", pm[0].get("pinner", "?"))
    check("pin names what is pinned", pm[0]["pinned"] == "black rook on a5",
          pm[0].get("pinned", "?"))
    check("pin names what it is pinned against",
          pm[0]["pinned_against"] == "black king on a8",
          pm[0].get("pinned_against", "?"))
    check("pin says whom it benefits", pm[0]["benefits"] == "white",
          pm[0].get("benefits", "?"))
    check("skewer uses attacker/front/behind roles",
          {"attacker", "front_piece", "piece_won_behind"} <= set(sm[0]),
          str(sorted(sm[0])))
    check("skewer says whom it benefits", sm[0]["benefits"] == "white")
    check("every piece name carries colour and square",
          all(" on " in str(v) and ("white" in str(v) or "black" in str(v))
              for k, v in pm[0].items()
              if k in ("pinner", "pinned", "pinned_against")),
          str(pm[0]))

    forkb = chess.Board("6k1/8/2q5/3N4/8/8/8/4K3 w - - 0 1")
    mv = chess.Move.from_uci("d5e7")
    after = forkb.copy(); after.push(mv)
    fk = partB.find_forks(after, mv)
    check("royal fork found", len(fk) == 1 and len(fk[0]["targets"]) == 2, str(fk))
    check("fork names king and queen with colours",
          fk and sorted(fk[0]["targets"]) ==
          ["black king on g8", "black queen on c6 (undefended)"],
          str(fk[0]["targets"]))
    check("fork names the forker", fk[0]["forker"] == "white knight on e7",
          fk[0].get("forker", "?"))
    check("fork says whom it benefits", fk[0]["benefits"] == "white")
    quiet = chess.Board()
    check("no fork in the start position", partB.find_forks(quiet, chess.Move.from_uci("e2e4")) == [])

    disc_before = chess.Board("6k1/6r1/8/8/3N4/8/1B6/6K1 w - - 0 1")
    dmv = chess.Move.from_uci("d4f5")
    disc_after = disc_before.copy(); disc_after.push(dmv)
    da = partB.find_discovered_attacks(disc_before, disc_after, dmv)
    check("discovered attack found", len(da) == 1, str(da))
    check("discovery names attacker and victim by role",
          da[0]["attacker"] == "white bishop on b2"
          and da[0]["victim"] == "black rook on g7",
          f"{da[0].get('attacker')} -> {da[0].get('victim')}")
    check("discovery names the vacated square",
          da[0]["opened_by_vacating"] == "d4", da[0].get("opened_by_vacating", "?"))
    check("discovery says whom it benefits", da[0]["benefits"] == "white")

    br = chess.Board("6k1/5ppp/8/8/8/8/8/R5K1 w - - 0 1")
    check("back rank weakness found", partB.back_rank_weakness(br, chess.BLACK) is not None)
    check("luft cancels back rank",
          partB.back_rank_weakness(chess.Board("6k1/5pp1/7p/8/8/8/8/R5K1 w - - 0 1"),
                                   chess.BLACK) is None)
    check("no heavy piece cancels back rank",
          partB.back_rank_weakness(chess.Board("6k1/5ppp/8/8/8/8/8/6K1 w - - 0 1"),
                                   chess.BLACK) is None)

    loose = chess.Board("r1bqkb1r/pppp1ppp/2n2n2/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 6 5")
    hs = partB.hanging_squares(loose, chess.WHITE)
    check("undefended attacked pawn is loose", chess.E4 in hs,
          str(partB._piece_list(loose, hs)))
    check("start position has nothing loose",
          partB.hanging_squares(chess.Board(), chess.WHITE) == set())
    check("material counts exclude kings", partB.material(chess.Board(), chess.WHITE) == 39,
          str(partB.material(chess.Board(), chess.WHITE)))
    op = partB.own_pinned_pieces(pin, chess.BLACK)
    check("own pinned pieces are reported separately",
          [m["motif"] for m in op] == ["own_piece_pinned"])
    check("own pin names the piece and benefits the opponent",
          op[0]["pinned"] == "black rook on a5" and op[0]["benefits"] == "white",
          str(op[0]))

    # squares_in_facts must still find squares now that they sit inside prose.
    sq = partB._squares_mentioned(fact(uci="e2e4", motifs=[
        {"motif": "pin", "pinner": "white bishop on c4",
         "pinned": "black pawn on f7", "pinned_against": "black king on g8",
         "benefits": "white"}]))
    check("squares extracted from role descriptions",
          {"c4", "f7", "g8", "e2", "e4"} <= set(sq), str(sq))

    # ---------------- B.4 selection ----------------
    print("B.1 motifs are diffed, and the refutation is carried")
    # A pin that exists BEFORE the move, with the mover playing elsewhere.
    # This is the shape of the reported bug: a queen had been aiming down a
    # diagonal for ten moves and the next move -- a knight on the far wing --
    # was credited with "now skewers", because facts handed to the model
    # under a move are implicitly that move's doing.
    standing = "4k3/3n4/8/1B6/8/2N5/8/4K3 w - - 0 1"
    check("the pin really is there before the move",
          [m["motif"] for m in
           partB.find_pins_and_skewers(chess.Board(standing), chess.WHITE)]
          == ["pin"])
    elsewhere = {
        "ply": 21, "move_number": 11, "color": "white", "san": "Na4",
        "uci": "c3a4", "fen_before": standing, "phase": "middlegame",
        "label": "mistake", "cp_before": 20, "cp_after": -120,
        "win_drop": 14.0, "accuracy": 55.0, "best_move": "Bd3",
        "best_pv": ["Bd3"], "refutation_move": "a6", "refutation_pv": ["a6"],
        "played_best": False, "only_move": False, "multipv_spread_cp": 40,
        "complexity_band": "low", "mate_status_change": None,
        "time_spent": 12.0, "time_flag": None,
    }
    moved_away = partB.extract_move_facts(elsewhere, in_book=False)
    check("a pre-existing pin is NOT attributed to an unrelated move",
          [m for m in moved_away.motifs if m["motif"] in ("pin", "skewer")] == [],
          str(moved_away.motifs))

    check("a faulted move carries the refutation",
          moved_away.refutation_move == "a6")
    check("and says whose move it is",
          moved_away.refutation_by == "black", str(moved_away.refutation_by))
    check("the refutation side is always the opponent",
          moved_away.refutation_by != moved_away.you_are)
    check("the prompt is shown the refutation",
          "refutation_move" in moved_away.for_prompt())

    # On a good move the same field would be an ordinary reply, and a comment
    # would dress it up as a punishment.
    good = dict(elsewhere, label="best", played_best=True)
    good_facts = partB.extract_move_facts(good, in_book=False)
    check("a good move carries NO refutation",
          good_facts.refutation_move is None and good_facts.refutation_pv == [],
          str(good_facts.refutation_move))
    check("and no refutation colour either",
          good_facts.refutation_by is None)

    # The whole engine line must be sayable, not just its first move.
    board0 = chess.Board()
    whole = partB._line_squares(board0, "e4", ["e4", "e5", "Nf3", "Nc6"])
    check("a PV walk does not replay the lead move and stop early",
          {"e2", "e4", "e7", "e5", "g1", "f3", "b8", "c6"} <= whole,
          str(sorted(whole)))
    check("a PV walk handles a lead that is not already in the line",
          {"e2", "e4", "e7", "e5"} <=
          partB._line_squares(board0, None, ["e4", "e5"]))
    check("and stops cleanly at an illegal continuation",
          partB._line_squares(board0, "e4", ["e4", "Qh8"]) == {"e2", "e4"})

    print("B.1b material is computed, not claimed")
    import claim_guard

    # The reported comment: "...Bxf4 Bxf4 gxf4 Rxf4, which would have won the
    # exchange." Black gives pawn+bishop and takes bishop+pawn -- dead level,
    # and no rook was captured by anyone.
    start = chess.Board(
        "r1bq1rk1/pp2ppbp/2np1np1/3p4/3P1B2/2N1PNP1/PPPQ1PBP/R3K2R b KQ - 0 9")
    lvl = claim_guard.walk_line(start, ["Nh5", "Bg5", "h6", "Bh4"])
    check("a quiet line computes level material", lvl.delta == 0, str(lvl.delta))
    check("and is not truncated", lvl.truncated_at is None, str(lvl.truncated_at))

    bad = claim_guard.check_comment("which would have won the exchange", lvl)
    check("a false exchange claim is rejected", bad.ok is False)
    check("and the rejection names the computed delta",
          any("delta=+0" in v for v in bad.violations), str(bad.violations))

    # Tense. The shipped comment said "would have WON", and a lexicon
    # matching only "wins/win" let it through.
    for verb in ("wins", "won", "winning", "gains", "gained"):
        r = claim_guard.check_comment(f"Black {verb} the exchange here", lvl)
        check(f'"{verb} the exchange" is caught', r.ok is False)

    # "The exchange" is a rook for a minor, never just +2.
    two_pawns = claim_guard.describe_material(2, [chess.PAWN, chess.PAWN], [])
    exch = claim_guard.describe_material(2, [chess.ROOK], [chess.BISHOP])
    check("two pawns is not described as the exchange",
          "exchange" not in two_pawns, two_pawns)
    check("a rook for a minor is", exch == "wins the exchange", exch)
    # And a piece identity is never inferred from the number alone.
    knight_for_pawn = claim_guard.describe_material(2, [chess.KNIGHT], [chess.PAWN])
    check("a knight won for a pawn is not called 'two pawns'",
          "two pawns" not in knight_for_pawn, knight_for_pawn)

    # A truncated line has delta 0 for the wrong reason, and must never be
    # read as "material is level".
    broken = claim_guard.line_facts(chess.Board(), ["Qh5", "Nf6"], label="x")
    check("a line that does not fit the position reports where it broke",
          broken["truncated_at"] == 0, str(broken["truncated_at"]))

    # POV. walk_line measures from the side to move at the START of the
    # line; a refutation starts with the OPPONENT, so the phrase must be
    # flipped or the comment says "you lose a rook" when the player won one.
    after_blunder = chess.Board()
    for san in "e4 e5 Nf3 Nc6 Bc4 Nf6 Ng5 d5 exd5 Nxd5".split():
        after_blunder.push_san(san)
    unflipped = partB._line_facts(after_blunder, "Nxf7",
                                  ["Nxf7", "Kxf7"], "refutation")
    flipped = partB._line_facts(after_blunder, "Nxf7",
                                ["Nxf7", "Kxf7"], "refutation", flip=True)
    check("a refutation's material flips to the player's point of view",
          flipped["material_delta_pawns"] == -unflipped["material_delta_pawns"],
          f'{unflipped["material_delta_pawns"]} vs {flipped["material_delta_pawns"]}')
    check("and the flipped side is the one the comment addresses",
          flipped["material_delta_pawns"] > 0,
          str(flipped["material_delta_pawns"]))

    print("B.4b budget and salvage")
    import batch_budget as bb

    # The measured failure: at medium effort a batch of 5 left ~16% headroom
    # against a reasoning cost that is not deterministic, so the eventful
    # batches -- the ones worth commenting on -- were the ones that blew it.
    check("low effort needs far less than medium for the same batch",
          bb.move_pass_budget(5, "low") < bb.move_pass_budget(5, "medium") * 0.6,
          f'{bb.move_pass_budget(5, "low")} vs {bb.move_pass_budget(5, "medium")}')
    check("the budget grows with the batch rather than being a constant",
          bb.move_pass_budget(8, "low") > bb.move_pass_budget(3, "low"))
    check("and is clamped to the provider's ceiling",
          bb.move_pass_budget(20, "medium", ceiling=1800) == 1800)
    check("at 1800 tokens low effort fits 8 moves where medium fit 4",
          bb.max_batch_for_budget(1800, "low") == 8
          and bb.max_batch_for_budget(1800, "medium") == 4,
          f'{bb.max_batch_for_budget(1800, "low")} / '
          f'{bb.max_batch_for_budget(1800, "medium")}')
    check("a batch of 5 at low effort clears the live ceiling with room",
          bb.move_pass_budget(5, "low") < 1800 * 0.7,
          str(bb.move_pass_budget(5, "low")))

    check("the schema caps comment length, so the answer half is knowable",
          partB.MOVE_SCHEMA["properties"]["comments"]["items"]
          ["properties"]["comment"]["maxLength"] == bb.MAX_COMMENT_CHARS)

    # Split-retry. A model that can only manage three at a time should still
    # produce every comment, rather than losing the whole batch.
    class TooBig(Exception):
        pass

    calls = {"n": 0}

    def only_three(items):
        calls["n"] += 1
        if len(items) > 3:
            raise TooBig("Response was truncated; lower the batch size")
        return list(items)

    res = bb.run_with_split_retry(list(range(9)), only_three, error_type=TooBig)
    check("a batch too large for the model is split until it fits",
          len(res.produced) == 9 and not res.failed,
          f"{len(res.produced)}/9 in {res.calls} calls, {res.splits} splits")
    check("and the old behaviour would have produced nothing",
          res.splits > 0)

    # But NOT for errors that splitting cannot fix.
    hard_calls = {"n": 0}

    def dead_key(items):
        hard_calls["n"] += 1
        raise TooBig("401 invalid api key")

    res2 = bb.run_with_split_retry(list(range(8)), dead_key, error_type=TooBig)
    check("a non-truncation error is not retried -- splitting a dead key or "
          "an exhausted quota just burns what is left",
          hard_calls["n"] == 1, str(hard_calls["n"]))
    check("and every item is reported failed", len(res2.failed) == 8)

    check("truncation is told apart from breakage",
          bb.is_truncation(TooBig("Response was truncated"))
          and bb.is_truncation(TooBig("json_validate_failed"))
          and not bb.is_truncation(TooBig("invalid api key")))

    # Reporting.
    gaps = bb.gap_placeholders([54, 56], "Response was truncated; lower the batch")
    check("every uncommented move gets its own entry, not one string at the end",
          len(gaps) == 2 and gaps[0]["ply"] == 54)
    check("the placeholder says why in plain words",
          "output budget" in gaps[0]["unavailable"], str(gaps[0]))
    check("a comment is explicitly null, not missing",
          gaps[0]["comment"] is None and "comment" in gaps[0])

    deduped = bb.dedupe_errors(["same", "same", "other"])
    check("repeated failures collapse instead of reading as three problems",
          deduped == ["same (x2)", "other"], str(deduped))

    print("B.4 selection")
    moves = [fact(ply=i, in_book=False) for i in range(1, 41)]
    moves[9] = fact(ply=10, label="blunder", win_drop=30.0, played_best=False, in_book=False)
    moves[19] = fact(ply=20, label="mistake", win_drop=12.0, played_best=False, in_book=False)
    moves[29] = fact(ply=30, label="inaccuracy", win_drop=6.0, played_best=False, in_book=False)
    sel = partB.select_commentable(moves)
    check("every engine-flagged move is selected",
          {10, 20, 30} <= set(sel), str(sel))
    check("budget respected", len(sel) <= partB.MAX_COMMENTS_PER_GAME, str(len(sel)))
    check("proportional cap applies to short games",
          len(partB.select_commentable(moves[:12])) <= 5,
          str(len(partB.select_commentable(moves[:12]))))
    check("empty game selects nothing", partB.select_commentable([]) == [])

    # A book move the engine faulted must still be selected -- the bug that
    # dropped the Fried Liver's key mistake out of the timeline.
    bookish = [fact(ply=i, in_book=True) for i in range(1, 25)]
    bookish[9] = fact(ply=10, label="mistake", win_drop=15.0, played_best=False, in_book=True)
    check("faulted book move is still selected",
          10 in partB.select_commentable(bookish),
          str(partB.select_commentable(bookish)))

    # Notability ordering
    blunder = fact(ply=5, label="blunder", win_drop=30.0, played_best=False, in_book=False)
    recapture = fact(ply=6, label="best", win_drop=0.0, played_best=True,
                     material_delta=3, in_book=False)
    check("a blunder outranks a routine recapture",
          partB._notability(blunder) > partB._notability(recapture),
          f"{partB._notability(blunder):.1f} vs {partB._notability(recapture):.1f}")
    endorsed = fact(ply=7, label="best", played_best=True, win_drop=10.0,
                    newly_hanging=[{"piece": "knight", "square": "f7"}], in_book=False)
    oversight = fact(ply=8, label="blunder", played_best=False, win_drop=10.0,
                     newly_hanging=[{"piece": "knight", "square": "f7"}], in_book=False)
    check("an engine-endorsed loose piece ranks below an oversight",
          partB._notability(endorsed) < partB._notability(oversight),
          f"{partB._notability(endorsed):.1f} vs {partB._notability(oversight):.1f}")

    # ---------------- B.4 cache ----------------
    print("B.4 cache")
    cache = partB.CommentCache()
    f1 = fact(ply=3)
    check("cache miss first", cache.get(f1, "m") is None)
    cache.put(f1, "m", "hello")
    check("cache hit after put", cache.get(f1, "m") == "hello")
    check("hit/miss counted", cache.stats()["hits"] == 1 and cache.stats()["misses"] == 1,
          str(cache.stats()))
    check("different model is a different key", cache.get(f1, "other") is None)
    check("different move is a different key",
          cache.get(fact(ply=3, uci="d2d4"), "m") is None)
    check("different label is a different key",
          cache.get(fact(ply=3, label="blunder"), "m") is None)
    k_before = partB.CommentCache.key(f1, "m")
    saved = partB.PROMPT_VERSION
    partB.PROMPT_VERSION = saved + "-changed"
    check("prompt version change invalidates the key",
          partB.CommentCache.key(f1, "m") != k_before)
    partB.PROMPT_VERSION = saved

    # ---------------- B.2 / B.3 request assembly ----------------
    print("B.2/B.3 request assembly (both providers, fake clients)")
    gf = partB.GameFacts(
        source_type="pgn", headers={"Result": "1-0"},
        opening={"eco": "C57", "name": "Fried Liver", "left_book_ply": 16},
        result="1-0",
        players={"white": {"name": "a"}, "black": {"name": "b"}},
        moves=[fact(ply=i, in_book=False) for i in range(1, 21)],
        recurring={"white": {}, "black": {}},
    )

    narrative = None
    for provider, expect_model in (("anthropic", "claude-opus-5"),
                                   ("groq", "openai/gpt-oss-120b")):
        com, fake = make_commentator(provider=provider)
        narrative = com.game_pass(gf)
        req = fake.requests[0]
        p = provider

        check(f"[{p}] default model", req["model"] == expect_model, req["model"])
        check(f"[{p}] narrative parsed", narrative.arc.startswith("White sacrificed"))
        check(f"[{p}] critical plies extracted", narrative.critical_plies() == {20})

        if provider == "anthropic":
            check(f"[{p}] adaptive thinking requested",
                  req["thinking"]["type"] == "adaptive", str(req["thinking"]))
            check(f"[{p}] structured output schema sent",
                  req["output_config"]["format"]["type"] == "json_schema")
            check(f"[{p}] schema forbids extra properties",
                  req["output_config"]["format"]["schema"]["additionalProperties"] is False)
            check(f"[{p}] game pass uses high effort",
                  req["output_config"]["effort"] == "high")
            check(f"[{p}] system prompt carries a cache breakpoint",
                  req["system"][0]["cache_control"]["type"] == "ephemeral")
        else:
            check(f"[{p}] strict json_schema requested",
                  req["response_format"]["type"] == "json_schema"
                  and req["response_format"]["json_schema"]["strict"] is True,
                  str(req["response_format"]["type"]))
            check(f"[{p}] schema forbids extra properties",
                  req["response_format"]["json_schema"]["schema"]["additionalProperties"] is False)
            check(f"[{p}] schema is named", bool(req["response_format"]["json_schema"]["name"]))
            # Clamped to medium: high reasoning does not fit Groq's free-tier
            # 8,000 tokens/minute window alongside the input (measured: the
            # game pass needs >4,800 output tokens at high effort).
            check(f"[{p}] reasoning_effort clamped to the affordable maximum",
                  req.get("reasoning_effort") == "medium",
                  str(req.get("reasoning_effort")))
            check(f"[{p}] system message sent as a role",
                  req["messages"][0]["role"] == "system"
                  and req["messages"][0]["content"] == partB.SYSTEM_GAME)
            check(f"[{p}] output cap uses max_completion_tokens",
                  "max_completion_tokens" in req and "max_tokens" not in req)

        batch = gf.moves[:3]
        comments = com.move_pass(batch, narrative, gf.players)
        mreq = fake.requests[1]
        check(f"[{p}] one comment per requested move", len(comments) == 3, str(len(comments)))
        check(f"[{p}] comments keyed to the right plies",
              {c.ply for c in comments} == {m.ply for m in batch})
        check(f"[{p}] token usage accumulated",
              com.usage["calls"] == 2 and com.usage["input_tokens"] > 0,
              str(dict(com.usage)))
        # The same prompt text reaches both providers; only the envelope differs.
        sent_system = (mreq["messages"][0]["content"] if provider == "groq"
                       else mreq["system"][0]["text"])
        check(f"[{p}] move pass sends the shared SYSTEM_MOVE prompt",
              sent_system == partB.SYSTEM_MOVE)

        if provider == "anthropic":
            blocks = mreq["messages"][0]["content"]
            check(f"[{p}] move pass sends two user blocks", len(blocks) == 2)
            check(f"[{p}] stable game-context block is cached",
                  blocks[0].get("cache_control", {}).get("type") == "ephemeral")
            check(f"[{p}] volatile move facts come after the breakpoint",
                  "cache_control" not in blocks[1])
            # LOW on the move pass, and that is the point: B.3 restates
            # computed facts, so reasoning buys nothing there while being
            # billed as output and spent before any JSON is emitted.
            check(f"[{p}] move pass uses LOW effort",
                  mreq["output_config"]["effort"] == "low",
                  str(mreq["output_config"].get("effort")))
            check(f"[{p}] the game pass still reasons",
                  fake.requests[0]["output_config"]["effort"] in ("high", "medium"),
                  str(fake.requests[0]["output_config"].get("effort")))
        else:
            check(f"[{p}] stable and volatile are concatenated (no caching)",
                  "Game context:" in mreq["messages"][-1]["content"]
                  and "Comment on these moves:" in mreq["messages"][-1]["content"])
            check(f"[{p}] reasoning_effort LOW on the move pass",
                  mreq.get("reasoning_effort") == "low",
                  str(mreq.get("reasoning_effort")))
            # And the budget scales with the batch instead of being a
            # constant, which is what left only 16% headroom against a
            # non-deterministic reasoning cost.
            check(f"[{p}] the move budget is sized for the batch",
                  mreq.get("max_completion_tokens", 0) < 1800,
                  str(mreq.get("max_completion_tokens")))

    # The clamp itself, independent of a request.
    gc = partB.PROVIDERS["groq"](api_key="x")
    ac = partB.PROVIDERS["anthropic"](api_key="x")
    check("groq clamps high effort to medium", gc.clamp_effort("high") == "medium")
    check("groq leaves low effort alone", gc.clamp_effort("low") == "low")
    check("anthropic does not clamp", ac.clamp_effort("high") == "high")
    check("groq budgets fit an 8k/min window",
          gc.game_pass_tokens + 3400 < 8000
          and gc.move_pass_tokens + 250 * gc.default_batch_size + 1400 < 8000,
          f"B.2={gc.game_pass_tokens} B.3={gc.move_pass_tokens} "
          f"batch={gc.default_batch_size}")
    check("groq batches are no larger than anthropic's",
          gc.default_batch_size <= ac.default_batch_size,
          f"{gc.default_batch_size} vs {ac.default_batch_size}")

    # for_prompt withholds what made comments worse in live testing.
    withheld = set(fact().to_dict()) - set(fact().for_prompt())
    check("B.3 payload withholds the board and raw evals",
          {"fen_before", "cp_before", "cp_after", "time_flag"} <= withheld,
          str(sorted(withheld)))
    check("B.3 payload keeps the facts a comment needs",
          {"label", "win_drop", "newly_hanging", "motifs", "best_move", "san"}
          <= set(fact().for_prompt()))

    # --- the validator: no comment beats a wrong comment ---
    print("B.3 comment validation")
    import partA as _pa
    _rep = _pa.analyse(_pa.load_pgn(
        '[Event "t"]\n[White "a"]\n[Black "b"]\n\n'
        '1. e4 e5 2. Nf3 Nc6 3. Bc4 Nf6 4. Ng5 d5 5. exd5 Nxd5 6. Nxf7 Kxf7 '
        '7. Qf3+ Ke6 8. Nc3 Nd4 9. Bxd5+ Kd6 10. Qf7 Be6 11. Bxe6 Nxe6 '
        '12. Ne4+ Kd5 13. c4+ Kxe4 14. Qxe6 1-0\n'),
        stockfish_path="stockfish", depth=8, multipv=3, maia2_path=None).to_dict()
    _by = {m.ply: m for m in partB.extract_game_facts(_rep).moves}

    check("facts say what stands on each relevant square",
          _by[26].pieces_on_those_squares.get("e4") == "black king",
          str(_by[26].pieces_on_those_squares))
    check("empty squares are marked, not omitted",
          "empty" in _by[26].pieces_on_those_squares.values(),
          str(_by[26].pieces_on_those_squares))

    # True statements must pass. A false rejection costs a real comment, so
    # these matter as much as the rejections.
    for ply, text in [
        (26, "White's queen on f7 is poised to take on e6."),
        (26, "Your king on e4 is badly exposed."),
        (17, "Your bishop on d5 pins the b7 pawn to the a8 rook."),
        (19, "The engine preferred Qd1."),
    ]:
        check(f"accepts a true claim (ply {ply})",
              partB.validate_comment(text, _by[ply]) == [],
              str(partB.validate_comment(text, _by[ply])))

    # False statements must be caught.
    for ply, text, why in [
        (26, "This wins a bishop on h7.", "square not in facts"),
        (26, "A hanging rook on d8 falls next.", "wrong piece on a real square"),
        (26, "A hanging knight on g1 is lost.", "piece on an empty square"),
    ]:
        check(f"rejects a false claim ({why})",
              partB.validate_comment(text, _by[ply]) != [], text)

    # A rejected comment must never reach the caller.
    class BadComments:
        def __init__(self) -> None:
            self.messages = self

        def create(self, **kwargs: Any) -> FakeAnthropicResponse:
            schema = kwargs["output_config"]["format"]["schema"]
            if "arc" in schema["properties"]:
                return FakeAnthropicResponse(NARRATIVE_PAYLOAD)
            asked = json.loads(
                kwargs["messages"][0]["content"][-1]["text"].rsplit(MOVES_MARKER, 1)[-1])
            # Every comment names a piece that is not on that square.
            return FakeAnthropicResponse({"comments": [
                {"ply": m["ply"], "comment": "This wins a bishop on h7 outright."}
                for m in asked]})

    com_bad, _ = make_commentator()
    com_bad._client = BadComments()
    out = com_bad.move_pass([_by[26]], GameNarrativeStub(), {"white": {}, "black": {}})
    check("a failing comment is marked rejected",
          out and out[0].rejected_because, str(out))
    res_bad = partB.generate_commentary(
        _rep, commentator=com_bad, cache=partB.CommentCache())
    check("rejected comments are withheld from the result",
          all(not c.get("rejected_because") for c in res_bad.comments),
          str(res_bad.comments[:1]))
    check("rejections are reported, not hidden",
          len(res_bad.usage["rejected"]) > 0, str(res_bad.usage["rejected"])[:120])
    check("a rejected comment is not cached",
          res_bad.usage["cache"]["entries"] == 0, str(res_bad.usage["cache"]))

    check("provider is resolvable by name",
          set(partB.PROVIDERS) == {"anthropic", "groq"}, str(sorted(partB.PROVIDERS)))
    try:
        partB.make_commentator("nope")
        check("unknown provider rejected", False)
    except partB.CommentaryError:
        check("unknown provider rejected", True)

    # Replies for plies that were not asked about are dropped.
    class Liar:
        messages = None

        def __init__(self) -> None:
            self.messages = self

        def create(self, **kwargs: Any) -> FakeAnthropicResponse:
            return FakeAnthropicResponse(
                {"comments": [{"ply": 999, "comment": "not asked"}]})

    com2, _ = make_commentator()
    com2._client = Liar()
    check("comments for unrequested plies are discarded",
          com2.move_pass(batch, narrative, gf.players) == [])

    # ---------------- orchestration ----------------
    print("orchestration")
    report = {
        "source_type": "pgn", "headers": {"Result": "1-0"},
        "opening": {"eco": "C57", "name": "Fried Liver", "left_book_ply": 3},
        "moves": [], "white": {}, "black": {},
    }
    # Build a real report-shaped payload from a real game instead of stubbing.
    import partA
    PGN = ('[Event "t"]\n[White "a"]\n[Black "b"]\n\n'
           '1. e4 e5 2. Nf3 Nc6 3. Bc4 Nf6 4. Ng5 d5 5. exd5 Nxd5 6. Nxf7 Kxf7 '
           '7. Qf3+ Ke6 8. Nc3 Nd4 9. Bxd5+ Kd6 10. Qf7 Be6 1-0\n')
    report = partA.analyse(partA.load_pgn(PGN), stockfish_path="stockfish",
                           depth=8, multipv=3, maia2_path=None).to_dict()

    streamed: List[tuple[str, float]] = []
    com3, fake3 = make_commentator()
    cache3 = partB.CommentCache()
    result = partB.generate_commentary(
        report, commentator=com3, cache=cache3,
        progress=lambda s, f, p: streamed.append((s, f)),
    )
    check("commentary available", result.available, str(result.reason))
    check("narrative attached", result.narrative is not None)
    check("comments produced", len(result.comments) > 0, str(len(result.comments)))
    check("B.1 facts attached", result.facts is not None)
    check("facts cover every move",
          len(result.facts["moves"]) == len(report["moves"]))
    check("usage reports provider and model",
          result.usage["provider"] == "anthropic"
          and result.usage["model"] == "claude-opus-5",
          f"{result.usage['provider']}/{result.usage['model']}")
    check("prompt version recorded", result.usage["prompt_version"] == partB.PROMPT_VERSION)
    stages = [s for s, _ in streamed]
    check("facts streamed first", stages[0] == "facts", str(stages[:3]))
    check("narrative streamed before comments",
          stages.index("narrative") < stages.index("comments"), str(stages))
    check("progress ends at 1.0", streamed[-1][1] == 1.0, str(streamed[-1]))
    check("batching bounded", result.usage["batches"] <= partB.MAX_BATCHES_PER_GAME,
          str(result.usage["batches"]))
    json.dumps(result.to_dict())
    check("result is JSON-serialisable", True)

    # Second run over the same game should hit the cache for non-critical plies.
    com4, _ = make_commentator()
    result2 = partB.generate_commentary(report, commentator=com4, cache=cache3)
    check("cache hits on a repeat analysis", result2.usage["cache"]["hits"] > 0,
          str(result2.usage["cache"]))
    check("fewer moves regenerated second time",
          result2.usage["generated"] < result.usage["generated"],
          f"{result2.usage['generated']} vs {result.usage['generated']}")

    # A failing batch loses its own comments, not the whole pass. Needs more
    # than one batch to mean anything, so the batch size is forced down --
    # this game selects only five plies, which is a single default batch.
    com5, fake5 = make_commentator(raise_on={3})   # request 1 = game pass
    result3 = partB.generate_commentary(report, commentator=com5,
                                        cache=partB.CommentCache(), batch_size=2)
    check("multiple batches were issued", result3.usage["batches"] >= 2,
          str(result3.usage["batches"]))
    check("a failed batch is isolated", result3.available, str(result3.reason))
    check("surviving batches still produced comments", len(result3.comments) > 0,
          str(len(result3.comments)))
    check("the failure is reported", bool(result3.usage["errors"]),
          str(result3.usage["errors"]))
    check("the failed batch's comments are absent, not faked",
          len(result3.comments) < result3.usage["generated"],
          f"{len(result3.comments)} of {result3.usage['generated']}")

    # Failure of the game pass degrades to facts only.
    com6, _ = make_commentator(raise_on={1})
    result4 = partB.generate_commentary(report, commentator=com6,
                                        cache=partB.CommentCache())
    check("game-pass failure degrades to facts", not result4.available
          and result4.facts is not None, str(result4.reason))

    # No key at all, for each provider.
    for prov in ("anthropic", "groq"):
        nokey = partB.generate_commentary(
            report, commentator=partB.PROVIDERS[prov](api_key=None))
        check(f"[{prov}] no API key -> facts only, with a reason",
              not nokey.available and nokey.facts is not None
              and "API key" in (nokey.reason or ""), str(nokey.reason))

    # The whole Groq path, end to end, against its fake client.
    comg, _ = make_commentator(provider="groq")
    rg = partB.generate_commentary(report, commentator=comg, cache=partB.CommentCache())
    check("[groq] full pipeline produces commentary", rg.available, str(rg.reason))
    check("[groq] usage reports groq", rg.usage["provider"] == "groq",
          rg.usage["provider"])
    check("[groq] comments produced", len(rg.comments) > 0, str(len(rg.comments)))

    width = max(len(n) for n, _, _ in _results)
    print()
    for name, ok, detail in _results:
        line = f"  [{'PASS' if ok else 'FAIL'}] {name:<{width}}"
        if detail and not ok:
            line += f"   <- {detail}"
        print(line)
    passed = sum(1 for _, ok, _ in _results if ok)
    print(f"\n{passed}/{len(_results)} checks passed")
    return 0 if passed == len(_results) else 1


if __name__ == "__main__":
    sys.exit(main())
