import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { Chess } from 'chess.js';
import { Board } from '../components/Board';
import { Footer } from '../components/Footer';
import { Logo } from '../components/Logo';
import { ThemeButton } from '../components/Header';
import type { Square } from '../api/types';
import './Landing.css';

/* The landing page is static: it never calls the backend, so it renders
 * instantly even while the engine server is asleep. The showcase is the
 * Opera Game (Morphy, Paris 1858) with illustrative annotations in the shape
 * a real ChessVision report produces. */

type Tag = 'best' | 'good' | 'inaccuracy' | 'mistake' | 'blunder';

interface Annotated {
  san: string;
  /** White's point of view, in pawns, after the move. */
  eval: number;
  tag: Tag;
  note: string;
}

const OPERA: Annotated[] = [
  { san: 'e4', eval: 0.3, tag: 'best', note: 'Claims the centre and opens lines for the bishop and queen.' },
  { san: 'e5', eval: 0.3, tag: 'best', note: 'The classical reply, matching White in the centre.' },
  { san: 'Nf3', eval: 0.3, tag: 'best', note: 'Develops with tempo against e5.' },
  { san: 'd6', eval: 0.6, tag: 'good', note: "Philidor's Defence: solid, but a little passive." },
  { san: 'd4', eval: 0.6, tag: 'best', note: 'Strikes at the centre before Black is settled.' },
  { san: 'Bg4', eval: 1.3, tag: 'inaccuracy', note: 'Pins the knight, but gives up the bishop pair after dxe5.' },
  { san: 'dxe5', eval: 1.2, tag: 'best', note: 'Forces Black to decide about the pin right now.' },
  { san: 'Bxf3', eval: 1.3, tag: 'good', note: 'Necessary: otherwise Black just loses a pawn.' },
  { san: 'Qxf3', eval: 1.3, tag: 'best', note: 'The queen recaptures and eyes f7.' },
  { san: 'dxe5', eval: 1.4, tag: 'good', note: 'Restores material, but Black is behind in development.' },
  { san: 'Bc4', eval: 1.5, tag: 'best', note: 'A second attacker on f7 — the threat is Qxf7#.' },
  { san: 'Nf6', eval: 1.6, tag: 'good', note: 'Blocks the queen and defends against mate.' },
  { san: 'Qb3', eval: 2.4, tag: 'best', note: 'A double attack on b7 and f7.' },
  { san: 'Qe7', eval: 2.5, tag: 'inaccuracy', note: 'Holds f7 but blocks the f8-bishop, keeping the king stuck.' },
  { san: 'Nc3', eval: 2.4, tag: 'best', note: 'Development over material — Morphy declines Qxb7.' },
  { san: 'c6', eval: 2.6, tag: 'good', note: 'Shields b7, at the cost of yet another pawn move.' },
  { san: 'Bg5', eval: 2.9, tag: 'best', note: 'Pins the f6 knight to the queen.' },
  { san: 'b5', eval: 5.2, tag: 'blunder', note: 'Opens lines towards Black’s own king. Qb4 was necessary.' },
  { san: 'Nxb5', eval: 5.0, tag: 'best', note: 'A sacrifice to rip open the queenside.' },
  { san: 'cxb5', eval: 5.1, tag: 'good', note: 'Accepting is natural; there is nothing better.' },
  { san: 'Bxb5+', eval: 5.2, tag: 'best', note: 'Check, with every White piece joining in.' },
  { san: 'Nbd7', eval: 5.3, tag: 'good', note: 'The only block, and now d7 is pinned.' },
  { san: 'O-O-O', eval: 6.0, tag: 'best', note: 'Castles straight into the attack on d7.' },
  { san: 'Rd8', eval: 6.1, tag: 'good', note: 'Defends d7 a second time.' },
  { san: 'Rxd7', eval: 6.4, tag: 'best', note: 'Removes the defender. The final combination begins.' },
  { san: 'Rxd7', eval: 6.5, tag: 'good', note: 'Forced.' },
  { san: 'Rd1', eval: 8.0, tag: 'best', note: 'Piles up on the pinned rook.' },
  { san: 'Qe6', eval: 99, tag: 'mistake', note: 'Allows mate in three. The position was lost anyway.' },
  { san: 'Bxd7+', eval: 99, tag: 'best', note: 'Clears the d-file with check.' },
  { san: 'Nxd7', eval: 99, tag: 'good', note: 'Forced.' },
  { san: 'Qb8+', eval: 99, tag: 'best', note: 'The famous queen sacrifice: deflecting the knight from d7.' },
  { san: 'Nxb8', eval: 99, tag: 'good', note: 'Forced.' },
  { san: 'Rd8#', eval: 99, tag: 'best', note: 'Checkmate with the last two pieces on the board.' },
];

