from contextlib import asynccontextmanager
from typing import Optional
from datetime import datetime

from fastapi import FastAPI, Request, Depends, HTTPException, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from sqlalchemy.orm import joinedload

from database import get_db, init_db
from models import Group, Subgroup, PendingScan, Document
from services.drive import get_flow, save_credentials_from_flow, get_credentials
from services.watcher import start_scheduler, stop_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(lifespan=lifespan, title="Scanny")
templates = Jinja2Templates(directory="templates")


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
    from sqlalchemy import func

    # 5 most recently used groups (ordered by max last_used of their subgroups, then alphabetical)
    subq = (
        select(Subgroup.group_id, func.max(Subgroup.last_used).label("max_last_used"))
        .group_by(Subgroup.group_id)
        .subquery()
    )
    result = await db.execute(
        select(Group)
        .outerjoin(subq, Group.id == subq.c.group_id)
        .order_by(desc(subq.c.max_last_used), Group.name)
        .limit(5)
    )
    recent_groups = result.scalars().all()

    # Active pending scans
    result = await db.execute(
        select(PendingScan)
        .options(joinedload(PendingScan.subgroup).joinedload(Subgroup.group))
        .where(PendingScan.status == "pending")
        .order_by(desc(PendingScan.created_at))
        .limit(5)
    )
    pending_scans = result.scalars().all()

    return templates.TemplateResponse("scan.html", {
        "request": request,
        "recent_groups": recent_groups,
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
    return templates.TemplateResponse("_subgroup_popup.html", {
        "request": request,
        "group": group,
        "subgroups": subgroups,
    })


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
    if q:
        stmt = stmt.where(Subgroup.name.ilike(f"%{q}%") | Subgroup.group.has(Group.name.ilike(f"%{q}%")))
    stmt = stmt.order_by(desc(Subgroup.last_used), Subgroup.name)
    if not all:
        stmt = stmt.limit(12)
    result = await db.execute(stmt)
    subgroups = result.scalars().all()

    # If called from the popup (group_id set), return popup-style list
    if group_id:
        group = await db.get(Group, group_id)
        return templates.TemplateResponse("_subgroup_popup.html", {
            "request": request,
            "group": group,
            "subgroups": subgroups,
        })
    return templates.TemplateResponse("_subgroup_list.html", {
        "request": request,
        "subgroups": subgroups,
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
    return templates.TemplateResponse("library.html", {
        "request": request,
        "active": "library",
    })


@app.get("/tasks", response_class=HTMLResponse)
async def tasks(request: Request):
    return templates.TemplateResponse("tasks.html", {
        "request": request,
        "active": "tasks",
    })


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
