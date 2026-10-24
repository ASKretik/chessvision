import { useCallback, useEffect, useState } from 'react';

export type Theme = 'dark' | 'light' | 'system';
const KEY = 'chess.theme';
const ORDER: Theme[] = ['dark', 'light', 'system'];

function read(): Theme {
  try {
    const v = localStorage.getItem(KEY);
    if (v === 'dark' || v === 'light' || v === 'system') return v;
  } catch {
    /* private window or blocked site data: fall through */
  }
  return 'dark';
}

function apply(theme: Theme) {
  const resolved =
    theme === 'system'
      ? window.matchMedia('(prefers-color-scheme: light)').matches
        ? 'light'
        : 'dark'
      : theme;
  document.documentElement.setAttribute('data-theme', resolved);
}

/** Three states, not two. The inline script in index.html applies the stored
 *  value before first paint; this hook keeps it in sync afterwards. */
export function useTheme() {
  const [theme, setTheme] = useState<Theme>(read);

  useEffect(() => {
    apply(theme);
    try {
      localStorage.setItem(KEY, theme);
    } catch {
      /* best effort; the theme still applies for this page view */
    }
  }, [theme]);

  // Follow the OS while on 'system'.
  useEffect(() => {
    if (theme !== 'system') return;
    const mq = window.matchMedia('(prefers-color-scheme: light)');
    const onChange = () => apply('system');
    mq.addEventListener('change', onChange);
    return () => mq.removeEventListener('change', onChange);
  }, [theme]);

  const cycle = useCallback(
    () => setTheme((t) => ORDER[(ORDER.indexOf(t) + 1) % ORDER.length] ?? 'dark'),
    [],
  );

  return { theme, setTheme, cycle };
}
