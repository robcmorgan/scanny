# Scanny

Web app replacing a FileMaker document-scanning workflow. Users tag a category before scanning; a background watcher picks up new files from Google Drive and files them automatically.

## Stack

- **Backend**: FastAPI (Python 3.12), async SQLAlchemy, PostgreSQL 16
- **Frontend**: HTMX + Tailwind CSS (CDN), Jinja2 templates — no build step
- **Drive**: Google OAuth (user's own account), polling via APScheduler every 30s
- **Deploy**: Docker Compose, separate Portainer stack, port 9841

## Structure

```
app/
  main.py          — all routes
  models.py        — Category, PendingScan, Document
  database.py      — SQLAlchemy async engine + init_db()
  config.py        — pydantic-settings (reads .env)
  services/
    drive.py       — Google Drive API helpers
    filer.py       — filename parsing + Drive move/rename logic
    watcher.py     — APScheduler background poll job
  templates/       — Jinja2 HTML (HTMX partials prefixed with _)
secrets/           — put client_secret.json here (git-ignored)
```

## Filing convention

`/{Group}/{Subgroup}/{YYYY-MM-DD} {HHMM} {Subgroup} {Group}.ext`

e.g. `/Health/Rob/2026-12-01 1149 Rob Health.pdf`

## Running locally (outside Docker)

```bash
cd app
pip install -r requirements.txt
DATABASE_URL=postgresql+asyncpg://... uvicorn main:app --reload
```

## Docker

```bash
cp .env.example .env   # fill in folder IDs + APP_BASE_URL
mkdir -p data
docker compose up --build
```

## Environment variables

| Variable | Description |
|---|---|
| `DRIVE_INCOMING_FOLDER_ID` | Google Drive folder ID where scanner drops files |
| `DRIVE_FILED_ROOT_FOLDER_ID` | Root folder ID for the organised archive |
| `APP_BASE_URL` | Public URL of this app (used for OAuth callback) |
| `POLL_INTERVAL_SECONDS` | How often to check Drive (default 30) |

## Setup checklist

1. Create Google Cloud project → enable Drive API → create OAuth 2.0 Web App credentials
2. Add `{APP_BASE_URL}/drive/callback` as an authorised redirect URI
3. Download `client_secret.json` → place in `secrets/`
4. Set `.env` with folder IDs and base URL
5. `docker compose up --build`
6. Visit `/status` → Connect Google Drive
