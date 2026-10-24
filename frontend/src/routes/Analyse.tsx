import { useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { api } from '../api/client';
import {
  AppError,
  isGame,
  type AnalyseSource,
  type PositionReport,
  type Report,
} from '../api/types';
import { useAnalysisJob } from '../hooks/useAnalysisJob';
import { GameView } from './GameView';
import { useBoardMoves } from '../hooks/useBoardMoves';
import { useFeature, useLimits } from '../context/ConfigContext';
import { Board } from '../components/Board';
import { FeatureOff } from '../components/FeatureOff';
import './Analyse.css';

export function Analyse({ saved = false }: { saved?: boolean }) {
  const { jobId, analysisId } = useParams();
  const navigate = useNavigate();
  const { on, reason } = useFeature('analysis');
  const job = useAnalysisJob(saved ? undefined : jobId);

  const [savedReport, setSavedReport] = useState<Report | null>(null);
  const [savedError, setSavedError] = useState<string | null>(null);

  useEffect(() => {
    if (!saved || !analysisId) return;
    let cancelled = false;
    api
      .savedAnalysis(analysisId)
      // Either backend: the local store sends `report`, Supabase the raw
      // row with `result_json`.
      .then((d) => !cancelled && setSavedReport(d.report ?? d.result_json ?? null))
      .catch((e: unknown) =>
        !cancelled && setSavedError(e instanceof Error ? e.message : 'Could not load it'),
      );
    return () => {
      cancelled = true;
    };
  }, [saved, analysisId]);

  if (!on) return <FeatureOff name="Analysis" reason={reason} />;

  if (!saved && !jobId) return <SourceForm onStarted={(id) => navigate(`/analyse/${id}`)} />;

  const report = saved ? savedReport : job.report;
  const error = saved ? savedError : job.error && describe(job.error);

  if (error) {
    return (
      <main className="page">
        <div className="panel pad" role="alert">
          {error}
          <p>
            <button type="button" className="btn" onClick={() => navigate('/analyse')}>
              Start another
            </button>
          </p>
        </div>
      </main>
    );
  }

  if (!report) {
    return (
      <main className="page">
        <div className="panel pad" role="status">
          <p className="mono">
            {job.stage ?? 'queued'}
            {job.progress ? ` — ${Math.round(job.progress * 100)}%` : ''}
          </p>
          <div className="prog" aria-hidden="true">
            <span style={{ width: `${Math.round((job.progress || 0.02) * 100)}%` }} />
          </div>
          {job.polling && (
            <p className="dim">The live stream dropped; polling instead.</p>
          )}
        </div>
      </main>
    );
  }

  return isGame(report) ? (
    <GameView report={report} done={saved || job.done} />
  ) : (
    <PositionView report={report} />
  );
}

function describe(e: AppError): string {
  if (e.kind === 'expired') return 'This analysis has expired. Jobs are kept for an hour.';
  if (e.kind === 'busy') return 'The server is at its memory limit. Try again in a moment.';
  if (e.kind === 'rate_limit') return e.message;
  return e.message;
}

/* ---------------------------------------------------------------- source */

type SourceKind = 'url' | 'pgn' | 'fen';

function SourceForm({ onStarted }: { onStarted: (jobId: string) => void }) {
  const limits = useLimits();
  const [kind, setKind] = useState<SourceKind>('url');
  const [text, setText] = useState('');
  const [username, setUsername] = useState('');
  const [depth, setDepth] = useState(limits.depth_default);
  const [commentary, setCommentary] = useState(true);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const isChesscom = kind === 'url' && /chess\.com/i.test(text);
  const commentaryOn = useFeature('commentary');

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr(null);
    const value = text.trim();
    if (!value) return setErr('Paste a game first.');

    let source: AnalyseSource;
    if (kind === 'pgn') source = { pgn: value };
    else if (kind === 'fen') source = { fen: value };
    else if (isChesscom) {
      if (!username.trim()) {
        return setErr(
          'chess.com needs the username too: their API has no get-by-url endpoint, ' +
            'so the month has to be searched.',
        );
      }
      source = { chesscom_url: value, chesscom_username: username.trim() };
    } else source = { lichess_url: value };

    setBusy(true);
    try {
      const job = await api.analyse(source, { depth, commentary });
      onStarted(job.job_id);
    } catch (e2: unknown) {
      setErr(e2 instanceof AppError ? describe(e2) : 'Could not start the analysis.');
      setBusy(false);
    }
  };

  const SOURCES = [
    ['url', 'Game link', 'Lichess or Chess.com', '⛓'],
    ['pgn', 'PGN', 'Paste a full game', '☰'],
    ['fen', 'Position', 'Analyse one FEN', '◫'],
  ] as const;

  return (
    <main className="page ws">
      <div className="ws-head">
        <div>
          <p className="ws-crumb dim">Workspace / Analysis</p>
          <h1>New analysis</h1>
          <p className="dim">Import a game and ChessVision will grade every move.</p>
        </div>
        <div className="ws-quota">
          <span className="mono">{limits.rate_limit_per_hour}</span>
          <span className="dim">analyses / hour</span>
        </div>
      </div>

      <div className="ws-grid">
        <form className="panel src" onSubmit={submit}>
          <div className="src-step">
            <span className="src-n">1</span>
            <h2>Choose a source</h2>
          </div>
          <div className="src-tiles" role="tablist" aria-label="Source type">
            {SOURCES.map(([k, label, hint, icon]) => (
              <button
                key={k}
                type="button"
                role="tab"
                aria-selected={kind === k}
                className={kind === k ? 'src-tile is-active' : 'src-tile'}
                onClick={() => setKind(k)}
              >
                <span className="src-tile-icon" aria-hidden="true">
                  {icon}
                </span>
                <b>{label}</b>
                <span className="dim">{hint}</span>
              </button>
            ))}
          </div>

          <div className="src-step">
            <span className="src-n">2</span>
            <h2>
              {kind === 'pgn' ? 'Paste the PGN' : kind === 'fen' ? 'Paste the FEN' : 'Paste the game link'}
            </h2>
          </div>
          {kind === 'pgn' ? (
            <textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              rows={10}
              placeholder={'[Event "…"]\n\n1. e4 e5 2. Nf3 …'}
              aria-label="PGN"
            />
          ) : (
            <input
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder={
                kind === 'fen'
                  ? 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'
                  : 'https://lichess.org/… or https://www.chess.com/game/live/…'
              }
              aria-label={kind === 'fen' ? 'FEN' : 'Game URL'}
            />
          )}

          {isChesscom && (
            <label className="field">
              Chess.com username
              <input
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                placeholder="the player whose archive holds this game"
              />
              <span className="dim">
                Their API has no get-by-url endpoint, so the monthly archive is searched.
              </span>
            </label>
          )}

          <div className="src-step">
            <span className="src-n">3</span>
            <h2>Settings</h2>
          </div>
          <div className="row src-settings">
            <label className="field">
              <span>
                Engine depth <b className="mono">{depth}</b>
              </span>
              <input
                type="range"
                min={limits.depth_min}
                max={limits.depth_max}
                value={depth}
                onChange={(e) => setDepth(Number(e.target.value))}
              />
              <span className="dim">Higher is more precise, but slower.</span>
            </label>
            <label className="check">
              <input
                type="checkbox"
                checked={commentary && commentaryOn.on}
                disabled={!commentaryOn.on}
                onChange={(e) => setCommentary(e.target.checked)}
              />
              <span>
                AI coaching comments
                {!commentaryOn.on && (
                  <span className="dim"> — {commentaryOn.reason ?? 'not configured'}</span>
                )}
              </span>
            </label>
          </div>

          {err && (
            <p className="err" role="alert">
              {err}
            </p>
          )}

          <div className="src-submit">
            <button type="submit" className="btn btn-primary btn-lg" disabled={busy}>
              {busy ? 'Starting…' : 'Run analysis'}
            </button>
            <span className="dim">
              Results are kept for {Math.round(limits.job_ttl_seconds / 60)} minutes.
            </span>
          </div>
        </form>

        <aside className="ws-side">
          <div className="panel pad">
            <h2 className="ws-side-h">Your report includes</h2>
            <ul className="ws-list">
              <li>
                <span className="tag-best">●</span> Move-by-move grading
              </li>
              <li>
                <span className="tag-inaccuracy">●</span> Accuracy per player
              </li>
              <li>
                <span className="tag-mistake">●</span> Win-probability graph
              </li>
              <li>
                <span className="tag-blunder">●</span> Turning points explained
              </li>
              <li>
                <span className="dim">●</span> Estimated playing strength
              </li>
            </ul>
          </div>
          <div className="panel pad ws-tip">
            <h2 className="ws-side-h">Tip</h2>
            <p className="dim">
              On Lichess, open a finished game and copy the URL from your browser. On
              Chess.com, use the game&rsquo;s Share link and add your username.
            </p>
          </div>
        </aside>
      </div>
    </main>
  );
}

