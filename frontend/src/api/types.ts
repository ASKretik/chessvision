/* Types for partE.py's responses.
 *
 * WRITTEN AGAINST THE REAL PAYLOADS in docs/examples/*.json, which
 * tools/dump_api_contract.py captures from actual runs. Do NOT generate
 * these from /openapi.json: the endpoints are annotated `-> Dict[str, Any]`,
 * so the published schema says nothing and would replace real field names
 * with `Record<string, unknown>`.
 *
 * tools/check_frontend_types.py diffs the interfaces below against those
 * captured payloads and fails on drift. Run it after any backend change to
 * a response shape.
 */

/* --- shared ----------------------------------------------------------- */

export type Square = string; // 'e4'
export type PieceType = 'q' | 'r' | 'b' | 'n';
export type Colour = 'white' | 'black';

/** partA.Label. Six values, and `book` is NOT one of them — an opening move
 *  is still classified on its merits. Opening membership is reported
 *  separately, by `Report.opening`. */
export type MoveClass =
  | 'best'
  | 'excellent'
  | 'good'
  | 'inaccuracy'
  | 'mistake'
  | 'blunder';

/** A fact, deliberately separate from the severity in `label`. */
export type MateStatusChange = 'mate_now_forced' | 'forced_mate_lost' | null;

export type Phase = 'opening' | 'middlegame' | 'endgame';

/** Decides how a rating is rendered. Never render on `elo` alone:
 *  `floor` means "2000+", `ceiling` means "under 1100". */
export type EloMethod = 'interpolated' | 'floor' | 'ceiling';

export type TimeFlag = 'wasted' | 'rushed' | null;

/* --- the per-move timeline (partA.MoveRecord) -------------------------- */

export interface MoveRecord {
  ply: number;
  move_number: number;
  color: Colour;
  san: string;
  uci: string;
  /** The position this move was played FROM. */
  fen_before: string;
  /** The position it reached. The last move's fen_after is the final
   *  position of the game, which has no row of its own. */
  fen_after: string;
  phase: Phase;

  /** Centipawns FROM WHITE'S POINT OF VIEW, always. Flip for black. */
  cp_before: number;
  cp_after: number;
  /** Win percentages are already from the mover's point of view. */
  win_before: number;
  win_after: number;
  win_drop: number;
  /** Capped centipawn loss, mover's view. Feeds ACPL. */
  cp_loss: number;
  accuracy: number;
  label: MoveClass;
  mate_status_change: MateStatusChange;
  /** True when this was the engine's own first choice. Such a move is
   *  always `best`, whatever the win drop. */
  played_best: boolean;

  best_move: string | null;
  best_pv: string[];

  /** The opponent's best reply, from the engine's read of the position
   *  AFTER this move. On a faulted move this is the refutation — the
   *  concrete reason the move was bad. Present on every move in the
   *  timeline; Part B withholds it from the comment prompt on good moves,
   *  where it is just the normal continuation. */
  refutation_move: string | null;
  refutation_pv: string[];

  complexity: number;
  complexity_band: 'low' | 'medium' | 'high';
  only_move: boolean;
  /** Legal moves in the position this was played from. A.5b uses it to
   *  decide whether the move was a real decision at all. */
  legal_moves: number;
  multipv_spread_cp: number | null;

  /** All null when the game carried no usable clocks. Check
   *  `Report.time_control.time_analysis_enabled` before rendering a time
   *  column at all. */
  time_spent: number | null;
  clock_after: number | null;
  time_ratio: number | null;
  time_flag: TimeFlag;
}

/* --- per-player summary ----------------------------------------------- */

export interface PhaseReport {
  phase: Phase;
  moves: number;
  acpl: number;
  accuracy: number;
  blunders: number;
  mistakes: number;
  inaccuracies: number;
}

