-- P13: the follower EA's poll is its heartbeat. Persist it so
-- /api/mt5copy/diag can say "EA offline" across API restarts.
-- Safe to run more than once. The API tolerates this column being
-- absent (it falls back to an in-process timestamp).
alter table public.copy_slaves
    add column if not exists last_poll_at timestamptz;
