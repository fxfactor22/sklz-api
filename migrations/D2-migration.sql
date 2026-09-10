-- ============================================================
-- D2 — crypto order capture for the provider demo
-- ============================================================
create table if not exists public.crypto_orders (
    id          uuid primary key default gen_random_uuid(),
    reference   text not null,
    name        text not null,
    email       text not null,
    telegram    text,
    package     text not null,          -- signal_desk | pro_trader_os
    asset       text not null,          -- USDT | SOL
    network     text not null,          -- TRC20 | Solana
    amount      text not null,
    wallet      text,
    tx_hash     text not null,
    status      text not null default 'submitted',
    notes       text,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now(),
    constraint crypto_orders_status_valid check (status in
      ('awaiting_payment','submitted','verifying','confirmed',
       'activation_pending','activated','rejected'))
);

create unique index if not exists crypto_orders_reference_uniq
    on public.crypto_orders (reference);
-- the same hash submitted twice is one order, not two
create unique index if not exists crypto_orders_tx_uniq
    on public.crypto_orders (lower(tx_hash));
create index if not exists crypto_orders_status
    on public.crypto_orders (status, created_at desc);

alter table public.crypto_orders enable row level security;
revoke all on public.crypto_orders from anon, authenticated;

select column_name, data_type from information_schema.columns
 where table_name='crypto_orders' order by ordinal_position;