export interface RatingEstimate {
  /** False is normal, not an error — `reason` says why. */
  available: boolean;
  /** The argmax bucket, e.g. '1600-1699', '<1100', '>=2000'. */
  band: string | null;
  /** A point estimate on a 100-point grid, set for every band. It can land
   *  one step OUTSIDE `band`: the band is the raw argmax, this is the
   *  interpolated vertex snapped to the grid. Not a contradiction — present
   *  this as the estimate and the band as the argmax. */
  elo: number | null;
  elo_method: EloMethod | null;
  elo_detail: Record<string, unknown>;
  /** How much the moves actually pin down. READ THIS BEFORE `elo`: on a flat
   *  likelihood curve the point estimate means very little, and one game
   *  often cannot distinguish 1400 from 2000. `confidence` is 'low' when the
   *  range spans more than three buckets. */
  interval: {
    low_band: string;
    high_band: string;
    /** null at an open end (`<1100` has no floor, `>=2000` no ceiling). */
    low_elo: number | null;
    high_elo: number | null;
    buckets: number;
    /** Posterior mass of the range under a UNIFORM prior over buckets — a
     *  description of the curve's flatness, not a calibrated probability. */
    mass: number;
    confidence: 'low' | 'medium' | 'high';
    spread_log_likelihood: number;
    /** null when either end is open. */
    width_elo: number | null;
  };
  bucket_index: number | null;
  moves_used: number;
  moves_skipped: number;
  log_likelihood: number | null;
  /** Log-likelihood per band — the whole curve, for a sparkline. */
  per_band: Record<string, number>;
  opponent_elo_used: number | null;
  opponent_elo_mode: string | null;
  model_type: string | null;
  reason: string | null;
}

export interface TimeOffender {
  ply: number;
  move_number: number;
  san: string;
  phase: Phase;
  seconds: number;
  ratio: number;
  complexity: number;
  complexity_band: string;
  label: MoveClass;
  only_move: boolean;
}

export interface TimeReport {
  /** False means no usable clocks: hide the whole time panel. */
  enabled: boolean;
  expected_per_move: number | null;
  total_seconds_used: number;
  median_seconds: number;
  seconds_wasted: number;
  wasted_count: number;
  rushed_count: number;
  top_wasted: TimeOffender[];
  top_rushed: TimeOffender[];
  /** The two teachable conjunctions, separate from the raw flags above.
   *  `critical_rush` is a hard position played fast that actually cost
   *  something; `wasted_think` is a long think on an easy position that cost
   *  nothing. The consequence is what makes either worth showing. */
  critical_rush: TimeOffender[];
  wasted_think: TimeOffender[];
}

/** A.5b. Error rates measured PER DECISION rather than per move, so they
 *  are comparable across games of different length and sharpness. This is
 *  the defensible headline; the Elo derived from it is secondary. */
export interface ErrorProfile {
  moves: number;
  /** Moves where going wrong was actually possible: more than one legal
   *  move, not an only-move, not still in book, position not already
   *  decided. A theory-heavy miniature can leave very few. */
  decisions: number;
  blunder_rate: number;
  mistake_rate: number;
  inaccuracy_rate: number;
  acpl: number;
  cpl_median: number;
  cpl_p90: number;
  cpl_volatility: number;
  top1_match: number;
  top3_match: number;
  weighted_error: number;
  phase_acpl: Record<string, number | null>;
  phase_weighted_acpl: number | null;
}


export interface PlayerReport {
  color: Colour;
  name: string;
  /** From the PGN header, not estimated. */
  rating_header: number | null;
  accuracy: number;
  acpl: number;
  moves: number;
  best_moves: number;
  blunders: number;
  mistakes: number;
  inaccuracies: number;
  phases: PhaseReport[];
  weakest_phase: Phase | null;
  rating_estimate: RatingEstimate;
  error_profile: ErrorProfile;
  time: TimeReport;
}

/* --- commentary (Part B) ---------------------------------------------- */

/** One written comment. The prose is `comment`; everything else about the
 *  move is looked up in the timeline by `ply`. There is no san/colour here,
 *  and an earlier version of this interface invented both. */
