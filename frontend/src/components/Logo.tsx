import { Link } from 'react-router-dom';

/** The ChessVision mark: an eye whose pupil is a board square. */
export function LogoMark({ size = 28 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden="true" focusable="false">
      <defs>
        <linearGradient id="cv-grad" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#6d6dff" />
          <stop offset="1" stopColor="#22d3ee" />
        </linearGradient>
      </defs>
      <rect width="32" height="32" rx="8" fill="url(#cv-grad)" />
      <path
        d="M5.5 16S9.6 9 16 9s10.5 7 10.5 7-4.1 7-10.5 7S5.5 16 5.5 16z"
        fill="none"
        stroke="#fff"
        strokeWidth="2.2"
        strokeLinejoin="round"
      />
      <rect x="12.6" y="12.6" width="6.8" height="6.8" rx="1" fill="#fff" />
      <rect x="12.6" y="12.6" width="3.4" height="3.4" fill="#4f46e5" />
      <rect x="16" y="16" width="3.4" height="3.4" fill="#4f46e5" />
    </svg>
  );
}

export function Logo({ to = '/' }: { to?: string }) {
  return (
    <Link to={to} className="logo" aria-label="ChessVision home">
      <LogoMark />
      <span className="logo-word">
        Chess<b>Vision</b>
      </span>
    </Link>
  );
}
