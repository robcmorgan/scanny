import asyncio
import io
import logging
import re
from datetime import datetime, date
from typing import Optional

import fitz
import pillow_heif
from PIL import Image, ImageOps
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from services.drive import (
    get_or_create_folder, move_and_rename_file, list_files_in_folder,
    download_file_bytes, upload_file_bytes, trash_or_move_to_can_delete,
)
from config import settings

pillow_heif.register_heif_opener()  # lets PIL.Image.open() decode HEIC alongside JPEG/PNG/TIFF

logger = logging.getLogger(__name__)


_DATE_PATTERNS = [
    r'(\d{4})[_\-](\d{2})[_\-](\d{2})[_\-T](\d{2})(\d{2})',  # 2026-12-01_1149
    r'(\d{4})(\d{2})(\d{2})[_\-T](\d{2})(\d{2})(\d{2})',       # 20261201_114900
    r'(\d{4})(\d{2})(\d{2})[_\-T](\d{2})(\d{2})',               # 20261201_1149
]


def extract_drive_prefix(filename: str) -> str:
    """Extract the human-meaningful part of a Drive filename for use as description.

    'Cheese Flyer Scanned_20260622-1305.pdf' → 'Cheese Flyer'
    'David Death Certificate.pdf'            → 'David Death Certificate'
    'Scanned_20260622-1305.pdf'              → ''
    """
    stem = filename.rsplit(".", 1)[0]
    # Google Drive auto-format with prefix: extract only the prefix
    m = re.match(r'^(.+?)\s+Scanned_\d{8}', stem, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # Pure auto-name with no human prefix — nothing useful to extract
    if re.search(r'Scanned_\d{8}', stem, re.IGNORECASE):
        return ""
    # Manually named file — the whole stem is the description
    return stem.strip()


def parse_date_from_filename(filename: str) -> Optional[datetime]:
    base = filename.rsplit(".", 1)[0]
    for pattern in _DATE_PATTERNS:
        m = re.search(pattern, base)
        if m:
            g = m.groups()
            try:
                return datetime(int(g[0]), int(g[1]), int(g[2]), int(g[3]), int(g[4]))
            except ValueError:
                continue
    return None


def build_filing_info(
    group_name: str,
    subgroup_name: str,
    filename_tag: Optional[str],
    custom_path: Optional[str],
    include_group_in_path: bool,
    scan_datetime: datetime,
    override_filename: Optional[str],
    ext: str,
) -> tuple[str, str]:
    """Return (folder_path, filename) for a document."""
    if filename_tag:
        label = filename_tag
    elif subgroup_name == "/":
        label = group_name
    else:
        label = subgroup_name

    date_str = scan_datetime.strftime("%Y-%m-%d")
    time_str = scan_datetime.strftime("%H%M%S")
    if override_filename:
        override_filename = re.sub(r'[\x00-\x1f\x7f]', '', override_filename).strip()
        stem = override_filename.rsplit(".", 1)[0] if "." in override_filename else override_filename
        filename = f"{stem}.{ext}"
    else:
        filename = f"{date_str} {label} scanned papers-{time_str}.{ext}"

    if custom_path:
        folder = custom_path.rstrip("/")
    else:
        folder = f"/{group_name}/{subgroup_name}"

    return folder, filename


def dedupe_filename(filename: str, existing_names: set[str]) -> str:
    """Disambiguate filename against a set of names already in the destination folder.

    Appends " (2)" on first collision, " (3)" on the next, etc. If filename
    already ends in " (N)", that suffix is replaced rather than stacked
    another one on top — filing the same name a third time produces
    "Foo (3).pdf", never "Foo (2) (2).pdf".
    """
    if filename not in existing_names:
        return filename
    stem, ext = filename.rsplit(".", 1) if "." in filename else (filename, "")
    m = re.match(r'^(.*) \((\d+)\)$', stem)
    base, n = (m.group(1), int(m.group(2)) + 1) if m else (stem, 2)
    while True:
        candidate = f"{base} ({n})." + ext if ext else f"{base} ({n})"
        if candidate not in existing_names:
            return candidate
        n += 1


def file_document(
    drive_file: dict,
    group_name: str,
    subgroup_name: str,
    filename_tag: Optional[str],
    custom_path: Optional[str],
    include_group_in_path: bool,
    date_override: Optional[date],
    override_filename: Optional[str],
    source_folder_id: Optional[str] = None,
) -> tuple[str, str, str]:
    """Move and rename a Drive file. Returns (folder_path, filename, new_parent_folder_id)."""
    scan_dt = parse_date_from_filename(drive_file["name"]) or datetime.utcnow()
    if date_override:
        scan_dt = datetime(date_override.year, date_override.month, date_override.day,
                           scan_dt.hour, scan_dt.minute)

    ext = drive_file["name"].rsplit(".", 1)[-1].lower() if "." in drive_file["name"] else "pdf"
    folder_path, filename = build_filing_info(
        group_name, subgroup_name, filename_tag, custom_path,
        include_group_in_path, scan_dt, override_filename, ext
    )

    # Build (or find) the folder hierarchy in Drive
    parent_id = settings.drive_filed_root_folder_id
    for part in [p for p in folder_path.strip("/").split("/") if p]:
        parent_id = get_or_create_folder(parent_id, part)

    existing_names = {f["name"] for f in list_files_in_folder(parent_id) if f["id"] != drive_file["id"]}
    filename = dedupe_filename(filename, existing_names)

    old_parent = source_folder_id or settings.drive_incoming_folder_id
    move_and_rename_file(drive_file["id"], filename, parent_id, old_parent)
    return folder_path, filename, parent_id


def _merge_to_pdf(files: list[tuple[bytes, str, str]]) -> bytes:
    """Merge ordered (content, mime_type, filename) tuples into one PDF's bytes.

    Existing PDF pages are copied in as-is; images (JPEG/PNG/TIFF/HEIC) each become
    one page, sized to the image's own DPI so the page doesn't come out stretched.
    """
    out = fitz.open()
    for data, mime, name in files:
        if mime == "application/pdf" or name.lower().endswith(".pdf"):
            out.insert_pdf(fitz.open(stream=data, filetype="pdf"))
        else:
            img = Image.open(io.BytesIO(data))
            img = ImageOps.exif_transpose(img).convert("RGB")  # fitz doesn't apply EXIF rotation itself
            dpi = img.info.get("dpi") or (200, 200)
            dpi_x = dpi[0] or 200
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=90)
            w_pt, h_pt = img.width / dpi_x * 72, img.height / dpi_x * 72
            page = out.new_page(width=w_pt, height=h_pt)
            page.insert_image(page.rect, stream=buf.getvalue())
    merged = out.tobytes()
    out.close()
    return merged


