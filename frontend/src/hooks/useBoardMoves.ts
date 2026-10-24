import { useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../api/client';
import type { BoardState, LegalMove, PieceType, Square } from '../api/types';

/**
 * Legal moves for a position, from the server.
 *
 * The client holds no chess rules. Legality, SAN and the position each move
 * reaches all come from GET /board/moves, so there is one implementation of
 * the rules and the board cannot disagree with the validator about what is
 * legal. Playing a move is: find it here, take its `fen_after`.
 *
 * `Board` wants a Map<from, to[]> for its dots; `find()` is for actually
 * playing one, because the UCI, the SAN and the resulting FEN all live on
 * the move and none of them can be computed here.
 *
 * A failed load is reported, never swallowed: without it the board silently
 * has no moves and looks broken rather than disconnected.
 */
export interface BoardMoves {
  state: BoardState | null;
  /** For Board's `legalMoves` prop. */
  map: Map<Square, Square[]>;
  /** The moves from a square, promotions included (four share from+to). */
  from: (sq: Square) => LegalMove[];
  /** One specific move, promotion disambiguated. */
  find: (from: Square, to: Square, promotion?: PieceType) => LegalMove | undefined;
  /** True when this from/to needs the player to pick a piece. */
  isPromotion: (from: Square, to: Square) => boolean;
  loading: boolean;
  error: string | null;
}

const PROMO_LETTER: Record<string, PieceType> = {
  queen: 'q',
  rook: 'r',
  bishop: 'b',
  knight: 'n',
};

const EMPTY = new Map<Square, Square[]>();

export function useBoardMoves(fen: string | null | undefined): BoardMoves {
  const [state, setState] = useState<BoardState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  // Guards against an older request landing after a newer one.
  const wanted = useRef<string | null>(null);

  useEffect(() => {
    wanted.current = fen ?? null;
    if (!fen) {
      setState(null);
      setError(null);
      return;
    }
    setLoading(true);
    let cancelled = false;

    api
      .boardMoves(fen)
      .then((d) => {
        if (cancelled || wanted.current !== fen) return;
        setState(d);
        setError(
          d.legal.length === 0 && !d.is_game_over
            ? 'The server returned no legal moves for this position.'
            : null,
        );
      })
      .catch((e: unknown) => {
        if (cancelled || wanted.current !== fen) return;
        setState(null);
        setError(e instanceof Error ? e.message : 'Could not load legal moves.');
      })
      .finally(() => {
        if (!cancelled && wanted.current === fen) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [fen]);

  const legal = state?.legal ?? [];

  // Memoised on the move list: Board's `destinations` useMemo keys off this
  // Map's identity, so a new one every render would defeat it.
  const map = useMemo(
    () =>
      legal.length
        ? legal.reduce((acc, m) => {
            const dests = acc.get(m.from);
            // Four promotion moves share a from/to pair; one dot is drawn.
            if (dests) {
              if (!dests.includes(m.to)) dests.push(m.to);
            } else {
              acc.set(m.from, [m.to]);
            }
            return acc;
          }, new Map<Square, Square[]>())
        : EMPTY,
    [legal],
  );

  const from = (sq: Square) => legal.filter((m) => m.from === sq);

  const find = (f: Square, t: Square, promotion?: PieceType) => {
    const options = legal.filter((m) => m.from === f && m.to === t);
    if (options.length <= 1) return options[0];
    return (
      options.find((m) => m.promotion && PROMO_LETTER[m.promotion] === promotion) ??
      options.find((m) => m.promotion === 'queen')
    );
  };

  const isPromotion = (f: Square, t: Square) =>
    legal.filter((m) => m.from === f && m.to === t).length > 1;

  return { state, map, from, find, isPromotion, loading, error };
}
