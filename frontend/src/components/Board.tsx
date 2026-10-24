import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { PieceType, Square } from '../api/types';
import { PIECE_NAMES, Piece, isPieceCode } from './pieces';
import './Board.css';

export interface Arrow {
  from: Square;
  to: Square;
  color?: string;
}
export interface Circle {
  square: Square;
  color?: string;
}

export interface BoardProps {
  /** Position. The component holds no game, no history and no turn. */
  fen: string;
  orientation: 'white' | 'black';
  lastMove?: [Square, Square];
  check?: Square;

  /** false = replay only. 'premove' additionally queues one move. */
  interactive: boolean | 'premove';
  /** Ignore input without changing the layout: used while a move is being
   *  validated by the server, so a second click cannot race the first. */
  busy?: boolean;
  movableBy?: 'white' | 'black' | 'both';
  /** Destinations per origin square. Comes from GET /board/moves via
   *  useBoardMoves, not from a client-side rules engine. undefined = any
   *  move allowed (the editor). */
  legalMoves?: Map<Square, Square[]>;
  /** Does this from/to need a promotion choice? Supplied by useBoardMoves,
   *  which knows because four legal moves share the from/to pair. Without
   *  it the board falls back to its own pawn-on-last-rank guess. */
  isPromotion?: (from: Square, to: Square) => boolean;
  onMove?: (from: Square, to: Square, promotion?: PieceType) => void;
  onPremove?: (from: Square, to: Square) => void;

  arrows?: Arrow[];
  circles?: Circle[];
  glyph?: { square: Square; kind: string };

  coordinates?: boolean;
  animationMs?: number;
  /** Announced in a live region after each completed move, e.g. "Nf6. Blunder."
   *  This, not the glyph colour, is the accessible route to classification. */
  announce?: string;
}

const FILES = ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h'] as const;

interface Cell {
  square: Square;
  piece: string | null; // 'P' | 'p' | ...
  light: boolean;
  file: number; // 0-7 as displayed, left to right
  rank: number; // 0-7 as displayed, top to bottom
}

/** FEN board field -> 64 cells in display order for the given orientation. */
function parseFen(fen: string, orientation: 'white' | 'black'): Cell[] {
  const rows = (fen.split(' ')[0] ?? '').split('/');
  const grid: (string | null)[][] = [];
  for (const row of rows) {
    const cells: (string | null)[] = [];
    for (const ch of row) {
      if (/\d/.test(ch)) {
        for (let i = 0; i < Number(ch); i++) cells.push(null);
      } else {
        cells.push(ch);
      }
    }
    while (cells.length < 8) cells.push(null);
    grid.push(cells);
  }
  while (grid.length < 8) grid.push(Array<null>(8).fill(null));

  const out: Cell[] = [];
  for (let r = 0; r < 8; r++) {
    for (let f = 0; f < 8; f++) {
      // r,f are display coordinates. Map them to board coordinates.
      const br = orientation === 'white' ? r : 7 - r;
      const bf = orientation === 'white' ? f : 7 - f;
      const square = `${FILES[bf]}${8 - br}`;
      out.push({
        square,
        piece: grid[br]?.[bf] ?? null,
        light: (br + bf) % 2 === 0,
        file: f,
        rank: r,
      });
    }
  }
  return out;
}

function describe(cell: Cell): string {
  if (!cell.piece) return `${cell.square}, empty`;
  const colour = cell.piece === cell.piece.toUpperCase() ? 'white' : 'black';
  return `${cell.square}, ${colour} ${PIECE_NAMES[cell.piece.toLowerCase()] ?? 'piece'}`;
}

