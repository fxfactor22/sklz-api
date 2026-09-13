-- ============================================================
-- D9 — demo_links.purpose: private demo vs public showcase
-- ============================================================
-- Additive. Every existing row becomes 'private_demo' by the column
-- default, so today's behaviour is what history keeps.
--
-- The public showcase link is printed on sklzlabs.com and must not
-- expire. It carries NO expires_at rather than a fabricated one far in
-- the future, so nothing downstream has to decide whether the year 9999
-- means "never" or a bug.

alter table public.demo_links
  add column if not exists purpose text not null default 'private_demo';

-- Only the two known purposes. Anything else is a mistake, not a feature.
do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'demo_links_purpose_ck'
  ) then
    alter table public.demo_links
      add constraint demo_links_purpose_ck
      check (purpose in ('private_demo', 'showcase'));
  end if;
end $$;

-- A showcase has no expiry; a private demo must still have one. Dropping
-- NOT NULL outright would let a private link be minted immortal by
-- accident, so the guarantee is kept as a check instead.
alter table public.demo_links
  alter column expires_at drop not null;

do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'demo_links_expiry_ck'
  ) then
    alter table public.demo_links
      add constraint demo_links_expiry_ck
      check (purpose = 'showcase' or expires_at is not null);
  end if;
end $$;

-- Exactly one live showcase. A revoked one does not block its
-- replacement, which is how a showcase gets rotated.
create unique index if not exists demo_links_one_active_showcase
  on public.demo_links ((true))
  where purpose = 'showcase' and revoked = false;

-- RLS, grants and the existing expiry index are untouched by this
-- migration: service role only, exactly as D4 left it.

-- verify
select column_name, is_nullable, column_default
  from information_schema.columns
 where table_name = 'demo_links' and column_name in ('purpose', 'expires_at')
 order by column_name;

select conname from pg_constraint
 where conname in ('demo_links_purpose_ck', 'demo_links_expiry_ck');

select indexname from pg_indexes
 where tablename = 'demo_links' and indexname = 'demo_links_one_active_showcase';
