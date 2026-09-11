-- ============================================================
-- D6 — demo auto-close and real demo Telegram delivery
-- ============================================================
alter table public.bot_orders
  add column if not exists demo_close_command_id uuid,
  add column if not exists demo_closed_at        timestamptz,
  add column if not exists demo_close_state      text,
  add column if not exists demo_tg_message_id    bigint,
  add column if not exists demo_tg_sent_at       timestamptz,
  add column if not exists demo_tg_error         text;

-- the sweep looks for filled demo orders that still need closing
create index if not exists bot_orders_demo_sweep
  on public.bot_orders (status, demo_closed_at)
  where demo_token is not null;

select column_name from information_schema.columns
 where table_name='bot_orders'
   and column_name like 'demo_%' order by column_name;