export function Board({
  fen,
  orientation,
  lastMove,
  check,
  interactive,
  busy = false,
  movableBy = 'both',
  legalMoves,
  isPromotion,
  onMove,
  onPremove,
  arrows = [],
  circles = [],
  glyph,
  coordinates = true,
  animationMs = 200,
  announce,
}: BoardProps) {
  const cells = useMemo(() => parseFen(fen, orientation), [fen, orientation]);

  const [selected, setSelected] = useState<Square | null>(null);
  const [cursor, setCursor] = useState(0); // index into cells, for the keyboard
  const [premove, setPremove] = useState<[Square, Square] | null>(null);
  const [promotion, setPromotion] = useState<{ from: Square; to: Square } | null>(null);
  const gridRef = useRef<HTMLDivElement>(null);

  const canMove = interactive !== false && !busy;
  const isPremoveMode = interactive === 'premove';

  // A new position clears transient selection state.
  useEffect(() => {
    setSelected(null);
    setPromotion(null);
  }, [fen]);

  const destinations = useMemo<Set<Square>>(() => {
    if (!selected) return new Set();
    if (!legalMoves) {
      // Editor mode: no legality, so no dots. Any drop is accepted.
      return new Set();
    }
    return new Set(legalMoves.get(selected) ?? []);
  }, [selected, legalMoves]);

  const ownPiece = useCallback(
    (cell: Cell) => {
      if (!cell.piece) return false;
      const colour = cell.piece === cell.piece.toUpperCase() ? 'white' : 'black';
      return movableBy === 'both' || movableBy === colour;
    },
    [movableBy],
  );

  const needsPromotion = useCallback(
    (from: Square, to: Square) => {
      // Prefer the server's answer: it is derived from the legal move list
      // rather than from a rule reimplemented here.
      if (isPromotion) return isPromotion(from, to);
      const cell = cells.find((c) => c.square === from);
      if (!cell || cell.piece?.toLowerCase() !== 'p') return false;
      const lastRank = cell.piece === 'P' ? '8' : '1';
      return to.endsWith(lastRank);
    },
    [cells, isPromotion],
  );

  const commit = useCallback(
    (from: Square, to: Square, promo?: PieceType) => {
      if (isPremoveMode && premove === null && legalMoves && !legalMoves.has(from)) {
        setPremove([from, to]);
        onPremove?.(from, to);
      } else {
        onMove?.(from, to, promo);
      }
      setSelected(null);
      setPromotion(null);
    },
    [isPremoveMode, premove, legalMoves, onMove, onPremove],
  );

  const attempt = useCallback(
    (from: Square, to: Square) => {
      if (from === to) {
        setSelected(null);
        return;
      }
      if (legalMoves && !destinations.has(to) && legalMoves.has(from)) {
        // Illegal target: treat as reselect rather than an error.
        setSelected(null);
        return;
      }
      if (needsPromotion(from, to)) {
        setPromotion({ from, to });
        return;
      }
      commit(from, to);
    },
    [legalMoves, destinations, needsPromotion, commit],
  );

  const activate = useCallback(
    (cell: Cell) => {
      if (!canMove) return;
      if (premove) {
        setPremove(null); // any click on the board clears a queued premove
      }
      if (selected) {
        attempt(selected, cell.square);
        return;
      }
      if (ownPiece(cell) && (!legalMoves || legalMoves.has(cell.square) || !legalMoves.size)) {
        setSelected(cell.square);
      }
    },
    [canMove, premove, selected, attempt, ownPiece, legalMoves],
  );

  const onKeyDown = (e: React.KeyboardEvent) => {
    const col = cursor % 8;
    const row = Math.floor(cursor / 8);
    let next = cursor;
    switch (e.key) {
      case 'ArrowRight':
        next = row * 8 + Math.min(7, col + 1);
        break;
      case 'ArrowLeft':
        next = row * 8 + Math.max(0, col - 1);
        break;
      case 'ArrowDown':
        next = Math.min(7, row + 1) * 8 + col;
        break;
      case 'ArrowUp':
        next = Math.max(0, row - 1) * 8 + col;
        break;
      case ' ':
      case 'Enter': {
        e.preventDefault();
        const cell = cells[cursor];
        if (cell) activate(cell);
        return;
      }
      case 'Escape':
        setSelected(null);
        setPromotion(null);
        return;
      default:
        return;
    }
    e.preventDefault();
    setCursor(next);
  };

  const squareClass = (cell: Cell) => {
    const cls = ['sq', cell.light ? 'sq-light' : 'sq-dark'];
    if (lastMove?.includes(cell.square)) cls.push('is-last');
    if (selected === cell.square) cls.push('is-sel');
    if (destinations.has(cell.square)) cls.push(cell.piece ? 'is-capture' : 'is-dest');
    if (check === cell.square) cls.push('is-check');
    if (premove?.includes(cell.square)) cls.push('is-premove');
    return cls.join(' ');
  };

  return (
    <div className="board-wrap">
      <div
        ref={gridRef}
        className={busy ? 'board is-busy' : 'board'}
        aria-busy={busy || undefined}
        role="grid"
        aria-label="Chess board"
        tabIndex={0}
        onKeyDown={onKeyDown}
        style={{ '--anim': `${animationMs}ms` } as React.CSSProperties}
      >
        {cells.map((cell, i) => (
          <div
            key={cell.square}
            role="gridcell"
            aria-label={describe(cell)}
            aria-selected={selected === cell.square}
            tabIndex={-1}
            data-focused={i === cursor ? 'true' : undefined}
            className={squareClass(cell)}
            onMouseDown={() => {
              setCursor(i);
              activate(cell);
            }}
            onDragStart={(e) => {
              if (!canMove || !ownPiece(cell)) {
                e.preventDefault();
                return;
              }
              e.dataTransfer.setData('text/plain', cell.square);
              setSelected(cell.square);
            }}
            onDragOver={(e) => canMove && e.preventDefault()}
            onDrop={(e) => {
              e.preventDefault();
              const from = e.dataTransfer.getData('text/plain');
              if (from) attempt(from, cell.square);
            }}
            draggable={canMove && ownPiece(cell)}
          >
            {cell.piece && isPieceCode(cell.piece) && <Piece code={cell.piece} />}
            {coordinates && cell.file === 0 && (
              <span className="coord coord-rank" aria-hidden="true">
                {cell.square[1]}
              </span>
            )}
            {coordinates && cell.rank === 7 && (
              <span className="coord coord-file" aria-hidden="true">
                {cell.square[0]}
              </span>
            )}
            {glyph?.square === cell.square && (
              <span className={`glyph glyph-${glyph.kind}`} aria-hidden="true" />
            )}
          </div>
        ))}

        {(arrows.length > 0 || circles.length > 0) && (
          <svg className="board-overlay" viewBox="0 0 8 8" aria-hidden="true">
            {circles.map((c) => {
              const p = centre(c.square, orientation);
              return (
                <circle
                  key={`c-${c.square}`}
                  cx={p.x}
                  cy={p.y}
                  r={0.45}
                  fill="none"
                  stroke={c.color ?? 'var(--arrow)'}
                  strokeWidth={0.08}
                />
              );
            })}
            {arrows.map((a) => {
              const f = centre(a.from, orientation);
              const t = centre(a.to, orientation);
              return (
                <line
                  key={`a-${a.from}${a.to}`}
                  x1={f.x}
                  y1={f.y}
                  x2={t.x}
                  y2={t.y}
                  stroke={a.color ?? 'var(--arrow)'}
                  strokeWidth={0.12}
                  strokeLinecap="round"
                />
              );
            })}
          </svg>
        )}

        {promotion && (
          <PromotionOverlay
            to={promotion.to}
            orientation={orientation}
            white={promotion.to.endsWith('8')}
            onPick={(p) => commit(promotion.from, promotion.to, p)}
            onCancel={() => setPromotion(null)}
          />
        )}
      </div>

      {/* Every completed move is announced once, as SAN plus its class. */}
      <div className="sr-only" aria-live="polite" aria-atomic="true">
        {announce}
      </div>
    </div>
  );
}

