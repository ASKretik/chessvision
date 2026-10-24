import {
  AppError,
  type AnalyseOptions,
  type AnalyseSource,
  type AnalysisSummary,
  type AppConfig,
  type AppErrorKind,
  type AttemptResponse,
  type AuthResponse,
  type BotInfo,
  type BoardState,
  type JobCreated,
  type JobSnapshot,
  type Me,
  type NextPuzzle,
  type PlayGame,
  type SavedAnalysis,
  type StepResult,
} from './types';

/* Same-origin by default: in production partE.py serves the SPA (spa.py), and
 * in dev Vite proxies the API paths. Set VITE_API_BASE only if you split the
 * two across origins, which also means turning CORS on in the backend. */
const BASE = (import.meta.env.VITE_API_BASE ?? '').replace(/\/$/, '');

/** The auth token getter. The auth layer installs the real one at boot so
 *  this module stays free of a Supabase import. */
let getToken: () => string | null = () => null;
export function setTokenSource(fn: () => string | null) {
  getToken = fn;
}

function classify(status: number, retryAfter: string | null): AppErrorKind {
  if (status === 401) return 'auth';
  if (status === 404) return 'expired';
  if (status === 422) return 'validation';
  if (status === 429) return 'rate_limit';
  // Both memory pressure and a missing core arrive as 503. The difference is
  // Retry-After: present means temporary (auto-retry once), absent means the
  // feature is gone (disable the control).
  if (status === 503) return retryAfter ? 'busy' : 'unavailable';
  return 'unknown';
}

async function request<T>(
  path: string,
  init: RequestInit = {},
  opts: { auth?: boolean } = {},
): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }
  if (opts.auth !== false) {
    const token = getToken();
    if (token) headers.set('Authorization', `Bearer ${token}`);
  }

  let res: Response;
  try {
    res = await fetch(`${BASE}${path}`, { ...init, headers });
  } catch {
    throw new AppError('offline', 0, 'Could not reach the server.');
  }

  if (res.status === 204) return undefined as T;

  const body: unknown = await res.json().catch(() => null);

  if (!res.ok) {
    const retryAfter = res.headers.get('Retry-After');
    const detail =
      (body as { detail?: unknown } | null)?.detail ?? `Request failed (${res.status})`;

    // FastAPI 422 bodies are a list of per-field errors. Surface the first
    // one against its field so a form can render it inline.
    if (res.status === 422 && Array.isArray(detail)) {
      const first = detail[0] as { loc?: unknown[]; msg?: string } | undefined;
      const field = Array.isArray(first?.loc) ? String(first.loc.at(-1)) : undefined;
      throw new AppError('validation', 422, first?.msg ?? 'Invalid input', { field });
    }

    throw new AppError(
      classify(res.status, retryAfter),
      res.status,
      typeof detail === 'string' ? detail : JSON.stringify(detail),
      { retryAfterMs: retryAfter ? Number(retryAfter) * 1000 : undefined },
    );
  }

  return body as T;
}

/* --- endpoints --------------------------------------------------------
 *
 * Every signature here matches a captured payload in docs/examples/ and is
 * checked by tools/check_frontend_types.py. Where the backend wraps a
 * response in an envelope ({puzzle, your_rating, …}), so does the return
 * type -- unwrapping it here would hide fields the UI needs.
 */

