-- Gigaverse Telegram Control Bot schema.
-- Run this in Supabase SQL Editor.
-- Use SUPABASE_SERVICE_KEY in GitHub Secrets. Do not expose it client-side.

create table if not exists giga_users (
    telegram_id bigint primary key,
    username text default '',
    first_name text default '',
    active boolean default false,
    bearer_token text default '',
    encrypted_bearer_token text default '',
    settings jsonb default '{}'::jsonb,
    state jsonb default '{}'::jsonb,
    created_at timestamptz default now(),
    updated_at timestamptz default now()
);

alter table giga_users add column if not exists bearer_token text default '';
alter table giga_users add column if not exists encrypted_bearer_token text default '';

create table if not exists giga_debug_runs (
    id bigserial primary key,
    telegram_id bigint references giga_users(telegram_id) on delete cascade,
    external_run_id text default '',
    started_at timestamptz,
    ended_at timestamptz,
    status text default 'unknown',
    rooms_cleared integer default 0,
    wins integer default 0,
    losses integer default 0,
    draws integer default 0,
    loot jsonb default '[]'::jsonb,
    drops jsonb default '[]'::jsonb,
    enemy_report jsonb default '{}'::jsonb,
    combat_log jsonb default '[]'::jsonb,
    account_snapshot jsonb default '{}'::jsonb,
    settings_snapshot jsonb default '{}'::jsonb,
    created_at timestamptz default now()
);

create table if not exists giga_bot_state (
    key text primary key,
    value jsonb default '{}'::jsonb,
    updated_at timestamptz default now()
);

create or replace function giga_update_updated_at()
returns trigger as $$
begin
    new.updated_at = now();
    return new;
end;
$$ language plpgsql;

drop trigger if exists giga_users_updated_at on giga_users;
create trigger giga_users_updated_at
    before update on giga_users
    for each row execute function giga_update_updated_at();

drop trigger if exists giga_bot_state_updated_at on giga_bot_state;
create trigger giga_bot_state_updated_at
    before update on giga_bot_state
    for each row execute function giga_update_updated_at();

create index if not exists idx_giga_users_active on giga_users(active) where active = true;
create index if not exists idx_giga_debug_runs_user_time on giga_debug_runs(telegram_id, created_at desc);
create index if not exists idx_giga_debug_runs_status on giga_debug_runs(status);

alter table giga_users enable row level security;
alter table giga_debug_runs enable row level security;
alter table giga_bot_state enable row level security;

-- No public RLS policies are created intentionally.
-- GitHub Actions uses the Supabase service_role key from repository secrets,
-- which bypasses RLS. Never put service_role key in Telegram messages or repo files.
