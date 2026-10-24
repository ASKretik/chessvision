import { NavLink } from 'react-router-dom';
import { useTheme } from '../hooks/useTheme';
import { useAuth } from '../context/AuthContext';
import { Logo } from './Logo';

const TABS = [
  { to: '/analyse', label: 'Analysis' },
  { to: '/puzzles', label: 'Puzzles' },
  { to: '/play', label: 'Play' },
  // Only once there is an account to store anything in. A tab that is
  // always empty is worse than no tab.
  { to: '/study', label: 'Library', signedInOnly: true },
];

export function ThemeButton() {
  const { theme, cycle } = useTheme();
  return (
    <button
      type="button"
      className="hdr-icon"
      onClick={cycle}
      aria-label={`Theme: ${theme}. Switch.`}
      title={`Theme: ${theme}`}
    >
      {theme === 'dark' ? '◑' : theme === 'light' ? '◐' : '○'}
    </button>
  );
}

export function Header() {
  const { user, stats, logout } = useAuth();

  return (
    <header className="hdr">
      <div className="hdr-inner">
        <Logo />
        <nav className="hdr-nav" aria-label="Product">
          {TABS.filter((t) => !t.signedInOnly || user).map((t) => (
            // A tab whose feature flag is false still renders. Hiding it makes
            // the app look broken rather than partly configured; the page itself
            // shows the reason from /config.
            <NavLink
              key={t.to}
              to={t.to}
              className={({ isActive }) => (isActive ? 'hdr-tab is-active' : 'hdr-tab')}
            >
              {t.label}
            </NavLink>
          ))}
        </nav>
        {/* Who is signed in, and their puzzle rating -- the one number that
            makes an account worth having. */}
        {user ? (
          <span className="hdr-user">
            <span className="hdr-avatar" aria-hidden="true">
              {user.username.slice(0, 1).toUpperCase()}
            </span>
            <b>{user.username}</b>
            {stats && <span className="hdr-rating mono">{stats.puzzle_rating}</span>}
            <button type="button" className="hdr-link" onClick={() => void logout()}>
              Sign out
            </button>
          </span>
        ) : (
          <NavLink to="/puzzles" className="btn btn-ghost btn-sm">
            Sign in
          </NavLink>
        )}
        <ThemeButton />
      </div>
    </header>
  );
}
