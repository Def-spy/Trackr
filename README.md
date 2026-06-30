# Trackr Internship Watcher

Twice-daily scrape of The Trackr's **UK Finance summer-internship** listings.
Alerts you (mobile push + email) only when something **new** happens — never about
existing listings.

## What counts as "new"

Two signals, labelled differently in alerts:

- **🆕 Newly listed** — a programme `id` you've never seen before.
- **🟢 Now open** — an existing programme whose `openingDate` flipped from `null` → a real date. This is the strong "this just opened" signal, since most rows sit with `openingDate: null` carrying last cycle's date as a placeholder.

First run saves a silent baseline (no "everything is new" spam). Each run logs row count + raw byte size and warns if the fetch comes back suspiciously short (early warning for API changes / rate-limiting).

## Schedule

Runs at **06:00 and 12:00 UTC** (≈ 07:00 / 13:00 UK during BST). Worst-case detection latency is ~12h, comfortably inside the 24-hour window where The Trackr's own season report shows application speed measurably affects outcomes. GitHub's cron can run a few minutes late under load — fine at this cadence.

---

## Setup

### 1. Mobile push (ntfy)

1. Install the **ntfy** app (iOS / Android / F-Droid).
2. Pick a hard-to-guess topic name, e.g. `trackr-hey-9fj3kd`. (Topics on the public `ntfy.sh` server are unauthenticated — anyone who knows the name can read it, so make it unguessable.)
3. Subscribe to that topic in the app.
4. That topic name is your `NTFY_TOPIC` secret.

### 2. Gmail app password

1. Enable 2-Step Verification on your Google account.
2. Go to **Google Account → Security → App passwords**, generate one for "Mail".
3. Use your Gmail address as `GMAIL_USER` and the 16-char app password as `GMAIL_APP_PASSWORD`. `EMAIL_TO` defaults to yourself if unset.

### 3a. Run on GitHub Actions (recommended)

1. Create a repo and push these files.
2. **Settings → Secrets and variables → Actions → New repository secret**, add:
   - `NTFY_TOPIC`
   - `GMAIL_USER`
   - `GMAIL_APP_PASSWORD`
   - `EMAIL_TO` (optional)
3. The workflow has `contents: write` permission so it can commit `state.json` back after each run — that's how it remembers what it's seen. No database needed.
4. Trigger once manually (**Actions → trackr-watcher → Run workflow**) to lay down the baseline. After that it's automatic.

### 3b. Run locally (alternative)

```bash
export NTFY_TOPIC="trackr-hey-9fj3kd"
export GMAIL_USER="you@gmail.com"
export GMAIL_APP_PASSWORD="xxxxxxxxxxxxxxxx"
python3 watcher.py
```

For a local schedule on Arch, wrap it in a systemd timer or a cron entry hitting 06:00/12:00. `state.json` is written next to the script.

---

## Files

- `watcher.py` — fetch, diff, notify, persist (stdlib only).
- `.github/workflows/watcher.yml` — twice-daily cron + state commit.
- `state.json` — auto-generated snapshot (created on first run).

## Tuning

- `MIN_EXPECTED_ROWS` (default 300) — short-fetch warning threshold. Live count is ~428.
- `NTFY_SERVER` — point at a self-hosted ntfy instance if you'd rather not use `ntfy.sh`.
