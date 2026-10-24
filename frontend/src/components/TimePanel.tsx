import type { Colour, GameReport, TimeOffender } from '../api/types';
import './TimePanel.css';

/**
 * A.4, which the backend has been computing from the start and the UI was
 * showing one line of.
 *
 * The useful content is not the totals — it is *which moves* went wrong, and
 * the two ways that happens are opposites:
 *
 *   wasted  long think on a position that did not need one. The cost is the
 *           clock, and it is only interesting when the move was easy.
 *   rushed  a fast move in a position that deserved time. The cost is the
 *           move, and `label` usually shows it.
 *
 * Both are shown with the move's complexity, because "28s on a low-complexity
 * move" is the finding, not "28s".
 *
 * Every row seeks the board to that ply: a time report you cannot click is a
 * list of regrets.
 */
export function TimePanel({
  report,
  onSeek,
}: {
  report: GameReport;
  onSeek: (ply: number) => void;
}) {
  if (!report.time_control.time_analysis_enabled) {
    return (
      <div className="tp panel">
        <h2>Time</h2>
        <p className="dim">
          This game carried no clock times, so there is no time analysis. PGNs
          from Lichess and chess.com include them; a pasted PGN often does not.
        </p>
      </div>
    );
  }

  return (
    <div className="tp panel">
      <h2>
        Time <span className="dim">· {report.time_control.raw}</span>
      </h2>
      {/* The thresholds are partA's WASTED_RATIO / RUSHED_RATIO and the
          complexity bands, stated exactly. The complexity half is the point
          of the metric: a long think on a hard move is not waste, and a fast
          move in a simple position is not rushing. */}
      <p className="dim tp-lead">
        Expected pace {fmt(report.time_control.expected_per_move)} a move. A move
        counts as <b>slow</b> only at 2× that pace or more, at least 3 seconds,
        <em> and</em> in a position the engine found easy; <b>rushed</b> only
        below 0.4× <em>and</em> in a position it found hard.
      </p>

      <div className="tp-sides">
        {(['white', 'black'] as const).map((side) => (
          <SideTime key={side} colour={side} report={report} onSeek={onSeek} />
        ))}
      </div>
    </div>
  );
}

function SideTime({
  colour,
  report,
  onSeek,
}: {
  colour: Colour;
  report: GameReport;
  onSeek: (ply: number) => void;
}) {
  const p = report[colour];
  const t = p.time;
  if (!t.enabled) return null;

  return (
    <section className="tp-side">
      <h3>
        <span className={`tp-dot is-${colour}`} aria-hidden="true" />
        {p.name || colour}
      </h3>

      <dl className="tp-stats">
        <div>
          <dt>Total</dt>
          <dd className="mono">{fmt(t.total_seconds_used)}</dd>
        </div>
        <div>
          <dt>Median</dt>
          <dd className="mono">{fmt(t.median_seconds)}</dd>
        </div>
        <div>
          <dt>Lost to slow moves</dt>
          <dd className="mono">{fmt(t.seconds_wasted)}</dd>
        </div>
      </dl>

      {/* The two conjunctions lead, because they are the ones with a
          consequence attached. Rushing a hard position and finding the move
          anyway is not a mistake; a long think that cost nothing is not
          waste. The raw flags below are the unfiltered view. */}
      <Offenders
        title="rushed a critical position — and it cost something"
        empty=""
        rows={t.critical_rush ?? []}
        onSeek={onSeek}
        kind="critical"
      />
      <Offenders
        title="long think, nothing at stake"
        empty=""
        rows={t.wasted_think ?? []}
        onSeek={onSeek}
        kind="wasted"
      />
      <Offenders
        title={`${t.wasted_count} slow on easy positions`}
        empty="No long think on an easy position."
        rows={t.top_wasted}
        onSeek={onSeek}
        kind="wasted"
      />
      <Offenders
        title={`${t.rushed_count} rushed in hard positions`}
        empty="No snap decision in a hard position."
        rows={t.top_rushed}
        onSeek={onSeek}
        kind="rushed"
      />
    </section>
  );
}

function Offenders({
  title,
  empty,
  rows,
  onSeek,
  kind,
}: {
  title: string;
  empty: string;
  rows: TimeOffender[];
  onSeek: (ply: number) => void;
  kind: 'wasted' | 'rushed' | 'critical';
}) {
  // An empty conjunction list is the normal case and says nothing useful,
  // so it is omitted rather than printed as "none".
  if (rows.length === 0 && !empty) return null;
  return (
    <div className="tp-group">
      <h4 className={`tp-h4 is-${kind}`}>{title}</h4>
      {rows.length === 0 ? (
        <p className="dim tp-empty">{empty}</p>
      ) : (
        <ul className="tp-list">
          {rows.map((r) => (
            <li key={r.ply}>
              <button type="button" className="tp-row" onClick={() => onSeek(r.ply)}>
                <span className="tp-move mono">
                  {r.move_number}
                  {r.ply % 2 === 1 ? '.' : '…'} {r.san}
                </span>
                <span className="tp-secs mono">{fmt(r.seconds)}</span>
                <span className="tp-ratio dim mono">{r.ratio.toFixed(1)}×</span>
                {/* The point of the row: a long think on an easy move, or a
                    fast move on a hard one. Complexity is what makes it a
                    finding rather than a statistic. */}
                <span className={`tp-cx is-${r.complexity_band}`}>
                  {r.complexity_band} complexity
                </span>
                {r.only_move && <span className="tp-only">only move</span>}
                {r.label !== 'best' && r.label !== 'excellent' && (
                  <span className={`tp-label is-${r.label}`}>{r.label}</span>
                )}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function fmt(seconds: number | null): string {
  if (seconds === null) return '—';
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)}s`;
  const m = Math.floor(seconds / 60);
  return `${m}m ${Math.round(seconds % 60)}s`;
}