async def finalize_pending_scan(db: AsyncSession, pending_id: int) -> None:
    """Close out an open scan session: merge every buffered file into one PDF and file it
    as a single Document. Called from the watcher's idle sweep, the "Finish scanning"
    route, and when a new PendingScan is created (which auto-closes any other open one).

    Safe to call more than once for the same session — a session already resolved
    (not status=="pending") is a no-op, guarding against a race between two triggers.
    """
    from models import PendingScan, Subgroup, Document  # local import avoids a circular import at module load

    result = await db.execute(
        select(PendingScan)
        .options(joinedload(PendingScan.subgroup).joinedload(Subgroup.group), selectinload(PendingScan.files))
        .where(PendingScan.id == pending_id)
    )
    pending = result.unique().scalar_one_or_none()
    if not pending or pending.status != "pending":
        return

    files = pending.files
    if not files:
        pending.status = "closed"
        await db.commit()
        return

    if not pending.subgroup:
        # Shouldn't happen (the confirm form requires a subgroup before a PendingScan can be
        # created) but guard rather than silently discarding buffered files.
        pending.status = "error"
        await db.commit()
        logger.error(f"PendingScan {pending_id} finalized with no subgroup — files left buffered")
        return

    subgroup = pending.subgroup
    group = subgroup.group

    try:
        downloaded = []
        for f in files:
            content, content_type = await download_file_bytes(f.drive_file_id)
            downloaded.append((content, f.mime_type or content_type, f.original_filename))

        merged_bytes = await asyncio.to_thread(_merge_to_pdf, downloaded)

        # Keep the first file's stem (scanner-generated names embed the scan date/time,
        # which parse_date_from_filename()/file_document() rely on) but force a .pdf extension.
        first_stem = files[0].original_filename.rsplit(".", 1)[0]
        merged_name = f"{first_stem}.pdf"

        uploaded = await asyncio.to_thread(
            upload_file_bytes, merged_name, merged_bytes, "application/pdf", settings.drive_incoming_folder_id
        )

        scan_dt = parse_date_from_filename(merged_name) or files[0].drive_created_time
        folder_path, filename, _ = file_document(
            drive_file=uploaded,
            group_name=group.name,
            subgroup_name=subgroup.name,
            filename_tag=subgroup.filename_tag,
            custom_path=subgroup.custom_path,
            include_group_in_path=group.include_in_path,
            date_override=pending.date_override,
            override_filename=pending.override_filename,
        )

        # Only clean up originals after the merged PDF is safely filed — if anything above
        # raised, every source file is still sitting untouched in the incoming folder.
        for f in files:
            await trash_or_move_to_can_delete(f.drive_file_id)

        doc = Document(
            drive_file_id=uploaded["id"],
            file_name_when_imported=merged_name,
            timestamp_from_filename=scan_dt,
            group_id=group.id,
            subgroup_id=subgroup.id,
            pending_scan_id=pending.id,
            assigned_item_one_off_description=pending.item_one_off_description,
            assigned_comment=pending.comment,
            assigned_rob_task=pending.rob_task,
            assigned_beccy_task=pending.beccy_task,
            assigned_task_note=pending.task_note,
            assigned_date_override=pending.date_override,
            photo_quality=pending.photo_quality,
            desired_folder_location=folder_path,
            desired_filename=filename,
            desired_full_path=f"{folder_path}/{filename}",
            current_full_path=f"{folder_path}/{filename}",
            moved_and_renamed_timestamp=datetime.utcnow(),
        )
        db.add(doc)
        pending.status = "filed"
        subgroup.use_count += 1
        subgroup.last_used = datetime.utcnow()
        for f in files:
            await db.delete(f)
        await db.commit()
        logger.info(f"Filed merged scan session {pending_id} ({len(files)} page(s)) → {folder_path}/{filename}")

        try:
            from main import _fetch_and_cache_thumbnail
            await _fetch_and_cache_thumbnail(uploaded["id"])
        except Exception:
            pass

    except Exception as e:
        logger.error(f"Failed to finalize pending scan {pending_id}: {e}")
        pending.status = "error"
        await db.commit()
