-- Part C.2 -- schema with Row Level Security.
--
-- Apply in the Supabase SQL editor, or:
--   psql "$SUPABASE_DB_URL" -f migrations/001_init.sql
--
-- The security model in one line: every table that holds user data has RLS
-- enabled with policies keyed on auth.uid(), so a user's own JWT can only
-- ever reach their own rows. The backend's service key bypasses RLS, which
-- is exactly why it is server-side only (E.5).
--
-- plan.md F requires proving this: "RLS: user A cannot read user B's
-- analyses". migrations/002_rls_test.sql does that, and it is worth running
-- after any policy change -- a policy that silently fails open looks
-- identical to one that works until someone else's data appears.

-- ---------------------------------------------------------------------------
-- analyses  (C.2)
-- ---------------------------------------------------------------------------
create table if not exists public.analyses (
    id          uuid primary key default gen_random_uuid(),
    user_id     uuid not null references auth.users (id) on delete cascade,
    source_type text not null check (source_type in ('lichess','chesscom','pgn','fen')),
    source_ref  text,
    pgn         text,
    -- The whole Part A report, plus Part B's commentary when it ran. jsonb
    -- rather than a wide table: the report's shape is still changing, and
    -- nothing queries inside it.
    result_json jsonb,
    created_at  timestamptz not null default now()
);

-- Supports the only query the app makes: this user's analyses, newest first.
create index if not exists analyses_user_created_idx
    on public.analyses (user_id, created_at desc);

alter table public.analyses enable row level security;

drop policy if exists analyses_select_own on public.analyses;
create policy analyses_select_own on public.analyses
    for select using (auth.uid() = user_id);

drop policy if exists analyses_insert_own on public.analyses;
create policy analyses_insert_own on public.analyses
    for insert with check (auth.uid() = user_id);

drop policy if exists analyses_delete_own on public.analyses;
create policy analyses_delete_own on public.analyses
    for delete using (auth.uid() = user_id);

-- No update policy: an analysis is a record of what the engine found. If it
-- is wrong, re-run it; editing it would make the history meaningless.

-- ---------------------------------------------------------------------------
-- puzzles  (C.3) -- reference data, not user data
-- ---------------------------------------------------------------------------
create table if not exists public.puzzles (
    id               text primary key,
    -- The position the PLAYER sees: the dump's FEN with the opponent's setup
    -- move already applied. Storing the raw dump FEN instead is the classic
    -- way to ship a puzzle set where every puzzle is off by one move.
    fen              text not null,
    solution         text[] not null,
    opponent_replies text[] not null default '{}',
    rating           integer not null,
    rating_deviation integer,
    popularity       integer,
    nb_plays         integer,
    themes           text[] not null default '{}',
    game_url         text,
    opening_tags     text[] not null default '{}'
);

-- The serving query is "a puzzle in this rating band", so rating leads.
create index if not exists puzzles_rating_idx on public.puzzles (rating);
create index if not exists puzzles_themes_idx on public.puzzles using gin (themes);

-- RLS on with NO select policy, which denies every client. Deliberate: the
-- `solution` column is the answer, and a client that can read this table can
-- read the answer out of its own network tab. Puzzles are served through the
-- backend, which holds the service key and returns a projection without the
-- solution (partC.Puzzle.to_dict withholds it by default).
alter table public.puzzles enable row level security;

-- ---------------------------------------------------------------------------
-- puzzle_attempts  (C.2)
-- ---------------------------------------------------------------------------
create table if not exists public.puzzle_attempts (
    id            uuid primary key default gen_random_uuid(),
    user_id       uuid not null references auth.users (id) on delete cascade,
    puzzle_id     text not null references public.puzzles (id) on delete cascade,
    solved        boolean not null,
    ms_taken      integer check (ms_taken is null or ms_taken >= 0),
    -- What they actually played, for a review UI and for spotting a client
    -- that is submitting nonsense.
    moves         jsonb,
    rating_before integer,
    rating_after  integer,
    created_at    timestamptz not null default now()
);

create index if not exists puzzle_attempts_user_created_idx
    on public.puzzle_attempts (user_id, created_at desc);
