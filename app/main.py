from contextlib import asynccontextmanager
from typing import Optional
from datetime import datetime, timezone
import asyncio
import re
import time
from pathlib import Path

from fastapi import FastAPI, Request, Depends, HTTPException, Form, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, FileResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, or_, and_, func
from sqlalchemy.orm import joinedload, selectinload

from database import get_db, init_db, AsyncSessionLocal
from models import Group, Subgroup, PendingScan, Document
from config import settings
from services.drive import (
    get_flow, save_credentials_from_flow, get_credentials, get_drive_service,
    download_file_bytes as _fetch_drive_media, trash_or_move_to_can_delete as _trash_or_move_to_can_delete,
)
from services.watcher import start_scheduler, stop_scheduler
import services.toodledo as toodledo


THUMB_CACHE = Path(__file__).parent.parent / "data" / "thumbnails"
THUMB_TTL = 7 * 24 * 3600  # 7 days


async def _fetch_and_cache_thumbnail(drive_file_id: str) -> bytes | None:
    """Fetch thumbnail from Drive, save to disk cache, return bytes. Returns None if unavailable."""
    import httpx
    try:
        service = get_drive_service()
        meta = await asyncio.to_thread(
            lambda: service.files().get(fileId=drive_file_id, fields="thumbnailLink").execute()
        )
        thumb_url = meta.get("thumbnailLink")
        if not thumb_url:
            return None
        thumb_url = thumb_url.replace("=s220", "=s400")
        creds = get_credentials()
        async with httpx.AsyncClient() as client:
            resp = await client.get(thumb_url, headers={"Authorization": f"Bearer {creds.token}"}, follow_redirects=True)
        if resp.status_code != 200:
            return None
        THUMB_CACHE.mkdir(parents=True, exist_ok=True)
        (THUMB_CACHE / f"{drive_file_id}.jpg").write_bytes(resp.content)
        return resp.content
    except Exception as e:
        print(f"[thumbnail] fetch error {drive_file_id}: {e}")
        return None


async def _warm_thumbnail_cache():
    """Background: pre-fetch thumbnails for all linked documents that aren't cached yet."""
    await asyncio.sleep(5)  # let the server finish starting
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Document.drive_file_id)
                .where(Document.drive_file_id.isnot(None), Document.broken_link == False)
            )
            file_ids = [r[0] for r in result.fetchall()]
        missing = [fid for fid in file_ids if not (THUMB_CACHE / f"{fid}.jpg").exists()]
        print(f"[thumbnail cache] {len(file_ids) - len(missing)} cached, warming {len(missing)}")
        for fid in missing:
            await _fetch_and_cache_thumbnail(fid)
            await asyncio.sleep(0.15)  # ~7/sec, gentle on Drive API
        print(f"[thumbnail cache] warm-up done")
    except Exception as e:
        print(f"[thumbnail cache] warm-up error: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    start_scheduler()
    asyncio.create_task(_warm_thumbnail_cache())
    yield
    stop_scheduler()


app = FastAPI(lifespan=lifespan, title="Scanny")
templates = Jinja2Templates(directory="templates")

_CHIP_PALETTE = [
    "bg-blue-100 text-blue-900",     "bg-orange-200 text-orange-900",
    "bg-green-300 text-green-900",   "bg-fuchsia-400 text-fuchsia-900",
    "bg-amber-100 text-amber-900",   "bg-sky-200 text-sky-900",
    "bg-red-300 text-red-900",       "bg-lime-400 text-lime-900",
    "bg-violet-100 text-violet-900", "bg-yellow-200 text-yellow-900",
    "bg-teal-300 text-teal-900",     "bg-pink-400 text-pink-900",
    "bg-rose-100 text-rose-900",     "bg-emerald-200 text-emerald-900",
    "bg-cyan-300 text-cyan-900",     "bg-indigo-400 text-indigo-900",
    "bg-purple-100 text-purple-900", "bg-blue-200 text-blue-900",
    "bg-orange-300 text-orange-900", "bg-green-400 text-green-900",
    "bg-fuchsia-100 text-fuchsia-900","bg-amber-200 text-amber-900",
    "bg-sky-300 text-sky-900",       "bg-red-400 text-red-900",
    "bg-lime-100 text-lime-900",     "bg-violet-200 text-violet-900",
    "bg-yellow-300 text-yellow-900", "bg-teal-400 text-teal-900",
    "bg-pink-100 text-pink-900",     "bg-rose-200 text-rose-900",
    "bg-emerald-300 text-emerald-900","bg-cyan-400 text-cyan-900",
    "bg-indigo-100 text-indigo-900", "bg-purple-200 text-purple-900",
    "bg-blue-300 text-blue-900",     "bg-orange-400 text-orange-900",
    "bg-green-100 text-green-900",   "bg-fuchsia-200 text-fuchsia-900",
    "bg-amber-300 text-amber-900",   "bg-sky-400 text-sky-900",
    "bg-red-100 text-red-900",       "bg-lime-200 text-lime-900",
    "bg-violet-300 text-violet-900", "bg-yellow-400 text-yellow-900",
    "bg-teal-100 text-teal-900",     "bg-pink-200 text-pink-900",
    "bg-rose-300 text-rose-900",     "bg-emerald-400 text-emerald-900",
    "bg-cyan-100 text-cyan-900",     "bg-indigo-200 text-indigo-900",
    "bg-purple-300 text-purple-900", "bg-blue-400 text-blue-900",
    "bg-orange-100 text-orange-900", "bg-green-200 text-green-900",
    "bg-fuchsia-300 text-fuchsia-900","bg-amber-400 text-amber-900",
    "bg-sky-100 text-sky-900",       "bg-red-200 text-red-900",
    "bg-lime-300 text-lime-900",     "bg-violet-400 text-violet-900",
    "bg-yellow-100 text-yellow-900", "bg-teal-200 text-teal-900",
    "bg-pink-300 text-pink-900",     "bg-rose-400 text-rose-900",
    "bg-emerald-100 text-emerald-900","bg-cyan-200 text-cyan-900",
    "bg-indigo-300 text-indigo-900", "bg-purple-400 text-purple-900",
]

_RESERVED_CHIP_HUES = [
    ("violet", "violet"),
    ("alice",  "fuchsia"),
    ("beccy",  "teal"),
    ("rob",    "indigo"),
]

_RESERVED_SHADES = [100, 200, 300, 400]

def _chip_color(key: str) -> str:
    kl = key.lower()
    for word, hue in _RESERVED_CHIP_HUES:
        if re.search(r'\b' + word + r'\b', kl):
            h = 0
            for c in key:
                h = (h * 31 + ord(c)) & 0x7FFFFFFF
            shade = _RESERVED_SHADES[h % len(_RESERVED_SHADES)]
            return f"bg-{hue}-{shade} text-{hue}-900"
    h = 0
    for c in key:
        h = (h * 31 + ord(c)) & 0x7FFFFFFF
    return _CHIP_PALETTE[h % len(_CHIP_PALETTE)]

templates.env.globals["chip_color"] = _chip_color


def _is_default_name(doc, subgroup=None) -> bool:
    """True when the document label carries no user-added information."""
    name = (doc.assigned_item_one_off_description or doc.desired_filename or doc.file_name_when_imported or "").strip()
    if not name:
        return True
    if "scanned papers" in name.lower():
        return True
    if subgroup:
        tag = (subgroup.filename_tag or subgroup.name or "").strip()
        if tag and name.lower() == tag.lower():
            return True
    return False

templates.env.globals["is_default_name"] = _is_default_name

def _localtime(dt: datetime) -> datetime:
    """Convert a naive UTC datetime (as stored by SQLAlchemy) to local wall-clock time."""
    if dt is None:
        return dt
    return dt.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)

templates.env.globals["localtime"] = _localtime


# ---------------------------------------------------------------------------
# Drive OAuth
# ---------------------------------------------------------------------------

@app.get("/drive/auth")
async def drive_auth():
    flow = get_flow()
    auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")
    return RedirectResponse(auth_url)


@app.get("/drive/callback")
async def drive_callback(code: str):
    flow = get_flow()
    save_credentials_from_flow(flow, code)
    return RedirectResponse("/")


# ---------------------------------------------------------------------------
# Toodledo OAuth
# ---------------------------------------------------------------------------

@app.get("/toodledo/auth")
async def toodledo_auth():
    return RedirectResponse(toodledo.get_authorize_url(state="scanny"))


@app.get("/toodledo/callback")
async def toodledo_callback(code: str):
    await asyncio.to_thread(toodledo.exchange_code, code)
    return RedirectResponse("/status")


@app.post("/toodledo/test-task", response_class=HTMLResponse)
async def toodledo_test_task():
    import json as _json
    try:
        result = await asyncio.to_thread(
            toodledo.create_task,
            "Scanny test task",
            f"Test task from Scanny.\n{settings.app_base_url}/",
        )
        return _json.dumps(result, indent=2)
    except Exception as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Main tagging UI
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Group).order_by(Group.name))
    quick_groups = result.scalars().all()

    # Pairs: recent + popular first, then remaining alphabetically
    result_priority = await db.execute(
        select(Subgroup)
        .options(joinedload(Subgroup.group))
        .where(Subgroup.name != "/")
        .where((Subgroup.last_used.isnot(None)) | (Subgroup.use_count > 0))
        .order_by(desc(Subgroup.last_used), desc(Subgroup.use_count))
    )
    result_rest = await db.execute(
        select(Subgroup)
        .options(joinedload(Subgroup.group))
        .where(Subgroup.name != "/")
        .where(Subgroup.last_used.is_(None))
        .where(Subgroup.use_count == 0)
        .order_by(Subgroup.name)
    )
    seen_ids = set()
    quick_pairs = []
    for sub in list(result_priority.scalars()) + list(result_rest.scalars()):
        if sub.id not in seen_ids:
            seen_ids.add(sub.id)
            quick_pairs.append(sub)

    # Most recent pending scan + count (for the queue row)
    last_result = await db.execute(
        select(PendingScan)
        .options(joinedload(PendingScan.subgroup).joinedload(Subgroup.group))
        .where(PendingScan.status == "pending")
        .order_by(desc(PendingScan.created_at))
        .limit(1)
    )
    count_result = await db.execute(
        select(func.count(PendingScan.id))
        .where(PendingScan.status == "pending")
    )
    pending_scans = last_result.scalars().all()
    pending_count = count_result.scalar() or 0

    return templates.TemplateResponse("scan.html", {
        "request": request,
        "quick_groups": quick_groups,
        "quick_pairs": quick_pairs,
        "pending_scans": pending_scans,
        "pending_count": pending_count,
        "drive_authed": get_credentials() is not None,
    })


@app.get("/search", response_class=HTMLResponse)
async def search_page(request: Request):
    return templates.TemplateResponse("search.html", {"request": request})


@app.get("/search/results", response_class=HTMLResponse)
async def search_results(request: Request, q: str = "", db: AsyncSession = Depends(get_db)):
    if not q.strip():
        return HTMLResponse('<p class="text-center text-gray-300 text-sm py-8">Start typing to search</p>')
    from sqlalchemy import or_
    ql = f"%{q.lower()}%"
    result = await db.execute(
        select(Document)
        .options(joinedload(Document.group), joinedload(Document.subgroup))
        .where(or_(
            Document.file_name_when_imported.ilike(ql),
            Document.desired_filename.ilike(ql),
            Document.assigned_item_one_off_description.ilike(ql),
            Document.assigned_task_note.ilike(ql),
            Document.assigned_comment.ilike(ql),
        ))
        .order_by(Document.timestamp_from_filename.desc())
        .limit(30)
    )
    docs = result.scalars().all()
    if not docs:
        return HTMLResponse('<p class="text-center text-gray-400 text-sm py-8">No results</p>')
    return templates.TemplateResponse("_search_results.html", {"request": request, "docs": docs})


