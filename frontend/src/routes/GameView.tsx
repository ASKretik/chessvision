import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import type {
  Colour,
  Comment,
  GameReport,
  MoveRecord,
  PlayerReport,
  RatingEstimate,
} from '../api/types';
import { Board } from '../components/Board';
import { EvalBar } from '../components/EvalBar';
import { EvalGraph } from '../components/EvalGraph';
import { MoveList } from '../components/MoveList';
import { PlayerStrip } from '../components/PlayerStrip';
import { TimePanel } from '../components/TimePanel';
import { useBoardMoves } from '../hooks/useBoardMoves';
import './GameView.css';

const START_FEN = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1';

/**
 * The analysis page proper.
 *
 * Cursor convention: `ply` is the number of moves PLAYED, so 0 is the start
 * and moves.length is the final position. moves[ply] is therefore the move
 * about to be played, and moves[ply - 1] the one just played. Getting this
 * off by one puts the comment on the wrong move, so it is stated once here
 * and never re-derived.
 */
export function GameView({ report, done }: { report: GameReport; done: boolean }) {
  const moves = report.moves;
  const navigate = useNavigate();

  const [ply, setPly] = useState(0);
  const [flipped, setFlipped] = useState(false);
  const [explore, setExplore] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);

  const played = ply > 0 ? moves[ply - 1] : undefined;
  const next = moves[ply];
  const gameFen = played ? played.fen_after : (next?.fen_before ?? START_FEN);
  const fen = explore ?? gameFen;
  const board = useBoardMoves(fen);

  const go = useCallback(
    (p: number) => {
      setExplore(null);
      setPly(Math.max(0, Math.min(p, moves.length)));
    },
    [moves.length],
  );

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (t && /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)) return;
      if (e.key === 'ArrowLeft') go(ply - 1);
      else if (e.key === 'ArrowRight') go(ply + 1);
      else if (e.key === 'Home') go(0);
      else if (e.key === 'End') go(moves.length);
      else if (e.key === 'f') setFlipped((f) => !f);
      else return;
      e.preventDefault();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [ply, moves.length, go]);

  // `commentary` is absent on the `analysis` event — Part A's report lands
  // seconds before Part B's. An empty map is the right answer then, not a
  // crash: the board, the timeline and the figures are all renderable.
  // Moves the pipeline could not comment on. Rendered inline so a gap is
  // visible where it happened rather than as one string at the bottom.
  const gaps = useMemo(() => {
    const m = new Map<number, string>();
    const usage = report.commentary?.usage as
      { gaps?: Array<{ ply: number; unavailable: string }> } | undefined;
    for (const g of usage?.gaps ?? []) m.set(g.ply, g.unavailable);
    return m;
  }, [report.commentary]);

  const comments = useMemo(() => {
    const m = new Map<number, Comment>();
    for (const c of report.commentary?.comments ?? []) m.set(c.ply, c);
    return m;
  }, [report.commentary]);

  const orientation: Colour = flipped ? 'black' : 'white';
  // Bottom of the board is whoever is facing you.
  const bottom = orientation;
  const top: Colour = orientation === 'white' ? 'black' : 'white';
  const showClock = report.time_control.time_analysis_enabled;

  const copy = async (what: 'FEN' | 'PGN') => {
    const text = what === 'FEN' ? fen : pgnOf(report);
    try {
      await navigator.clipboard.writeText(text);
      setCopied(what);
      window.setTimeout(() => setCopied(null), 1600);
    } catch {
      // Clipboard needs a secure context and permission; neither is
      // guaranteed. Say so rather than silently doing nothing.
      setCopied('denied');
      window.setTimeout(() => setCopied(null), 2400);
    }
  };

  return (
    <main className="gv">
      <div className="gv-board-col">
        <PlayerStrip
          colour={top}
          player={report[top]}
          moves={moves}
          ply={ply}
          showClock={showClock}
          baseSeconds={report.time_control.base_seconds}
        />

        <div className="gv-board-row">
          <EvalBar
            winPercentWhite={winPercentWhite(played, next)}
            mateIn={null}
            orientation={orientation}
          />
          <Board
            fen={fen}
            orientation={orientation}
            interactive
            legalMoves={board.map}
            isPromotion={board.isPromotion}
            lastMove={
              !explore && played
                ? ([played.uci.slice(0, 2), played.uci.slice(2, 4)] as [string, string])
                : undefined
            }
            onMove={(from, to, promo) => {
              const mv = board.find(from, to, promo);
              if (mv) setExplore(mv.fen_after);
            }}
            announce={played ? `${played.san}. ${played.label}.` : undefined}
          />
        </div>

        <PlayerStrip
          colour={bottom}
          player={report[bottom]}
          moves={moves}
          ply={ply}
          showClock={showClock}
          baseSeconds={report.time_control.base_seconds}
        />

        {board.error && (
          <p className="gv-err" role="alert">
            {board.error}
          </p>
        )}

        <div className="gv-controls">
          <div className="gv-nav">
            <button type="button" className="btn" onClick={() => go(0)} disabled={ply === 0}
                    aria-label="First move" title="Home">⏮</button>
            <button type="button" className="btn" onClick={() => go(ply - 1)} disabled={ply === 0}
                    aria-label="Previous move" title="Left arrow">◀</button>
            <button type="button" className="btn" onClick={() => go(ply + 1)}
                    disabled={ply >= moves.length && !explore}
                    aria-label="Next move" title="Right arrow">▶</button>
            <button type="button" className="btn" onClick={() => go(moves.length)}
                    disabled={ply >= moves.length && !explore}
                    aria-label="Last move" title="End">⏭</button>
          </div>
          <div className="gv-tools">
            <button type="button" className="btn" onClick={() => setFlipped((f) => !f)}
                    title="f">Flip</button>
            <button type="button" className="btn" onClick={() => void copy('FEN')}>FEN</button>
            <button type="button" className="btn" onClick={() => void copy('PGN')}>PGN</button>
          </div>
        </div>

        <p className="gv-copied" role="status">
          {copied === 'denied'
            ? 'The browser refused clipboard access.'
            : copied
              ? `${copied} copied.`
              : ' '}
        </p>

        {explore && (
          <div className="gv-explore">
            <span>Exploring a variation.</span>
            <button type="button" className="btn" onClick={() => go(ply)}>
              Back to the game
            </button>
          </div>
        )}

        <ReportStrip report={report} />
        <TimePanel report={report} onSeek={(p) => go(p)} />
      </div>

      <aside className="gv-rail panel">
        <EvalHeader report={report} played={played} next={next} />
        <EvalGraph moves={moves} ply={ply} onSeek={(i) => go(i + 1)} />
        {!done && (
          <p className="gv-writing" role="status">
            {report.commentary
              ? 'Writing comments…'
              : 'Analysis ready — comments are still being written…'}
          </p>
        )}
        <MoveList
          moves={moves}
          ply={ply}
          comments={comments}
          gaps={gaps}
          factsFor={(m) => factsFor(report, m)}
          onSelect={(p) => go(p)}
          onTrain={(m) => navigate(`/positions?fen=${encodeURIComponent(m.fen_before)}`)}
        />
        {report.commentary && !report.commentary.available && (
          <p className="gv-nocomment">
            {report.commentary.reason ?? 'No written comments for this game.'}
          </p>
        )}
      </aside>
    </main>
  );
}

