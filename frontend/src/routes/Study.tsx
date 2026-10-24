import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../api/client';
import { AppError, type AnalysisSummary } from '../api/types';
import { useAuth } from '../context/AuthContext';
import { SignIn } from '../components/SignIn';
import './Study.css';

/**
 * Every analysis you have run, kept.
 *
 * The tab only exists when signed in -- see Header -- because without an
 * account there is nowhere to keep anything, and a tab that is always empty
 * is worse than no tab.
 *
 * The list is built from denormalised columns, not from the reports: a
 * saved report is 80-150 KB, and fetching thirty to render a list would be
 * megabytes for a few hundred bytes of output.
 */
export function Study() {
  const { user, ready } = useAuth();
  const [rows, setRows] = useState<AnalysisSummary[] | null>(null);
  const [cap, setCap] = useState(30);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!user) return;
    try {
      const d = await api.myAnalyses();
      setRows(d.analyses);
      if (d.cap) setCap(d.cap);
    } catch (e) {
      setErr(e instanceof AppError ? e.message : 'Could not load your analyses.');
      setRows([]);
    }
  }, [user]);

  useEffect(() => {
    void load();
  }, [load]);

  if (ready && !user) {
    return (
      <main className="study">
        <h1>Study</h1>
        <p className="dim" style={{ maxWidth: '60ch' }}>
          Sign in and every game you analyse is kept here — the last {cap}, with
          the board, the move list and the comments exactly as they were.
        </p>
        <SignIn onDone={() => void load()} />
      </main>
    );
  }

  return (
    <main className="study">
      <h1>Study</h1>
      <p className="dim study-lead">
        Your last {cap} analyses. Re-analysing a game you already have replaces
        it rather than pushing out an older one.
      </p>

      {err && <p className="study-err" role="alert">{err}</p>}

      {rows === null ? (
        <p className="dim" role="status">Loading…</p>
      ) : rows.length === 0 ? (
        <div className="panel study-empty">
          <p>Nothing saved yet.</p>
          <p className="dim">
            Analyse a game while signed in and it will appear here.
          </p>
          <Link className="btn btn-primary" to="/analyse">Analyse a game</Link>
        </div>
      ) : (
        <>
          <p className="dim study-count">
            {rows.length} of {cap} kept
          </p>
          <ul className="study-list">
            {rows.map((a) => (
              <li key={a.id} className="panel study-row">
                <Link className="study-main" to={`/analyse/saved/${a.id}`}>
                  <span className="study-players">
                    <b>{a.white || 'White'}</b>
                    <span className="dim"> vs </span>
                    <b>{a.black || 'Black'}</b>
                    {a.result && <span className="mono dim"> {a.result}</span>}
                  </span>
                  <span className="study-meta dim">
                    {a.plies ? `${Math.ceil(a.plies / 2)} moves` : a.source_type}
                    {a.accuracy_white != null && a.accuracy_black != null && (
                      <>
                        {' · '}
                        <span className="mono">{a.accuracy_white}%</span>
                        {' / '}
                        <span className="mono">{a.accuracy_black}%</span>
                      </>
                    )}
                    {' · '}
                    {formatWhen(a.created_at)}
                  </span>
                </Link>
                <button
                  type="button"
                  className="btn study-del"
                  aria-label={`Delete the analysis of ${a.white} vs ${a.black}`}
                  onClick={async () => {
                    // Removed from the list first, then confirmed: the row
                    // is already gone server-side by the time this resolves,
                    // and a reload on failure puts it back.
                    setRows((r) => (r ?? []).filter((x) => x.id !== a.id));
                    try {
                      await api.deleteAnalysis(a.id);
                    } catch {
                      void load();
                    }
                  }}
                >
                  Delete
                </button>
              </li>
            ))}
          </ul>
        </>
      )}
    </main>
  );
}

/** The local store sends epoch seconds, Supabase an ISO string. */
function formatWhen(value: string | number | undefined): string {
  if (value == null) return '';
  const d = typeof value === 'number' ? new Date(value * 1000) : new Date(value);
  if (Number.isNaN(d.getTime())) return String(value);
  const days = Math.floor((Date.now() - d.getTime()) / 86_400_000);
  if (days <= 0) return 'today';
  if (days === 1) return 'yesterday';
  if (days < 30) return `${days} days ago`;
  return d.toLocaleDateString();
}
