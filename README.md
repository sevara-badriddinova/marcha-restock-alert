# Marukyu Koyamaen Matcha Restock Bot

Free Telegram restock alerts for the Marukyu Koyamaen International Online Shop matcha catalog.

The bot runs from GitHub Actions. It does not use a hosted server, webhook, frontend, or database. State is stored in `state.json` and committed back to the repository.

## What It Does

- Checks the Matcha catalog during Japan business hours.
- Extracts product URLs from the catalog page.
- Checks product detail pages in parallel.
- Parses stock status from JSON-LD, WooCommerce classes, and out-of-stock page text.
- Saves product status and Telegram subscribers in `state.json`.
- Sends normal subscribers alerts only when products become available.
- Optionally sends all status changes to an admin chat.

## Schedule

Marukyu Koyamaen says Matcha restocks randomly during business hours:

Monday-Friday, 9:00am-5:30pm Japan time.

GitHub Actions cron runs every 5 minutes during the matching UTC window. The Python script also enforces the exact Japan-time business-hours guard.

## GitHub Secrets

Required:

- `BOT_TOKEN`: Telegram bot token from BotFather.

Optional but recommended:

- `ADMIN_CHAT_ID`: receives all status changes, including products becoming sold out.

Optional fallback:

- `CHAT_ID`: used only if no Telegram subscribers exist yet.

Do not commit tokens or chat secrets to the repository.

## Telegram Commands

Because this bot uses GitHub Actions polling instead of a live server, commands may take a few minutes to process.

- `/start`: subscribe to restock alerts.
- `/status`: show currently available products.
- `/all`: show inventory summary and product list.
- `/stop`: unsubscribe.

Subscribers do not receive full inventory dumps automatically.

## Manual Testing

Run outside Japan business hours:

```bash
SKIP_BUSINESS_HOURS_CHECK=true python3 check_stock.py
```

Create or refresh the first baseline without sending stock-change alerts:

```bash
BASELINE_ONLY=true SKIP_BUSINESS_HOURS_CHECK=true python3 check_stock.py
```

For local runs, provide `BOT_TOKEN` in your shell environment. `CHAT_ID` is optional when subscriber support is active.

## GitHub Actions Manual Runs

Use **Run workflow** with:

- `skip_business_hours_check=true` for testing outside Japan business hours.
- `baseline_only=true` to save current statuses without stock-change alerts.

Scheduled production runs set both flags to `false`.

## State File

Expected structure:

```json
{
  "products": {},
  "telegram": {
    "subscribers": {},
    "last_update_id": null
  },
  "last_checked_at": "2026-06-03T22:00:00+00:00"
}
```

`state.json` is intentionally tracked because GitHub Actions uses it as persistent storage.

## Known Limitations

- Telegram commands are delayed until the next GitHub Actions run.
- If the shop blocks a request or returns an error page, the script fails closed and does not overwrite product state.
- GitHub Actions commits can still race if many runs overlap, so the workflow includes a fetch/reset/retry push loop for `state.json`.