/** White's win percentage at the cursor. */
function winPercentWhite(played: MoveRecord | undefined, next: MoveRecord | undefined): number {
  if (played) return played.color === 'white' ? played.win_after : 100 - played.win_after;
  if (next) return next.color === 'white' ? next.win_before : 100 - next.win_before;
  return 50;
}

function formatEval(cp: number): string {
  const pawns = cp / 100;
  return `${pawns >= 0 ? '+' : '−'}${Math.abs(pawns).toFixed(1)}`;
}

function EvalHeader({
  report,
  played,
  next,
}: {
  report: GameReport;
  played: MoveRecord | undefined;
  next: MoveRecord | undefined;
}) {
  const cp = played ? played.cp_after : (next?.cp_before ?? 0);
  const depth = (report.meta as { depth?: number }).depth;
  const best = next?.best_pv?.length ? next.best_pv : played?.best_pv;

  return (
    <div className="gv-evalhead">
      <div className="gv-eval mono">{formatEval(cp)}</div>
      <div className="gv-evalsub">
        for {cp >= 0 ? 'White' : 'Black'}
        {depth ? ` · depth ${depth}` : ''}
      </div>
      {best && best.length > 0 && (
        <div className="gv-best">
          <span className="gv-bestlabel">Best:</span>{' '}
          <span className="mono">{best.slice(0, 6).join(' ')}</span>
        </div>
      )}
    </div>
  );
}

