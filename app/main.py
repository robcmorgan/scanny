from contextlib import asynccontextmanager
from typing import Optional
from datetime import datetime, timezone

from fastapi import FastAPI, Request, Depends, HTTPException, Form, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from sqlalchemy.orm import joinedload

from database import get_db, init_db, AsyncSessionLocal
from models import Group, Subgroup, PendingScan, Document
from services.drive import get_flow, save_credentials_from_flow, get_credentials, get_drive_service
from services.watcher import start_scheduler, stop_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(lifespan=lifespan, title="Scanny")
templates = Jinja2Templates(directory="templates")

_CHIP_PALETTE = [
    "bg-blue-100 text-blue-900",    "bg-orange-200 text-orange-900",
    "bg-green-300 text-green-900",  "bg-fuchsia-400 text-fuchsia-900",
    "bg-amber-100 text-amber-900",  "bg-sky-200 text-sky-900",
    "bg-red-300 text-red-900",      "bg-lime-400 text-lime-900",
    "bg-violet-100 text-violet-900","bg-yellow-200 text-yellow-900",
    "bg-teal-300 text-teal-900",    "bg-pink-400 text-pink-900",
    "bg-blue-200 text-blue-900",    "bg-orange-300 text-orange-900",
    "bg-green-400 text-green-900",  "bg-fuchsia-100 text-fuchsia-900",
    "bg-amber-200 text-amber-900",  "bg-sky-300 text-sky-900",
    "bg-red-400 text-red-900",      "bg-lime-100 text-lime-900",
    "bg-violet-200 text-violet-900","bg-yellow-300 text-yellow-900",
    "bg-teal-400 text-teal-900",    "bg-pink-100 text-pink-900",
    "bg-blue-300 text-blue-900",    "bg-orange-400 text-orange-900",
    "bg-green-100 text-green-900",  "bg-fuchsia-200 text-fuchsia-900",
    "bg-amber-300 text-amber-900",  "bg-sky-400 text-sky-900",
    "bg-red-100 text-red-900",      "bg-lime-200 text-lime-900",
    "bg-violet-300 text-violet-900","bg-yellow-400 text-yellow-900",
    "bg-teal-100 text-teal-900",    "bg-pink-200 text-pink-900",
    "bg-blue-400 text-blue-900",    "bg-orange-100 text-orange-900",
    "bg-green-200 text-green-900",  "bg-fuchsia-300 text-fuchsia-900",
    "bg-amber-400 text-amber-900",  "bg-sky-100 text-sky-900",
    "bg-red-200 text-red-900",      "bg-lime-300 text-lime-900",
    "bg-violet-400 text-violet-900","bg-yellow-100 text-yellow-900",
    "bg-teal-200 text-teal-900",    "bg-pink-300 text-pink-900",
]

def _chip_color(key: str) -> str:
    h = 0
    for c in key:
        h = (h * 31 + ord(c)) & 0x7FFFFFFF
    return _CHIP_PALETTE[h % len(_CHIP_PALETTE)]

templates.env.globals["chip_color"] = _chip_color

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

    # Most recent pending scan (just for the "last added" indicator)
    result = await db.execute(
        select(PendingScan)
        .options(joinedload(PendingScan.subgroup).joinedload(Subgroup.group))
        .where(PendingScan.status == "pending")
        .order_by(desc(PendingScan.created_at))
        .limit(1)
    )
    pending_scans = result.scalars().all()

    return templates.TemplateResponse("scan.html", {
        "request": request,
        "quick_groups": quick_groups,
        "quick_pairs": quick_pairs,
        "pending_scans": pending_scans,
        "drive_authed": get_credentials() is not None,
    })