/* -------------------------------------------------------- position view */

function formatCp(cp: number): string {
  // White's point of view, always — the backend never flips it.
  const pawns = cp / 100;
  return `${pawns >= 0 ? '+' : '\u2212'}${Math.abs(pawns).toFixed(1)}`;
}


function PositionView({ report }: { report: PositionReport }) {
  const [fen, setFen] = useState(report.fen);
  const board = useBoardMoves(fen);

  return (
    <main className="page">
      <div className="split">
        <div>
          <Board
            fen={fen}
            orientation={report.turn === 'black' ? 'black' : 'white'}
            interactive
            legalMoves={board.map}
            isPromotion={board.isPromotion}
            onMove={(from, to, promo) => {
              const mv = board.find(from, to, promo);
              if (mv) setFen(mv.fen_after);
            }}
          />
          {board.error && (
            <p className="err" role="alert">
              {board.error}
            </p>
          )}
          {fen !== report.fen && (
            <div className="navrow">
              <span className="dim nav-label">exploring from the given position</span>
              <button type="button" className="btn" onClick={() => setFen(report.fen)}>
                back
              </button>
            </div>
          )}
        </div>
        <aside className="panel rail pad">
          <h2>
            {report.eval_cp_white !== null ? formatCp(report.eval_cp_white) : '—'}{' '}
            <span className="dim">{report.turn} to move</span>
          </h2>
          <ol className="lines">
            {report.lines.map((l) => (
              <li key={l.rank}>
                <b className="mono">{l.move}</b>{' '}
                <span className="mono dim">
                  {l.mate_in !== null ? `#${l.mate_in}` : formatCp(l.cp_white ?? 0)}
                </span>
                <p className="dim mono pv">{l.pv.join(' ')}</p>
              </li>
            ))}
          </ol>
          {/* A FEN carries no clock, no accuracy and no rating signal. The
              backend says which panels do not apply so the UI hides them
              rather than rendering them empty. */}
          <ul className="counts dim">
            {report.unavailable.map((u) => (
              <li key={u}>{u}</li>
            ))}
          </ul>
        </aside>
      </div>
    </main>
  );
}
