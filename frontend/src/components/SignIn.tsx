import { useState } from 'react';
import { AppError } from '../api/types';
import { useAuth } from '../context/AuthContext';
import './SignIn.css';

/**
 * Sign in or create an account.
 *
 * One form, two modes. The honest warning about password recovery is shown
 * on the signup side rather than buried: there is no email on file, so a
 * forgotten password genuinely cannot be reset, and finding that out later
 * would be worse than reading it now.
 */
export function SignIn({ onDone }: { onDone?: () => void }) {
  const { signup, login } = useAuth();
  const [mode, setMode] = useState<'login' | 'signup'>('login');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      if (mode === 'signup') await signup(username.trim(), password);
      else await login(username.trim(), password);
      onDone?.();
    } catch (e2: unknown) {
      // The server's messages name the rule that was broken ("Password must
      // be at least 8 characters"), so they are shown as-is.
      setErr(e2 instanceof AppError ? e2.message : 'Something went wrong.');
      setBusy(false);
    }
  };

  return (
    <form className="signin panel" onSubmit={submit}>
      <div className="seg" role="tablist" aria-label="Sign in or sign up">
        {(['login', 'signup'] as const).map((m) => (
          <button
            key={m}
            type="button"
            role="tab"
            aria-selected={mode === m}
            className={mode === m ? 'seg-opt is-active' : 'seg-opt'}
            onClick={() => { setMode(m); setErr(null); }}
          >
            {m === 'login' ? 'Sign in' : 'Create account'}
          </button>
        ))}
      </div>

      <label className="field">
        Username
        <input
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          autoComplete="username"
          autoCapitalize="none"
          spellCheck={false}
          required
        />
      </label>

      <label className="field">
        Password
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete={mode === 'signup' ? 'new-password' : 'current-password'}
          required
        />
      </label>

      {err && (
        <p className="signin-err" role="alert">
          {err}
        </p>
      )}

      <button type="submit" className="btn btn-primary" disabled={busy}>
        {busy ? '…' : mode === 'signup' ? 'Create account' : 'Sign in'}
      </button>

      {mode === 'signup' && (
        <p className="signin-note">
          There is no email on file and no password reset. If you forget it,
          the account cannot be recovered — pick something you will remember.
        </p>
      )}
    </form>
  );
}
