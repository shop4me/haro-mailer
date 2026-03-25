# HARO Auto-Responder

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
- Deduplication key (`haro_query_id`) prevents duplicate request records and repeated responses for same query content.

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

The remote command **refuses** to run if `.haro-mailer-root` is missing in the target directory (prevents accidental deploy to the wrong folder).

## Tests
- Run:
  - `pytest -q`
- Current tests include:
  - de-dupe hashing idempotency
  - fallback HARO extraction behavior
