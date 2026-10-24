import { useMemo } from 'react';
import type { MoveRecord } from '../api/types';
import './EvalGraph.css';

/**
 * The sparkline over the rail.
 *
 * Plotted on win percentage rather than centipawns, for the reason EvalBar
 * gives: a cp axis wastes its range on decided positions. The midline is the
 * drawn position, the fill shows who is ahead, and the faulted moves get a
 * dot each so the eye lands on them before reading a single move.
 *
 * Clicking anywhere seeks: this is the fastest way through a long game, and
 * it is the one control that makes a 120-ply list navigable.
 */
const W = 100;
const H = 28;

export function EvalGraph({
  moves,
  ply,
  onSeek,
}: {
  moves: MoveRecord[];
  /** Cursor, 0 = before the first move. */
  ply: number;
  onSeek: (ply: number) => void;
}) {
  const { area, line, dots } = useMemo(() => {
    if (!moves.length) return { area: '', line: '', dots: [] };

    const x = (i: number) => (moves.length === 1 ? W / 2 : (i / (moves.length - 1)) * W);
    // win_after is the mover's view; the graph is White's, so flip on Black.
    const yOf = (m: MoveRecord) => {
      const whiteWin = m.color === 'white' ? m.win_after : 100 - m.win_after;
      return H - (whiteWin / 100) * H;
    };

    const pts = moves.map((m, i) => `${x(i).toFixed(2)},${yOf(m).toFixed(2)}`);
    return {
      line: `M ${pts.join(' L ')}`,
      area: `M 0,${H / 2} L ${pts.join(' L ')} L ${W},${H / 2} Z`,
      dots: moves
        .map((m, i) => ({ m, i }))
        .filter(({ m }) =>
          m.label === 'blunder' || m.label === 'mistake' || m.label === 'inaccuracy',
        )
        .map(({ m, i }) => ({ cx: x(i), cy: yOf(m), label: m.label, ply: m.ply })),
    };
  }, [moves]);

  if (!moves.length) return null;

  const cursorX = moves.length === 1 ? W / 2 : (Math.min(ply, moves.length - 1) / (moves.length - 1)) * W;

  const seekFromEvent = (clientX: number, target: SVGSVGElement) => {
    const box = target.getBoundingClientRect();
    const frac = Math.max(0, Math.min(1, (clientX - box.left) / box.width));
    onSeek(Math.round(frac * (moves.length - 1)));
  };

  return (
    <svg
      className="evalgraph"
      viewBox={`0 0 ${W} ${H}`}
      preserveAspectRatio="none"
      role="img"
      aria-label={`Evaluation over ${moves.length} moves. Click to seek.`}
      onClick={(e) => seekFromEvent(e.clientX, e.currentTarget)}
    >
      <rect x={0} y={0} width={W} height={H} className="eg-bg" />
      {/* The drawn line. Above it White is better, below it Black. */}
      <line x1={0} y1={H / 2} x2={W} y2={H / 2} className="eg-mid" />
      <path d={area} className="eg-area" />
      <path d={line} className="eg-line" />
      {dots.map((d) => (
        <circle key={d.ply} cx={d.cx} cy={d.cy} r={1.1} className={`eg-dot is-${d.label}`} />
      ))}
      <line x1={cursorX} y1={0} x2={cursorX} y2={H} className="eg-cursor" />
    </svg>
  );
}
