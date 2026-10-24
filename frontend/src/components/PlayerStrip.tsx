import type { Colour, MoveRecord, PlayerReport } from '../api/types';
import { Piece, type PieceCode } from './pieces';
import './PlayerStrip.css';

/** Piece values for the material count. The king is never captured. */
const VALUE: Record<string, number> = { p: 1, n: 3, b: 3, r: 5, q: 9 };
const ORDER = ['q', 'r', 'b', 'n', 'p'] as const;

/**
 * Name, rating, captured material and clock — the row above and below the
 * board.
 *
 * The clock is the position's clock, not the game's final one: stepping
 * through the moves has to move it, or it is decoration. It comes from
 * `clock_after` on the last move this player made at or before the cursor.
 */
export function PlayerStrip({
  colour,
  player,
  moves,
  ply,
  showClock,
  baseSeconds,
}: {
  colour: Colour;
  player: PlayerReport;
  moves: MoveRecord[];
  ply: number;
  showClock: boolean;
  /** The time control's starting seconds, shown before either side has
   *  moved — otherwise the clock is blank at the start of every game, which
   *  reads as missing data rather than as "no move yet". */
  baseSeconds: number | null;
}) {
  const captured = capturedBy(moves, ply, colour);
  const balance = materialBalance(moves, ply, colour);
  const clock = clockAt(moves, ply, colour) ?? baseSeconds;

  return (
    <div className="pstrip">
      <span className={`pstrip-dot is-${colour}`} aria-hidden="true" />
      <span className="pstrip-name">{player.name || colour}</span>
      {player.rating_header !== null && (
        <span className="pstrip-rating mono">{player.rating_header}</span>
      )}
      <span className="pstrip-captured" aria-label={describeCaptured(captured, balance)}>
        {ORDER.flatMap((kind) =>
          Array.from({ length: captured[kind] ?? 0 }, (_, i) => (
            <Piece
              key={`${kind}${i}`}
              code={(colour === 'white' ? kind : kind.toUpperCase()) as PieceCode}
            />
          )),
        )}
        {balance > 0 && <span className="pstrip-plus mono">+{balance}</span>}
      </span>
      {showClock && clock !== null && (
        <span className="pstrip-clock mono">{formatClock(clock)}</span>
      )}
    </div>
  );
}

/** What `colour` has captured by `ply`, i.e. the opponent's missing pieces. */
function capturedBy(moves: MoveRecord[], ply: number, colour: Colour) {
  const counts: Record<string, number> = {};
  const seen = moves.slice(0, ply);
  for (const m of seen) {
    if (m.color !== colour) continue;
    // A capture is readable straight off the SAN: nothing else carries an 'x'.
    if (!m.san.includes('x')) continue;
    const victim = victimOf(m);
    if (victim) counts[victim] = (counts[victim] ?? 0) + 1;
  }
  return counts;
}

/** The captured piece's kind, read off the position the move was played
 *  from. Only the move is needed: `fen_before` is the board it landed on. */
function victimOf(m: MoveRecord): string | null {
  const to = m.uci.slice(2, 4);
  const file = to.charCodeAt(0) - 97;
  const rank = 8 - Number(to[1]);
  const rows = m.fen_before.split(' ')[0]?.split('/') ?? [];
  const row = rows[rank];
  if (!row) return null;
  let f = 0;
  for (const ch of row) {
    if (/\d/.test(ch)) {
      f += Number(ch);
    } else {
      if (f === file) return ch.toLowerCase();
      f += 1;
    }
    if (f > file) break;
  }
  // No piece on the target square: an en-passant capture takes a pawn.
  return 'p';
}

function materialBalance(moves: MoveRecord[], ply: number, colour: Colour): number {
  const mine = capturedBy(moves, ply, colour);
  const theirs = capturedBy(moves, ply, colour === 'white' ? 'black' : 'white');
  const sum = (c: Record<string, number>) =>
    Object.entries(c).reduce((t, [k, n]) => t + (VALUE[k] ?? 0) * n, 0);
  return sum(mine) - sum(theirs);
}

/** The clock after this player's most recent move at or before the cursor. */
function clockAt(moves: MoveRecord[], ply: number, colour: Colour): number | null {
  for (let i = Math.min(ply, moves.length) - 1; i >= 0; i--) {
    const m = moves[i];
    if (m && m.color === colour && m.clock_after !== null) return m.clock_after;
  }
  return null;
}

function formatClock(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  return h > 0
    ? `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`
    : `${m}:${String(sec).padStart(2, '0')}`;
}

/* The piece row is decorative; screen readers get this instead. */
function describeCaptured(counts: Record<string, number>, balance: number): string {
  const parts = ORDER.filter((k) => counts[k]).map((k) => `${counts[k]} ${k}`);
  if (!parts.length) return 'No material captured';
  return `Captured ${parts.join(', ')}${balance > 0 ? `, up ${balance}` : ''}`;
}