function centre(square: Square, orientation: 'white' | 'black') {
  const f = FILES.indexOf(square[0] as (typeof FILES)[number]);
  const r = 8 - Number(square[1]);
  const x = orientation === 'white' ? f : 7 - f;
  const y = orientation === 'white' ? r : 7 - r;
  return { x: x + 0.5, y: y + 0.5 };
}

function PromotionOverlay({
  to,
  orientation,
  white,
  onPick,
  onCancel,
}: {
  to: Square;
  orientation: 'white' | 'black';
  white: boolean;
  onPick: (p: PieceType) => void;
  onCancel: () => void;
}) {
  const col = centre(to, orientation).x - 0.5;
  const pieces: PieceType[] = ['q', 'r', 'b', 'n'];
  return (
    <>
      <div className="promo-scrim" onClick={onCancel} />
      <div
        className="promo"
        role="dialog"
        aria-label="Choose a promotion piece"
        style={{ left: `${(col / 8) * 100}%`, top: white ? 0 : 'auto', bottom: white ? 'auto' : 0 }}
        onKeyDown={(e) => e.key === 'Escape' && onCancel()}
      >
        {pieces.map((p) => {
          const code = white ? (p.toUpperCase() as 'Q' | 'R' | 'B' | 'N') : p;
          return (
            <button
              key={p}
              type="button"
              className="promo-opt"
              aria-label={PIECE_NAMES[p]}
              onClick={() => onPick(p)}
            >
              <Piece code={code} />
            </button>
          );
        })}
      </div>
    </>
  );
}