export const api = {
  config: () => request<AppConfig>('/config', {}, { auth: false }),

  health: () =>
    request<{ status: string; version: string; env: string; uptime_seconds: number }>(
      '/health',
      {},
      { auth: false },
    ),

  /** Auth is optional here; an anonymous job works but is not saved. */
  analyse: (source: AnalyseSource, options: AnalyseOptions = {}) =>
    request<JobCreated>('/analyse', {
      method: 'POST',
      body: JSON.stringify({ ...source, ...options }),
    }),

  /** Polling fallback for when the SSE stream goes quiet. The report is at
   *  `result`, and is null until the job reaches it. */
  job: (id: string) => request<JobSnapshot>(`/jobs/${encodeURIComponent(id)}`),

  me: () => request<Me>('/me'),

  myAnalyses: () =>
    request<{
      analyses: AnalysisSummary[];
      limit: number;
      offset: number;
      total?: number;
      cap?: number;
    }>('/me/analyses'),

  deleteAnalysis: (id: string) =>
    request<{ deleted: string }>(`/me/analyses/${encodeURIComponent(id)}`,
                                 { method: 'DELETE' }),

  /** The raw row: the report is `result_json`, which is null if the analysis
   *  was stored before the report existed. */
  savedAnalysis: (id: string) =>
    request<SavedAnalysis>(`/me/analyses/${encodeURIComponent(id)}`),

  /* --- the board ------------------------------------------------------
   *
   * The one source of chess rules. The client ships none: this returns every
   * legal move with the position it reaches, so playing a move is a lookup.
   * No auth -- it reads no data. */
  boardMoves: (fen: string) =>
    request<BoardState>(`/board/moves?fen=${encodeURIComponent(fen)}`, {}, { auth: false }),

  /* --- puzzles -------------------------------------------------------- */

  /** Returns an envelope: the puzzle plus your rating and the window it was
   *  drawn from. The solution is not in it and never will be. */
  nextPuzzle: () => request<NextPuzzle>('/puzzles/next'),

  /** Validate the line so far and get the opponent's scripted reply, which
   *  lives inside the hidden solution. Does NOT move the rating. */
  step: (id: string, moves: string[]) =>
    request<StepResult>(`/puzzles/${encodeURIComponent(id)}/step`, {
      method: 'POST',
      body: JSON.stringify({ moves }),
    }),

  /** The whole line, not one move: this is the rating-bearing call, and the
   *  server validates it. The client never decides correctness. */
  attempt: (id: string, moves: string[], msTaken?: number) =>
    request<AttemptResponse>(`/puzzles/${encodeURIComponent(id)}/attempt`, {
      method: 'POST',
      body: JSON.stringify({ moves, ms_taken: msTaken }),
    }),

  /* --- accounts ------------------------------------------------------ */

  signup: (username: string, password: string) =>
    request<AuthResponse>('/api/auth/signup', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    }, { auth: false }),

  login: (username: string, password: string) =>
    request<AuthResponse>('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    }, { auth: false }),

  /** Drops the session server-side, so the token stops working even if it
   *  is still sitting in someone's localStorage. */
  logout: () => request<{ ok: boolean }>('/api/auth/logout', { method: 'POST' }),

  /* --- play ---------------------------------------------------------- */

  bots: () => request<{ bots: BotInfo[] }>('/api/play/bots', {}, { auth: false }),

  newGame: (bot: string, color: 'white' | 'black' | 'random') =>
    request<PlayGame>('/api/play/new', {
      method: 'POST',
      body: JSON.stringify({ bot, color }),
    }),

  game: (id: string) => request<PlayGame>(`/api/play/${encodeURIComponent(id)}`),

  /** Your move and the bot's reply in one round trip: there is nothing
   *  useful to show between the two halves. */
  playMove: (id: string, uci: string) =>
    request<PlayGame>(`/api/play/${encodeURIComponent(id)}/move`, {
      method: 'POST',
      body: JSON.stringify({ uci }),
    }),

  resign: (id: string) =>
    request<PlayGame>(`/api/play/${encodeURIComponent(id)}/resign`,
                      { method: 'POST' }),
};

/** Build the SSE URL. EventSource cannot send an Authorization header, which
 *  is fine because job streams accept anonymous access. If that ever changes,
 *  this must move to a fetch-based reader rather than gaining a ?token=. */
export function jobEventsUrl(jobId: string): string {
  return `${BASE}/jobs/${encodeURIComponent(jobId)}/events`;
}
