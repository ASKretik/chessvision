import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../api/client';
import {
  AppError,
  type AttemptResponse,
  type NextPuzzle,
  type PieceType,
  type Square,
} from '../api/types';
import { useBoardMoves } from '../hooks/useBoardMoves';
import { useFeature } from '../context/ConfigContext';
import { useAuth } from '../context/AuthContext';
import { SignIn } from '../components/SignIn';
import { Board } from '../components/Board';
import { Piece } from '../components/pieces';
import { FeatureOff } from '../components/FeatureOff';
import './Puzzles.css';

/** One of the player's moves, with the reply it drew. */
interface Played {
  uci: string;
  san: string;
  replySan?: string;
}

export function Puzzles() {
  const { on, reason } = useFeature('puzzles');
  const sample = useFeature('puzzles_dev_sample');
  const localAccounts = useFeature('local_accounts');
  const { user, ready, stats } = useAuth();

  const [data, setData] = useState<NextPuzzle | null>(null);
  const [fen, setFen] = useState<string | null>(null);
  const [line, setLine] = useState<Played[]>([]);
  const [verdict, setVerdict] = useState<AttemptResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // Positions the player moved from, so Undo rewinds without replaying.
  const history = useRef<string[]>([]);
  const started = useRef<number>(Date.now());

  const board = useBoardMoves(fen);
  const puzzle = data?.puzzle;
  const done = verdict !== null;

  const load = useCallback(async () => {
    setBusy(true);
    setErr(null);
    setVerdict(null);
    setLine([]);
    try {
      const d = await api.nextPuzzle();
      setData(d);
      setFen(d.puzzle.fen);
      history.current = [d.puzzle.fen];
      started.current = Date.now();
    } catch (e: unknown) {
      setErr(e instanceof AppError ? e.message : 'Could not load a puzzle.');
      setData(null);
      setFen(null);
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    if (on) void load();
  }, [on, load]);

  if (!on) return <FeatureOff name="Puzzles" reason={reason} />;

  // Sign-in first, when there is an account system to sign in to. Without
  // it every visitor would share one anonymous rating, which makes the
  // rating meaningless and the progress someone else's.
  if (localAccounts.on && ready && !user) {
    return (
      <main className="pz">
        <h1>Puzzles</h1>
        <p className="dim" style={{ maxWidth: '60ch' }}>
          Sign in so your rating and solved puzzles are still here next time.
          There are 2,000 puzzles, served near your rating.
        </p>
        <SignIn />
      </main>
    );
  }

  const submit = async (moves: string[]) => {
    if (!puzzle) return;
    setBusy(true);
    try {
      const res = await api.attempt(puzzle.id, moves, Date.now() - started.current);
      setVerdict(res);
    } catch (e: unknown) {
      setErr(e instanceof AppError ? e.message : 'Could not submit the attempt.');
    } finally {
      setBusy(false);
    }
  };

  /* One clicked move. The server decides whether it is right AND supplies the
     opponent's answer, which lives inside the hidden solution — the client
     cannot know it. The rating moves only on /attempt, which is sent the
     moment the line completes or a move comes back wrong. */
  const onMove = async (from: Square, to: Square, promo?: PieceType) => {
    if (!puzzle || done) return;
    const mv = board.find(from, to, promo);
    if (!mv) return;

    const moves = [...line.map((p) => p.uci), mv.uci];
    setBusy(true);
    setErr(null);
    // Show the move at once; the server's answer either keeps it or the
    // verdict explains it.
    setFen(mv.fen_after);

    try {
      const step = await api.step(puzzle.id, moves);

      if (!step.correct) {
        setLine([...line, { uci: mv.uci, san: mv.san }]);
        await submit(moves);
        return;
      }

      const entry: Played = { uci: mv.uci, san: mv.san };
      let next = mv.fen_after;
      if (step.opponent_reply) {
        // Resolve the reply's SAN through the same endpoint the board reads,
        // so nothing is formatted here.
        const after = await api.boardMoves(mv.fen_after);
        const rep = after.legal.find((m) => m.uci === step.opponent_reply);
        entry.replySan = rep?.san ?? step.opponent_reply;
        next = step.fen_after ?? rep?.fen_after ?? mv.fen_after;
      }
      setFen(next);
      history.current = [...history.current, next];
      setLine([...line, entry]);

      if (step.complete) await submit(moves);
    } catch (e: unknown) {
      setErr(e instanceof AppError ? e.message : 'Could not check that move.');
      setFen(history.current[history.current.length - 1] ?? puzzle.fen);
    } finally {
      setBusy(false);
    }
  };

  const undo = () => {
    if (!line.length || done) return;
    history.current = history.current.slice(0, -1);
    setFen(history.current[history.current.length - 1] ?? puzzle?.fen ?? null);
    setLine(line.slice(0, -1));
  };

  return (
    <main className="pz">
      {sample.on && (
        // Flagged by the backend, and only when the fallback sample is in
        // use. With the subset built this does not appear at all.
        <div className="banner-info" role="status">
          Playing the fallback sample — a few dozen puzzles. Build the subset
          with tools/build_puzzle_subset.py for the full 2,000.
        </div>
      )}
      {user && stats && (
        <p className="dim pz-signed">
          Signed in as <b>{user.username}</b> · rating{' '}
          <b className="mono">{stats.puzzle_rating}</b> ·{' '}
          {stats.puzzles_solved}/{stats.puzzle_attempts} solved
        </p>
      )}

      <div className="pz-split">
        <div className="pz-board-col">
          {fen && puzzle ? (
            <>
              {/* The puzzle FEN is already past the opponent's setup move, so
                  the side to move is always the player's colour. */}
              <div className={`pz-side is-${puzzle.side_to_move}`}>
                <span className="pz-side-icon" aria-hidden="true">
                  <Piece code={puzzle.side_to_move === 'white' ? 'K' : 'k'} />
                </span>
                <span>
                  {done ? 'You played as ' : 'You play as '}
                  <b>{puzzle.side_to_move === 'white' ? 'White' : 'Black'}</b>
                  {!done && (
                    <span className="dim">
                      {' '}
                      · find the best move for{' '}
                      {puzzle.side_to_move === 'white' ? 'White' : 'Black'}
                    </span>
                  )}
                </span>
              </div>
              <Board
                fen={fen}
                orientation={puzzle.side_to_move}
                interactive={!done}
                busy={busy}
                legalMoves={board.map}
                isPromotion={board.isPromotion}
                onMove={(f, t, p) => void onMove(f, t, p)}
                announce={
                  done
                    ? verdict?.result.solved
                      ? 'Solved.'
                      : 'Not solved.'
                    : undefined
                }
              />
              {board.error && (
                <p className="pz-err" role="alert">
                  {board.error}
                </p>
              )}
              <div className="pz-controls">
                <span className={`pz-turn is-${puzzle.side_to_move}`}>
                  {done
                    ? 'Finished'
                    : `${puzzle.side_to_move === 'white' ? 'White' : 'Black'} to move — your move ${line.length + 1} of ${puzzle.moves_required}`}
                </span>
                <button
                  type="button"
                  className="btn"
                  onClick={undo}
                  disabled={!line.length || done || busy}
                >
                  Undo
                </button>
                <button type="button" className="btn" onClick={() => void load()} disabled={busy}>
                  {done ? 'Next puzzle' : 'Skip'}
                </button>
              </div>
            </>
          ) : (
            <div className="panel pz-loading" role="status">
              {err ?? 'Loading a puzzle…'}
            </div>
          )}
        </div>

        <aside className="panel pz-rail">
          {puzzle && (
            <>
              <div className="pz-head">
                <span className="pz-rating mono">{puzzle.rating}</span>
                <span className="dim">
                  {puzzle.moves_required} move{puzzle.moves_required === 1 ? '' : 's'} to find
                </span>
              </div>
              {/* Themes are withheld until the puzzle is over: "backRankMate"
                  beside the board is the answer. */}
              <p className="dim pz-themes">
                {done
                  ? puzzle.themes.join(' · ') || 'no themes'
                  : 'Themes are hidden until you finish — they give it away.'}
              </p>
              {data && (
                <p className="dim">
                  Your rating <b className="mono">{data.your_rating}</b> · drawn from{' '}
                  {data.rating_window[0]}–{data.rating_window[1]}
                </p>
              )}

              <h2>Your line</h2>
              {line.length ? (
                <ol className="pz-line">
                  {line.map((p, i) => (
                    <li key={i}>
                      <span className="pz-no mono">{i + 1}.</span>
                      <b className="mono">{p.san}</b>{' '}
                      {p.replySan && <span className="dim mono">{p.replySan}</span>}
                    </li>
                  ))}
                </ol>
              ) : (
                <p className="dim">Play a move on the board.</p>
              )}

              {err && (
                <p className="pz-err" role="alert">
                  {err}
                </p>
              )}

              {verdict && <Verdict res={verdict} onNext={() => void load()} />}
            </>
          )}
        </aside>
      </div>
    </main>
  );
}

function Verdict({ res, onNext }: { res: AttemptResponse; onNext: () => void }) {
  const r = res.result;
  const rt = res.rating;
  return (
    <div role="status">
      <p className={`pz-verdict ${r.solved ? 'is-solved' : 'is-failed'}`}>
        {r.solved ? 'Solved' : 'Not solved'} — {r.moves_correct}/{r.moves_required}
      </p>
      {/* Every failure path names the move the line wanted: a puzzle that
          reports a failure without the answer is unlearnable. */}
      {r.expected_instead && (
        <p className="dim">
          Expected <b className="mono">{r.expected_instead}</b>
          {r.first_wrong_move ? `, you played ${r.first_wrong_move}` : ''}
        </p>
      )}
      {r.reason && <p className="dim">{r.reason}</p>}
      <p>
        {rt.before} → <b className="mono">{rt.after}</b>{' '}
        <span className="dim">
          ({rt.delta >= 0 ? '+' : ''}
          {rt.delta}, k={rt.k})
        </span>
      </p>
      {res.solution && (
        <p className="dim mono">solution: {res.solution.join(' ')}</p>
      )}
      {res.stats && (
        <p className="dim">
          {res.stats.puzzles_solved}/{res.stats.puzzle_attempts} solved · streak{' '}
          {res.stats.current_streak} (best {res.stats.best_streak})
        </p>
      )}
      <button type="button" className="btn btn-primary" onClick={onNext}>
        Next puzzle
      </button>
    </div>
  );
}
