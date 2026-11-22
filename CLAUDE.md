# Scanny

Web app replacing a FileMaker document-scanning workflow. Users tag a category before scanning; a background watcher picks up new files from Google Drive and files them automatically.

## Stack

- **Backend**: FastAPI (Python 3.12), async SQLAlchemy, SQLite (`data/scanny.db` on otis)
- **Frontend**: HTMX + Tailwind CSS (CDN), Jinja2 templates — no build step
- **Drive**: Google OAuth (user's own account), polling via APScheduler every 30s
- **Deploy**: Docker container `scanny` on **otis** (moved from rains 2026-11-21), compose project in `otis:~/scanny`, on the `media-stack_otis_net` network, host port 9841 → container 8000. Public URL `scanny.rob.me.uk` via the remotely-managed `cloudflared` tunnel on otis (route `http://scanny:8000`, behind Cloudflare Access). **This working tree (`otis:~/scanny`) is both the git repo and the compose project — editing files does nothing to prod until you rebuild (code + templates are baked into the image; no live reload).**

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

## Running / deploying

Everything lives in `otis:~/scanny` (git working tree + compose project). `.env`, `secrets/` and `data/` (SQLite DB `data/scanny.db`, Google/Toodledo tokens, thumbnail cache) are git-ignored and mounted into the container.

```bash
docker compose up -d --build     # deploy: rebuild image from working tree + restart
docker logs -f scanny            # tail logs
docker compose restart           # restart without rebuilding
```

Commit before deploying so prod always matches a commit. otis has no GitHub access yet (no SSH key / known_hosts), so pushing needs that set up first.

For ad-hoc dev against a scratch DB (doesn't touch prod):

```bash
cd app && pip install -r requirements.txt
DATABASE_URL=sqlite+aiosqlite:///./scratch.db uvicorn main:app --reload --port 9842
```

The old checkout on rains (`~/scanny.old-rains`) and its stopped/disabled `scanny.service` unit are retired — never start that unit; two pollers would race to file the same Drive files.

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
5. `docker compose up -d --build`
6. Visit `/status` → Connect Google Drive
7. Register an app at Toodledo (sign in → API docs → register application) with redirect URI `{APP_BASE_URL}/toodledo/callback`; put the client ID/secret in `.env`
8. Visit `/status` → Connect Toodledo → "Send test task" to verify, then check 2Do (2Do's own sync account must be set to Toodledo)
