import { useEffect, useRef } from 'react';
import type { Comment, MoveClass, MoveRecord } from '../api/types';
import './MoveList.css';

/** Suffix shown after the SAN, the way an annotated game reads. */
const NAG: Partial<Record<MoveClass, string>> = {
  blunder: '??',
  mistake: '?',
  inaccuracy: '?!',
};

/** Only faults are coloured. Colouring every move leaves nothing standing out. */
function classOf(m: MoveRecord): string {
  return NAG[m.label] ? `is-${m.label}` : '';
}

/**
 * Two columns of moves, numbered, with the comment opening inline beneath
 * the move it belongs to.
 *
 * The comment is a row in the same list rather than a panel elsewhere: a
 * comment about 14.Nbd2 that is not next to 14.Nbd2 makes the reader hold
 * both in their head.
 */
export function MoveList({
  moves,
  ply,
  comments,
  gaps,
  factsFor,
  onSelect,
  onTrain,
}: {
  moves: MoveRecord[];
  /** Cursor: 0 = before the first move, so move i is at index i. */
  ply: number;
  comments: Map<number, Comment>;
  /** Moves the pipeline could not comment on, by ply. Shown as a muted
   *  line rather than silence: an invisible gap looks like the feature
   *  does not work, and is impossible to debug from the outside. */
  gaps?: Map<number, string>;
  factsFor: (m: MoveRecord) => string[];
  onSelect: (ply: number) => void;
  onTrain?: (m: MoveRecord) => void;
}) {
  const selected = useRef<HTMLButtonElement>(null);

  // Keep the cursor visible while stepping with the arrow keys. `nearest`
  // rather than `center`, or every step jolts the whole list.
  useEffect(() => {
    selected.current?.scrollIntoView({ block: 'nearest' });
  }, [ply]);

  const rows: Array<[MoveRecord | undefined, MoveRecord | undefined]> = [];
  for (let i = 0; i < moves.length; i += 2) rows.push([moves[i], moves[i + 1]]);

  const cell = (m: MoveRecord | undefined) => {
    if (!m) return <span className="ml-cell" />;
    const isSel = ply === m.ply;
    return (
      <button
        ref={isSel ? selected : undefined}
        type="button"
        className={`ml-cell ml-move ${classOf(m)}${isSel ? ' is-sel' : ''}`}
        onClick={() => onSelect(m.ply)}
        aria-current={isSel ? 'true' : undefined}
      >
        {m.san}
        {NAG[m.label]}
      </button>
    );
  };

  return (
    <div className="movelist">
      {rows.map(([w, b], i) => {
        // BOTH moves in a row can carry a comment. An earlier version used
        // find() here and silently dropped the second of every pair -- nine
        // comments rendered as six.
        const commented = [w, b].filter(
          (m): m is MoveRecord => !!m && comments.has(m.ply),
        );
        return (
          <div className="ml-row-group" key={i}>
            <div className="ml-row">
              <span className="ml-no mono">{i + 1}.</span>
              {cell(w)}
              {cell(b)}
            </div>
            {commented.map((m) => (
              <CommentCard
                key={m.ply}
                move={m}
                comment={comments.get(m.ply)!}
                facts={factsFor(m)}
                label={`${m.move_number}${m.color === 'white' ? '.' : '…'} ${m.san}`}
                onTrain={onTrain}
              />
            ))}
            {[w, b]
              .filter((m): m is MoveRecord => !!m && !!gaps?.has(m.ply))
              .map((m) => (
                <p className="ml-gap" key={`gap-${m.ply}`}>
                  <span className="mono">
                    {m.move_number}
                    {m.color === 'white' ? '.' : '…'} {m.san}
                  </span>{' '}
                  — no comment: {gaps!.get(m.ply)}
                </p>
              ))}
          </div>
        );
      })}
    </div>
  );
}

function CommentCard({
  move,
  comment,
  facts,
  label,
  onTrain,
}: {
  move: MoveRecord;
  comment: Comment;
  facts: string[];
  /** Which move this is about. Needed once a row can show two cards. */
  label: string;
  onTrain?: (m: MoveRecord) => void;
}) {
  return (
    <div className="ml-comment">
      <p className="ml-about mono">{label}</p>
      <p className="ml-prose">{comment.comment}</p>
      {/* The B.1 facts the comment was grounded in. Shown because a claim a
          reader can check is worth more than one they must take on trust. */}
      {facts.length > 0 && <p className="ml-facts mono">{facts.join(' · ')}</p>}
      {onTrain && (
        <button type="button" className="ml-train" onClick={() => onTrain(move)}>
          Train this position →
        </button>
      )}
    </div>
  );
}
