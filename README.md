# Floatfire HARO (HARO / SOS outreach)

Production-oriented Python app to ingest HARO emails, split individual journalist requests, match each request to a business, draft a targeted response, and optionally send via SMTP with strict idempotency safeguards.

## Stack
- Flask + Jinja web dashboard
- SQLAlchemy with **PostgreSQL**
- IMAP polling for inbound mail
- SMTP sending per business account
- OpenAI API for extraction, classification, and drafting

## Project structure
```
app/
  __init__.py
  config.py
  db.py
  models.py
  imap_worker.py
  haro_parser.py
  classifier.py
  drafter.py
  smtp_sender.py
  routes.py
  poll_once.py
  scheduler_service.py
  templates/
migrations/
run.py
tests/
```

## Setup
1. Create venv and install dependencies:
   - `python -m venv .venv`
   - `source .venv/bin/activate` (or `.venv\Scripts\activate` on Windows)
   - `pip install -r requirements.txt`
2. **PostgreSQL**: Create database and user:
   - `sudo -u postgres createuser -P haro`  (set password when prompted, e.g. `haro`)
   - `sudo -u postgres createdb -O haro haro`
   - Or with psql: `CREATE USER haro WITH PASSWORD 'haro'; CREATE DATABASE haro OWNER haro;`
3. Create `.env` in project root (copy from `.env.example` or use template below). Set `DATABASE_URL` to your Postgres URL.
4. Start web app: `python run.py`
5. Open `http://localhost:5000` and log in with `ADMIN_PASSWORD`.

## .env template
```env
APP_DOMAIN=floatfire.com
PUBLIC_BASE_URL=https://floatfire.com
APP_NAME=Floatfire HARO

OPENAI_API_KEY=
ADMIN_PASSWORD=change-me
FLASK_SECRET_KEY=change-me-too

# PostgreSQL (create user/db first — see Setup above)
DATABASE_URL=postgresql://haro:haro@127.0.0.1:5432/haro

GLOBAL_AUTO_SEND=false
GLOBAL_DRY_RUN=true
GLOBAL_REVIEW_MODE=true
MAX_SENDS_PER_RUN=20
LOOKBACK_HOURS=48

# Optional scheduler format HH:MM,HH:MM,HH:MM
RUN_TIMES=08:00,13:00,18:00
```

## How to add a mailbox
1. Log in and go to `Mailboxes`.
2. Add IMAP host/port/user/password/folder.
3. Ensure `Enabled` is checked.
4. Run `python -m app.poll_once` to test ingestion.

## How to add a business
1. Go to `Businesses`.
2. Fill:
   - `name`, `contact_name`, `nature_of_business`
   - `keywords` (comma-separated)
   - SMTP settings + `sending_email`
   - `signature`
3. Set `auto_send_enabled` and a safe `auto_send_threshold` (e.g. `0.85`) if using AUTO mode.

## Running pipeline
- One-shot poll/extract/classify/draft/send:
  - `python -m app.poll_once`
- Web dashboard:
  - `python run.py`
- Optional always-on scheduler:
  - `python -m app.scheduler_service`

## Cron (3x/day) example
```cron
0 8,13,18 * * * cd /home/mike/Desktop/cursor/HARO && /home/mike/Desktop/cursor/HARO/.venv/bin/python -m app.poll_once >> /home/mike/Desktop/cursor/HARO/haro_cron.log 2>&1
```

## Safety notes
- `GLOBAL_DRY_RUN=true`: drafts only, no sends.
- `GLOBAL_REVIEW_MODE=true`: queue drafts for manual approval.
- `GLOBAL_AUTO_SEND=true` sends automatically only when:
  - business auto-send is enabled,
  - match confidence >= business threshold,
  - not dry-run/review mode,
  - max sends per run is not exceeded.
- Deduplication key (`haro_query_id`) is derived **only** from the journalist **reply-to / HARO request-ID email** (case-insensitive). Same address → one row. Rows with no reply address use a per-inbound/slot key and do not merge. A send-time check skips SMTP if the **same reply-to** already has **SENT** for that business.