const START_FEN = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1';

interface Frame {
  fen: string;
  last?: [Square, Square];
  move?: Annotated;
  ply: number;
}

function buildFrames(): Frame[] {
  const game = new Chess();
  const frames: Frame[] = [{ fen: START_FEN, ply: 0 }];
  OPERA.forEach((m, i) => {
    const mv = game.move(m.san);
    frames.push({ fen: game.fen(), last: [mv.from, mv.to], move: m, ply: i + 1 });
  });
  return frames;
}

const TAG_LABEL: Record<Tag, string> = {
  best: 'Best move',
  good: 'Good',
  inaccuracy: 'Inaccuracy',
  mistake: 'Mistake',
  blunder: 'Blunder',
};

function evalText(v: number): string {
  if (v >= 99) return 'M';
  return `${v >= 0 ? '+' : '−'}${Math.abs(v).toFixed(1)}`;
}

/** Lichess's win% curve, as the real eval bar uses. */
function winPct(pawns: number): number {
  if (pawns >= 99) return 100;
  return 50 + 50 * (2 / (1 + Math.exp(-0.00368208 * pawns * 100)) - 1);
}

function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(
    () => window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false,
  );
  useEffect(() => {
    const mq = window.matchMedia?.('(prefers-reduced-motion: reduce)');
    if (!mq) return;
    const on = () => setReduced(mq.matches);
    mq.addEventListener('change', on);
    return () => mq.removeEventListener('change', on);
  }, []);
  return reduced;
}

/* ---------------------------------------------------------------- page */

export function Landing() {
  return (
    <div className="lp">
      <LandingNav />
      <main>
        <Hero />
        <SourcesStrip />
        <Features />
        <HowItWorks />
        <ReportPreview />
        <FinalCta />
      </main>
      <Footer />
    </div>
  );
}

function LandingNav() {
  return (
    <header className="hdr hdr-lp">
      <div className="hdr-inner">
        <Logo />
        <nav className="hdr-nav" aria-label="Sections">
          <a className="hdr-tab" href="#features">Features</a>
          <a className="hdr-tab" href="#how">How it works</a>
          <a className="hdr-tab" href="#report">Sample report</a>
        </nav>
        <ThemeButton />
        <Link to="/analyse" className="btn btn-primary btn-sm">
          Open app
        </Link>
      </div>
    </header>
  );
}

/* ---------------------------------------------------------------- hero */