/** The B.1 facts behind a comment, as the short mono lines under the prose. */
/** Labels that mean the move lost ground. Mirrors partB.FAULTED_LABELS. */
const FAULTED = new Set(['blunder', 'mistake', 'inaccuracy']);

/** The refutation as one readable line. `pv[0]` is normally the move itself,
 *  so prefixing it unconditionally would print it twice. */
function refutationLine(m: MoveRecord): string | null {
  if (!m.refutation_move) return null;
  const pv = m.refutation_pv ?? [];
  const moves = pv.length && pv[0] === m.refutation_move ? pv : [m.refutation_move, ...pv];
  return moves.slice(0, 4).join(' ');
}

function factsFor(report: GameReport, m: MoveRecord): string[] {
  const out: string[] = [];
  // Leads, on a faulted move, because it is the reason the move was bad and
  // the comment is now required to open with it — so the reader can check
  // the claim against the line it came from.
  const refutation = FAULTED.has(m.label) ? refutationLine(m) : null;
  if (refutation) out.push(`refuted by ${refutation}`);
  out.push(`eval ${formatEval(m.cp_before)} → ${formatEval(m.cp_after)}`);
  if (m.best_move && !m.played_best) out.push(`best was ${m.best_move}`);
  if (m.mate_status_change === 'forced_mate_lost') out.push('a forced mate was lost');
  if (m.mate_status_change === 'mate_now_forced') out.push('mate is now forced');
  if (m.time_spent !== null && report.time_control.time_analysis_enabled) {
    out.push(`${Math.round(m.time_spent)}s spent`);
  }
  out.push(`${m.complexity_band} complexity`);
  if (m.only_move) out.push('only move');
  return out;
}

/** A PGN good enough to paste back into a board, rebuilt from the timeline. */
function pgnOf(report: GameReport): string {
  const headers = Object.entries(report.headers)
    .map(([k, v]) => `[${k} "${v}"]`)
    .join('\n');
  const body = report.moves
    .map((m, i) => (i % 2 === 0 ? `${m.move_number}. ${m.san}` : m.san))
    .join(' ');
  const result = report.headers.Result ?? '*';
  return `${headers}\n\n${body} ${result}`;
}

/**
 * The rating estimate, shown with what it actually supports.
 *
 * A bare number here was misleading. The estimate is a maximum likelihood
 * over Maia-2's eleven Elo buckets, and on one game that curve is often
 * nearly flat — a real case spanned 1100 to 2000+ within the standard
 * likelihood-ratio cutoff, yet printed "2000+" as though it were a finding.
 * So the range leads when it is wide, and the point estimate is secondary.
 */
function RatingReadout({ est }: { est: RatingEstimate }) {
  const iv = est.interval;
  const point =
    est.elo_method === 'floor'
      ? `${est.elo}+`
      : est.elo_method === 'ceiling'
        ? `under ${est.elo}`
        : String(est.elo);

  const range =
    !iv || iv.buckets <= 1
      ? null
      : iv.low_elo === null && iv.high_elo === null
        ? 'any rating'
        : iv.low_elo === null
          ? `under ${(iv.high_elo ?? 0) + 1}`
          : iv.high_elo === null
            ? `${iv.low_elo}+`
            : `${iv.low_elo}–${iv.high_elo}`;

  return (
    <>
      <p>
        <b className="mono">{point}</b>{' '}
        <span className="dim">estimated from {est.moves_used} moves</span>
      </p>
      {range && (
        <p className={`gv-conf is-${iv.confidence}`}>
          {iv.confidence === 'low' ? 'Very rough: ' : 'Range: '}
          <b>{range}</b>{' '}
          <span className="dim">
            — one game rarely pins this down
            {iv.confidence === 'low' ? ', and this curve is nearly flat' : ''}
          </span>
        </p>
      )}
    </>
  );
}

