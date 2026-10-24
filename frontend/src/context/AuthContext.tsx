import {
  createContext, useCallback, useContext, useEffect, useMemo, useState,
  type ReactNode,
} from 'react';
import { api, setTokenSource } from '../api/client';
import { AppError, type AuthUser, type UserStats } from '../api/types';

/* The token lives in localStorage so a reload does not sign you out. That is
 * a deliberate trade: localStorage is readable by any script on this origin,
 * so an XSS bug becomes a session theft. The alternative — an httpOnly
 * cookie — needs CSRF handling and a same-site story the single-origin
 * deploy does not currently have. Sessions are revocable server-side
 * (logout deletes the row) and expire in 30 days, which bounds the damage.
 */
const KEY = 'chess.session';

function readToken(): string | null {
  try {
    return localStorage.getItem(KEY);
  } catch {
    return null;        // private window, or blocked site data
  }
}

function writeToken(token: string | null) {
  try {
    if (token) localStorage.setItem(KEY, token);
    else localStorage.removeItem(KEY);
  } catch {
    /* best effort: the session still works for this page view */
  }
}

interface AuthState {
  user: AuthUser | null;
  stats: UserStats | null;
  ready: boolean;
  signup: (u: string, p: string) => Promise<void>;
  login: (u: string, p: string) => Promise<void>;
  logout: () => Promise<void>;
  refresh: () => Promise<void>;
}

const Ctx = createContext<AuthState | null>(null);

export function useAuth(): AuthState {
  const state = useContext(Ctx);
  if (!state) throw new Error('useAuth used outside AuthProvider');
  return state;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setToken] = useState<string | null>(readToken);
  const [user, setUser] = useState<AuthUser | null>(null);
  const [stats, setStats] = useState<UserStats | null>(null);
  const [ready, setReady] = useState(false);

  // The api module reads the token through this rather than importing the
  // context, so it stays free of React.
  useEffect(() => {
    setTokenSource(() => token);
  }, [token]);

  const refresh = useCallback(async () => {
    if (!token) {
      setUser(null);
      setStats(null);
      setReady(true);
      return;
    }
    try {
      const me = await api.me();
      setUser({ id: me.user_id, username: me.email ?? 'you' });
      setStats(me.stats);
    } catch (e) {
      // A dead token should not leave the app in a half-signed-in state.
      if (e instanceof AppError && (e.kind === 'auth' || e.status === 401)) {
        writeToken(null);
        setToken(null);
        setUser(null);
        setStats(null);
      }
    } finally {
      setReady(true);
    }
  }, [token]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const adopt = (r: { token: string; user: AuthUser; stats: UserStats }) => {
    writeToken(r.token);
    setToken(r.token);
    setUser(r.user);
    setStats(r.stats);
  };

  const value = useMemo<AuthState>(() => ({
    user,
    stats,
    ready,
    signup: async (u, p) => adopt(await api.signup(u, p)),
    login: async (u, p) => adopt(await api.login(u, p)),
    logout: async () => {
      try {
        await api.logout();      // kill it server-side, not just locally
      } catch {
        /* already invalid is fine */
      }
      writeToken(null);
      setToken(null);
      setUser(null);
      setStats(null);
    },
    refresh,
  }), [user, stats, ready, refresh]);

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
