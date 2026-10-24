import { useCallback, useEffect, useState } from 'react';
import { api } from '../api/client';
import { AppError, type BotInfo, type PieceType, type PlayGame, type Square } from '../api/types';
import { Board } from '../components/Board';
import { FeatureOff } from '../components/FeatureOff';
import { useBoardMoves } from '../hooks/useBoardMoves';
import { useFeature } from '../context/ConfigContext';
import './Play.css';

/**
 * Play a bot.
 *
 * The opponents are Maia-2 at four rating bands plus Stockfish. Maia is a
 * policy trained on human games at each band, so its mistakes are the
 * mistakes players of that rating actually make — which is the point. A
 * weakened engine plays perfectly and then drops a piece at random, and
 * practising against that teaches the wrong reflexes.
 *
 * One round trip per move: the server plays your move and the bot's reply
 * together, because there is nothing worth showing in between.
 */
export function Play() {
  const { on, reason } = useFeature('bots');
  const [bots, setBots] = useState<BotInfo[] | null>(null);
  const [game, setGame] = useState<PlayGame | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!on) return;
    api.bots().then((d) => setBots(d.bots)).catch(() => setBots([]));
  }, [on]);

  const board = useBoardMoves(game?.fen ?? null);

  const start = useCallback(async (bot: string, colour: 'white' | 'black' | 'random') => {
    setBusy(true);
    setErr(null);
    try {
      setGame(await api.newGame(bot, colour));
    } catch (e) {
      setErr(e instanceof AppError ? e.message : 'Could not start a game.');
    } finally {
      setBusy(false);
    }
  }, []);

  const move = async (from: Square, to: Square, promo?: PieceType) => {
    if (!game || busy) return;
    const mv = board.find(from, to, promo);
    if (!mv) return;
    setBusy(true);
    setErr(null);
    try {
      setGame(await api.playMove(game.game_id, mv.uci));
    } catch (e) {
      setErr(e instanceof AppError ? e.message : 'That move was refused.');
    } finally {
      setBusy(false);
    }
  };

  if (!on) return <FeatureOff name="Play" reason={reason} />;

  if (!game) {
    return (
      <main className="play">
        <h1>Play a bot</h1>
        <p className="play-lead">
          These are Maia-2, a model trained on human games at each rating — so
          the mistakes are the ones players of that strength actually make,
          not an engine throwing a piece away at random.
        </p>
        {err && <p className="play-err" role="alert">{err}</p>}
        <ul className="play-bots">
          {(bots ?? []).map((b) => (
            <li key={b.id}>
              <div className="play-bot panel">
                <h2>
                  {b.name}{' '}
                  <span className="mono dim">{b.elo ? `~${b.elo}` : 'engine'}</span>
                </h2>
                <p className="dim">{b.blurb}</p>
                <div className="play-colours">
                  {(['white', 'black', 'random'] as const).map((c) => (
                    <button key={c} type="button" className="btn"
                            disabled={busy} onClick={() => void start(b.id, c)}>
                      {c === 'random' ? 'Random' : `As ${c}`}
                    </button>
                  ))}
                </div>
              </div>
            </li>
          ))}
          {bots !== null && bots.length === 0 && (
            <li className="dim">No bots are available on this deployment.</li>
          )}
        </ul>
      </main>
    );
  }

  const pairs: Array<[string | undefined, string | undefined]> = [];
  for (let i = 0; i < game.moves.length; i += 2) {
    pairs.push([game.moves[i], game.moves[i + 1]]);
  }

  return (
    <main className="play play-live">
      <div className="play-board">
        <div className="play-who">
          <span className={`play-dot is-${game.you_are === 'white' ? 'black' : 'white'}`} />
          <b>{game.bot.name}</b>
          <span className="dim mono">{game.bot.elo ? `~${game.bot.elo}` : 'engine'}</span>
        </div>
        <Board
          fen={game.fen}
          orientation={game.you_are}
          interactive={game.your_move && !game.is_over}
          busy={busy}
          legalMoves={board.map}
          isPromotion={board.isPromotion}
          onMove={(f, t, p) => void move(f, t, p)}
          announce={game.outcome ?? undefined}
        />
        <div className="play-who">
          <span className={`play-dot is-${game.you_are}`} />
          <b>You</b>
          <span className="dim">
            {game.is_over ? '' : game.your_move ? 'your move' : 'thinking…'}
          </span>
        </div>

        {board.error && <p className="play-err" role="alert">{board.error}</p>}
        {err && <p className="play-err" role="alert">{err}</p>}

        <div className="play-controls">
          <button type="button" className="btn" onClick={() => setGame(null)}>
            ← Other bots
          </button>
          <button
            type="button"
            className="btn"
            disabled={game.is_over || busy}
            onClick={async () => setGame(await api.resign(game.game_id))}
          >
            Resign
          </button>
          <button type="button" className="btn btn-primary" disabled={busy}
                  onClick={() => void start(game.bot.id, game.you_are)}>
            New game
          </button>
        </div>
      </div>

      <aside className="panel play-rail">
        {game.is_over ? (
          <p className={`play-result ${/You win/.test(game.outcome ?? '') ? 'is-win' : ''}`}
             role="status">
            {game.outcome}
          </p>
        ) : (
          <p className="dim">
            {game.in_check ? 'Check.' : `${game.turn} to move.`}
          </p>
        )}
        <h2>Moves</h2>
        {pairs.length === 0 ? (
          <p className="dim">No moves yet.</p>
        ) : (
          <ol className="play-moves">
            {pairs.map(([w, b], i) => (
              <li key={i}>
                <span className="mono dim">{i + 1}.</span>
                <span className="mono">{w}</span>
                <span className="mono">{b ?? ''}</span>
              </li>
            ))}
          </ol>
        )}
      </aside>
    </main>
  );
}
