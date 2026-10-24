import './EvalBar.css';

/**
 * The vertical bar beside the board.
 *
 * Height is win percentage, NOT centipawns: a linear cp scale spends most of
 * its range on positions that are already decided, so +1 and +9 would look
 * nearly the same. partA already computes win% with Lichess's own curve, so
 * the bar is just that number.
 *
 * White fills from the bottom, matching the board's orientation — flipping
 * the board flips the bar, or the two disagree about who is winning.
 */
export function EvalBar({
  winPercentWhite,
  mateIn,
  orientation,
}: {
  /** 0-100, White's point of view. */
  winPercentWhite: number | null;
  mateIn: number | null;
  orientation: 'white' | 'black';
}) {
  // A forced mate is not a percentage. Pin the bar to the mating side rather
  // than letting a capped evaluation read as "almost winning".
  const pct =
    mateIn !== null ? (mateIn > 0 ? 100 : 0) : (winPercentWhite ?? 50);
  const white = Math.max(0, Math.min(100, pct));

  const label =
    mateIn !== null
      ? `Mate in ${Math.abs(mateIn)} for ${mateIn > 0 ? 'White' : 'Black'}`
      : `White ${white.toFixed(0)}%`;

  return (
    <div
      className={`evalbar${orientation === 'black' ? ' is-flipped' : ''}`}
      role="img"
      aria-label={label}
      title={label}
    >
      <div className="evalbar-white" style={{ height: `${white}%` }} />
    </div>
  );
}
