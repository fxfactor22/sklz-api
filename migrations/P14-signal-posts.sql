-- P14 — one trade, one Telegram message: remember where each signal card
-- was posted so "secured" / "closed" edit it in place.
alter table public.signals add column if not exists tg_posts   jsonb not null default '[]'::jsonb;
alter table public.signals add column if not exists tg_text    text;
alter table public.signals add column if not exists tg_text_ar text;
