-- Gigaverse Telegram Control Bot schema.
-- Run this in Supabase SQL Editor.
-- Use SUPABASE_SERVICE_KEY in GitHub Secrets. Do not expose it client-side.

create table if not exists giga_users (
    telegram_id bigint primary key,
    username text default '',
    first_name text default '',
    active boolean default false,
    settings jsonb default '{}'::jsonb,
    state jsonb default '{}'::jsonb,
    created_at timestamptz default now(),
    updated_at timestamptz default now()
);

create table if not exists giga_user_secrets (
    telegram_id bigint primary key references giga_users(telegram_id) on delete cascade,
    bearer_token text default '',
    encrypted_bearer_token text default '',
    created_at timestamptz default now(),
    updated_at timestamptz default now()
);

-- Migration path for older versions that temporarily kept tokens in giga_users.
do $$
begin
    if exists (
        select 1 from information_schema.columns
        where table_schema = 'public' and table_name = 'giga_users' and column_name = 'bearer_token'
    ) then
        insert into giga_user_secrets (telegram_id, bearer_token, encrypted_bearer_token)
        select
            telegram_id,
            coalesce(bearer_token, ''),
            coalesce(encrypted_bearer_token, '')
        from giga_users
        where coalesce(bearer_token, encrypted_bearer_token, '') <> ''
        on conflict (telegram_id) do update set
            bearer_token = excluded.bearer_token,
            encrypted_bearer_token = excluded.encrypted_bearer_token,
            updated_at = now();
    end if;
end $$;

alter table giga_users drop column if exists bearer_token;
alter table giga_users drop column if exists encrypted_bearer_token;

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
    loot_value jsonb default '{}'::jsonb,
    enemy_report jsonb default '{}'::jsonb,
    combat_log jsonb default '[]'::jsonb,
    account_snapshot jsonb default '{}'::jsonb,
    settings_snapshot jsonb default '{}'::jsonb,
    created_at timestamptz default now()
);

alter table giga_debug_runs add column if not exists loot_value jsonb default '{}'::jsonb;

create table if not exists giga_debug_turns (
    id bigserial primary key,
    run_row_id bigint references giga_debug_runs(id) on delete cascade,
    telegram_id bigint references giga_users(telegram_id) on delete cascade,
    external_run_id text default '',
    turn_index integer not null,
    room integer,
    floor integer,
    enemy_id text default '',
    our_move text default '',
    enemy_move text default '',
    result text default '',
    before_state jsonb default '{}'::jsonb,
    after_state jsonb default '{}'::jsonb,
    decision jsonb default '{}'::jsonb,
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

drop trigger if exists giga_user_secrets_updated_at on giga_user_secrets;
create trigger giga_user_secrets_updated_at
    before update on giga_user_secrets
    for each row execute function giga_update_updated_at();

drop trigger if exists giga_bot_state_updated_at on giga_bot_state;
create trigger giga_bot_state_updated_at
    before update on giga_bot_state
    for each row execute function giga_update_updated_at();

create index if not exists idx_giga_users_active on giga_users(active) where active = true;
create index if not exists idx_giga_debug_runs_user_time on giga_debug_runs(telegram_id, created_at desc);
create index if not exists idx_giga_debug_runs_status on giga_debug_runs(status);
create index if not exists idx_giga_debug_turns_run on giga_debug_turns(run_row_id, turn_index);
create index if not exists idx_giga_debug_turns_user_time on giga_debug_turns(telegram_id, created_at desc);
create index if not exists idx_giga_debug_turns_enemy on giga_debug_turns(enemy_id, result);

alter table giga_users enable row level security;
alter table giga_user_secrets enable row level security;
alter table giga_debug_runs enable row level security;
alter table giga_debug_turns enable row level security;
alter table giga_bot_state enable row level security;

-- No public RLS policies are created intentionally.
-- GitHub Actions uses the Supabase service_role key from repository secrets,
-- which bypasses RLS. Never put service_role key in Telegram messages or repo files.
-- Important: bearer tokens live only in giga_user_secrets.
-- Do not add public/anon policies to this table. The bot is a backend worker and
-- must use SUPABASE_SERVICE_KEY from GitHub Secrets.
