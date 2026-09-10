-- ============================================================
-- P0 — canonical provider identity
-- ============================================================
-- Additive. Nothing existing is rekeyed, nothing is migrated, and every
-- current retail flow keeps working untouched: no existing table gains a
-- NOT NULL column and no foreign key is repointed.
--
-- SKLZ owns provider identity. `iskra_tenant_id` is a link to the ISKRA
-- tenant, not a source of truth, and it is nullable because a provider
-- exists here before it exists there.
-- ============================================================

create table if not exists public.providers (
    id             uuid primary key default gen_random_uuid(),

    -- The SKLZ identity that owns this business. auth.users is the
    -- identity system for the whole platform; `profiles` mirrors it.
    owner_user_id  uuid not null references auth.users (id) on delete restrict,

    -- The ISKRA tenant this provider maps to. NULL until linked. Never
    -- an email, never a name — a UUID or nothing.
    iskra_tenant_id uuid,

    display_name   text not null,
    slug           text not null,

    -- draft: created, not trading. active: operating. suspended:
    -- temporarily stopped by SKLZ. closed: offboarded.
    -- Deliberately independent of subscription, tenant and trading
    -- account status — conflating them is how an unpaid invoice
    -- silently stops someone's signals.
    status         text not null default 'draft',

    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now(),

    constraint providers_status_valid
        check (status in ('draft', 'active', 'suspended', 'closed')),
    constraint providers_slug_shape
        check (slug ~ '^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$')
);

-- One ISKRA tenant maps to at most one SKLZ provider. A partial unique
-- index rather than a plain UNIQUE so that many providers may sit
-- unlinked (NULL) without colliding with each other.
create unique index if not exists providers_iskra_tenant_uniq
    on public.providers (iskra_tenant_id)
    where iskra_tenant_id is not null;

create unique index if not exists providers_slug_uniq
    on public.providers (lower(slug));

create index if not exists providers_owner
    on public.providers (owner_user_id, status);

-- ---- keep updated_at honest ----
create or replace function public.touch_providers_updated_at()
returns trigger language plpgsql as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists providers_touch_updated_at on public.providers;
create trigger providers_touch_updated_at
    before update on public.providers
    for each row execute function public.touch_providers_updated_at();

-- ============================================================
-- SECURITY
-- ============================================================
-- RLS ON with NO policies = deny to anon and authenticated keys entirely.
-- The API reaches this table with the service role, which bypasses RLS,
-- and applies ownership rules in code where it can also distinguish a
-- platform admin. A provider record must never be readable or writable
-- through a public Supabase key.
alter table public.providers enable row level security;

revoke all on public.providers from anon, authenticated;

-- ============================================================
-- Verification
-- ============================================================
select column_name, data_type, is_nullable
  from information_schema.columns
 where table_name = 'providers' order by ordinal_position;

select indexname from pg_indexes where tablename = 'providers';

select relrowsecurity as rls_enabled
  from pg_class where relname = 'providers';

-- must be empty: no policy means no access for public keys
select policyname from pg_policies where tablename = 'providers';