async def _subgroup_popup_ctx(group_id: int, db) -> dict:
    """Shared context builder for the subgroup popup."""
    group = await db.get(Group, group_id)
    recent = await db.execute(
        select(Subgroup)
        .where(Subgroup.group_id == group_id, Subgroup.name != "/")
        .order_by(desc(Subgroup.last_used), Subgroup.name)
    )
    all_subs = await db.execute(
        select(Subgroup)
        .where(Subgroup.group_id == group_id, Subgroup.name != "/")
        .order_by(Subgroup.name)
    )
    slash = await db.execute(
        select(Subgroup).where(Subgroup.group_id == group_id, Subgroup.name == "/").limit(1)
    )
    return {
        "group": group,
        "subgroups": recent.scalars().all(),
        "all_subgroups": all_subs.scalars().all(),
        "slash_sub": slash.scalar_one_or_none(),
    }


@app.get("/groups/{group_id}/recent-subgroups", response_class=HTMLResponse)
async def recent_subgroups(group_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    if not await db.get(Group, group_id):
        raise HTTPException(404)
    ctx = await _subgroup_popup_ctx(group_id, db)
    return templates.TemplateResponse("_subgroup_popup.html", {"request": request, **ctx})


@app.post("/groups/{group_id}/subgroups-popup", response_class=HTMLResponse)
async def create_subgroup_popup(
    group_id: int, request: Request,
    name: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    db.add(Subgroup(group_id=group_id, name=name.strip()))
    await db.commit()
    ctx = await _subgroup_popup_ctx(group_id, db)
    return templates.TemplateResponse("_subgroup_popup.html", {"request": request, **ctx})


@app.get("/pending", response_class=HTMLResponse)
async def list_pending(request: Request, db: AsyncSession = Depends(get_db)):
    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(days=3)
    result = await db.execute(
        select(PendingScan)
        .options(
            joinedload(PendingScan.subgroup).joinedload(Subgroup.group),
            joinedload(PendingScan.document),
        )
        .where(or_(
            PendingScan.status == "pending",
            and_(PendingScan.status == "filed", PendingScan.modified_at >= cutoff),
        ))
        .order_by(desc(PendingScan.created_at))
        .limit(30)
    )
    scans = result.scalars().all()
    return templates.TemplateResponse("_pending_list.html", {
        "request": request,
        "scans": scans,
    })


@app.get("/pending/{pending_id}/edit", response_class=HTMLResponse)
async def edit_pending_form(pending_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(PendingScan)
        .options(joinedload(PendingScan.subgroup).joinedload(Subgroup.group))
        .where(PendingScan.id == pending_id)
    )
    scan = result.scalar_one_or_none()
    if not scan:
        raise HTTPException(404)
    return templates.TemplateResponse("_pending_edit.html", {"request": request, "scan": scan})


@app.post("/pending/{pending_id}", response_class=HTMLResponse)
async def update_pending(
    pending_id: int,
    request: Request,
    subgroup_id: int = Form(...),
    item_one_off_description: Optional[str] = Form(None),
    rob_task: Optional[str] = Form(None),
    beccy_task: Optional[str] = Form(None),
    task_note: Optional[str] = Form(None),
    comment: Optional[str] = Form(None),
    date_override: Optional[str] = Form(None),
    override_filename: Optional[str] = Form(None),
    photo_quality: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    p = await db.get(PendingScan, pending_id)
    if not p:
        raise HTTPException(404)

    def _clean(s: Optional[str]) -> Optional[str]:
        return re.sub(r'[\x00-\x1f\x7f]', '', s).strip() or None if s else None

    p.subgroup_id = subgroup_id
    p.item_one_off_description = _clean(item_one_off_description)
    p.rob_task = bool(rob_task)
    p.beccy_task = bool(beccy_task)
    p.task_note = _clean(task_note)
    p.comment = _clean(comment)
    p.override_filename = _clean(override_filename)
    p.photo_quality = bool(photo_quality)
    if date_override:
        try:
            from datetime import date as _date
            p.date_override = _date.fromisoformat(date_override)
        except ValueError:
            p.date_override = None
    else:
        p.date_override = None

    await db.commit()

    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(days=3)
    result = await db.execute(
        select(PendingScan)
        .options(
            joinedload(PendingScan.subgroup).joinedload(Subgroup.group),
            joinedload(PendingScan.document),
        )
        .where(or_(
            PendingScan.status == "pending",
            and_(PendingScan.status == "filed", PendingScan.modified_at >= cutoff),
        ))
        .order_by(desc(PendingScan.created_at))
        .limit(30)
    )
    scans = result.scalars().all()
    return templates.TemplateResponse("_pending_list.html", {"request": request, "scans": scans})


def _token_match(sub: "Subgroup", q: str) -> bool:
    """Match each space-separated part of q against word starts in 'Group Subgroup'.
    Multi-char parts that don't match a word prefix are expanded as initials,
    so 'ks' matches 'Kids Scanned' the same as 'k s' does."""
    haystack = f"{sub.group.name} {sub.name}".lower()
    words = haystack.split()
    tokens = []
    for part in q.lower().split():
        if len(part) > 1 and part.isalpha() and not any(w.startswith(part) for w in words):
            tokens.extend(list(part))
        else:
            tokens.append(part)
    return all(any(w.startswith(t) for w in words) for t in tokens)


@app.get("/subgroups/search", response_class=HTMLResponse)
async def search_subgroups(
    request: Request,
    q: str = "",
    group_id: Optional[int] = None,
    all: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Subgroup).options(joinedload(Subgroup.group)).where(Subgroup.name != "/")
    if group_id:
        stmt = stmt.where(Subgroup.group_id == group_id)
    stmt = stmt.order_by(desc(Subgroup.last_used), Subgroup.name)
    result = await db.execute(stmt)
    all_subs = result.scalars().all()

    if q:
        subgroups = [s for s in all_subs if _token_match(s, q)][:12]
    elif all:
        subgroups = all_subs
    else:
        subgroups = all_subs[:12]

    # If called from the popup (group_id set), return popup-style list
    if group_id:
        group = await db.get(Group, group_id)
        return templates.TemplateResponse("_subgroup_popup.html", {
            "request": request,
            "group": group,
            "subgroups": subgroups,
        })

    # Also return matching group chips for the main search sheet
    matching_groups: list[Group] = []
    if q:
        ql = q.lower()
        groups_result = await db.execute(select(Group).order_by(Group.name))
        matching_groups = [g for g in groups_result.scalars().all() if ql in g.name.lower()][:6]

    return templates.TemplateResponse("_subgroup_list.html", {
        "request": request,
        "subgroups": subgroups,
        "groups": matching_groups,
    })


# ---------------------------------------------------------------------------
# Group / Subgroup management
# ---------------------------------------------------------------------------

@app.post("/groups")
async def create_group(
    name: str = Form(...),
    include_in_path: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    group = Group(name=name.strip(), include_in_path=bool(include_in_path))
    db.add(group)
    await db.commit()
    return RedirectResponse("/settings", status_code=303)


@app.post("/subgroups")
async def create_subgroup(
    group_id: int = Form(...),
    name: str = Form(...),
    filename_tag: Optional[str] = Form(None),
    custom_path: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    sub = Subgroup(
        group_id=group_id,
        name=name.strip(),
        filename_tag=filename_tag.strip() or None if filename_tag else None,
        custom_path=custom_path.strip() or None if custom_path else None,
    )
    db.add(sub)
    await db.commit()
    return RedirectResponse("/settings", status_code=303)


# ---------------------------------------------------------------------------
# Pending scans
# ---------------------------------------------------------------------------

@app.delete("/pending/{pending_id}", response_class=HTMLResponse)
async def delete_pending(pending_id: int, db: AsyncSession = Depends(get_db)):
    p = await db.get(PendingScan, pending_id)
    if not p:
        raise HTTPException(404)
    await db.delete(p)
    await db.commit()
    return HTMLResponse("")


async def _finalize_pending_scan_task(pending_id: int):
    """Finalize a scan session in the background, after the HTTP response has been sent."""
    from services.filer import finalize_pending_scan
    try:
        async with AsyncSessionLocal() as db:
            await finalize_pending_scan(db, pending_id)
    except Exception as e:
        print(f"[finalize scan session error] {pending_id}: {e}")


@app.post("/pending")
async def create_pending(
    background_tasks: BackgroundTasks,
    subgroup_id: int = Form(...),
    item_one_off_description: Optional[str] = Form(None),
    rob_task: Optional[str] = Form(None),
    beccy_task: Optional[str] = Form(None),
    task_note: Optional[str] = Form(None),
    comment: Optional[str] = Form(None),
    date_override: Optional[str] = Form(None),
    override_filename: Optional[str] = Form(None),
    photo_quality: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    parsed_date = None
    if date_override:
        try:
            from datetime import date
            parsed_date = date.fromisoformat(date_override)
        except ValueError:
            pass

    def _clean(s: Optional[str]) -> Optional[str]:
        import re as _re
        return _re.sub(r'[\x00-\x1f\x7f]', '', s).strip() or None if s else None

    pending = PendingScan(
        subgroup_id=subgroup_id,
        item_one_off_description=_clean(item_one_off_description),
        rob_task=bool(rob_task),
        beccy_task=bool(beccy_task),
        task_note=_clean(task_note),
        comment=_clean(comment),
        date_override=parsed_date,
        override_filename=_clean(override_filename),
        photo_quality=bool(photo_quality),
    )
    db.add(pending)

    sub = await db.get(Subgroup, subgroup_id)
    if sub:
        sub.use_count = (sub.use_count or 0) + 1
        sub.last_used = datetime.utcnow()

    await db.commit()

    # Starting a new scan session always closes out any other still-open one, whether or
    # not it ever received a file.
    other_open = await db.execute(
        select(PendingScan.id).where(PendingScan.status == "pending", PendingScan.id != pending.id)
    )
    for other_id in other_open.scalars().all():
        background_tasks.add_task(_finalize_pending_scan_task, other_id)

    return RedirectResponse(f"/pending/{pending.id}/scanning", status_code=303)


async def _load_pending_scan(pending_id: int, db: AsyncSession) -> Optional[PendingScan]:
    result = await db.execute(
        select(PendingScan)
        .options(
            joinedload(PendingScan.subgroup).joinedload(Subgroup.group),
            joinedload(PendingScan.document),
            selectinload(PendingScan.files),
        )
        .where(PendingScan.id == pending_id)
    )
    return result.unique().scalar_one_or_none()


@app.get("/pending/{pending_id}/scanning", response_class=HTMLResponse)
async def pending_scanning(pending_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    scan = await _load_pending_scan(pending_id, db)
    if not scan:
        raise HTTPException(404)
    return templates.TemplateResponse("pending_scanning.html", {"request": request, "scan": scan})


@app.get("/pending/{pending_id}/scanning-status", response_class=HTMLResponse)
async def pending_scanning_status(pending_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    # No 404 here even if the row is gone (e.g. cancelled from the Queue sheet in another
    # tab) — the fragment renders a terminal "no longer available" state instead, so a
    # client that's mid-poll always reaches a stable end state rather than an error.
    scan = await _load_pending_scan(pending_id, db)
    return templates.TemplateResponse("_pending_scanning.html", {"request": request, "scan": scan})


@app.post("/pending/{pending_id}/finish", response_class=HTMLResponse)
async def finish_pending_scan(pending_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    from services.filer import finalize_pending_scan
    scan = await _load_pending_scan(pending_id, db)
    if not scan:
        raise HTTPException(404)
    if scan.status == "pending":
        await finalize_pending_scan(db, pending_id)
        scan = await _load_pending_scan(pending_id, db)
    return templates.TemplateResponse("_pending_scanning.html", {"request": request, "scan": scan})


# ---------------------------------------------------------------------------
# Group / Subgroup editing (HTMX partials)
# ---------------------------------------------------------------------------

@app.delete("/groups/{group_id}", response_class=HTMLResponse)
async def delete_group(group_id: int, db: AsyncSession = Depends(get_db)):
    g = await db.get(Group, group_id)
    if not g:
        raise HTTPException(404)
    real_subs = await db.execute(
        select(Subgroup).where(Subgroup.group_id == group_id, Subgroup.name != "/")
    )
    if real_subs.scalars().first():
        raise HTTPException(409, "Remove all subgroups first")
    # Delete the '/' placeholder subgroups too
    placeholder = await db.execute(select(Subgroup).where(Subgroup.group_id == group_id))
    for s in placeholder.scalars().all():
        await db.delete(s)
    await db.delete(g)
    await db.commit()
    return HTMLResponse("")


@app.delete("/subgroups/{sub_id}", response_class=HTMLResponse)
async def delete_subgroup(sub_id: int, db: AsyncSession = Depends(get_db)):
    s = await db.get(Subgroup, sub_id)
    if not s:
        raise HTTPException(404)
    docs = await db.execute(select(Document).where(Document.subgroup_id == sub_id).limit(1))
    if docs.scalar_one_or_none():
        raise HTTPException(409, "Subgroup has documents")
    await db.delete(s)
    await db.commit()
    return HTMLResponse("")


@app.get("/groups/{group_id}/edit", response_class=HTMLResponse)
async def edit_group_form(group_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    g = await db.get(Group, group_id)
    if not g:
        raise HTTPException(404)
    return templates.TemplateResponse("_group_edit.html", {"request": request, "group": g})


@app.post("/groups/{group_id}")
async def update_group(
    group_id: int,
    request: Request,
    name: str = Form(...),
    include_in_path: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    g = await db.get(Group, group_id)
    if not g:
        raise HTTPException(404)
    g.name = name.strip()
    g.include_in_path = bool(include_in_path)
    await db.commit()
    await db.refresh(g)
    result = await db.execute(select(Group).options(joinedload(Group.subgroups)).where(Group.id == group_id))
    g = result.scalar_one()
    return templates.TemplateResponse("_group_row.html", {"request": request, "group": g})


@app.get("/subgroups/{sub_id}/edit", response_class=HTMLResponse)
async def edit_subgroup_form(sub_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    s = await db.get(Subgroup, sub_id)
    if not s:
        raise HTTPException(404)
    return templates.TemplateResponse("_subgroup_edit.html", {"request": request, "sub": s})


@app.post("/subgroups/{sub_id}")
async def update_subgroup(
    sub_id: int,
    request: Request,
    name: str = Form(...),
    filename_tag: Optional[str] = Form(None),
    custom_path: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    s = await db.get(Subgroup, sub_id)
    if not s:
        raise HTTPException(404)
    s.name = name.strip()
    s.filename_tag = filename_tag.strip() or None if filename_tag else None
    s.custom_path = custom_path.strip() or None if custom_path else None
    await db.commit()
    return templates.TemplateResponse("_subgroup_row.html", {"request": request, "sub": s})


# ---------------------------------------------------------------------------
# Library / Tasks / Settings
# ---------------------------------------------------------------------------

@app.post("/library/groups")
async def library_create_group(name: str = Form(...), db: AsyncSession = Depends(get_db)):
    group = Group(name=name.strip(), include_in_path=False)
    db.add(group)
    await db.commit()
    return RedirectResponse("/library", status_code=303)


@app.post("/library/{group_id}/subgroups")
async def library_create_subgroup(
    group_id: int,
    name: str = Form(...),
    filename_tag: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    sub = Subgroup(
        group_id=group_id,
        name=name.strip(),
        filename_tag=filename_tag.strip() or None if filename_tag else None,
    )
    db.add(sub)
    await db.commit()
    return RedirectResponse(f"/library/{group_id}", status_code=303)


@app.post("/library/{group_id}/update")
async def library_update_group(
    group_id: int,
    name: str = Form(...),
    include_in_path: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    g = await db.get(Group, group_id)
    if not g:
        raise HTTPException(404)
    g.name = name.strip()
    g.include_in_path = bool(include_in_path)
    await db.commit()
    return RedirectResponse(f"/library/{group_id}", status_code=303)


@app.post("/library/{group_id}/{sub_id}/update")
async def library_update_subgroup(
    group_id: int,
    sub_id: int,
    name: str = Form(...),
    filename_tag: Optional[str] = Form(None),
    custom_path: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    s = await db.get(Subgroup, sub_id)
    if not s:
        raise HTTPException(404)
    s.name = name.strip()
    s.filename_tag = filename_tag.strip() or None if filename_tag else None
    s.custom_path = custom_path.strip() or None if custom_path else None
    await db.commit()
    return RedirectResponse(f"/library/{group_id}/{sub_id}", status_code=303)


@app.get("/library/{group_id}/edit", response_class=HTMLResponse)
async def edit_group_page(group_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    g = await db.get(Group, group_id)
    if not g:
        raise HTTPException(404)
    from sqlalchemy import func
    doc_count = (await db.execute(
        select(func.count(Document.id))
        .join(Subgroup, Subgroup.id == Document.subgroup_id)
        .where(Subgroup.group_id == group_id)
    )).scalar_one()
    sub_count = (await db.execute(
        select(func.count(Subgroup.id)).where(Subgroup.group_id == group_id, Subgroup.name != "/")
    )).scalar_one()
    return templates.TemplateResponse("edit_group.html", {
        "request": request, "group": g,
        "doc_count": doc_count, "sub_count": sub_count,
    })


@app.post("/library/{group_id}/delete")
async def library_delete_group(group_id: int, db: AsyncSession = Depends(get_db)):
    g = await db.get(Group, group_id)
    if not g:
        raise HTTPException(404)
    real_subs = (await db.execute(
        select(Subgroup).where(Subgroup.group_id == group_id, Subgroup.name != "/")
    )).scalars().first()
    if real_subs:
        raise HTTPException(409, "Remove all subgroups before deleting a group")
    placeholders = (await db.execute(select(Subgroup).where(Subgroup.group_id == group_id))).scalars().all()
    for s in placeholders:
        await db.delete(s)
    await db.delete(g)
    await db.commit()
    return RedirectResponse("/library", status_code=303)


@app.get("/library/{group_id}/{subgroup_id}/edit", response_class=HTMLResponse)
async def edit_subgroup_page(group_id: int, subgroup_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    s = await db.get(Subgroup, subgroup_id)
    g = await db.get(Group, group_id)
    if not s or not g:
        raise HTTPException(404)
    from sqlalchemy import func
    doc_count = (await db.execute(
        select(func.count(Document.id)).where(Document.subgroup_id == subgroup_id)
    )).scalar_one()
    return templates.TemplateResponse("edit_subgroup.html", {
        "request": request, "subgroup": s, "group": g, "doc_count": doc_count,
    })


@app.post("/library/{group_id}/{subgroup_id}/delete")
async def library_delete_subgroup(group_id: int, subgroup_id: int, db: AsyncSession = Depends(get_db)):
    s = await db.get(Subgroup, subgroup_id)
    if not s:
        raise HTTPException(404)
    # Move any documents to the group's root ('/') subgroup before deleting
    root_sub = (await db.execute(
        select(Subgroup).where(Subgroup.group_id == group_id, Subgroup.name == "/")
    )).scalar_one_or_none()
    if not root_sub:
        root_sub = Subgroup(group_id=group_id, name="/")
        db.add(root_sub)
        await db.flush()
    await db.execute(
        Document.__table__.update()
        .where(Document.subgroup_id == subgroup_id)
        .values(subgroup_id=root_sub.id)
    )
    await db.delete(s)
    await db.commit()
    return RedirectResponse(f"/library/{group_id}", status_code=303)


@app.get("/library", response_class=HTMLResponse)
async def library(request: Request, db: AsyncSession = Depends(get_db)):
    from sqlalchemy import func
    result = await db.execute(
        select(Group, func.count(Document.id).label("doc_count"))
        .outerjoin(Subgroup, Subgroup.group_id == Group.id)
        .outerjoin(Document, Document.subgroup_id == Subgroup.id)
        .group_by(Group.id)
        .order_by(Group.name)
    )
    groups = result.all()
    broken_count_result = await db.execute(
        select(func.count(Document.id)).where(Document.broken_link == True)
    )
    broken_count = broken_count_result.scalar() or 0
    recent_result = await db.execute(
        select(Subgroup)
        .options(joinedload(Subgroup.group))
        .where((Subgroup.last_used.isnot(None)) | (Subgroup.use_count > 0))
        .order_by(desc(Subgroup.last_used), desc(Subgroup.use_count))
        .limit(30)
    )
    return templates.TemplateResponse("library.html", {
        "request": request,
        "active": "library",
        "groups": groups,
        "broken_count": broken_count,
        "recent_subgroups": recent_result.scalars().all(),
    })


@app.get("/library/{group_id}", response_class=HTMLResponse)
async def library_group(group_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    from sqlalchemy import func, case as sa_case
    group = await db.get(Group, group_id)
    if not group:
        raise HTTPException(404)
    result = await db.execute(
        select(
            Subgroup,
            func.count(Document.id).label("doc_count"),
            func.sum(sa_case((Document.broken_link == True, 1), else_=0)).label("broken_count"),
        )
        .outerjoin(Document, Document.subgroup_id == Subgroup.id)
        .where(Subgroup.group_id == group_id)
        .where(Subgroup.name != "/")
        .group_by(Subgroup.id)
        .order_by(Subgroup.name)
    )
    subgroups = result.all()
    # Fetch the '/' subgroup (docs filed directly under the group) if it has documents
    slash_result = await db.execute(
        select(Subgroup, func.count(Document.id).label("doc_count"))
        .outerjoin(Document, Document.subgroup_id == Subgroup.id)
        .where(Subgroup.group_id == group_id)
        .where(Subgroup.name == "/")
        .group_by(Subgroup.id)
    )
    slash_row = slash_result.first()
    slash_sub = slash_row if slash_row and slash_row[1] > 0 else None

    # Skip the group page when there are no named subgroups — go straight to the document grid
    if not subgroups and slash_row:
        slash_sub_obj, _ = slash_row
        return RedirectResponse(f"/library/{group_id}/{slash_sub_obj.id}", status_code=302)

    return templates.TemplateResponse("library_group.html", {
        "request": request,
        "active": "library",
        "group": group,
        "subgroups": subgroups,
        "slash_sub": slash_sub,
    })


@app.get("/library/{group_id}/{subgroup_id}", response_class=HTMLResponse)
async def library_subgroup(group_id: int, subgroup_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    group = await db.get(Group, group_id)
    subgroup = await db.get(Subgroup, subgroup_id)
    if not group or not subgroup:
        raise HTTPException(404)
    result = await db.execute(
        select(Document)
        .where(Document.subgroup_id == subgroup_id)
        .order_by(desc(Document.timestamp_from_filename))
    )
    documents = result.scalars().all()
    return templates.TemplateResponse("library_subgroup.html", {
        "request": request,
        "active": "library",
        "group": group,
        "subgroup": subgroup,
        "documents": documents,
    })


@app.get("/thumbnail/{document_id}")
async def thumbnail(document_id: int, db: AsyncSession = Depends(get_db)):
    doc = await db.get(Document, document_id)
    if not doc or not doc.drive_file_id:
        raise HTTPException(404)
    cache_path = THUMB_CACHE / f"{doc.drive_file_id}.jpg"
    if cache_path.exists() and (time.time() - cache_path.stat().st_mtime) < THUMB_TTL:
        return FileResponse(cache_path, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})
    data = await _fetch_and_cache_thumbnail(doc.drive_file_id)
    if not data:
        raise HTTPException(404)
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})


@app.get("/document/{document_id}/file")
async def document_file(document_id: int, db: AsyncSession = Depends(get_db)):
    """Proxy the PDF from Drive for in-browser viewing."""
    import httpx, traceback
    doc = await db.get(Document, document_id)
    if not doc or not doc.drive_file_id:
        raise HTTPException(404)
    try:
        creds = get_credentials()
        url = f"https://www.googleapis.com/drive/v3/files/{doc.drive_file_id}?alt=media"
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                url,
                headers={"Authorization": f"Bearer {creds.token}"},
                follow_redirects=True,
            )
        if resp.status_code == 404:
            raise HTTPException(404)
        content_type = resp.headers.get("content-type", "application/pdf")
        return StreamingResponse(
            iter([resp.content]),
            media_type=content_type,
            headers={"Cache-Control": "no-store", "Content-Disposition": "inline"},
        )
    except HTTPException:
        raise
    except Exception as e:
        print(f"Document file error doc={document_id}: {e}\n{traceback.format_exc()}")
        raise HTTPException(502, str(e))


@app.get("/document/{document_id}", response_class=HTMLResponse)
async def document_viewer(document_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Document)
        .options(joinedload(Document.group), joinedload(Document.subgroup))
        .where(Document.id == document_id)
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(404)
    groups_result = await db.execute(select(Group).order_by(Group.name))
    all_groups = groups_result.scalars().all()
    pairs_result = await db.execute(
        select(Subgroup)
        .options(joinedload(Subgroup.group))
        .where((Subgroup.last_used.isnot(None)) | (Subgroup.use_count > 0))
        .order_by(desc(Subgroup.last_used), desc(Subgroup.use_count))
        .limit(4)
    )
    quick_pairs = pairs_result.scalars().all()
    from services.filer import extract_drive_prefix as _pfx
    filename_prefix = _pfx(doc.file_name_when_imported or "") if not doc.assigned_item_one_off_description else ""
    scan_date_str = doc.timestamp_from_filename.strftime("%Y-%m-%d") if doc.timestamp_from_filename else ""
    parent_folder_id = None
    if doc.drive_file_id:
        try:
            service = get_drive_service()
            meta = await asyncio.to_thread(
                lambda: service.files().get(fileId=doc.drive_file_id, fields="parents").execute()
            )
            parent_folder_id = (meta.get("parents") or [None])[0]
        except Exception:
            parent_folder_id = None
    return templates.TemplateResponse("document_viewer.html", {
        "request": request,
        "doc": doc,
        "parent_folder_id": parent_folder_id,
        "all_groups": all_groups,
        "quick_pairs": quick_pairs,
        "filename_prefix": filename_prefix,
        "scan_date_str": scan_date_str,
    })


@app.get("/document/{document_id}/move-groups", response_class=HTMLResponse)
async def document_move_groups(document_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    groups_result = await db.execute(select(Group).order_by(Group.name))
    all_groups = groups_result.scalars().all()
    pairs_result = await db.execute(
        select(Subgroup)
        .options(joinedload(Subgroup.group))
        .where((Subgroup.last_used.isnot(None)) | (Subgroup.use_count > 0))
        .order_by(desc(Subgroup.last_used), desc(Subgroup.use_count))
        .limit(4)
    )
    quick_pairs = pairs_result.scalars().all()
    return templates.TemplateResponse("_move_groups.html", {
        "request": request,
        "document_id": document_id,
        "all_groups": all_groups,
        "quick_pairs": quick_pairs,
    })


@app.get("/document/{document_id}/move-subgroups", response_class=HTMLResponse)
async def document_move_subgroups(
    document_id: int, group_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    group = await db.get(Group, group_id)
    if not group:
        raise HTTPException(404)
    subs_result = await db.execute(
        select(Subgroup)
        .where(Subgroup.group_id == group_id, Subgroup.name != "/")
        .order_by(Subgroup.name)
    )
    subgroups = subs_result.scalars().all()
    slash_result = await db.execute(
        select(Subgroup).where(Subgroup.group_id == group_id, Subgroup.name == "/").limit(1)
    )
    slash_sub = slash_result.scalar_one_or_none()
    return templates.TemplateResponse("_move_subgroup_list.html", {
        "request": request,
        "document_id": document_id,
        "group": group,
        "subgroups": subgroups,
        "slash_sub": slash_sub,
    })


@app.post("/document/{document_id}/update")
async def update_document(
    document_id: int,
    item_one_off_description: str = Form(""),
    comment: str = Form(""),
    rob_task: Optional[str] = Form(None),
    beccy_task: Optional[str] = Form(None),
    task_note: str = Form(""),
    photo_quality: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Document)
        .options(joinedload(Document.group), joinedload(Document.subgroup))
        .where(Document.id == document_id)
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(404)
    prev_rob = doc.assigned_rob_task
    prev_beccy = doc.assigned_beccy_task
    prev_description = doc.assigned_item_one_off_description
    new_description = item_one_off_description.strip() or None
    doc.assigned_item_one_off_description = new_description
    doc.assigned_comment = comment.strip() or None
    doc.assigned_rob_task = rob_task == "1"
    doc.assigned_beccy_task = beccy_task == "1"
    doc.assigned_task_note = task_note.strip() or None
    doc.photo_quality = bool(photo_quality)

    # Renaming the description renames the actual file in Drive to match,
    # same convention as recategorizing — falls back to the default
    # "{date} {tag} scanned papers-{time}.ext" name when cleared.
    if new_description != prev_description and doc.drive_file_id and doc.group and doc.subgroup:
        from services.filer import build_filing_info
        current_name = doc.desired_filename or doc.file_name_when_imported or ""
        ext = current_name.rsplit(".", 1)[-1].lower() if "." in current_name else "pdf"
        scan_dt = doc.timestamp_from_filename or datetime.utcnow()
        folder_path, new_filename = build_filing_info(
            group_name=doc.group.name,
            subgroup_name=doc.subgroup.name,
            filename_tag=doc.subgroup.filename_tag,
            custom_path=doc.subgroup.custom_path,
            include_group_in_path=doc.group.include_in_path,
            scan_datetime=scan_dt,
            override_filename=new_description,
            ext=ext,
        )
        service = get_drive_service()
        await asyncio.to_thread(
            lambda: service.files().update(
                fileId=doc.drive_file_id, body={"name": new_filename}, fields="id,name"
            ).execute()
        )
        doc.desired_folder_location = folder_path
        doc.desired_filename = new_filename
        doc.desired_full_path = f"{folder_path}/{new_filename}"
        doc.current_full_path = f"{folder_path}/{new_filename}"

    await db.commit()
    # Queue 2Do tasks for newly-checked boxes only
    title = doc.assigned_task_note or doc.assigned_item_one_off_description or doc.desired_filename or doc.file_name_when_imported or "Scanny document"
    g = doc.group.name if doc.group else ""
    s = doc.subgroup.name if doc.subgroup and doc.subgroup.name != "/" else ""
    category = f"{g} / {s}" if s else g
    scan_date = doc.timestamp_from_filename.strftime("%d %B %Y") if doc.timestamp_from_filename else ""
    if doc.assigned_rob_task and not prev_rob:
        await _queue_2do_task(doc.id, title, category, "rob", scan_date)
    if doc.assigned_beccy_task and not prev_beccy:
        await _queue_2do_task(doc.id, title, category, "beccy", scan_date)
    return RedirectResponse(f"/document/{document_id}", status_code=303)


@app.post("/document/{document_id}/recategorize")
async def recategorize_document(
    document_id: int,
    subgroup_id: int = Form(...),
    db: AsyncSession = Depends(get_db),
):
    import asyncio as _asyncio
    from services.filer import build_filing_info
    from services.drive import get_or_create_folder, get_drive_service
    from config import settings as _settings

    result = await db.execute(
        select(Document)
        .options(joinedload(Document.subgroup), joinedload(Document.group))
        .where(Document.id == document_id)
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(404)

    new_sub = await db.get(Subgroup, subgroup_id)
    if not new_sub:
        raise HTTPException(404)
    new_group = await db.get(Group, new_sub.group_id)

    current_name = doc.desired_filename or doc.file_name_when_imported or ""
    ext = current_name.rsplit(".", 1)[-1].lower() if "." in current_name else "pdf"
    scan_dt = doc.timestamp_from_filename or datetime.utcnow()

    # Default filename → rebuild with new tag; custom → preserve stem, change folder
    is_default = "scanned papers" in current_name.lower()
    override_filename = None if is_default else (current_name.rsplit(".", 1)[0] if "." in current_name else current_name)

    folder_path, new_filename = build_filing_info(
        group_name=new_group.name,
        subgroup_name=new_sub.name,
        filename_tag=new_sub.filename_tag,
        custom_path=new_sub.custom_path,
        include_group_in_path=new_group.include_in_path,
        scan_datetime=scan_dt,
        override_filename=override_filename,
        ext=ext,
    )

    # Build (or find) the target folder in Drive
    parent_id = _settings.drive_filed_root_folder_id
    for part in [p for p in folder_path.strip("/").split("/") if p]:
        parent_id = await _asyncio.to_thread(get_or_create_folder, parent_id, part)

    # Move and rename the Drive file if one exists
    if doc.drive_file_id:
        service = get_drive_service()
        meta = await _asyncio.to_thread(
            lambda: service.files().get(fileId=doc.drive_file_id, fields="parents").execute()
        )
        old_parents = ",".join(meta.get("parents", []))
        await _asyncio.to_thread(
            lambda: service.files().update(
                fileId=doc.drive_file_id,
                addParents=parent_id,
                removeParents=old_parents,
                body={"name": new_filename},
                fields="id,name",
            ).execute()
        )

    doc.subgroup_id = new_sub.id
    doc.group_id = new_group.id
    doc.desired_folder_location = folder_path
    doc.desired_filename = new_filename
    doc.desired_full_path = f"{folder_path}/{new_filename}"
    doc.current_full_path = f"{folder_path}/{new_filename}"
    doc.broken_link = False
    await db.commit()

    return RedirectResponse(f"/document/{document_id}", status_code=303)


# ---------------------------------------------------------------------------
# Import from Drive
# ---------------------------------------------------------------------------

@app.get("/import", response_class=HTMLResponse)
async def import_page(request: Request, db: AsyncSession = Depends(get_db)):
    from services.drive import list_files_in_folder
    drive_authed = bool(get_credentials())
    files = []

    # IDs of files already properly filed (group assigned) — exclude from Drive listing
    filed_result = await db.execute(
        select(Document.drive_file_id).where(
            Document.drive_file_id.isnot(None),
            Document.group_id.isnot(None),
        )
    )
    filed_ids = {r for r in filed_result.scalars().all() if r}

    folder_id = settings.drive_import_folder_id or settings.drive_incoming_folder_id
    if drive_authed and folder_id:
        try:
            raw = await asyncio.to_thread(list_files_in_folder, folder_id)
            accepted = {"application/pdf", "image/jpeg", "image/png", "image/tiff", "image/heic"}
            raw = [f for f in raw if f.get("mimeType") in accepted]
            files = [f for f in raw if f["id"] not in filed_ids]
        except Exception as e:
            print(f"Import list error: {e}")

    # Also include untagged scanner files (Documents with no group yet)
    untagged_result = await db.execute(
        select(Document)
        .where(Document.group_id.is_(None))
        .where(Document.drive_file_id.isnot(None))
        .order_by(Document.timestamp_from_filename.desc())
    )
    untagged_ids = set()
    for doc in untagged_result.scalars().all():
        if doc.drive_file_id not in {f["id"] for f in files}:
            files.append({
                "id": doc.drive_file_id,
                "name": doc.file_name_when_imported or "",
                "createdTime": (doc.timestamp_from_filename.isoformat() if doc.timestamp_from_filename else datetime.utcnow().isoformat()),
                "source": "scanner",
            })
        untagged_ids.add(doc.drive_file_id)

    files.sort(key=lambda f: f.get("createdTime") or "", reverse=True)

    return templates.TemplateResponse("import.html", {
        "request": request,
        "drive_authed": drive_authed,
        "files": files,
    })


def _parse_bulk_ids(ids: str) -> list[str]:
    file_ids = [i for i in ids.split(",") if i]
    if not file_ids:
        raise HTTPException(400, "No files selected")
    return file_ids


@app.get("/import/bulk", response_class=HTMLResponse)
async def import_bulk_page(request: Request, ids: str, db: AsyncSession = Depends(get_db)):
    file_ids = _parse_bulk_ids(ids)
    groups_result = await db.execute(select(Group).order_by(Group.name))
    return templates.TemplateResponse("import_bulk.html", {
        "request": request,
        "ids": ids,
        "file_ids": file_ids,
        "groups": groups_result.scalars().all(),
    })


@app.get("/import/bulk/groups", response_class=HTMLResponse)
async def import_bulk_groups(
    request: Request, ids: str,
    group_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
):
    _parse_bulk_ids(ids)
    pairs_result = await db.execute(
        select(Subgroup)
        .options(joinedload(Subgroup.group))
        .where((Subgroup.last_used.isnot(None)) | (Subgroup.use_count > 0))
        .order_by(desc(Subgroup.last_used), desc(Subgroup.use_count))
        .limit(4)
    )
    quick_pairs = pairs_result.scalars().all()
    if group_id:
        group = await db.get(Group, group_id)
        if not group:
            raise HTTPException(404)
        recent = await db.execute(
            select(Subgroup)
            .where(Subgroup.group_id == group_id, Subgroup.name != "/")
            .order_by(desc(Subgroup.last_used), Subgroup.name)
            .limit(5)
        )
        all_subs = await db.execute(
            select(Subgroup).where(Subgroup.group_id == group_id, Subgroup.name != "/").order_by(Subgroup.name)
        )
        slash = await db.execute(
            select(Subgroup).where(Subgroup.group_id == group_id, Subgroup.name == "/").limit(1)
        )
        return templates.TemplateResponse("_import_bulk_subgroups.html", {
            "request": request,
            "ids": ids,
            "group": group,
            "subgroups": recent.scalars().all(),
            "all_subgroups": all_subs.scalars().all(),
            "slash_sub": slash.scalar_one_or_none(),
        })
    groups_result = await db.execute(select(Group).order_by(Group.name))
    return templates.TemplateResponse("_import_bulk_groups.html", {
        "request": request,
        "ids": ids,
        "groups": groups_result.scalars().all(),
        "quick_pairs": quick_pairs,
    })


@app.post("/import/bulk/create-group", response_class=HTMLResponse)
async def import_bulk_create_group(
    request: Request, ids: str,
    name: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    _parse_bulk_ids(ids)
    group = Group(name=name.strip(), include_in_path=True)
    db.add(group)
    await db.flush()
    await db.refresh(group)
    await db.commit()
    return templates.TemplateResponse("_import_bulk_subgroups.html", {
        "request": request,
        "ids": ids,
        "group": group,
        "subgroups": [],
        "all_subgroups": [],
        "slash_sub": None,
    })


@app.post("/import/bulk/create-subgroup", response_class=HTMLResponse)
async def import_bulk_create_subgroup(
    request: Request, ids: str,
    group_id: int = Form(...),
    name: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    file_ids = _parse_bulk_ids(ids)
    new_sub = Subgroup(group_id=group_id, name=name.strip())
    db.add(new_sub)
    await db.flush()
    await db.refresh(new_sub)
    group = await db.get(Group, group_id)
    new_sub.group = group
    await db.commit()
    subgroup_label = new_sub.name if new_sub.name != "/" else group.name
    color_key = (group.name + "/" + new_sub.name) if new_sub.name != "/" else group.name
    return templates.TemplateResponse("_import_bulk_confirm.html", {
        "request": request,
        "ids": ids,
        "file_ids": file_ids,
        "subgroup": new_sub,
        "group": group,
        "subgroup_label": subgroup_label,
        "color_key": color_key,
    })


@app.get("/import/bulk/confirm", response_class=HTMLResponse)
async def import_bulk_confirm(
    request: Request, ids: str,
    subgroup_id: int = 0,
    db: AsyncSession = Depends(get_db),
):
    file_ids = _parse_bulk_ids(ids)
    sub_result = await db.execute(
        select(Subgroup).options(joinedload(Subgroup.group)).where(Subgroup.id == subgroup_id)
    )
    subgroup = sub_result.scalar_one_or_none()
    if not subgroup:
        raise HTTPException(404)
    group = subgroup.group
    subgroup_label = subgroup.name if subgroup.name != "/" else group.name
    color_key = (group.name + "/" + subgroup.name) if subgroup.name != "/" else group.name
    return templates.TemplateResponse("_import_bulk_confirm.html", {
        "request": request,
        "ids": ids,
        "file_ids": file_ids,
        "subgroup": subgroup,
        "group": group,
        "subgroup_label": subgroup_label,
        "color_key": color_key,
    })


async def _do_bulk_filing(
    file_ids: list[str], subgroup_id: int,
    override_filename: Optional[str], item_one_off_description: Optional[str],
    comment: Optional[str], rob_task: bool, beccy_task: bool,
    task_note: Optional[str], photo_quality: bool,
):
    """Sequentially file a batch of Drive files into the same subgroup.

    Sequential (not concurrent) on purpose: dedupe_filename() needs to see each
    prior file's assigned name before picking the next one, or two files sharing
    the same one-off name would race and both land on "Foo.pdf".
    """
    from services.filer import parse_date_from_filename, build_filing_info, dedupe_filename
    from services.drive import get_or_create_folder, move_and_rename_file, list_files_in_folder

    async with AsyncSessionLocal() as db:
        sub_result = await db.execute(
            select(Subgroup).options(joinedload(Subgroup.group)).where(Subgroup.id == subgroup_id)
        )
        subgroup = sub_result.scalar_one_or_none()
    if not subgroup:
        print(f"[bulk filing error] subgroup {subgroup_id} not found")
        return
    group = subgroup.group

    def _resolve_folder():
        folder_path = subgroup.custom_path.rstrip("/") if subgroup.custom_path else f"/{group.name}/{subgroup.name}"
        parent_id = settings.drive_filed_root_folder_id
        for part in [p for p in folder_path.strip("/").split("/") if p]:
            parent_id = get_or_create_folder(parent_id, part)
        existing_names = {f["name"] for f in list_files_in_folder(parent_id)}
        return folder_path, parent_id, existing_names

    try:
        folder_path, parent_id, existing_names = await asyncio.to_thread(_resolve_folder)
    except Exception as e:
        print(f"[bulk filing error] resolving destination folder: {e}")
        return

    service = get_drive_service()

    for file_id in file_ids:
        try:
            drive_file = await asyncio.to_thread(
                lambda: service.files().get(fileId=file_id, fields="id,name,createdTime,parents").execute()
            )
            fname = drive_file["name"]
            source_folder = (drive_file.get("parents") or [settings.drive_incoming_folder_id])[0]
            file_created = datetime.fromisoformat(drive_file["createdTime"].replace("Z", "+00:00")).replace(tzinfo=None)
            scan_dt = parse_date_from_filename(fname) or file_created
            ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else "pdf"

            _, filename = build_filing_info(
                group.name, subgroup.name, subgroup.filename_tag, subgroup.custom_path,
                group.include_in_path, scan_dt, override_filename or item_one_off_description, ext,
            )
            filename = dedupe_filename(filename, existing_names)
            existing_names.add(filename)

            await asyncio.to_thread(move_and_rename_file, file_id, filename, parent_id, source_folder)

            async with AsyncSessionLocal() as db:
                existing = await db.execute(select(Document).where(Document.drive_file_id == file_id))
                doc = existing.scalar_one_or_none()
                if doc is None:
                    doc = Document(drive_file_id=file_id, file_name_when_imported=fname)
                    db.add(doc)
                doc.timestamp_from_filename = scan_dt
                doc.group_id = group.id
                doc.subgroup_id = subgroup.id
                doc.desired_folder_location = folder_path
                doc.desired_filename = filename
                doc.desired_full_path = f"{folder_path}/{filename}"
                doc.current_full_path = f"{folder_path}/{filename}"
                doc.moved_and_renamed_timestamp = datetime.utcnow()
                doc.assigned_item_one_off_description = item_one_off_description
                doc.assigned_comment = comment
                doc.assigned_rob_task = rob_task
                doc.assigned_beccy_task = beccy_task
                doc.assigned_task_note = task_note
                doc.photo_quality = photo_quality
                await db.commit()
                doc_id = doc.id

            await _fetch_and_cache_thumbnail(file_id)

            if rob_task or beccy_task:
                _title = task_note or item_one_off_description or filename or fname
                _cat = f"{group.name} / {subgroup.name}" if subgroup.name != "/" else group.name
                _date = scan_dt.strftime("%d %B %Y")
                if rob_task:
                    await _queue_2do_task(doc_id, _title, _cat, "rob", _date)
                if beccy_task:
                    await _queue_2do_task(doc_id, _title, _cat, "beccy", _date)
        except Exception as e:
            print(f"[bulk filing error] {file_id}: {e}")
            continue

    async with AsyncSessionLocal() as db:
        sub = await db.get(Subgroup, subgroup_id)
        if sub:
            sub.use_count += 1
            sub.last_used = datetime.utcnow()
            await db.commit()


@app.post("/import/bulk/file", response_class=HTMLResponse)
async def import_bulk_file(
    background_tasks: BackgroundTasks,
    file_ids: list[str] = Form(...),
    subgroup_id: int = Form(...),
    override_filename: Optional[str] = Form(None),
    item_one_off_description: Optional[str] = Form(None),
    comment: Optional[str] = Form(None),
    rob_task: Optional[str] = Form(None),
    beccy_task: Optional[str] = Form(None),
    task_note: Optional[str] = Form(None),
    photo_quality: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    subgroup = await db.get(Subgroup, subgroup_id)
    if not subgroup:
        raise HTTPException(404)

    background_tasks.add_task(
        _do_bulk_filing,
        file_ids,
        subgroup_id,
        override_filename.strip() if override_filename and override_filename.strip() else None,
        item_one_off_description.strip() if item_one_off_description and item_one_off_description.strip() else None,
        comment.strip() if comment and comment.strip() else None,
        bool(rob_task),
        bool(beccy_task),
        task_note.strip() if task_note and task_note.strip() else None,
        bool(photo_quality),
    )
    return RedirectResponse("/import", status_code=303)


@app.get("/import/{drive_file_id}", response_class=HTMLResponse)
async def import_detail_page(drive_file_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    service = get_drive_service()
    file = await asyncio.to_thread(
        lambda: service.files().get(fileId=drive_file_id, fields="id,name,createdTime,mimeType,parents").execute()
    )
    parent_folder_id = (file.get("parents") or [settings.drive_incoming_folder_id])[0]
    groups_result = await db.execute(select(Group).order_by(Group.name))
    pairs_result = await db.execute(
        select(Subgroup)
        .options(joinedload(Subgroup.group))
        .where((Subgroup.last_used.isnot(None)) | (Subgroup.use_count > 0))
        .order_by(desc(Subgroup.last_used), desc(Subgroup.use_count))
        .limit(4)
    )
    return templates.TemplateResponse("import_detail.html", {
        "request": request,
        "file": file,
        "file_id": drive_file_id,
        "parent_folder_id": parent_folder_id,
        "groups": groups_result.scalars().all(),
        "quick_pairs": pairs_result.scalars().all(),
    })


@app.get("/import/{drive_file_id}/detail-groups", response_class=HTMLResponse)
async def import_detail_groups(
    drive_file_id: str, request: Request,
    group_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
):
    pairs_result = await db.execute(
        select(Subgroup)
        .options(joinedload(Subgroup.group))
        .where((Subgroup.last_used.isnot(None)) | (Subgroup.use_count > 0))
        .order_by(desc(Subgroup.last_used), desc(Subgroup.use_count))
        .limit(4)
    )
    quick_pairs = pairs_result.scalars().all()
    if group_id:
        group = await db.get(Group, group_id)
        if not group:
            raise HTTPException(404)
        recent = await db.execute(
            select(Subgroup)
            .where(Subgroup.group_id == group_id, Subgroup.name != "/")
            .order_by(desc(Subgroup.last_used), Subgroup.name)
            .limit(5)
        )
        all_subs = await db.execute(
            select(Subgroup).where(Subgroup.group_id == group_id, Subgroup.name != "/").order_by(Subgroup.name)
        )
        slash = await db.execute(
            select(Subgroup).where(Subgroup.group_id == group_id, Subgroup.name == "/").limit(1)
        )
        return templates.TemplateResponse("_import_detail_subgroups.html", {
            "request": request,
            "file_id": drive_file_id,
            "group": group,
            "subgroups": recent.scalars().all(),
            "all_subgroups": all_subs.scalars().all(),
            "slash_sub": slash.scalar_one_or_none(),
        })
    groups_result = await db.execute(select(Group).order_by(Group.name))
    return templates.TemplateResponse("_import_detail_groups.html", {
        "request": request,
        "file_id": drive_file_id,
        "groups": groups_result.scalars().all(),
        "quick_pairs": quick_pairs,
    })


@app.post("/import/{drive_file_id}/create-group", response_class=HTMLResponse)
async def import_create_group(
    drive_file_id: str, request: Request,
    name: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    group = Group(name=name.strip(), include_in_path=True)
    db.add(group)
    await db.flush()
    await db.refresh(group)
    await db.commit()
    return templates.TemplateResponse("_import_detail_subgroups.html", {
        "request": request,
        "file_id": drive_file_id,
        "group": group,
        "subgroups": [],
        "all_subgroups": [],
        "slash_sub": None,
    })


@app.post("/import/{drive_file_id}/create-subgroup", response_class=HTMLResponse)
async def import_create_subgroup(
    drive_file_id: str, request: Request,
    group_id: int = Form(...),
    name: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    new_sub = Subgroup(group_id=group_id, name=name.strip())
    db.add(new_sub)
    await db.flush()  # get the new id before commit
    await db.refresh(new_sub)
    group = await db.get(Group, group_id)
    new_sub.group = group
    await db.commit()
    # Go straight to confirm for the new subgroup
    service = get_drive_service()
    file = await asyncio.to_thread(
        lambda: service.files().get(fileId=drive_file_id, fields="id,name,createdTime,parents").execute()
    )
    subgroup_label = new_sub.name if new_sub.name != "/" else group.name
    color_key = (group.name + "/" + new_sub.name) if new_sub.name != "/" else group.name
    from services.filer import parse_date_from_filename as _parse_dt, extract_drive_prefix as _pfx
    scan_dt = _parse_dt(file["name"]) or datetime.utcnow()
    source_folder = (file.get("parents") or [settings.drive_incoming_folder_id])[0]
    return templates.TemplateResponse("_import_detail_confirm.html", {
        "request": request,
        "file_id": drive_file_id,
        "drive_filename": file["name"],
        "source_folder_id": source_folder,
        "filename_prefix": _pfx(file["name"]),
        "filename_tag": new_sub.filename_tag or "",
        "subgroup": new_sub,
        "group": group,
        "subgroup_label": subgroup_label,
        "color_key": color_key,
        "scan_date_str": scan_dt.strftime("%Y-%m-%d"),
        "scan_time_str": scan_dt.strftime("%H%M%S"),
    })


@app.get("/import/{drive_file_id}/confirm", response_class=HTMLResponse)
async def import_confirm(
    drive_file_id: str, request: Request,
    subgroup_id: int = 0,
    db: AsyncSession = Depends(get_db),
):
    sub_result = await db.execute(
        select(Subgroup).options(joinedload(Subgroup.group)).where(Subgroup.id == subgroup_id)
    )
    subgroup = sub_result.scalar_one_or_none()
    if not subgroup:
        raise HTTPException(404)
    group = subgroup.group
    service = get_drive_service()
    file = await asyncio.to_thread(
        lambda: service.files().get(fileId=drive_file_id, fields="id,name,createdTime,parents").execute()
    )
    subgroup_label = subgroup.name if subgroup.name != "/" else group.name
    color_key = (group.name + "/" + subgroup.name) if subgroup.name != "/" else group.name
    from services.filer import parse_date_from_filename as _parse_dt, extract_drive_prefix as _pfx
    scan_dt = _parse_dt(file["name"]) or datetime.utcnow()
    source_folder = (file.get("parents") or [settings.drive_incoming_folder_id])[0]
    return templates.TemplateResponse("_import_detail_confirm.html", {
        "request": request,
        "file_id": drive_file_id,
        "drive_filename": file["name"],
        "source_folder_id": source_folder,
        "filename_prefix": _pfx(file["name"]),
        "filename_tag": subgroup.filename_tag or "",
        "subgroup": subgroup,
        "group": group,
        "subgroup_label": subgroup_label,
        "color_key": color_key,
        "scan_date_str": scan_dt.strftime("%Y-%m-%d"),
        "scan_time_str": scan_dt.strftime("%H%M%S"),
    })


def _render_pdf_preview(data: bytes, max_pages: int, dpi: int = 110) -> bytes:
    """Rasterize the first max_pages pages of a PDF and stack them into one tall JPEG.

    Mobile browsers (notably iOS Safari) won't render a PDF inline inside an
    iframe — they show a static preview card with an "Open" button instead of
    the document. A plain <img> has no such restriction, so we render the
    pages ourselves rather than relying on the browser's native PDF viewer.
    """
    import io
    import fitz  # pymupdf
    from PIL import Image
    doc = fitz.open(stream=data, filetype="pdf")
    n = min(max_pages, len(doc))
    pages = []
    for i in range(n):
        pix = doc[i].get_pixmap(dpi=dpi)
        pages.append(Image.frombytes("RGB", [pix.width, pix.height], pix.samples))
    gap = 12
    width = max(p.width for p in pages)
    height = sum(p.height for p in pages) + gap * (len(pages) - 1)
    canvas = Image.new("RGB", (width, height), "white")
    y = 0
    for p in pages:
        canvas.paste(p, (0, y))
        y += p.height + gap
    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=82)
    return buf.getvalue()


@app.get("/import/{drive_file_id}/preview-image")
async def import_preview_image(drive_file_id: str, max_pages: int = 4):
    try:
        content, content_type = await _fetch_drive_media(drive_file_id)
        if "pdf" not in content_type.lower():
            # Not a PDF (e.g. a plain photo scan) — nothing to paginate, pass it through as-is.
            return Response(content, media_type=content_type, headers={"Cache-Control": "no-store"})
        image = await asyncio.to_thread(_render_pdf_preview, content, max_pages)
        return Response(image, media_type="image/jpeg", headers={"Cache-Control": "no-store"})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, str(e))


@app.get("/document/{document_id}/preview-image")
async def document_preview_image(document_id: int, max_pages: int = 20, db: AsyncSession = Depends(get_db)):
    doc = await db.get(Document, document_id)
    if not doc or not doc.drive_file_id:
        raise HTTPException(404)
    try:
        content, content_type = await _fetch_drive_media(doc.drive_file_id)
        if "pdf" not in content_type.lower():
            return Response(content, media_type=content_type, headers={"Cache-Control": "no-store"})
        image = await asyncio.to_thread(_render_pdf_preview, content, max_pages)
        return Response(image, media_type="image/jpeg", headers={"Cache-Control": "no-store"})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, str(e))


@app.get("/import/{drive_file_id}/view")
async def import_view(drive_file_id: str):
    try:
        content, content_type = await _fetch_drive_media(drive_file_id)
        return StreamingResponse(
            iter([content]),
            media_type=content_type,
            headers={"Cache-Control": "no-store", "Content-Disposition": "inline"},
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, str(e))


@app.get("/import/thumbnail/{drive_file_id}")
async def import_thumbnail(drive_file_id: str):
    cache_path = THUMB_CACHE / f"{drive_file_id}.jpg"
    if cache_path.exists() and (time.time() - cache_path.stat().st_mtime) < THUMB_TTL:
        return FileResponse(cache_path, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})
    data = await _fetch_and_cache_thumbnail(drive_file_id)
    if not data:
        raise HTTPException(404)
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})


@app.get("/import/{drive_file_id}/card", response_class=HTMLResponse)
async def import_card(drive_file_id: str, request: Request):
    service = get_drive_service()
    file = await asyncio.to_thread(
        lambda: service.files().get(fileId=drive_file_id, fields="id,name,createdTime,mimeType").execute()
    )
    return templates.TemplateResponse("_import_file_card.html", {"request": request, "file": file})


@app.get("/import/{drive_file_id}/pick", response_class=HTMLResponse)
async def import_pick(
    drive_file_id: str, request: Request,
    group_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
):
    pairs_result = await db.execute(
        select(Subgroup)
        .options(joinedload(Subgroup.group))
        .where((Subgroup.last_used.isnot(None)) | (Subgroup.use_count > 0))
        .order_by(desc(Subgroup.last_used), desc(Subgroup.use_count))
        .limit(4)
    )
    quick_pairs = pairs_result.scalars().all()

    if group_id:
        group = await db.get(Group, group_id)
        if not group:
            raise HTTPException(404)
        subs_result = await db.execute(
            select(Subgroup).where(Subgroup.group_id == group_id, Subgroup.name != "/").order_by(Subgroup.name)
        )
        slash_result = await db.execute(
            select(Subgroup).where(Subgroup.group_id == group_id, Subgroup.name == "/").limit(1)
        )
        return templates.TemplateResponse("_import_pick_subgroups.html", {
            "request": request,
            "file_id": drive_file_id,
            "group": group,
            "subgroups": subs_result.scalars().all(),
            "slash_sub": slash_result.scalar_one_or_none(),
        })

    groups_result = await db.execute(select(Group).order_by(Group.name))
    return templates.TemplateResponse("_import_pick_groups.html", {
        "request": request,
        "file_id": drive_file_id,
        "groups": groups_result.scalars().all(),
        "quick_pairs": quick_pairs,
    })


async def _do_drive_filing(file_id: str, folder_path: str, filename: str, source_folder: str):
    """Move and rename a Drive file in the background after the HTTP response has been sent."""
    from services.drive import get_or_create_folder, move_and_rename_file, list_files_in_folder
    from services.filer import dedupe_filename
    def _sync():
        parent_id = settings.drive_filed_root_folder_id
        for part in [p for p in folder_path.strip("/").split("/") if p]:
            parent_id = get_or_create_folder(parent_id, part)
        existing_names = {f["name"] for f in list_files_in_folder(parent_id) if f["id"] != file_id}
        final_filename = dedupe_filename(filename, existing_names)
        move_and_rename_file(file_id, final_filename, parent_id, source_folder)
        return final_filename
    try:
        final_filename = await asyncio.to_thread(_sync)
        if final_filename != filename:
            async with AsyncSessionLocal() as db:
                result = await db.execute(select(Document).where(Document.drive_file_id == file_id))
                doc = result.scalar_one_or_none()
                if doc:
                    doc.desired_filename = final_filename
                    doc.desired_full_path = f"{folder_path}/{final_filename}"
                    doc.current_full_path = f"{folder_path}/{final_filename}"
                    await db.commit()
    except Exception as e:
        print(f"[filing error] {file_id} → {folder_path}/{filename}: {e}")


@app.post("/import/{drive_file_id}/file", response_class=HTMLResponse)
async def import_file(
    drive_file_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    subgroup_id: int = Form(...),
    from_detail: Optional[str] = Form(None),
    override_filename: Optional[str] = Form(None),
    item_one_off_description: Optional[str] = Form(None),
    comment: Optional[str] = Form(None),
    rob_task: Optional[str] = Form(None),
    beccy_task: Optional[str] = Form(None),
    task_note: Optional[str] = Form(None),
    drive_filename: Optional[str] = Form(None),
    source_folder_id: Optional[str] = Form(None),
    scan_date_str: Optional[str] = Form(None),
    scan_time_str: Optional[str] = Form(None),
    photo_quality: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    from services.filer import parse_date_from_filename, build_filing_info

    sub_result = await db.execute(
        select(Subgroup).options(joinedload(Subgroup.group)).where(Subgroup.id == subgroup_id)
    )
    subgroup = sub_result.scalar_one_or_none()
    if not subgroup:
        raise HTTPException(404)
    group = subgroup.group

    clean_override = override_filename.strip() if override_filename and override_filename.strip() else None
    clean_one_off = item_one_off_description.strip() if item_one_off_description and item_one_off_description.strip() else None

    if drive_filename and source_folder_id and scan_date_str and scan_time_str:
        # Metadata embedded in form — no Drive API call needed
        _fname = drive_filename
        source_folder = source_folder_id
        scan_dt = datetime.strptime(f"{scan_date_str} {scan_time_str}", "%Y-%m-%d %H%M%S")
    else:
        # Fallback for requests without embedded metadata
        service = get_drive_service()
        _df = await asyncio.to_thread(
            lambda: service.files().get(fileId=drive_file_id, fields="id,name,createdTime,parents").execute()
        )
        _fname = _df["name"]
        source_folder = (_df.get("parents") or [settings.drive_incoming_folder_id])[0]
        file_created = datetime.fromisoformat(_df["createdTime"].replace("Z", "+00:00")).replace(tzinfo=None)
        scan_dt = parse_date_from_filename(_fname) or file_created

    ext = _fname.rsplit(".", 1)[-1].lower() if "." in _fname else "pdf"

    # Compute destination purely in Python — no Drive calls yet
    folder_path, filename = build_filing_info(
        group.name, subgroup.name, subgroup.filename_tag, subgroup.custom_path,
        group.include_in_path, scan_dt, clean_override or clean_one_off, ext,
    )

    # Update existing Document if present (scanner file queued by watcher, or re-filing a phone import)
    existing_result = await db.execute(
        select(Document).where(Document.drive_file_id == drive_file_id)
    )
    doc = existing_result.scalar_one_or_none()
    if doc is None:
        doc = Document(drive_file_id=drive_file_id, file_name_when_imported=_fname)
        db.add(doc)

    doc.timestamp_from_filename = scan_dt
    doc.group_id = group.id
    doc.subgroup_id = subgroup.id
    doc.desired_folder_location = folder_path
    doc.desired_filename = filename
    doc.desired_full_path = f"{folder_path}/{filename}"
    doc.current_full_path = f"{folder_path}/{filename}"
    doc.moved_and_renamed_timestamp = datetime.utcnow()
    doc.assigned_item_one_off_description = clean_one_off
    doc.assigned_comment = comment.strip() if comment and comment.strip() else None
    doc.assigned_rob_task = bool(rob_task)
    doc.assigned_beccy_task = bool(beccy_task)
    doc.assigned_task_note = task_note.strip() if task_note and task_note.strip() else None
    doc.photo_quality = bool(photo_quality)
    subgroup.use_count += 1
    subgroup.last_used = datetime.utcnow()
    await db.commit()

    # Fire Drive move + thumbnail pre-cache in background — response returns immediately
    background_tasks.add_task(_do_drive_filing, drive_file_id, folder_path, filename, source_folder)
    background_tasks.add_task(_fetch_and_cache_thumbnail, drive_file_id)

    # Queue 2Do tasks in background too
    if doc.assigned_rob_task or doc.assigned_beccy_task:
        _title = doc.assigned_task_note or clean_one_off or filename or _fname
        _cat = f"{group.name} / {subgroup.name}" if subgroup.name != "/" else group.name
        _date = scan_dt.strftime("%d %B %Y")
        if doc.assigned_rob_task:
            background_tasks.add_task(_queue_2do_task, doc.id, _title, _cat, "rob", _date)
        if doc.assigned_beccy_task:
            background_tasks.add_task(_queue_2do_task, doc.id, _title, _cat, "beccy", _date)

    if from_detail:
        return RedirectResponse("/import", status_code=303)
    label = subgroup.name if subgroup.name != "/" else group.name
    display = f"{group.name} / {label}" if subgroup.name != "/" else group.name
    return HTMLResponse(
        f'<div id="card-{drive_file_id}" class="col-span-2 text-center text-sm text-green-600 py-3 bg-white rounded-2xl shadow-sm font-medium">✓ Filed to {display}</div>'
    )


@app.post("/import/{drive_file_id}/delete-from-drive")
async def import_delete_from_drive(drive_file_id: str, db: AsyncSession = Depends(get_db)):
    await _trash_or_move_to_can_delete(drive_file_id)
    queued = (await db.execute(
        select(Document).where(Document.drive_file_id == drive_file_id, Document.group_id.is_(None))
    )).scalar_one_or_none()
    if queued:
        await db.delete(queued)
    await db.commit()
    return RedirectResponse("/import", status_code=303)


@app.post("/document/{document_id}/delete-record", response_class=HTMLResponse)
async def document_delete_record(document_id: int, db: AsyncSession = Depends(get_db)):
    """Delete just the library record (no Drive action). Returns empty string so HTMX removes the element."""
    doc = await db.get(Document, document_id)
    if not doc:
        raise HTTPException(404)
    await db.delete(doc)
    await db.commit()
    return HTMLResponse("")


@app.post("/document/{document_id}/delete-from-drive")
async def document_delete_from_drive(document_id: int, db: AsyncSession = Depends(get_db)):
    doc = await db.get(Document, document_id)
    if not doc:
        raise HTTPException(404)
    back_url = f"/library/{doc.group_id}/{doc.subgroup_id}" if doc.subgroup_id else f"/library/{doc.group_id}" if doc.group_id else "/library"
    if doc.drive_file_id:
        await _trash_or_move_to_can_delete(doc.drive_file_id)
    await db.delete(doc)
    await db.commit()
    return RedirectResponse(back_url, status_code=303)


# ---------------------------------------------------------------------------
# 2Do task queue
# ---------------------------------------------------------------------------

async def _queue_2do_task(doc_id: int, title: str, category: str, assignee: str, scan_date: str = ""):
    """Create a Toodledo task for the given assignee, synced into 2Do."""
    if not toodledo.is_connected():
        return
    folder_name = "go with Rob incoming" if assignee == "rob" else "Beccy incoming"

    try:
        async with AsyncSessionLocal() as db:
            doc = await db.get(Document, doc_id)
            if not doc:
                return
            note_lines = [
                category,
                f"{settings.app_base_url}/document/{doc_id}",
                f"https://drive.google.com/file/d/{doc.drive_file_id}/view" if doc.drive_file_id else "",
                f"Scanned {scan_date}" if scan_date else "",
            ]
            note = "\n".join(l for l in note_lines if l)

            folder_id = await asyncio.to_thread(toodledo.get_or_create_folder, folder_name)
            result = await asyncio.to_thread(toodledo.create_task, title, note, folder_id)
            task_id = result[0]["id"]

            if assignee == "rob":
                doc.toodledo_rob_task_id = task_id
            else:
                doc.toodledo_beccy_task_id = task_id
            await db.commit()
    except Exception as e:
        print(f"[2Do queue] doc={doc_id} assignee={assignee}: {e}")


# ---------------------------------------------------------------------------
# Broken links background scan
# ---------------------------------------------------------------------------

_link_scan: dict = {"running": False, "total": 0, "checked": 0}


async def _duplicate_broken_ids(db: AsyncSession, broken_docs: list) -> set:
    """Return IDs of broken docs whose filename+folder is already covered by a linked doc."""
    if not broken_docs:
        return set()
    linked = await db.execute(
        select(Document.desired_filename, Document.desired_folder_location)
        .where(Document.drive_file_id.isnot(None), Document.broken_link == False)
        .distinct()
    )
    linked_pairs = {(r[0], r[1]) for r in linked.fetchall()}
    dup_ids = {
        doc.id for doc in broken_docs
        if (doc.desired_filename, doc.desired_folder_location) in linked_pairs
    }
    # Also flag within the broken set: keep the lowest-ID record, flag the rest
    from collections import defaultdict
    groups: dict = defaultdict(list)
    for doc in broken_docs:
        groups[(doc.desired_filename, doc.desired_folder_location)].append(doc.id)
    for ids in groups.values():
        if len(ids) > 1:
            ids.sort()
            dup_ids.update(ids[1:])  # everything after the first is a duplicate
    return dup_ids


async def _run_broken_links_scan() -> None:
    import asyncio as _asyncio
    from googleapiclient.errors import HttpError

    _link_scan.update({"running": True, "checked": 0, "total": 0})
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Document))
            all_docs = result.scalars().all()
            _link_scan["total"] = len(all_docs)

            # Split: docs with no Drive file are immediately broken
            no_id_docs = [d for d in all_docs if not d.drive_file_id]
            check_docs = [d for d in all_docs if d.drive_file_id]

            for doc in no_id_docs:
                if not doc.broken_link:
                    doc.broken_link = True
                _link_scan["checked"] += 1

            service = get_drive_service()
            for doc in check_docs:
                try:
                    req = service.files().get(fileId=doc.drive_file_id, fields="id,trashed")
                    meta = await _asyncio.to_thread(req.execute)
                    is_broken = meta.get("trashed", False)
                except HttpError as e:
                    is_broken = int(e.resp.status) in (404, 403)
                except Exception as e:
                    print(f"broken-links check error doc={doc.id}: {e}")
                    _link_scan["checked"] += 1
                    continue

                if is_broken != doc.broken_link:
                    doc.broken_link = is_broken
                _link_scan["checked"] += 1

            await db.commit()
    finally:
        _link_scan["running"] = False


# ---------------------------------------------------------------------------
# Re-link background scan
# ---------------------------------------------------------------------------

_relink_state: dict = {"running": False, "total": 0, "checked": 0, "linked": 0, "duplicate": 0, "error": None}


async def _resolve_drive_path(path: str) -> tuple[Optional[str], str]:
    """Walk a folder path. Returns (folder_id_or_None, diagnostic_string).

    Three-tier resolution:
    1. From filed root (documents/) — handles normal paths
    2. From My Drive root — handles folders at My Drive top level
    3. Drive-wide name search for first segment — handles orphaned peer
       folders like Work/ that share no parent with documents/
    """
    import asyncio as _asyncio
    from config import settings as _settings
    service = get_drive_service()
    parts = [p for p in path.strip("/").split("/") if p]
    if not parts:
        return _settings.drive_filed_root_folder_id, ""

    root_meta = await _asyncio.to_thread(
        service.files().get(fileId="root", fields="id").execute
    )
    my_drive_root_id = root_meta["id"]

    async def _walk(start_id: str) -> Optional[str]:
        current_id = start_id
        for part in parts:
            req = service.files().list(
                q=f"name = '{_dq(part)}' and '{current_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false",
                fields="files(id)",
                pageSize=1,
            )
            files = (await _asyncio.to_thread(req.execute)).get("files", [])
            if not files:
                return None
            current_id = files[0]["id"]
        return current_id

    # Tier 1: filed root
    result = await _walk(_settings.drive_filed_root_folder_id)
    if result:
        return result, ""

    # Tier 2: My Drive root (proper hierarchy)
    result = await _walk(my_drive_root_id)
    if result:
        return result, ""

    # Tier 3: drive-wide search for first segment, then walk the rest
    # Handles orphaned peer folders (e.g. Work/) that have no parent in hierarchy
    head, tail = parts[0], parts[1:]
    resp = await _asyncio.to_thread(
        service.files().list(
            q=f"name = '{_dq(head)}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false",
            fields="files(id,name,parents)",
            pageSize=10,
        ).execute
    )
    candidates = resp.get("files", [])
    for candidate in candidates:
        current_id = candidate["id"]
        found = True
        for part in tail:
            req = service.files().list(
                q=f"name = '{_dq(part)}' and '{current_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false",
                fields="files(id)",
                pageSize=1,
            )
            files = (await _asyncio.to_thread(req.execute)).get("files", [])
            if not files:
                found = False
                break
            current_id = files[0]["id"]
        if found:
            return current_id, ""

    return None, f"'{head}' not found in filed root, My Drive, or anywhere in Drive"


async def _run_relink_scan(old_prefix: str, folder_input: str) -> None:
    import asyncio as _asyncio
    from collections import deque
    _relink_state.update({"running": True, "checked": 0, "linked": 0, "duplicate": 0, "total": 0, "error": None})
    try:
        # Resolve folder: treat as path if it contains '/' or looks short, else as Drive ID
        folder_input_stripped = folder_input.strip().strip("/")
        if not folder_input_stripped or folder_input_stripped == "/":
            folder_id = _settings.drive_filed_root_folder_id
        elif "/" in folder_input or len(folder_input) < 28:
            folder_id, diag = await _resolve_drive_path(folder_input)
            if not folder_id:
                _relink_state["error"] = f"Folder not found: {folder_input} — {diag}"
                return
        else:
            folder_id = folder_input.strip()

        async with AsyncSessionLocal() as db:
            clean_prefix = old_prefix.strip("/").strip()
            conditions = [Document.broken_link == True]
            if clean_prefix:
                # Word-by-word match so "Car Elgrand" matches "Car/Elgrand/"
                # and "For Creatives" matches "For Creatives/"
                words = clean_prefix.replace("/", " ").split()
                for word in words:
                    conditions.append(Document.desired_folder_location.contains(word))
            result = await db.execute(select(Document).where(*conditions))
            docs = result.scalars().all()
            _relink_state["total"] = len(docs)
            if not docs:
                return

            service = get_drive_service()
            files_by_name: dict = {}

            # BFS: index all files in the folder tree by lowercased name
            queue: deque = deque([folder_id])
            while queue:
                fid = queue.popleft()
                page_token = None
                while True:
                    req = service.files().list(
                        q=f"'{fid}' in parents and trashed = false",
                        fields="nextPageToken,files(id,name,mimeType)",
                        pageSize=1000,
                        pageToken=page_token,
                    )
                    resp = await _asyncio.to_thread(req.execute)
                    for f in resp.get("files", []):
                        if f["mimeType"] == "application/vnd.google-apps.folder":
                            queue.append(f["id"])
                        else:
                            files_by_name.setdefault(f["name"].strip().lower(), []).append(f["id"])
                    page_token = resp.get("nextPageToken")
                    if not page_token:
                        break

            # Build set of drive_file_ids already claimed by other documents
            doc_ids_being_fixed = {d.id for d in docs}
            used_ids_result = await db.execute(
                select(Document.drive_file_id).where(
                    Document.drive_file_id.isnot(None),
                    ~Document.id.in_(doc_ids_being_fixed),
                )
            )
            already_used: set = {row[0] for row in used_ids_result}
            assigned_this_run: set = set()

            def _word_match(target_name: str, drive_name: str) -> bool:
                """All words from target_name appear in drive_name (handles extra words like 'V5')."""
                target_words = set(re.findall(r'[a-z0-9]+', target_name.lower()))
                drive_words = set(re.findall(r'[a-z0-9]+', drive_name.lower()))
                return bool(target_words) and target_words.issubset(drive_words)

            for doc in docs:
                target = (doc.desired_filename or doc.file_name_when_imported or "").strip().lower()
                if not target:
                    _relink_state["checked"] += 1
                    continue
                # Exact match first, filter to unclaimed only
                candidates = [
                    f for f in files_by_name.get(target, [])
                    if f not in already_used and f not in assigned_this_run
                ]
                # Word-overlap fallback if no unclaimed exact matches
                if not candidates:
                    candidates = [
                        fid
                        for name, ids in files_by_name.items()
                        if _word_match(target, name)
                        for fid in ids
                        if fid not in already_used and fid not in assigned_this_run
                    ]
                # Sort for determinism; prefer exact-name match first
                candidates.sort(key=lambda f: (
                    0 if files_by_name.get(target) and f in files_by_name[target] else 1,
                    f,
                ))
                if candidates:
                    doc.drive_file_id = candidates[0]
                    doc.broken_link = False
                    assigned_this_run.add(candidates[0])
                    _relink_state["linked"] += 1
                else:
                    # Check if all Drive matches are already claimed by other docs
                    all_matches = list(files_by_name.get(target, []))
                    if not all_matches:
                        all_matches = [
                            fid for name, ids in files_by_name.items()
                            if _word_match(target, name) for fid in ids
                        ]
                    if all_matches and all(f in already_used or f in assigned_this_run for f in all_matches):
                        _relink_state["duplicate"] += 1
                _relink_state["checked"] += 1

            await db.commit()
    except Exception as e:
        _relink_state["error"] = str(e)
    finally:
        _relink_state["running"] = False


@app.post("/tasks/relink", response_class=HTMLResponse)
async def start_relink(request: Request, background_tasks: BackgroundTasks):
    if not get_credentials():
        return HTMLResponse('<p class="text-red-500 text-sm text-center py-4">Drive not connected.</p>')
    form = await request.form()
    old_prefix = form.get("old_prefix", "").strip()
    folder_input = form.get("folder_input", "").strip()
    if old_prefix and folder_input and not _relink_state.get("running"):
        _relink_state.update({"running": True, "checked": 0, "linked": 0, "duplicate": 0, "total": 0, "error": None})
        background_tasks.add_task(_run_relink_scan, old_prefix, folder_input)
    return templates.TemplateResponse("_relink.html", {
        "request": request,
        "relink_state": _relink_state,
        "duplicate_ids": set(),
    })


@app.get("/tasks/relink/status", response_class=HTMLResponse)
async def relink_status(request: Request, db: AsyncSession = Depends(get_db)):
    # When relink just finished, also return updated broken-links list via OOB swap
    broken: list = []
    dup_ids: set = set()
    refresh = not _relink_state["running"] and bool(_relink_state.get("checked"))
    if refresh and not _link_scan["running"]:
        result = await db.execute(
            select(Document)
            .options(joinedload(Document.group), joinedload(Document.subgroup))
            .where(Document.broken_link == True)
            .order_by(desc(Document.time_file_was_imported))
        )
        broken = result.scalars().all()
        dup_ids = await _duplicate_broken_ids(db, broken)
    return templates.TemplateResponse("_relink.html", {
        "request": request,
        "relink_state": _relink_state,
        "refresh_broken": refresh,
        "broken_links": broken,
        "duplicate_ids": dup_ids,
        "scan_state": _link_scan,
    })


# ---------------------------------------------------------------------------
# Path-scan: find broken docs at their desired_full_path in Drive
# ---------------------------------------------------------------------------

_path_scan_state: dict = {"running": False, "total": 0, "checked": 0, "linked": 0, "error": None}


def _dq(s: str) -> str:
    """Escape single quotes for Drive API query strings."""
    return s.replace("\\", "\\\\").replace("'", "\\'")


async def _run_path_scan() -> None:
    import asyncio as _asyncio
    from config import settings as _settings

    _path_scan_state.update({"running": True, "total": 0, "checked": 0, "linked": 0, "error": None})
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Document).where(
                    Document.broken_link == True,
                    Document.desired_full_path.isnot(None),
                )
            )
            docs = result.scalars().all()
            _path_scan_state["total"] = len(docs)

            used_result = await db.execute(
                select(Document.drive_file_id).where(Document.drive_file_id.isnot(None))
            )
            already_used: set = {r[0] for r in used_result}

            service = get_drive_service()
            folder_cache: dict = {}

            for doc in docs:
                path = doc.desired_full_path
                for marker in ("My Drive/", "My drive/"):
                    if marker in path:
                        path = "/" + path.split(marker, 1)[1]
                        break

                parts = [p for p in path.strip("/").split("/") if p]
                if len(parts) < 2:
                    _path_scan_state["checked"] += 1
                    continue

                folder_parts, filename = parts[:-1], parts[-1]
                folder_key = "/".join(folder_parts)

                if folder_key not in folder_cache:
                    current_id = _settings.drive_filed_root_folder_id
                    ok = True
                    for part in folder_parts:
                        req = service.files().list(
                            q=f"name = '{_dq(part)}' and '{current_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false",
                            fields="files(id)", pageSize=1,
                        )
                        resp = await _asyncio.to_thread(req.execute)
                        hits = resp.get("files", [])
                        if not hits:
                            ok = False
                            break
                        current_id = hits[0]["id"]
                    folder_cache[folder_key] = current_id if ok else None

                folder_id = folder_cache.get(folder_key)
                if folder_id:
                    req = service.files().list(
                        q=f"name = '{_dq(filename)}' and '{folder_id}' in parents and trashed = false",
                        fields="files(id)", pageSize=1,
                    )
                    resp = await _asyncio.to_thread(req.execute)
                    hits = resp.get("files", [])
                    if hits and hits[0]["id"] not in already_used:
                        fid = hits[0]["id"]
                        doc.drive_file_id = fid
                        doc.broken_link = False
                        already_used.add(fid)
                        _path_scan_state["linked"] += 1

                _path_scan_state["checked"] += 1

            await db.commit()
    except Exception as e:
        _path_scan_state["error"] = str(e)
    finally:
        _path_scan_state["running"] = False


@app.post("/tasks/path-scan", response_class=HTMLResponse)
async def start_path_scan(request: Request, background_tasks: BackgroundTasks):
    if not get_credentials():
        return HTMLResponse('<p class="text-red-500 text-sm text-center py-4">Drive not connected.</p>')
    if not _path_scan_state["running"]:
        background_tasks.add_task(_run_path_scan)
    return templates.TemplateResponse("_path_scan.html", {"request": request, "path_scan_state": _path_scan_state})


@app.get("/tasks/path-scan/status", response_class=HTMLResponse)
async def path_scan_status(request: Request, db: AsyncSession = Depends(get_db)):
    broken: list = []
    dup_ids: set = set()
    refresh = not _path_scan_state["running"] and bool(_path_scan_state.get("checked"))
    if refresh and not _link_scan["running"]:
        result = await db.execute(
            select(Document)
            .options(joinedload(Document.group), joinedload(Document.subgroup))
            .where(Document.broken_link == True)
            .order_by(desc(Document.time_file_was_imported))
        )
        broken = result.scalars().all()
        dup_ids = await _duplicate_broken_ids(db, broken)
    return templates.TemplateResponse("_path_scan.html", {
        "request": request,
        "path_scan_state": _path_scan_state,
        "refresh_broken": refresh,
        "broken_links": broken,
        "duplicate_ids": dup_ids,
        "scan_state": _link_scan,
    })


@app.get("/tasks", response_class=HTMLResponse)
async def tasks(request: Request, db: AsyncSession = Depends(get_db)):
    broken: list = []
    dup_ids: set = set()
    if not _link_scan["running"]:
        result = await db.execute(
            select(Document)
            .options(joinedload(Document.group), joinedload(Document.subgroup))
            .where(Document.broken_link == True)
            .order_by(desc(Document.time_file_was_imported))
        )
        broken = result.scalars().all()
        dup_ids = await _duplicate_broken_ids(db, broken)
    return templates.TemplateResponse("tasks.html", {
        "request": request,
        "active": "tasks",
        "broken_links": broken,
        "duplicate_ids": dup_ids,
        "scan_state": _link_scan,
        "relink_state": _relink_state,
        "path_scan_state": _path_scan_state,
    })


@app.post("/tasks/check-broken-links", response_class=HTMLResponse)
async def start_broken_links_check(
    request: Request, background_tasks: BackgroundTasks
):
    if not get_credentials():
        return HTMLResponse('<p class="text-red-500 text-sm text-center py-4">Drive not connected.</p>')
    if not _link_scan["running"]:
        _link_scan.update({"running": True, "checked": 0, "total": 0})
        background_tasks.add_task(_run_broken_links_scan)
    return templates.TemplateResponse("_broken_links.html", {
        "request": request,
        "scan_state": _link_scan,
        "broken_links": [],
        "duplicate_ids": set(),
    })


@app.get("/tasks/broken-links/status", response_class=HTMLResponse)
async def broken_links_status(request: Request, db: AsyncSession = Depends(get_db)):
    broken: list = []
    dup_ids: set = set()
    if not _link_scan["running"]:
        result = await db.execute(
            select(Document)
            .options(joinedload(Document.group), joinedload(Document.subgroup))
            .where(Document.broken_link == True)
            .order_by(desc(Document.time_file_was_imported))
        )
        broken = result.scalars().all()
        dup_ids = await _duplicate_broken_ids(db, broken)
    return templates.TemplateResponse("_broken_links.html", {
        "request": request,
        "scan_state": _link_scan,
        "broken_links": broken,
        "duplicate_ids": dup_ids,
    })


@app.post("/admin/restart", response_class=HTMLResponse)
async def admin_restart():
    import subprocess
    subprocess.Popen(
        "sleep 2 && sudo -n /bin/systemctl restart scanny.service",
        shell=True,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return HTMLResponse(
        '<span id="restart-status" class="text-amber-600">Restarting&hellip;</span>'
        '<script>setTimeout(()=>location.reload(),5000)</script>'
    )


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, db: AsyncSession = Depends(get_db)):
    from config import settings as app_settings
    result = await db.execute(
        select(Group).options(joinedload(Group.subgroups)).order_by(Group.name)
    )
    all_groups = result.scalars().unique().all()
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "active": "settings",
        "drive_authed": get_credentials() is not None,
        "settings": app_settings,
        "all_groups": all_groups,
    })


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

@app.get("/status", response_class=HTMLResponse)
async def status(request: Request):
    from config import settings
    return templates.TemplateResponse("status.html", {
        "request": request,
        "drive_authed": get_credentials() is not None,
        "toodledo_authed": toodledo.is_connected(),
        "settings": settings,
    })
