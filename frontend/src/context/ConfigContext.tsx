import { createContext, useContext, useEffect, useState, type ReactNode } from 'react';
import { api } from '../api/client';
import { AppError, type AppConfig, type FeatureName } from '../api/types';
import { Link } from 'react-router-dom';

/* Render's free tier sleeps after 15 minutes idle and takes about 60s to wake.
 * That is the first thing most visitors experience, so it gets a designed
 * state rather than a spinner: show nothing for 2s (a warm server answers
 * well inside that), then an explicit "waking the server" card with an
 * elapsed counter, and only treat it as an error after 90s. */
const QUIET_MS = 2_000;
const GIVE_UP_MS = 90_000;

interface ConfigState {
  config: AppConfig | null;
  /** True once we have decided this is a cold start worth explaining. */
  waking: boolean;
  elapsedMs: number;
  failed: boolean;
  retry: () => void;
}

const Ctx = createContext<ConfigState | null>(null);

export function useConfig(): AppConfig {
  const state = useContext(Ctx);
  if (!state?.config) throw new Error('useConfig used outside a resolved ConfigProvider');
  return state.config;
}

/** Is a feature on? Returns the backend's own reason when it is not, so the
 *  UI can state it rather than render a blank panel. `reasons` is keyed the
 *  same as `features`; an absent key means the feature is on. */
export function useFeature(name: FeatureName): { on: boolean; reason?: string } {
  const config = useConfig();
  return { on: !!config.features?.[name], reason: config.reasons?.[name] };
}

/** The server's own limits, so UI copy cannot drift from what it enforces. */
export function useLimits(): AppConfig['limits'] {
  return useConfig().limits;
}

/** Supabase's public values, or null when accounts are off. They arrive from
 *  /config rather than the build so one bundle works across environments. */
export function useSupabase(): { url: string; anonKey: string } | null {
  const c = useConfig();
  if (!c.features?.accounts || !c.supabase_url || !c.supabase_anon_key) return null;
  return { url: c.supabase_url, anonKey: c.supabase_anon_key };
}

export function ConfigProvider({ children }: { children: ReactNode }) {
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [waking, setWaking] = useState(false);
  const [elapsedMs, setElapsed] = useState(0);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    const started = Date.now();
    setFailed(false);
    setWaking(false);
    setElapsed(0);

    const quiet = window.setTimeout(() => !cancelled && setWaking(true), QUIET_MS);
    const ticker = window.setInterval(
      () => !cancelled && setElapsed(Date.now() - started),
      250,
    );

    (async function poll(delay = 0) {
      if (cancelled) return;
      if (delay) await new Promise((r) => window.setTimeout(r, delay));
      try {
        const cfg = await api.config();
        if (cancelled) return;
        setConfig(cfg);
        setWaking(false);
      } catch (e) {
        if (cancelled) return;
        // A 404 is a static host with no API behind it (e.g. GitHub Pages),
        // not a sleeping server: waiting will not help.
        if ((e instanceof AppError && e.status === 404) || Date.now() - started > GIVE_UP_MS) {
          setFailed(true);
          return;
        }
        // Backoff, capped: a waking instance answers somewhere in 30-70s.
        void poll(Math.min(5_000, 500 + (Date.now() - started) / 8));
      }
    })();

    return () => {
      cancelled = true;
      window.clearTimeout(quiet);
      window.clearInterval(ticker);
    };
  }, [attempt]);

  if (config) {
    return (
      <Ctx.Provider
        value={{ config, waking: false, elapsedMs, failed: false, retry: () => {} }}
      >
        {children}
      </Ctx.Provider>
    );
  }

  return (
    <div className="boot">
      {failed ? (
        <div className="panel boot-card" role="alert">
          <h1>The analysis engine is offline</h1>
          <p>
            ChessVision&rsquo;s engine runs on a separate server, and it did not answer.
            It may be asleep or not deployed for this site yet.
          </p>
          <div className="boot-actions">
            <button type="button" className="btn btn-primary" onClick={() => setAttempt((a) => a + 1)}>
              Try again
            </button>
            <Link to="/" className="btn">
              Back to home
            </Link>
          </div>
        </div>
      ) : waking ? (
        <div className="panel boot-card" role="status">
          <h1>Starting the analysis engine</h1>
          <p>It sleeps when idle and takes about a minute to wake.</p>
          <div className="boot-bar" aria-hidden="true">
            <span />
          </div>
          <p className="mono boot-elapsed">{Math.floor(elapsedMs / 1000)}s</p>
        </div>
      ) : null}
    </div>
  );
}
