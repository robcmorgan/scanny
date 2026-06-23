from contextlib import asynccontextmanager
from typing import Optional
from datetime import datetime, timezone
import asyncio
import re

from fastapi import FastAPI, Request, Depends, HTTPException, Form, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from sqlalchemy.orm import joinedload

from database import get_db, init_db, AsyncSessionLocal
from models import Group, Subgroup, PendingScan, Document
from config import settings
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

def _chip_color(key: str) -> str:
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
        .limit(5)
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
    return templates.TemplateResponse("library.html", {
        "request": request,
        "active": "library",
        "groups": groups,
        "broken_count": broken_count,
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
    return templates.TemplateResponse("document_viewer.html", {
        "request": request,
        "doc": doc,
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
    doc.assigned_item_one_off_description = item_one_off_description.strip() or None
    doc.assigned_comment = comment.strip() or None
    doc.assigned_rob_task = rob_task == "1"
    doc.assigned_beccy_task = beccy_task == "1"
    doc.assigned_task_note = task_note.strip() or None
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


@app.get("/import/{drive_file_id}", response_class=HTMLResponse)
async def import_detail_page(drive_file_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    service = get_drive_service()
    file = await asyncio.to_thread(
        lambda: service.files().get(fileId=drive_file_id, fields="id,name,createdTime,mimeType").execute()
    )
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
        lambda: service.files().get(fileId=drive_file_id, fields="id,name,createdTime").execute()
    )
    subgroup_label = new_sub.name if new_sub.name != "/" else group.name
    color_key = (group.name + "/" + new_sub.name) if new_sub.name != "/" else group.name
    from services.filer import parse_date_from_filename as _parse_dt, extract_drive_prefix as _pfx
    scan_dt = _parse_dt(file["name"]) or datetime.utcnow()
    return templates.TemplateResponse("_import_detail_confirm.html", {
        "request": request,
        "file_id": drive_file_id,
        "drive_filename": file["name"],
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
        lambda: service.files().get(fileId=drive_file_id, fields="id,name,createdTime").execute()
    )
    subgroup_label = subgroup.name if subgroup.name != "/" else group.name
    color_key = (group.name + "/" + subgroup.name) if subgroup.name != "/" else group.name
    from services.filer import parse_date_from_filename as _parse_dt, extract_drive_prefix as _pfx
    scan_dt = _parse_dt(file["name"]) or datetime.utcnow()
    return templates.TemplateResponse("_import_detail_confirm.html", {
        "request": request,
        "file_id": drive_file_id,
        "drive_filename": file["name"],
        "filename_prefix": _pfx(file["name"]),
        "filename_tag": subgroup.filename_tag or "",
        "subgroup": subgroup,
        "group": group,
        "subgroup_label": subgroup_label,
        "color_key": color_key,
        "scan_date_str": scan_dt.strftime("%Y-%m-%d"),
        "scan_time_str": scan_dt.strftime("%H%M%S"),
    })


@app.get("/import/{drive_file_id}/view")
async def import_view(drive_file_id: str):
    import httpx
    try:
        creds = get_credentials()
        url = f"https://www.googleapis.com/drive/v3/files/{drive_file_id}?alt=media"
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {creds.token}"}, follow_redirects=True)
        if resp.status_code == 404:
            raise HTTPException(404)
        return StreamingResponse(
            iter([resp.content]),
            media_type=resp.headers.get("content-type", "application/pdf"),
            headers={"Cache-Control": "no-store", "Content-Disposition": "inline"},
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, str(e))


@app.get("/import/thumbnail/{drive_file_id}")
async def import_thumbnail(drive_file_id: str):
    import httpx
    try:
        service = get_drive_service()
        meta = await asyncio.to_thread(
            lambda: service.files().get(fileId=drive_file_id, fields="thumbnailLink").execute()
        )
        thumb_url = meta.get("thumbnailLink")
        if not thumb_url:
            raise HTTPException(404)
        thumb_url = thumb_url.replace("=s220", "=s400")
        creds = get_credentials()
        async with httpx.AsyncClient() as client:
            resp = await client.get(thumb_url, headers={"Authorization": f"Bearer {creds.token}"}, follow_redirects=True)
        return StreamingResponse(iter([resp.content]), media_type=resp.headers.get("content-type", "image/jpeg"), headers={"Cache-Control": "no-store"})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, str(e))


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


@app.post("/import/{drive_file_id}/file", response_class=HTMLResponse)
async def import_file(
    drive_file_id: str,
    request: Request,
    subgroup_id: int = Form(...),
    from_detail: Optional[str] = Form(None),
    override_filename: Optional[str] = Form(None),
    item_one_off_description: Optional[str] = Form(None),
    comment: Optional[str] = Form(None),
    rob_task: Optional[str] = Form(None),
    beccy_task: Optional[str] = Form(None),
    task_note: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    from services.filer import parse_date_from_filename, file_document as _file_document

    sub_result = await db.execute(
        select(Subgroup).options(joinedload(Subgroup.group)).where(Subgroup.id == subgroup_id)
    )
    subgroup = sub_result.scalar_one_or_none()
    if not subgroup:
        raise HTTPException(404)
    group = subgroup.group

    service = get_drive_service()
    drive_file = await asyncio.to_thread(
        lambda: service.files().get(fileId=drive_file_id, fields="id,name,createdTime,mimeType,parents").execute()
    )
    file_created = datetime.fromisoformat(drive_file["createdTime"].replace("Z", "+00:00")).replace(tzinfo=None)

    # Use the file's actual current parent folder (works for both scanner and phone files)
    source_folder = (drive_file.get("parents") or [settings.drive_import_folder_id or settings.drive_incoming_folder_id])[0]
    clean_override = override_filename.strip() if override_filename and override_filename.strip() else None
    clean_one_off = item_one_off_description.strip() if item_one_off_description and item_one_off_description.strip() else None
    try:
        folder_path, filename, _ = await asyncio.to_thread(
            _file_document,
            drive_file, group.name, subgroup.name,
            subgroup.filename_tag, subgroup.custom_path,
            group.include_in_path, None, clean_override or clean_one_off,
            source_folder,
        )
    except Exception as e:
        raise HTTPException(500, str(e))

    scan_dt = parse_date_from_filename(drive_file["name"]) or file_created

    # Update existing Document if present (scanner file queued by watcher, or re-filing a phone import)
    existing_result = await db.execute(
        select(Document).where(Document.drive_file_id == drive_file_id)
    )
    doc = existing_result.scalar_one_or_none()
    if doc is None:
        doc = Document(drive_file_id=drive_file_id, file_name_when_imported=drive_file["name"])
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
    subgroup.use_count += 1
    subgroup.last_used = datetime.utcnow()
    await db.commit()

    # Queue 2Do tasks
    if doc.assigned_rob_task or doc.assigned_beccy_task:
        _title = doc.assigned_task_note or clean_one_off or filename or drive_file["name"]
        _cat = f"{group.name} / {subgroup.name}" if subgroup.name != "/" else group.name
        _date = scan_dt.strftime("%d %B %Y")
        if doc.assigned_rob_task:
            await _queue_2do_task(doc.id, _title, _cat, "rob", _date)
        if doc.assigned_beccy_task:
            await _queue_2do_task(doc.id, _title, _cat, "beccy", _date)

    if from_detail:
        return RedirectResponse("/import", status_code=303)
    label = subgroup.name if subgroup.name != "/" else group.name
    display = f"{group.name} / {label}" if subgroup.name != "/" else group.name
    return HTMLResponse(
        f'<div id="card-{drive_file_id}" class="col-span-2 text-center text-sm text-green-600 py-3 bg-white rounded-2xl shadow-sm font-medium">✓ Filed to {display}</div>'
    )


async def _trash_or_move_to_can_delete(service, drive_file_id: str):
    """Trash a Drive file. If not owner (403), move it to a root-level 'can delete' folder instead."""
    from googleapiclient.errors import HttpError as _HttpError
    from services.drive import get_or_create_folder
    try:
        await asyncio.to_thread(lambda: service.files().update(fileId=drive_file_id, body={"trashed": True}).execute())
    except _HttpError as e:
        if e.resp.status == 404:
            return  # already gone
        if e.resp.status == 403:
            # Not owner — move to 'can delete' at My Drive root instead
            file_meta = await asyncio.to_thread(
                lambda: service.files().get(fileId=drive_file_id, fields="parents").execute()
            )
            root_id = (await asyncio.to_thread(
                lambda: service.files().get(fileId="root", fields="id").execute()
            ))["id"]
            can_delete_id = await asyncio.to_thread(get_or_create_folder, root_id, "can delete")
            old_parents = ",".join(file_meta.get("parents", []))
            await asyncio.to_thread(lambda: service.files().update(
                fileId=drive_file_id,
                addParents=can_delete_id,
                removeParents=old_parents,
                body={},
                fields="id",
            ).execute())
        else:
            raise HTTPException(502, f"Drive error: {e}")


@app.post("/import/{drive_file_id}/delete-from-drive")
async def import_delete_from_drive(drive_file_id: str, db: AsyncSession = Depends(get_db)):
    service = get_drive_service()
    await _trash_or_move_to_can_delete(service, drive_file_id)
    queued = (await db.execute(
        select(Document).where(Document.drive_file_id == drive_file_id, Document.group_id.is_(None))
    )).scalar_one_or_none()
    if queued:
        await db.delete(queued)
    await db.commit()
    return RedirectResponse("/import", status_code=303)


@app.post("/document/{document_id}/delete-from-drive")
async def document_delete_from_drive(document_id: int, db: AsyncSession = Depends(get_db)):
    doc = await db.get(Document, document_id)
    if not doc:
        raise HTTPException(404)
    back_url = f"/library/{doc.group_id}/{doc.subgroup_id}" if doc.subgroup_id else f"/library/{doc.group_id}" if doc.group_id else "/library"
    if doc.drive_file_id:
        service = get_drive_service()
        await _trash_or_move_to_can_delete(service, doc.drive_file_id)
    await db.delete(doc)
    await db.commit()
    return RedirectResponse(back_url, status_code=303)


# ---------------------------------------------------------------------------
# 2Do task queue
# ---------------------------------------------------------------------------

async def _queue_2do_task(doc_id: int, title: str, category: str, assignee: str, scan_date: str = ""):
    """Write a task JSON file to the Drive tasks folder for the Mac sync agent to pick up."""
    if not settings.drive_tasks_folder_id:
        return
    import json as _json
    from googleapiclient.http import MediaInMemoryUpload

    list_name = "go with Rob incoming" if assignee == "rob" else "Beccy incoming"
    note_lines = [l for l in [category, f"{settings.app_base_url}/document/{doc_id}", f"Scanned {scan_date}" if scan_date else ""] if l]
    task = {"title": title, "note": "\n".join(note_lines), "list": list_name, "doc_id": doc_id}
    filename = f"scanny_{assignee}_{doc_id}.json"
    content = _json.dumps(task, indent=2).encode()

    def _upload():
        svc = get_drive_service()
        existing = svc.files().list(
            q=f"name='{filename}' and '{settings.drive_tasks_folder_id}' in parents and trashed=false",
            fields="files(id)",
        ).execute().get("files", [])
        for f in existing:
            svc.files().delete(fileId=f["id"]).execute()
        svc.files().create(
            body={"name": filename, "parents": [settings.drive_tasks_folder_id]},
            media_body=MediaInMemoryUpload(content, mimetype="application/json"),
            fields="id",
        ).execute()

    try:
        await asyncio.to_thread(_upload)
    except Exception as e:
        print(f"[2Do queue] doc={doc_id} assignee={assignee}: {e}")


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
    _relink_state.update({"running": True, "checked": 0, "linked": 0, "total": 0, "error": None})
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
                # Exact match first
                matches = files_by_name.get(target, [])
                # Word-overlap fallback: all words from desired must appear in Drive filename
                if not matches:
                    matches = [
                        fid
                        for name, ids in files_by_name.items()
                        if _word_match(target, name)
                        for fid in ids
                    ]
                # If multiple matches, drop already-claimed ones — handles Drive's "(1)" duplicates
                if len(matches) > 1:
                    matches = [f for f in matches if f not in already_used and f not in assigned_this_run]
                if len(matches) == 1:
                    fid = matches[0]
                    if fid not in already_used and fid not in assigned_this_run:
                        doc.drive_file_id = fid
                        doc.broken_link = False
                        assigned_this_run.add(fid)
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
async def relink_status(request: Request, db: AsyncSession = Depends(get_db)):
    # When relink just finished, also return updated broken-links list via OOB swap
    broken: list = []
    refresh = not _relink_state["running"] and bool(_relink_state.get("checked"))
    if refresh and not _link_scan["running"]:
        result = await db.execute(
            select(Document)
            .options(joinedload(Document.group), joinedload(Document.subgroup))
            .where(Document.broken_link == True)
            .order_by(desc(Document.time_file_was_imported))
        )
        broken = result.scalars().all()
    return templates.TemplateResponse("_relink.html", {
        "request": request,
        "relink_state": _relink_state,
        "refresh_broken": refresh,
        "broken_links": broken,
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
    refresh = not _path_scan_state["running"] and bool(_path_scan_state.get("checked"))
    if refresh and not _link_scan["running"]:
        result = await db.execute(
            select(Document)
            .options(joinedload(Document.group), joinedload(Document.subgroup))
            .where(Document.broken_link == True)
            .order_by(desc(Document.time_file_was_imported))
        )
        broken = result.scalars().all()
    return templates.TemplateResponse("_path_scan.html", {
        "request": request,
        "path_scan_state": _path_scan_state,
        "refresh_broken": refresh,
        "broken_links": broken,
        "scan_state": _link_scan,
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
