-- ============================================================
-- D8 — funnel fields on the existing tg_leads
-- ============================================================
-- Additive only. Every column is nullable with no default change, so
-- existing consumers reading tg_leads are unaffected.
alter table public.tg_leads
  add column if not exists f_type          text,
  add column if not exists f_workflow      text,
  add column if not exists f_audience      text,
  add column if not exists f_accounts      text,
  add column if not exists f_problem       text,
  add column if not exists recommended     text,
  add column if not exists human_requested boolean not null default false,
  add column if not exists name            text;

-- funnel leads awaiting a human, newest first
create index if not exists tg_leads_human
  on public.tg_leads (human_requested, updated_at desc)
  where human_requested = true;

select column_name, data_type, is_nullable
  from information_schema.columns
 where table_name = 'tg_leads'
 order by ordinal_position;
