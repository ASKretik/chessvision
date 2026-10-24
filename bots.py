"""
D.1 -- bots to play against.

plan.md put "Play" in Part D, the designated cut, and the scaffold's Play
page has been an honest "not implemented" ever since. This builds it on
what is already loaded.

WHY MAIA-2 AND NOT A WEAKENED ENGINE
------------------------------------
The usual way to make a weak bot is to hobble a strong one: shallow depth,
random blunders, a skill level knob. Those produce an opponent that plays
like nothing at all -- long stretches of engine-perfect moves punctuated by
a random piece drop. Nobody plays that way, so practising against it teaches
the wrong reflexes.

Maia-2 is already in the image for A.5, and it is a policy trained on human
games at eleven rating bands. Asking it for its top move at a band IS a
human-like opponent at that strength: the mistakes it makes are the mistakes
players of that rating actually make. The weights are loaded once for the
whole process either way, so the bots cost no extra memory.

Stockfish is offered as one additional opponent at the top, because Maia's
bands stop at 2000 and some users want something that simply does not lose.

SAMPLING
--------
Not argmax. Always taking the single most likely move makes a bot that is
deterministic and therefore exploitable -- the same position gives the same
reply forever. Moves are sampled from the policy, with the distribution
sharpened or flattened by `temperature`, and restricted to the top `k` so
the bot never plays an absurd move that the policy gave 0.1% to.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import chess


@dataclass(frozen=True)
class Bot:
    """One opponent the user can pick."""

    id: str
    name: str
    blurb: str
    # The Maia-2 band this bot plays at, or None for the engine bot.
    elo: Optional[int] = None
    engine_depth: Optional[int] = None
    # Higher flattens the policy (more variety, weaker); lower sharpens it.
    temperature: float = 1.0
    top_k: int = 5

    @property
    def is_engine(self) -> bool:
        return self.engine_depth is not None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "blurb": self.blurb,
            "elo": self.elo, "kind": "engine" if self.is_engine else "maia",
        }


# Five rungs across Maia's range plus one engine. Not eleven: a list of
# eleven near-identical options is a worse choice than five clearly
# different ones, and the bands between these are not distinguishable to a
# player in a single game.
BOTS: Tuple[Bot, ...] = (
    # Temperatures below 1 on purpose. At 1.0 the sampler follows the policy
    # exactly, which sounds right but is not: the policy's tail includes
    # moves a human of that rating would consider and reject, and sampling
    # them produced things like Nc6 followed by Nb8. The weakness should
    # come from the BAND, which is already trained on how those players
    # actually move, not from extra noise on top of it. Lower k for the same
    # reason.
    Bot("maia1100", "Pawnbroker", "Hangs pieces, misses tactics. A gentle start.",
        elo=1100, temperature=0.85, top_k=4),
    Bot("maia1400", "Castle Keep", "Plays sensibly, then blunders under pressure.",
        elo=1400, temperature=0.8, top_k=4),
    Bot("maia1700", "Knightfall", "Solid club strength. Punishes loose pieces.",
        elo=1700, temperature=0.7, top_k=3),
    Bot("maia2000", "Endgame", "The strongest human-like setting Maia-2 has.",
        elo=2000, temperature=0.6, top_k=3),
    Bot("sf", "Iron", "Stockfish. Does not blunder, does not get tired.",
        engine_depth=12, temperature=0.0, top_k=1),
)

BOTS_BY_ID: Dict[str, Bot] = {b.id: b for b in BOTS}


def pick_move(
    session: Any,
    board: chess.Board,
    bot: Bot,
    *,
    rng: Optional[random.Random] = None,
    book: Any = None,
) -> chess.Move:
    """
    The bot's move in this position, sampled from Maia-2's policy.

    `session` is a partA.Maia2Session. The opponent Elo fed to the model is
    the bot's own: a game between equals is what the policy was trained on,
    and pretending the human is much stronger or weaker distorts the move
    choice in a way that has nothing to do with the position.

    `book` is A.6's opening book, and it matters more than it looks. Maia
    models are trained with the opening plies skipped -- those moves are
    memorised, not chosen, so including them teaches nothing. The result is
    that the policy has barely seen the STARTING position, and asking it
    there returns something that is not human play at all: measured at band
    1700, Nf3 0.394 / Nc3 0.211 / e4 0.046, and self-play produced knights
    shuffling out and back to their own squares.

    The model is fine -- mate-in-one in the same check scores 0.989. It is
    simply being asked outside its training distribution. So while the game
    is still in known theory the choice is restricted to moves that STAY in
    theory, and Maia picks among those. Out of book, it chooses freely.
    """
    rng = rng or random
    legal = list(board.legal_moves)
    if not legal:
        raise ValueError("No legal moves in this position")
    if len(legal) == 1:
        return legal[0]

    elo = bot.elo or 1500
    row = session.log_probs([board], elo_self=elo, elo_oppo=elo)[0]

    scored: List[Tuple[float, chess.Move]] = []
    for mv in legal:
        col = session.move_column_for(board, mv)
        if col is None:
            continue
        lp = float(row[col])
        if math.isfinite(lp):
            scored.append((lp, mv))
    if not scored:
        # Vocabulary miss on every move: a mismatched export, which is a bug
        # worth failing loudly for elsewhere, but mid-game the playable
        # answer is a legal move rather than an exception.
        return rng.choice(legal)

    scored.sort(key=lambda t: -t[0])

    # Two filters on Maia's own ranking, in order. Both keep the bot's
    # preferences and only narrow what it may prefer.

    # 1. Do not walk back into a position this game has already had. The
    #    policy has no memory, so nothing stops it answering Nf3 with Ng1;
    #    the book filter alone does not help, because the position it
    #    returns to is in book too. This is what stopped the shuffling.
    fresh = []
    for lp, mv in scored:
        board.push(mv)
        try:
            if not board.is_repetition(2):
                fresh.append((lp, mv))
        finally:
            board.pop()
    if fresh:
        scored = fresh

    # 2. In book, keep only the moves that stay in book. A filter on the
    #    ranking rather than a separate opening chooser, so the bot still
    #    has preferences -- they are just confined to lines people play.
    if book is not None:
        in_book = []
        for lp, mv in scored:
            board.push(mv)
            try:
                if book.contains(board):
                    in_book.append((lp, mv))
            finally:
                board.pop()
        if in_book:
            scored = in_book

    scored = scored[: max(1, bot.top_k)]

    if bot.temperature <= 0.01:
        return scored[0][1]

    # Softmax over the kept log-probabilities, with temperature. Subtracting
    # the max first keeps exp() from underflowing on a long tail.
    top = scored[0][0]
    weights = [math.exp((lp - top) / bot.temperature) for lp, _ in scored]
    total = sum(weights)
    if total <= 0:
        return scored[0][1]
    r = rng.random() * total
    for (lp, mv), w in zip(scored, weights):
        r -= w
        if r <= 0:
            return mv
    return scored[-1][1]


@dataclass
class Game:
    """One game in progress. Lives in memory; see the store below."""

    id: str
    bot_id: str
    user_color: bool                      # chess.WHITE / chess.BLACK
    board: chess.Board = field(default_factory=chess.Board)
    moves: List[str] = field(default_factory=list)   # SAN
    created_at: float = 0.0
    updated_at: float = 0.0
    resigned: bool = False

    @property
    def bot(self) -> Bot:
        return BOTS_BY_ID[self.bot_id]

    @property
    def user_to_move(self) -> bool:
        return self.board.turn == self.user_color

    def outcome(self) -> Optional[str]:
        """Human-readable result, or None while the game is live."""
        if self.resigned:
            return "You resigned"
        oc = self.board.outcome(claim_draw=True)
        if oc is None:
            return None
        if oc.winner is None:
            return f"Draw ({oc.termination.name.lower().replace('_', ' ')})"
        won = oc.winner == self.user_color
        return "You win by checkmate" if won else "Bot wins by checkmate"

    @property
    def over(self) -> bool:
        return self.resigned or self.board.outcome(claim_draw=True) is not None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "game_id": self.id,
            "bot": self.bot.to_dict(),
            "you_are": "white" if self.user_color == chess.WHITE else "black",
            "fen": self.board.fen(),
            "moves": list(self.moves),
            "turn": "white" if self.board.turn == chess.WHITE else "black",
            "your_move": self.user_to_move and not self.over,
            "in_check": self.board.is_check(),
            "is_over": self.over,
            "outcome": self.outcome(),
            "last_move": self.moves[-1] if self.moves else None,
        }


class GameStore:
    """
    In-memory games with a TTL, the same shape as JobStore in partE.

    Deliberately not persisted. A game in progress is worth less than a
    puzzle rating -- losing one costs a few minutes, and persisting them
    would mean a schema, a migration and a cleanup job for something nobody
    asked to resume.
    """

    def __init__(self, ttl_seconds: int = 60 * 60 * 6, max_games: int = 200):
        self.ttl = ttl_seconds
        self.max_games = max_games
        self._games: Dict[str, Game] = {}

    def put(self, game: Game) -> None:
        self._sweep()
        if len(self._games) >= self.max_games:
            oldest = min(self._games.values(), key=lambda g: g.updated_at)
            self._games.pop(oldest.id, None)
        self._games[game.id] = game

    def get(self, game_id: str) -> Optional[Game]:
        self._sweep()
        return self._games.get(game_id)

    def _sweep(self) -> None:
        import time
        cutoff = time.time() - self.ttl
        for gid in [g.id for g in self._games.values() if g.updated_at < cutoff]:
            self._games.pop(gid, None)

    def __len__(self) -> int:
        return len(self._games)