## Troubleshooting IMAP/SMTP
- IMAP login fails:
  - verify host/port/folder and account app-password requirements.
  - confirm SSL IMAP on port 993 (or update mailbox port).
- SMTP send fails:
  - verify SMTP host/port/starttls compatibility.
  - check account-specific sending restrictions and app passwords.
- No destination found:
  - app prioritizes `reply_to_email`, then `Reply-To` header, then request text patterns (`Email:` / `Send responses to:`).
- Parsing poor quality:
  - confirm `OPENAI_API_KEY` is valid.
  - fallback parser runs automatically when OpenAI parsing fails.

## Deploy (git + server)

Script: `scripts/deploy.sh` — pushes `main` to GitHub, then SSHs to **only** the configured app directory and runs `git pull --ff-only`. Other sites on the same server are not modified.

1. **One-time on the server** (as user `haro`, path must match `DEPLOY_PATH` default or your override):
   - `mkdir -p /home/haro/haro-mailer && cd /home/haro/haro-mailer`
   - `git clone git@github.com:shop4me/haro-mailer.git .` (or HTTPS), so `.haro-mailer-root` from the repo is present.
   - Copy `.env` (not in git), create venv, `pip install -r requirements.txt`, configure process manager only for this app if needed.

2. **Local**: copy `.deploy.env.example` → `.deploy.env` (gitignored). Set `DEPLOY_PATH` if the clone lives elsewhere. Prefer **SSH key** auth: `ssh-copy-id haro@142.93.187.80`. Optional: `sshpass` + `DEPLOY_SSH_PASSWORD` (avoid committing passwords).

3. Run: `chmod +x scripts/deploy.sh && ./scripts/deploy.sh`

4. **Reload the app** so new code and dependencies load (after `git pull` on the server):
   - `chmod +x scripts/server_restart.sh && ./scripts/server_restart.sh` — runs `pip install -r requirements.txt` on the server and sends **HUP** to the gunicorn master (no `sudo`).
   - If you use systemd and have passwordless `sudo`: `ssh haro@HOST 'sudo systemctl restart haro-mailer'`.

The remote command **refuses** to run if `.haro-mailer-root` is missing in the target directory (prevents accidental deploy to the wrong folder).

### Production on the server (gunicorn + systemd, isolated)

- Gunicorn binds **`0.0.0.0:18080`** by default (see `gunicorn.conf.py`; override with `GUNICORN_BIND`) so it does not use **`:8001`**, which may already be taken by another app on a shared host.
- **One-time** on the server, after `git clone` and `.env` exist under `/home/haro/haro-mailer`:

```bash
cd /home/haro/haro-mailer
git pull --ff-only origin main
chmod +x scripts/server_install.sh
./scripts/server_install.sh --with-systemd
```

- **Public URL (default):** `http://YOUR_SERVER_IP:18080/` (or your hostname on that port).
- **UFW:** If the host uses `ufw` with default deny, allow this app’s port once: `sudo ufw allow 18080/tcp comment 'HARO mailer'` then `sudo ufw reload`.
- **Nginx (optional):** add a `location /` block only inside the `server { }` for this app’s hostname; see `deploy/nginx-location-snippet.conf.example`. Reload nginx — other vhosts stay unchanged if you only edit that one server block.
- **Stop** any manual `python run.py` on port 5000 before starting the service, or you will have two processes.

## Local browser smoke test (Puppeteer)

Optional, on your **desktop** only — `node_modules/` is gitignored and is **not** part of the Python server deploy.

```bash
npm install
npm run smoke              # headless (no window)
npm run smoke:headed       # opens real Chromium so you can watch (~20s then closes)
```

Uses `HARO_SMOKE_URL` if set (default: `http://142.93.187.80:18080/login`). Headed mode needs a graphical session (`DISPLAY`, e.g. run from your desktop terminal, not a headless SSH session).

## Tests
- Run:
  - `pytest -q`
- Current tests include:
  - de-dupe hashing idempotency
  - fallback HARO extraction behavior
