# Gigaverse Telegram Control Center

Telegram bot + GitHub Actions workers for running Gigaverse dungeon runs from Telegram.

The project is designed for an open GitHub repository: no bearer tokens, Telegram tokens, or Supabase keys are stored in the repo. User bearer tokens are stored in a separate locked Supabase table, and debug run rows are sanitized before they are written.

## What It Does

- Starts one run or a batch of runs from Telegram.
- Shows live battle/account status in a pinned Telegram message.
- Lets every user configure their own bearer token, wallet address, dungeon, gear IDs, run count, delay, auto-continue, and loot mode.
- Runs users in GitHub Actions with a matrix, so each active Telegram user gets a separate worker job.
- Stores debug data for every run in Supabase so combat and loot behavior can be analyzed later.

## Important Repository Layout

GitHub only reads workflows from the repository root `.github/workflows` folder.

If this folder is the root of the GitHub repository, nothing else is needed:

```text
TG BOT/
  .github/workflows/
  giga_tg_bot.py
  requirements.txt
```

If you keep `TG BOT` inside a bigger repository, move `TG BOT/.github` to the repository root and set each workflow step to use `working-directory: TG BOT`.

## Required Secrets

Add these in GitHub: `Settings -> Secrets and variables -> Actions`.

| Secret | Description |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Token from BotFather. |
| `SUPABASE_URL` | Project URL from Supabase. |
| `SUPABASE_SERVICE_KEY` | Supabase service role key. Required. Do not use the anon key here. |
| `GIGAVERSE_BASE_URL` | Optional. Defaults to `https://gigaverse.io`. |

Optional extra secret:

| Secret | Description |
| --- | --- |
| `GIGA_SECRET_KEY` | Optional Fernet key. If set, bearer tokens are encrypted before saving to Supabase. |

You do not need `GIGA_SECRET_KEY` for the simple setup. If you want extra app-level encryption later, generate it locally:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Supabase Setup

Create a Supabase project and run `supabase_schema.sql` in the SQL editor.

The schema creates:

- `giga_users` - Telegram users, settings, pinned message state.
- `giga_user_secrets` - bearer tokens only. RLS is enabled and no public read policy is created.
- `giga_debug_runs` - sanitized combat/loot/debug history for analysis.
- `giga_bot_state` - Telegram polling offset.

Row Level Security is enabled and no public policies are added. The bot uses `SUPABASE_SERVICE_KEY` only from GitHub Actions secrets.

Do not configure this bot with `SUPABASE_ANON_KEY`. It is intentionally a backend worker, so it uses the service role key from GitHub Secrets. Keep token data out of `giga_users`.

## Workflows

### `Gigaverse Telegram Bot`

Long-polling Telegram controller. It reads commands/buttons and updates Supabase.

Run it manually from the Actions tab once after deployment. The workflow re-dispatches itself while started manually, similar to the SFL notification bot pattern.

### `Gigaverse Matrix Workers`

Builds a matrix from active users in `giga_users`, then runs:

```bash
python giga_tg_bot.py worker --user <telegram_id>
```

Each user is isolated by GitHub Actions concurrency:

```yaml
group: giga-user-${{ matrix.telegram_id }}
```

If a worker exits with code `2`, it means temporary rate-limit/backoff and the workflow re-dispatches only that user.

When `/run` is pressed in Telegram, the controller also dispatches `giga_matrix.yml` for that specific Telegram user, so a run does not have to wait for the cron schedule.

## Telegram Commands

Use `/start` first.

```text
/settoken <bearer-token>
/token
/setaddress <wallet-address>
/setdungeon 1
/setruns 3
/setgear 123,456
/setdelay 1.2
/run
/run 5
/stop
/status
/settings
```

The bot deletes `/settoken` messages after saving when Telegram allows it.

Users can also press `Set bearer token` in the inline keyboard. The bot then waits for the next message, accepts either `ey...` or `Bearer ey...`, deletes that message, normalizes it, and saves it to `giga_user_secrets.bearer_token`.

## Debug Data

Every finished run is saved to `giga_debug_runs` with:

- account snapshot
- run settings snapshot without bearer token
- combat log
- enemy move history
- loot choices
- drops
- final status

This is the main database for improving battle and loot logic across all users.

## Current Limitations

- This is a separate GitHub/Telegram version, not a full copy of the local browser UI.
- Combat and loot logic are intentionally compact but already include charge conservation, boss aggression, Magic deprioritization, AddMaxHealth priority, and debug logging.
- Marketplace/inventory valuation is not included yet.
- GitHub runners are not real-time servers. The pinned message is updated often while a worker is alive, but Telegram commands are processed by the controller workflow.

## Local Test

Install dependencies:

```bash
pip install -r requirements.txt
```

Run the controller:

```bash
python giga_tg_bot.py bot --duration 600
```

Run one user worker:

```bash
python giga_tg_bot.py worker --user 123456789 --duration 600
```

Build the matrix:

```bash
python giga_tg_bot.py matrix
```
