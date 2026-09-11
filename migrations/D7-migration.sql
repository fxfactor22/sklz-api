-- D7 — carry a positions read back to the caller
alter table public.bot_orders
  add column if not exists positions jsonb;
select column_name from information_schema.columns
 where table_name='bot_orders' and column_name='positions';