function Hero() {
  const frames = useMemo(buildFrames, []);
  const reduced = useReducedMotion();
  const [i, setI] = useState(reduced ? frames.length - 1 : 0);
  const [paused, setPaused] = useState(reduced);

  useEffect(() => {
    if (paused) return;
    const last = i >= frames.length - 1;
    const t = window.setTimeout(() => setI(last ? 0 : i + 1), last ? 4000 : 1500);
    return () => window.clearTimeout(t);
  }, [i, paused, frames.length]);

  const f = frames[i] ?? frames[0]!;
  const m = f.move;
  const white = winPct(m?.eval ?? 0.2);
  const moveNo = Math.ceil(f.ply / 2);

  return (
    <section className="hero">
      <div className="hero-glow" aria-hidden="true" />
      <div className="lp-wrap hero-grid">
        <div className="hero-copy">
          <span className="eyebrow">
            <span className="eyebrow-dot" /> Engine analysis · AI coaching · Rating insight
          </span>
          <h1>
            See every game the way a <span className="grad">grandmaster</span> would.
          </h1>
          <p className="lede">
            ChessVision turns any game into a clear coaching report: where it turned, why,
            and what you should have played instead. Paste a link, get answers in under a
            minute.
          </p>
          <div className="hero-ctas">
            <Link to="/analyse" className="btn btn-primary btn-lg">
              Analyse a game — free
            </Link>
            <Link to="/puzzles" className="btn btn-lg">
              Train with puzzles
            </Link>
          </div>
          <ul className="hero-proof">
            <li>
              <b>Stockfish</b> move grading
            </li>
            <li>
              <b>Maia-2</b> rating estimate
            </li>
            <li>
              <b>Lichess</b> &amp; <b>Chess.com</b> import
            </li>
          </ul>
        </div>

        <div
          className="hero-demo"
          onMouseEnter={() => !reduced && setPaused(true)}
          onMouseLeave={() => !reduced && setPaused(false)}
        >
          <div className="demo-card">
            <div className="demo-head">
              <span>
                <b>Morphy</b> vs <b>Duke Karl / Count Isouard</b>
              </span>
              <span className="dim mono">Paris 1858</span>
            </div>
            <div className="demo-board">
              <div className="demo-eval" aria-hidden="true">
                <span style={{ height: `${white}%` }} />
              </div>
              <Board
                fen={f.fen}
                orientation="white"
                lastMove={f.last}
                interactive={false}
                coordinates={false}
              />
            </div>
            <div className="demo-foot" aria-live="polite">
              {m ? (
                <>
                  <div className="demo-move">
                    <span className="mono dim">
                      {moveNo}
                      {f.ply % 2 === 1 ? '.' : '…'}
                    </span>
                    <b className="mono">{m.san}</b>
                    <span className={`pill pill-${m.tag}`}>{TAG_LABEL[m.tag]}</span>
                    <span className="mono demo-evalnum">{evalText(m.eval)}</span>
                  </div>
                  <p className="demo-note">{m.note}</p>
                </>
              ) : (
                <p className="demo-note dim">Starting position. The report plays itself.</p>
              )}
              <div className="demo-ctrl">
                <button type="button" onClick={() => { setPaused(true); setI(Math.max(0, i - 1)); }} aria-label="Previous move">‹</button>
                <button type="button" onClick={() => setPaused((p) => !p)} aria-label={paused ? 'Play' : 'Pause'}>
                  {paused ? '▶' : '❚❚'}
                </button>
                <button type="button" onClick={() => { setPaused(true); setI(Math.min(frames.length - 1, i + 1)); }} aria-label="Next move">›</button>
                <div className="demo-track" aria-hidden="true">
                  <span style={{ width: `${(i / (frames.length - 1)) * 100}%` }} />
                </div>
              </div>
            </div>
          </div>
          <div className="float float-acc" aria-hidden="true">
            <span className="dim">Accuracy</span>
            <b>96.4</b>
          </div>
          <div className="float float-rating" aria-hidden="true">
            <span className="dim">Est. rating</span>
            <b>2450</b>
          </div>
        </div>
      </div>
    </section>
  );
}

function SourcesStrip() {
  return (
    <section className="sources" aria-label="Supported sources">
      <div className="lp-wrap sources-row">
        <span className="dim">Works with the games you already play</span>
        <span className="src-chip">lichess.org</span>
        <span className="src-chip">chess.com</span>
        <span className="src-chip">PGN files</span>
        <span className="src-chip">FEN positions</span>
      </div>
    </section>
  );
}

/* ------------------------------------------------------------ features */

const FEATURES = [
  {
    icon: '◎',
    title: 'Every move graded',
    body: 'Stockfish evaluates each position and labels best moves, inaccuracies, mistakes and blunders — with the line you missed.',
  },
  {
    icon: '✎',
    title: 'Coaching in plain English',
    body: 'Key moments get a written explanation of the idea behind them, not just a number. Learn the why, not only the what.',
  },
  {
    icon: '↗',
    title: 'Accuracy & win probability',
    body: 'A per-player accuracy score and a move-by-move win% graph show exactly where the game slipped away.',
  },
  {
    icon: '◈',
    title: 'Rating estimate',
    body: 'Maia-2, a neural network trained on human games, estimates the strength each side actually played at.',
  },
  {
    icon: '♞',
    title: 'Adaptive puzzles',
    body: 'Thousands of real-game tactics that adjust to your rating, so every session sits right at the edge of your ability.',
  },
  {
    icon: '♜',
    title: 'Sparring bots',
    body: 'Play human-like opponents at the level you choose, then send the game straight into a full analysis.',
  },
];

function Features() {
  return (
    <section id="features" className="lp-sec">
      <div className="lp-wrap">
        <header className="sec-head">
          <span className="eyebrow">Platform</span>
          <h2>Everything you need to improve, in one report</h2>
          <p className="lede">
            Professional-grade tooling that used to take an engine, a coach and a spreadsheet.
          </p>
        </header>
        <div className="feat-grid">
          {FEATURES.map((f) => (
            <article key={f.title} className="feat">
              <span className="feat-icon" aria-hidden="true">
                {f.icon}
              </span>
              <h3>{f.title}</h3>
              <p>{f.body}</p>
            </article>
          ))}
        </div>
      </div>
    </section>
  );
}

