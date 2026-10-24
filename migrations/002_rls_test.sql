-- Part F: "RLS: user A cannot read user B's analyses".
--
-- Run in the Supabase SQL editor AFTER 001_init.sql. It creates two users,
-- writes a row as each, proves neither can see the other's, then cleans up.
-- Raises an exception on any failure, so a silent pass is a real pass.
--
-- Worth re-running after any policy change: a policy that fails open looks
-- exactly like one that works, right up until someone else's data appears.
--
-- The service key bypasses RLS by design, so this must be run as a role
-- that does NOT -- the SQL editor runs as `postgres`, hence the explicit
-- `set local role authenticated` plus a forged JWT claim below. That is the
-- only way to exercise the policies as a client would.

do $$
declare
    user_a uuid := '00000000-0000-0000-0000-00000000000a';
    user_b uuid := '00000000-0000-0000-0000-00000000000b';
    visible integer;
    inserted integer;
begin
    -- Seed two analyses, one per user, bypassing RLS as the owner.
    delete from public.analyses where user_id in (user_a, user_b);
    insert into public.analyses (user_id, source_type, source_ref, pgn)
    values (user_a, 'pgn', 'A-owned', '1. e4 *'),
           (user_b, 'pgn', 'B-owned', '1. d4 *');

    -- ---- as user A ----
    set local role authenticated;
    perform set_config('request.jwt.claims',
                       json_build_object('sub', user_a, 'role', 'authenticated')::text,
                       true);

    select count(*) into visible from public.analyses;
    if visible <> 1 then
        raise exception 'RLS FAIL: user A sees % analyses, expected exactly 1', visible;
    end if;

    select count(*) into visible from public.analyses where source_ref = 'B-owned';
    if visible <> 0 then
        raise exception 'RLS FAIL: user A can read user B''s analysis';
    end if;

    -- A must not be able to write a row owned by B.
    begin
        insert into public.analyses (user_id, source_type, source_ref)
        values (user_b, 'pgn', 'forged-by-A');
        raise exception 'RLS FAIL: user A inserted a row owned by user B';
    exception
        when insufficient_privilege then null;   -- expected
    end;

    -- ---- as user B ----
    perform set_config('request.jwt.claims',
                       json_build_object('sub', user_b, 'role', 'authenticated')::text,
                       true);
    select count(*) into visible from public.analyses where source_ref = 'A-owned';
    if visible <> 0 then
        raise exception 'RLS FAIL: user B can read user A''s analysis';
    end if;

    -- ---- puzzles: RLS on with no select policy means no client access ----
    select count(*) into visible from public.puzzles;
    if visible <> 0 then
        raise exception 'RLS FAIL: a client can read the puzzles table, '
                        'which exposes the solution column';
    end if;

    -- ---- user_stats is readable but not writable by the client ----
    begin
        update public.user_stats set puzzle_rating = 3000 where user_id = user_b;
        get diagnostics inserted = row_count;
        if inserted > 0 then
            raise exception 'RLS FAIL: a client set its own puzzle rating';
        end if;
    exception
        when insufficient_privilege then null;   -- also acceptable
    end;

    reset role;
    delete from public.analyses where user_id in (user_a, user_b);
    raise notice 'RLS OK: isolation, write-forgery, puzzle solutions and rating writes all blocked';
end;
$$;