export interface Comment {
  ply: number;
  comment: string;
  /** True when it came from the response cache rather than a fresh call. */
  cached: boolean;
  /** Fact-validation failures. A comment that failed is withheld entirely
   *  rather than shown wrong, so this is normally empty. */
  rejected_because: string[];
}

/** A move the pipeline could not comment on at all, with the reason.
 *  Distinct from a rejected comment: that one was written and refused by
 *  fact-validation; this one was never produced. */
export interface CommentaryGap {
  ply: number;
  comment: null;
  unavailable: string;
}

export interface Commentary {
  /** False still carries `facts`: the deterministic B.1 layer needs no key. */
  available: boolean;
  reason: string | null;
  narrative: string | null;
  /** Sparse — only selected moves get one, and a comment that failed
   *  fact-validation is withheld rather than shown wrong. */
  comments: Comment[];
  facts: Record<string, unknown>;
  usage: Record<string, unknown>;
}

/* --- the report ------------------------------------------------------- */

export interface Opening {
  eco: string;
  name: string;
  left_book_ply: number | null;
  left_book_move: string | null;
  left_book_move_number: number | null;
  source: string;
}

export interface TimeControl {
  raw: string | null;
  base_seconds: number | null;
  increment_seconds: number | null;
  expected_per_move: number | null;
  /** The gate for every time-based cell in the UI. */
  time_analysis_enabled: boolean;
}

export interface GameReport {
  kind: 'game';
  source_type: string;
  source_ref: string | null;
  headers: Record<string, string>;
  moves: MoveRecord[];
  white: PlayerReport;
  black: PlayerReport;
  opening: Opening;
  time_control: TimeControl;
  has_clock: boolean;
  /** ABSENT on the `analysis` SSE event, which carries Part A's report
   *  before Part B has run — that is the whole point of it, and it arrives
   *  several seconds before `result`. Every read must guard. Treating this
   *  as required crashed the page to blank on every analysis. */
  commentary?: Commentary;
  meta: Record<string, unknown>;
}

/** A single FEN produces a DIFFERENT shape, deliberately (plan.md §8.2).
 *  Branch on `kind`; do not render the game layout with empty cells. */
export interface PositionReport {
  kind: 'position';
  fen: string;
  turn: Colour;
  legal_moves: number;
  terminal: boolean;
  outcome: string | null;
  eval_cp_white: number | null;
  win_percent_white: number | null;
  lines: Array<{
    rank: number;
    move: string;
    uci: string;
    cp_white: number | null;
    mate_in: number | null;
    win_percent_mover: number;
    pv: string[];
  }>;
  phase: Phase;
  complexity: Record<string, unknown>;
  opening: Opening;
  /** Prose sentences naming what does not apply to one position, so the UI
   *  hides those panels rather than rendering them empty. */
  unavailable: string[];
  meta: Record<string, unknown>;
}

export type Report = GameReport | PositionReport;

export function isGame(r: Report): r is GameReport {
  return r.kind === 'game';
}

/* --- /config ---------------------------------------------------------- */

export type FeatureName =
  | 'analysis'
  | 'move_facts'
  | 'commentary'
  | 'rating_estimate'
  | 'accounts'
  | 'puzzles'
  | 'puzzles_dev_sample'
  | 'dev_user'
  /** Username/password accounts backed by SQLite, used when Supabase is
   *  not configured. Progress survives a restart. */
  | 'local_accounts'
  | 'bots';

export interface AppConfig {
  env: string;
  version: string;
  /** Empty strings when Supabase is not configured — check `features`. */
  supabase_url: string;
  supabase_anon_key: string;
  features: Record<FeatureName, boolean>;
  /** Why an off feature is off, keyed like `features`. An absent key means
   *  the feature is on. Render these verbatim. */
  reasons: Partial<Record<FeatureName, string>>;
  limits: {
    rate_limit_per_hour: number;
    max_comments_per_game: number;
    job_ttl_seconds: number;
    depth_default: number;
    depth_min: number;
    depth_max: number;
    multipv_default: number;
    multipv_max: number;
    max_moves_per_attempt: number;
  };
  llm_provider: string | null;
  llm_model: string | null;
}

