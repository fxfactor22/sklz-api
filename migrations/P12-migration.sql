-- ============================================================
-- P1.2 — adoption intents
-- ============================================================
-- Reuses the P1 idempotency ledger rather than creating a parallel one.
-- Adoption is a provisioning call like any other; only its meaning
-- differs, and meaning belongs in `kind`, not in a second table.
-- ============================================================

alter table public.provisioning_intents
  drop constraint if exists provisioning_intents_kind_valid;

alter table public.provisioning_intents
  add constraint provisioning_intents_kind_valid
      check (kind in ('tenant', 'owner_invite', 'lifecycle', 'adopt'));

-- At most ONE live adoption intent per provider — the same anchor that
-- makes tenant creation crash-recoverable. A retry finds this row and
-- reuses its key instead of minting a new one.
create unique index if not exists provisioning_intents_one_adopt
    on public.provisioning_intents (provider_id)
    where kind = 'adopt' and state in ('in_flight', 'succeeded');

-- ============================================================
-- Verification
-- ============================================================
select conname, pg_get_constraintdef(oid)
  from pg_constraint
 where conname = 'provisioning_intents_kind_valid';

select indexname from pg_indexes
 where tablename = 'provisioning_intents' order by indexname;

-- unchanged: adoption adds no table and no column
select count(*) as intents_today from public.provisioning_intents;
