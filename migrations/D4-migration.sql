-- ============================================================
-- D4 — private 48-hour prospect demo links
-- ============================================================
create table if not exists public.demo_links (
    token            text primary key,
    provider_name    text not null,
    telegram_channel text not null,
    language         text not null default 'en',
    contact_name     text,
    contact_email    text,
    logo_url         text,
    note             text,
    expires_at       timestamptz not null,
    revoked          boolean not null default false,
    opened_count     integer not null default 0,
    first_opened_at  timestamptz,
    last_opened_at   timestamptz,
    created_by       uuid,
    created_at       timestamptz not null default now()
);

create index if not exists demo_links_expiry on public.demo_links (expires_at desc);

-- Service role only. A prospect reads their own link through the API,
-- which returns one row by token and nothing else — there is no listing
-- path for anonymous callers, so one prospect cannot enumerate another.
alter table public.demo_links enable row level security;
revoke all on public.demo_links from anon, authenticated;

select column_name, data_type from information_schema.columns
 where table_name = 'demo_links' order by ordinal_position;