/* --- POST /analyse ---------------------------------------------------- */

/** Exactly one source. The backend 422s on zero or two. chess.com needs the
 *  username alongside the URL because its API has no get-by-url endpoint. */
export type AnalyseSource =
  | { lichess_url: string }
  | { chesscom_url: string; chesscom_username: string; chesscom_month?: string }
  | { pgn: string }
  | { fen: string };

export interface AnalyseOptions {
  depth?: number;
  multipv?: number;
  estimate_rating?: boolean;
  commentary?: boolean;
}

export interface JobCreated {
  job_id: string;
  state: string;
  poll: string;
  events: string;
}

/* --- jobs ------------------------------------------------------------- */

export type JobState = 'queued' | 'running' | 'done' | 'failed';

/** Free-form: commentary stages carry a batch number. */
export type JobStage = string;

export interface JobSnapshot {
  job_id: string;
  state: JobState;
  progress: number;
  stage: JobStage;
  created_at: number;
  finished_at: number | null;
  error: string | null;
  result: Report | null;
}

/** The SSE frames. `analysis` and `result` carry the report at the TOP
 *  level, not wrapped in `{report}`. `error` carries `error`, not `detail`. */
export type JobEvent =
  | { event: 'state'; data: { state: JobState; stage: JobStage } }
  | {
      event: 'progress';
      data: { stage: JobStage; progress: number; partial: unknown };
    }
  | { event: 'analysis'; data: Report }
  | { event: 'result'; data: Report }
  | { event: 'error'; data: { error: string } };

/* --- accounts --------------------------------------------------------- */

export interface UserStats {
  user_id: string;
  puzzle_rating: number;
  puzzle_attempts: number;
  puzzles_solved: number;
  games_analysed: number;
  current_streak: number;
  best_streak: number;
}

export interface Me {
  user_id: string;
  email: string | null;
  stats: UserStats;
}

/** A row in Study. Denormalised on purpose: the list needs names, result,
 *  date and accuracy, and pulling a whole report per row to render a list
 *  would be megabytes for a few hundred bytes of output. */
export interface AnalysisSummary {
  id: string;
  source_type: string;
  source_ref: string | null;
  /** ISO string from Supabase, epoch seconds from the local store. */
  created_at: string | number;
  white?: string | null;
  black?: string | null;
  result?: string | null;
  played_at?: string | null;
  accuracy_white?: number | null;
  accuracy_black?: number | null;
  plies?: number | null;
  /** What produced it. A saved report is a snapshot, so these identify a
   *  stale one rather than merely an old one. */
  depth?: number | null;
  prompt_version?: string | null;
}

/** GET /me/analyses/{id} returns the database ROW, not a wrapper: the report
 *  is under `result_json`. The listing omits it deliberately — twenty full
 *  reports would be megabytes. */
export interface SavedAnalysis extends AnalysisSummary {
  /** The decompressed report. Named `report` rather than `result` because
   *  the summary's `result` is the game's outcome ("1-0"). The Supabase
   *  path returns the raw row, where it is `result_json`. */
  report?: Report | null;
  result_json?: Report | null;
  pgn?: string | null;
  user_id?: string;
}

/* --- board ------------------------------------------------------------ */

/** One legal move, from GET /board/moves. The client holds no chess rules:
 *  playing a move is finding it here and taking its `fen_after`. */
export interface LegalMove {
  uci: string;
  san: string;
  from: Square;
  to: Square;
  /** Set only on a promotion. Four moves share from+to, which is how the
   *  board detects one without knowing the promotion rules. */
  promotion: 'queen' | 'rook' | 'bishop' | 'knight' | null;
  fen_after: string;
  is_check: boolean;
  is_checkmate: boolean;
}

export interface BoardState {
  fen: string;
  turn: Colour;
  in_check: boolean;
  is_game_over: boolean;
  outcome: string | null;
  legal: LegalMove[];
}

/* --- puzzles ---------------------------------------------------------- */