function HowItWorks() {
  const steps = [
    ['Import', 'Paste a Lichess or Chess.com link, drop in a PGN, or set up any position from a FEN.'],
    ['Analyse', 'The engine reviews every move while you watch the report fill in, live.'],
    ['Improve', 'Read the turning points, replay the better lines, and drill the patterns you missed.'],
  ];
  return (
    <section id="how" className="lp-sec lp-sec-alt">
      <div className="lp-wrap">
        <header className="sec-head">
          <span className="eyebrow">How it works</span>
          <h2>From game to insight in three steps</h2>
        </header>
        <ol className="steps">
          {steps.map(([t, b], k) => (
            <li key={t} className="step">
              <span className="step-no mono">0{k + 1}</span>
              <h3>{t}</h3>
              <p>{b}</p>
            </li>
          ))}
        </ol>
      </div>
    </section>
  );
}

/* --------------------------------------------------------------- report */

function ReportPreview() {
  const points = useMemo(() => [0.2, ...OPERA.map((m) => m.eval)], []);
  const w = 560;
  const h = 140;
  const path = points
    .map((p, k) => {
      const x = (k / (points.length - 1)) * w;
      const y = h - (winPct(p) / 100) * h;
      return `${k === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(' ');
  const counts = OPERA.reduce<Record<Tag, number>>(
    (acc, m) => ({ ...acc, [m.tag]: acc[m.tag] + 1 }),
    { best: 0, good: 0, inaccuracy: 0, mistake: 0, blunder: 0 },
  );
  const blunderPly = OPERA.findIndex((m) => m.tag === 'blunder') + 1;

  return (
    <section id="report" className="lp-sec">
      <div className="lp-wrap report-grid">
        <div>
          <span className="eyebrow">Sample report</span>
          <h2>A report you can actually read</h2>
          <p className="lede">
            Accuracy, a win-probability curve, and the handful of moments that decided the
            game — summarised at the top so you know where to look first.
          </p>
          <ul className="checks">
            <li>Turning points flagged automatically</li>
            <li>Best-line replay on an interactive board</li>
            <li>Shareable links and a saved library with an account</li>
          </ul>
          <Link to="/analyse" className="btn btn-primary">
            Generate your own
          </Link>
        </div>

        <div className="report-card">
          <div className="rc-players">
            <div>
              <span className="rc-dot rc-w" /> <b>Morphy</b>
              <div className="rc-acc">
                96.4<span>accuracy</span>
              </div>
            </div>
            <div>
              <span className="rc-dot rc-b" /> <b>Allies</b>
              <div className="rc-acc">
                58.1<span>accuracy</span>
              </div>
            </div>
          </div>
          <svg className="rc-graph" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" role="img"
               aria-label="Win probability for White across the game">
            <line x1="0" x2={w} y1={h / 2} y2={h / 2} className="rc-mid" />
            <path d={`${path} L${w},${h} L0,${h} Z`} className="rc-area" />
            <path d={path} className="rc-line" />
            <circle
              cx={(blunderPly / (points.length - 1)) * w}
              cy={h - (winPct(points[blunderPly] ?? 0) / 100) * h}
              r="5"
              className="rc-blunder"
            />
          </svg>
          <ul className="rc-counts">
            <li><span className="pill pill-best">{counts.best}</span> best</li>
            <li><span className="pill pill-inaccuracy">{counts.inaccuracy}</span> inaccuracies</li>
            <li><span className="pill pill-mistake">{counts.mistake}</span> mistakes</li>
            <li><span className="pill pill-blunder">{counts.blunder}</span> blunder</li>
          </ul>
          <div className="rc-moment">
            <span className="pill pill-blunder">Turning point · move 9</span>
            <p>
              <b className="mono">9…b5?</b> opened the queenside toward Black’s own king.
              After <b className="mono">10.Nxb5!</b> White’s advantage was decisive.
            </p>
          </div>
        </div>
      </div>
    </section>
  );
}

function FinalCta() {
  return (
    <section className="lp-sec">
      <div className="lp-wrap">
        <div className="cta-band">
          <div>
            <h2>Your next game deserves a second look.</h2>
            <p>Free to use. No account needed for your first analysis.</p>
          </div>
          <Link to="/analyse" className="btn btn-lg btn-light">
            Start analysing
          </Link>
        </div>
      </div>
    </section>
  );
}