@app.get("/groups/{group_id}/recent-subgroups", response_class=HTMLResponse)
async def recent_subgroups(group_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    group = await db.get(Group, group_id)
    if not group:
        raise HTTPException(404)
    result = await db.execute(
        select(Subgroup)
        .where(Subgroup.group_id == group_id)
        .where(Subgroup.name != "/")
        .order_by(desc(Subgroup.last_used), Subgroup.name)
        .limit(5)
    )
    subgroups = result.scalars().all()
    # Fetch the '/' placeholder subgroup so the popup can offer "just this group"
    slash_result = await db.execute(
        select(Subgroup)
        .where(Subgroup.group_id == group_id)
        .where(Subgroup.name == "/")
        .limit(1)
    )
    slash_sub = slash_result.scalar_one_or_none()
    return templates.TemplateResponse("_subgroup_popup.html", {
        "request": request,
        "group": group,
        "subgroups": subgroups,
        "slash_sub": slash_sub,
    })


@app.get("/pending", response_class=HTMLResponse)
async def list_pending(request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(PendingScan)
        .options(joinedload(PendingScan.subgroup).joinedload(Subgroup.group))
        .where(PendingScan.status == "pending")
        .order_by(desc(PendingScan.created_at))
        .limit(20)
    )
    pending_scans = result.scalars().all()
    return templates.TemplateResponse("_pending_list.html", {
        "request": request,
        "pending_scans": pending_scans,
    })


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


@app.post("/pending")
async def create_pending(
    subgroup_id: int = Form(...),
    item_one_off_description: Optional[str] = Form(None),
    rob_task: Optional[str] = Form(None),
    beccy_task: Optional[str] = Form(None),
    task_note: Optional[str] = Form(None),
    comment: Optional[str] = Form(None),
    date_override: Optional[str] = Form(None),
    override_filename: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    parsed_date = None
    if date_override:
        try:
            from datetime import date
            parsed_date = date.fromisoformat(date_override)
        except ValueError:
            pass

    pending = PendingScan(
        subgroup_id=subgroup_id,
        item_one_off_description=item_one_off_description or None,
        rob_task=bool(rob_task),
        beccy_task=bool(beccy_task),
        task_note=task_note or None,
        comment=comment or None,
        date_override=parsed_date,
        override_filename=override_filename or None,
    )
    db.add(pending)

    sub = await db.get(Subgroup, subgroup_id)
    if sub:
        sub.use_count = (sub.use_count or 0) + 1
        sub.last_used = datetime.utcnow()

    await db.commit()
    return RedirectResponse("/", status_code=303)


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
    return templates.TemplateResponse("library.html", {
        "request": request,
        "active": "library",
        "groups": groups,
    })


@app.get("/library/{group_id}", response_class=HTMLResponse)
async def library_group(group_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    from sqlalchemy import func
    group = await db.get(Group, group_id)
    if not group:
        raise HTTPException(404)
    result = await db.execute(
        select(Subgroup, func.count(Document.id).label("doc_count"))
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
    import httpx, traceback
    doc = await db.get(Document, document_id)
    if not doc or not doc.drive_file_id:
        raise HTTPException(404)
    try:
        service = get_drive_service()
        meta = service.files().get(
            fileId=doc.drive_file_id,
            fields="thumbnailLink"
        ).execute()
        thumb_url = meta.get("thumbnailLink")
        if not thumb_url:
            raise HTTPException(404)
        thumb_url = thumb_url.replace("=s220", "=s400")
        creds = get_credentials()
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                thumb_url,
                headers={"Authorization": f"Bearer {creds.token}"},
                follow_redirects=True,
            )
        return StreamingResponse(
            iter([resp.content]),
            media_type=resp.headers.get("content-type", "image/jpeg"),
            headers={"Cache-Control": "no-store"},
        )
    except HTTPException:
        raise
    except Exception as e:
        print(f"Thumbnail error doc={document_id}: {e}\n{traceback.format_exc()}")
        raise HTTPException(502, str(e))


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
    return templates.TemplateResponse("document_viewer.html", {
        "request": request,
        "doc": doc,
    })


# ---------------------------------------------------------------------------
# Broken links background scan
# ---------------------------------------------------------------------------

_link_scan: dict = {"running": False, "total": 0, "checked": 0}


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

_relink_state: dict = {"running": False, "total": 0, "checked": 0, "linked": 0, "error": None}


async def _resolve_drive_path(path: str) -> Optional[str]:
    import asyncio as _asyncio
    from config import settings as _settings
    service = get_drive_service()
    parts = [p for p in path.strip("/").split("/") if p]
    if not parts:
        return _settings.drive_filed_root_folder_id
    current_id = _settings.drive_filed_root_folder_id
    for part in parts:
        req = service.files().list(
            q=f"name = '{part}' and '{current_id}' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false",
            fields="files(id)",
            pageSize=1,
        )
        resp = await _asyncio.to_thread(req.execute)
        files = resp.get("files", [])
        if not files:
            return None
        current_id = files[0]["id"]
    return current_id


async def _run_relink_scan(old_prefix: str, folder_input: str) -> None:
    import asyncio as _asyncio
    from collections import deque
    _relink_state.update({"running": True, "checked": 0, "linked": 0, "total": 0, "error": None})
    try:
        # Resolve folder: treat as path if it contains '/' or looks short, else as Drive ID
        if "/" in folder_input or len(folder_input) < 20:
            folder_id = await _resolve_drive_path(folder_input)
            if not folder_id:
                _relink_state["error"] = f"Folder not found in Drive: {folder_input}"
                return
        else:
            folder_id = folder_input.strip()

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Document).where(
                    Document.broken_link == True,
                    Document.desired_full_path.contains(old_prefix),
                )
            )
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
                            files_by_name.setdefault(f["name"].lower(), []).append(f["id"])
                    page_token = resp.get("nextPageToken")
                    if not page_token:
                        break

            for doc in docs:
                target = (doc.desired_filename or doc.file_name_when_imported or "").strip().lower()
                if target:
                    matches = files_by_name.get(target, [])
                    if len(matches) == 1:
                        doc.drive_file_id = matches[0]
                        doc.broken_link = False
                        _relink_state["linked"] += 1
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
        _relink_state.update({"running": True, "checked": 0, "linked": 0, "total": 0, "error": None})
        background_tasks.add_task(_run_relink_scan, old_prefix, folder_input)
    return templates.TemplateResponse("_relink.html", {
        "request": request,
        "relink_state": _relink_state,
    })


@app.get("/tasks/relink/status", response_class=HTMLResponse)
async def relink_status(request: Request):
    return templates.TemplateResponse("_relink.html", {
        "request": request,
        "relink_state": _relink_state,
    })


@app.get("/tasks", response_class=HTMLResponse)
async def tasks(request: Request, db: AsyncSession = Depends(get_db)):
    broken: list = []
    if not _link_scan["running"]:
        result = await db.execute(
            select(Document)
            .options(joinedload(Document.group), joinedload(Document.subgroup))
            .where(Document.broken_link == True)
            .order_by(desc(Document.time_file_was_imported))
        )
        broken = result.scalars().all()
    return templates.TemplateResponse("tasks.html", {
        "request": request,
        "active": "tasks",
        "broken_links": broken,
        "scan_state": _link_scan,
        "relink_state": _relink_state,
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
    })


@app.get("/tasks/broken-links/status", response_class=HTMLResponse)
async def broken_links_status(request: Request, db: AsyncSession = Depends(get_db)):
    broken: list = []
    if not _link_scan["running"]:
        result = await db.execute(
            select(Document)
            .options(joinedload(Document.group), joinedload(Document.subgroup))
            .where(Document.broken_link == True)
            .order_by(desc(Document.time_file_was_imported))
        )
        broken = result.scalars().all()
    return templates.TemplateResponse("_broken_links.html", {
        "request": request,
        "scan_state": _link_scan,
        "broken_links": broken,
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
        "settings": settings,
    })
