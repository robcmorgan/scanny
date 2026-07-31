# Scanny

Web app replacing a FileMaker document-scanning workflow. Users tag a category before scanning; a background watcher picks up new files from Google Drive and files them automatically.

## Stack

- **Backend**: FastAPI (Python 3.12), async SQLAlchemy, SQLite (`app/scanny.db`)
- **Frontend**: HTMX + Tailwind CSS (CDN), Jinja2 templates — no build step
- **Drive**: Google OAuth (user's own account), polling via APScheduler every 30s
- **Deploy**: systemd service `scanny.service` on this host, running bare `uvicorn main:app` (no `--reload`) on port 9841. `docker-compose.yml` exists in the repo but is unused — the app was never actually moved into Docker. **Code changes to `.py` files require `sudo systemctl restart scanny` to take effect; only Jinja templates are picked up live.**

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
    toodledo.py    — Toodledo OAuth2 + task creation (2Do sync)
  templates/       — Jinja2 HTML (HTMX partials prefixed with _)
secrets/           — put client_secret.json here (git-ignored)
```

## Filing convention

Default: `/{Group}/{Subgroup}/{YYYY-MM-DD} {String from tag} scanned papers-{HHMMSS}.ext`

e.g. `/Health/Alice Dentist/2016-12-04 Alice Dentist scanned papers-152513.pdf`

With override filename: `/{Group}/{Subgroup}/{override stem}.ext`

`String from tag` is stored as `Subgroup.filename_tag`. Fallback when NULL: group name (for "/" subgroup) or subgroup name.

## Running / restarting

The live app runs as the `scanny` systemd service (`/etc/systemd/system/scanny.service`), `WorkingDirectory=/home/robmorgan/scanny/app`, `ExecStart=/home/robmorgan/.local/bin/uvicorn main:app --host 0.0.0.0 --port 9841`. It uses the `.env` in the repo root and the default SQLite DB path (no `DATABASE_URL` override).

```bash
sudo systemctl restart scanny   # required after any .py change
sudo systemctl status scanny
journalctl -u scanny -f          # tail logs
```

For ad-hoc local dev against a scratch DB instead of the live one:

```bash
cd app
pip install -r requirements.txt
DATABASE_URL=sqlite+aiosqlite:///./scratch.db uvicorn main:app --reload --port 9842
```

`docker-compose.yml` in the repo root is unused/aspirational — don't assume Docker is involved when debugging deploy issues.

## Environment variables

| Variable | Description |
|---|---|
| `DRIVE_INCOMING_FOLDER_ID` | Google Drive folder ID where scanner drops files |
| `DRIVE_FILED_ROOT_FOLDER_ID` | Root folder ID for the organised archive |
| `APP_BASE_URL` | Public URL of this app (used for OAuth callback) |
| `POLL_INTERVAL_SECONDS` | How often to check Drive (default 30) |
| `TOODLEDO_CLIENT_ID` | Toodledo API app client ID (2Do task sync) |
| `TOODLEDO_CLIENT_SECRET` | Toodledo API app client secret |

## Setup checklist

1. Create Google Cloud project → enable Drive API → create OAuth 2.0 Web App credentials
2. Add `{APP_BASE_URL}/drive/callback` as an authorised redirect URI
3. Download `client_secret.json` → place in `secrets/`
4. Set `.env` with folder IDs and base URL
5. `docker compose up --build`
6. Visit `/status` → Connect Google Drive
7. Register an app at Toodledo (sign in → API docs → register application) with redirect URI `{APP_BASE_URL}/toodledo/callback`; put the client ID/secret in `.env`
8. Visit `/status` → Connect Toodledo → "Send test task" to verify, then check 2Do (2Do's own sync account must be set to Toodledo)