export interface Puzzle {
  id: string;
  /** Already advanced past the opponent's setup move: the player moves
   *  first. The solution is NOT here and never will be. */
  fen: string;
  side_to_move: Colour;
  rating: number;
  rating_deviation: number;
  popularity: number;
  nb_plays: number;
  themes: string[];
  /** How many of the player's own moves the line needs. */
  moves_required: number;
  game_url: string | null;
  opening_tags: string[];
}

export interface NextPuzzle {
  puzzle: Puzzle;
  your_rating: number;
  rating_window: [number, number];
}

/** POST /puzzles/{id}/step — validates the line so far and hands back the
 *  opponent's scripted reply. Does NOT change the rating. */
export interface StepResult {
  puzzle_id: string;
  index: number;
  correct: boolean;
  complete: boolean;
  moves_required: number;
  /** Null on a wrong move: a wrong guess reveals nothing. */
  opponent_reply: string | null;
  fen_after: string | null;
  accepted_alternative: boolean;
}

export interface MoveCheck {
  index: number;
  submitted: string;
  expected: string;
  correct: boolean;
  opponent_reply: string | null;
  fen_after: string | null;
  accepted_alternative: boolean;
}

export interface AttemptVerdict {
  puzzle_id: string;
  solved: boolean;
  moves_correct: number;
  moves_required: number;
  checks: MoveCheck[];
  first_wrong_move: string | null;
  /** The move the line wanted, in SAN. Present on every failure path —
   *  reporting a failure without the answer makes a puzzle unlearnable. */
  expected_instead: string | null;
  reason: string | null;
}

export interface RatingChange {
  before: number;
  after: number;
  delta: number;
  expected: number;
  k: number;
  attempts_before: number;
}

export interface AttemptResponse {
  result: AttemptVerdict;
  rating: RatingChange;
  stats: UserStats | null;
  /** Populated ONLY when solved, so a deliberate wrong move cannot reveal
   *  the answer. */
  solution: string[] | null;
  dev_mode?: boolean;
}

/* --- accounts --------------------------------------------------------- */

export interface AuthUser {
  id: string;
  username: string;
}

export interface AuthResponse {
  /** Opaque session token. Sent as `Authorization: Bearer`. The server
   *  stores only its hash, so this value cannot be recovered if lost. */
  token: string;
  user: AuthUser;
  stats: UserStats;
}

/* --- play ------------------------------------------------------------- */

export interface BotInfo {
  id: string;
  name: string;
  blurb: string;
  /** The Maia-2 band it plays at; null for the engine bot. */
  elo: number | null;
  kind: 'maia' | 'engine';
}

export interface PlayGame {
  game_id: string;
  bot: BotInfo;
  you_are: Colour;
  fen: string;
  /** SAN, both sides, in order. */
  moves: string[];
  turn: Colour;
  /** False while the bot is to move or the game is over. */
  your_move: boolean;
  in_check: boolean;
  is_over: boolean;
  outcome: string | null;
  last_move: string | null;
}

/* --- errors ----------------------------------------------------------- */

export type AppErrorKind =
  | 'validation' // 422
  | 'rate_limit' // 429
  | 'busy' // 503 + Retry-After: memory admission, TEMPORARY, auto-retry
  | 'unavailable' // 503 no Retry-After: feature absent, PERMANENT, disable
  | 'auth' // 401
  | 'expired' // 404 on a job past JOB_TTL_SECONDS
  | 'offline' // the fetch itself failed
  | 'unknown';

export class AppError extends Error {
  readonly kind: AppErrorKind;
  readonly status: number;
  readonly retryAfterMs?: number;
  readonly field?: string;

  constructor(
    kind: AppErrorKind,
    status: number,
    message: string,
    extra?: { retryAfterMs?: number; field?: string },
  ) {
    super(message);
    this.name = 'AppError';
    this.kind = kind;
    this.status = status;
    this.retryAfterMs = extra?.retryAfterMs;
    this.field = extra?.field;
  }
}