-- Supports "has this user already seen this puzzle", which the serving
-- endpoint needs to avoid repeats.
create index if not exists puzzle_attempts_user_puzzle_idx
    on public.puzzle_attempts (user_id, puzzle_id);

alter table public.puzzle_attempts enable row level security;

drop policy if exists attempts_select_own on public.puzzle_attempts;
create policy attempts_select_own on public.puzzle_attempts
    for select using (auth.uid() = user_id);

drop policy if exists attempts_insert_own on public.puzzle_attempts;
create policy attempts_insert_own on public.puzzle_attempts
    for insert with check (auth.uid() = user_id);

-- No update or delete: an attempt happened. Letting a client delete a failed
-- attempt would make the puzzle rating meaningless.

-- ---------------------------------------------------------------------------
-- user_stats  (C.2)
-- ---------------------------------------------------------------------------
create table if not exists public.user_stats (
    user_id         uuid primary key references auth.users (id) on delete cascade,
    puzzle_rating   integer not null default 1200,
    puzzle_attempts integer not null default 0,
    puzzles_solved  integer not null default 0,
    games_analysed  integer not null default 0,
    current_streak  integer not null default 0,
    best_streak     integer not null default 0,
    updated_at      timestamptz not null default now()
);

alter table public.user_stats enable row level security;

drop policy if exists stats_select_own on public.user_stats;
create policy stats_select_own on public.user_stats
    for select using (auth.uid() = user_id);

-- No client insert or update policy. The rating is computed server-side from
-- a validated attempt; a client that could write it could simply set itself
-- to 3000. The backend writes it with the service key.

-- A row per user, created at signup so nothing has to handle "stats missing".
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
    insert into public.user_stats (user_id) values (new.id)
    on conflict (user_id) do nothing;
    return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
    after insert on auth.users
    for each row execute function public.handle_new_user();

-- ---------------------------------------------------------------------------
-- Recording an attempt, atomically
-- ---------------------------------------------------------------------------
-- The attempt insert and the stats update must not be two round trips: a
-- read-modify-write on the rating loses an update if a user has two tabs
-- open. The new rating is computed in Python (partC.update_rating) and passed
-- in, so the Elo logic lives in one place and the transaction lives here.
create or replace function public.record_puzzle_attempt(
    p_user_id       uuid,
    p_puzzle_id     text,
    p_solved        boolean,
    p_ms_taken      integer,
    p_moves         jsonb,
    p_rating_before integer,
    p_rating_after  integer
) returns public.user_stats
language plpgsql
security definer
set search_path = public
as $$
declare
    result public.user_stats;
begin
    insert into public.puzzle_attempts
        (user_id, puzzle_id, solved, ms_taken, moves, rating_before, rating_after)
    values
        (p_user_id, p_puzzle_id, p_solved, p_ms_taken, p_moves,
         p_rating_before, p_rating_after);

    insert into public.user_stats (user_id) values (p_user_id)
    on conflict (user_id) do nothing;

    update public.user_stats set
        puzzle_rating   = p_rating_after,
        puzzle_attempts = puzzle_attempts + 1,
        puzzles_solved  = puzzles_solved + (case when p_solved then 1 else 0 end),
        current_streak  = case when p_solved then current_streak + 1 else 0 end,
        best_streak     = greatest(best_streak,
                                   case when p_solved then current_streak + 1 else 0 end),
        updated_at      = now()
    where user_id = p_user_id
    returning * into result;

    return result;
end;
$$;

-- Counting analyses is the same shape of problem, so it gets the same
-- treatment rather than a read-modify-write from the app.
create or replace function public.bump_games_analysed(p_user_id uuid)
returns integer
language plpgsql
security definer
set search_path = public
as $$
declare
    total integer;
begin
    insert into public.user_stats (user_id) values (p_user_id)
    on conflict (user_id) do nothing;

    update public.user_stats
       set games_analysed = games_analysed + 1,
           updated_at = now()
     where user_id = p_user_id
    returning games_analysed into total;

    return total;
end;
$$;