/**
 * The error profile, as the headline.
 *
 * "4.2 blunders per 100 real decisions, ACPL 62, engine-best agreement 34%"
 * is defensible and useful; an Elo derived from it is neither until the
 * calibration is fitted against labelled games. The denominator is the
 * point — per MOVE, error counts are dominated by game length and by how
 * forcing the game was, and a 70-move grind is not comparable to a 25-move
 * miniature. Per DECISION they are.
 */
function ProfileReadout({ p }: { p: PlayerReport }) {
  const ep = p.error_profile;
  if (!ep || !ep.moves) return null;

  // Below this the rates are noise, and saying so beats printing them.
  const thin = ep.decisions < 8;
  return (
    <div className="gv-profile">
      <p className="gv-profile-head">
        <b className="mono">{ep.decisions}</b>{' '}
        <span className="dim">
          real decisions of {ep.moves} moves
          {thin ? ' — too few to read much into' : ''}
        </span>
      </p>
      {!thin && (
        <ul className="gv-rates">
          <li>
            <b className="mono">{ep.blunder_rate.toFixed(1)}</b>
            <span className="dim">blunders /100</span>
          </li>
          <li>
            <b className="mono">{ep.mistake_rate.toFixed(1)}</b>
            <span className="dim">mistakes /100</span>
          </li>
          <li>
            <b className="mono">{Math.round(ep.acpl)}</b>
            <span className="dim">ACPL</span>
          </li>
          <li>
            <b className="mono">{Math.round(ep.top1_match * 100)}%</b>
            <span className="dim">engine-best</span>
          </li>
        </ul>
      )}

    </div>
  );
}

function ReportStrip({ report }: { report: GameReport }) {
  return (
    <div className="gv-strip">
      {(['white', 'black'] as const).map((side) => {
        const p = report[side];
        const est = p.rating_estimate;
        return (
          <div className="gv-card panel" key={side}>
            <h2>
              {p.name || side} <span className="dim">· {side}</span>
            </h2>
            <p className="gv-acc mono">{p.accuracy}%</p>
            <p className="dim">accuracy · {p.acpl} ACPL</p>
            <ul className="gv-counts">
              <li className="is-blunder">{p.blunders} blunders</li>
              <li className="is-mistake">{p.mistakes} mistakes</li>
              <li className="is-inaccuracy">{p.inaccuracies} inaccuracies</li>
            </ul>
            {/* A.5b leads: these are measured per decision and defensible.
                The Elo derived from them is reported only once the
                calibration has been fitted, which it has not been. */}
            <ProfileReadout p={p} />
            {est.available ? <RatingReadout est={est} /> : (
              <p className="dim">{est.reason ?? 'No rating estimate.'}</p>
            )}

            {p.time.enabled ? (
              <p className="dim">
                {Math.round(p.time.seconds_wasted)}s on {p.time.wasted_count} slow moves ·{' '}
                {p.time.rushed_count} rushed
              </p>
            ) : (
              <p className="dim">No clocks in this game, so no time analysis.</p>
            )}
            {p.weakest_phase && <p className="dim">Weakest phase: {p.weakest_phase}</p>}
          </div>
        );
      })}
      <div className="gv-card panel">
        <h2>Opening</h2>
        <p>
          <b className="mono">{report.opening.eco}</b> {report.opening.name}
        </p>
        {report.opening.left_book_move && (
          <p className="dim">
            Left the book at {report.opening.left_book_move_number}.
            {report.opening.left_book_move}
          </p>
        )}
        <p className="dim">
          {report.moves.length} plies ·{' '}
          {report.time_control.raw ?? 'no time control in the headers'}
        </p>
      </div>
    </div>
  );
}
