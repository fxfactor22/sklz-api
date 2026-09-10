-- ============================================================
-- P1 — SKLZ → ISKRA provisioning bridge
-- ============================================================
-- Two tables. Neither touches `providers` beyond the existing
-- `iskra_tenant_id` column that P0 already created.
-- ============================================================

-- One row per provisioning INTENT, written BEFORE the call.
--
-- The idempotency key must survive the crash that loses the response.
-- Deriving a key from provider fields would look simpler and would break
-- the moment somebody edits a display name between the timeout and the
-- retry — a different key, and a second company.
create table if not exists public.provisioning_intents (
    id              uuid primary key default gen_random_uuid(),
    provider_id     uuid not null references public.providers (id)
                        on delete cascade,
    kind            text not null,      -- tenant | owner_invite | lifecycle
    idempotency_key text not null,
    request_hash    text not null,      -- canonical sha256, key removed
    state           text not null default 'in_flight',
                                        -- in_flight | succeeded | failed
    result          jsonb,              -- REDACTED. never an invite token
    error_code      text,
    error_detail    text,
    attempts        integer not null default 0,
    created_at      timestamptz not null default now(),
    settled_at      timestamptz,

    constraint provisioning_intents_kind_valid
        check (kind in ('tenant', 'owner_invite', 'lifecycle')),
    constraint provisioning_intents_state_valid
        check (state in ('in_flight', 'succeeded', 'failed'))
);

create unique index if not exists provisioning_intents_key_uniq
    on public.provisioning_intents (idempotency_key);

-- At most ONE live tenant-creation intent per provider. This is the
-- crash-recovery anchor: a retry finds the existing row and reuses its
-- key rather than minting a new one.
create unique index if not exists provisioning_intents_one_tenant
    on public.provisioning_intents (provider_id)
    where kind = 'tenant' and state in ('in_flight', 'succeeded');

create index if not exists provisioning_intents_provider
    on public.provisioning_intents (provider_id, kind, created_at desc);

-- Anything older than 15 minutes and still in flight is a customer who
-- may have paid and may have no business. Nothing deletes these; a
-- person should see them.
create or replace view public.provisioning_stuck as
    select id, provider_id, kind, idempotency_key, attempts, created_at
      from public.provisioning_intents
     where state = 'in_flight'
       and created_at < now() - interval '15 minutes';

-- ---- audit ----
-- What was asked for and what happened. Never a token, never a secret.
create table if not exists public.provider_audit (
    id          bigserial primary key,
    provider_id uuid references public.providers (id) on delete set null,
    event       text not null,
    actor       uuid,               -- the SKLZ user who acted
    detail      jsonb,              -- redacted before it reaches here
    created_at  timestamptz not null default now()
);

create index if not exists provider_audit_provider
    on public.provider_audit (provider_id, created_at desc);

-- ============================================================
-- SECURITY — same posture as providers: service role only
-- ============================================================
alter table public.provisioning_intents enable row level security;
alter table public.provider_audit       enable row level security;

revoke all on public.provisioning_intents from anon, authenticated;
revoke all on public.provider_audit       from anon, authenticated;
revoke all on public.provisioning_stuck   from anon, authenticated;

-- ============================================================
-- Verification
-- ============================================================
select table_name from information_schema.tables
 where table_schema = 'public'
   and table_name in ('provisioning_intents', 'provider_audit')
 order by table_name;

select indexname from pg_indexes
 where tablename = 'provisioning_intents' order by indexname;

select relname, relrowsecurity from pg_class
 where relname in ('provisioning_intents', 'provider_audit');

-- must be empty
select policyname from pg_policies
 where tablename in ('provisioning_intents', 'provider_audit');
