import { Link } from 'react-router-dom';
import { useConfig } from '../context/ConfigContext';
import { LogoMark } from './Logo';

/**
 * The cburnett pieces are CC BY-SA 3.0, which requires attribution wherever
 * they are shown. That is why the credit line is here on every page. The
 * build line is optional so the landing page can render without /config.
 */
export function Footer({ build }: { build?: string }) {
  return (
    <footer className="ftr">
      <div className="ftr-inner">
        <div className="ftr-brand">
          <span className="logo">
            <LogoMark size={24} />
            <span className="logo-word">
              Chess<b>Vision</b>
            </span>
          </span>
          <p>Coaching-grade game reports for players who want to improve.</p>
        </div>
        <nav className="ftr-cols" aria-label="Footer">
          <div>
            <h3>Product</h3>
            <Link to="/analyse">Game analysis</Link>
            <Link to="/puzzles">Puzzle trainer</Link>
            <Link to="/play">Play the bots</Link>
          </div>
          <div>
            <h3>Technology</h3>
            <a href="https://stockfishchess.org" rel="noreferrer noopener" target="_blank">
              Stockfish engine
            </a>
            <a href="https://github.com/CSSLab/maia2" rel="noreferrer noopener" target="_blank">
              Maia-2 ratings
            </a>
            <a href="https://lichess.org/page/accuracy" rel="noreferrer noopener" target="_blank">
              Accuracy formula
            </a>
          </div>
        </nav>
      </div>
      <div className="ftr-base">
        <span>© {new Date().getFullYear()} ChessVision</span>
        <span>
          Chess pieces by{' '}
          <a href="https://en.wikipedia.org/wiki/User:Cburnett" rel="noreferrer noopener"
             target="_blank">
            Colin M.L. Burnett
          </a>
          ,{' '}
          <a href="https://creativecommons.org/licenses/by-sa/3.0/" rel="noreferrer noopener"
             target="_blank">
            CC BY-SA 3.0
          </a>
          . Accuracy and win% use Lichess&rsquo;s published formulas.
        </span>
        {build && <span className="ftr-build mono">{build}</span>}
      </div>
    </footer>
  );
}

/** The in-app footer, which also names the build the server reports. */
export function AppFooter() {
  const c = useConfig();
  return <Footer build={`v${c.version} · ${c.env}${c.llm_model ? ` · ${c.llm_model}` : ''}`} />;
}
