import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select, or_
from sqlalchemy.orm import joinedload
from database import AsyncSessionLocal
from models import PendingScan, Document, Subgroup, Group
from services.drive import list_files_in_folder, get_credentials
from services.filer import parse_date_from_filename, file_document
import services.toodledo as toodledo
from config import settings

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()

ACCEPTED_MIMETYPES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/tiff",
    "image/heic",
}

# Only process files created after the service started
_service_start = datetime.now(timezone.utc)

# A pending scan must have been created within this window before the file arrived
MATCH_WINDOW = timedelta(minutes=10)


async def poll_drive():
    if not get_credentials() or not settings.drive_incoming_folder_id:
        return

    try:
        files = list_files_in_folder(settings.drive_incoming_folder_id)
    except Exception as e:
        logger.error(f"Drive list error: {e}")
        return

    if not files:
        return

    async with AsyncSessionLocal() as db:
        for drive_file in files:
            # Skip non-document file types
            if drive_file.get("mimeType") not in ACCEPTED_MIMETYPES:
                continue

            # Skip files that existed before this service started
            file_created_utc = datetime.fromisoformat(drive_file["createdTime"].replace("Z", "+00:00"))
            if file_created_utc < _service_start:
                continue

            existing = await db.execute(select(Document).where(Document.drive_file_id == drive_file["id"]))
            if existing.scalar_one_or_none():
                continue

            file_created = file_created_utc.replace(tzinfo=None)

            result = await db.execute(
                select(PendingScan)
                .options(joinedload(PendingScan.subgroup).joinedload(Subgroup.group))
                .where(PendingScan.status == "pending")
                .where(PendingScan.created_at <= file_created)
                .where(PendingScan.created_at >= file_created - MATCH_WINDOW)
                .order_by(PendingScan.created_at.desc())
                .limit(1)
            )
            pending = result.scalar_one_or_none()

            if pending and pending.subgroup:
                subgroup = pending.subgroup
                group = subgroup.group
                try:
                    scan_dt = parse_date_from_filename(drive_file["name"]) or file_created
                    folder_path, filename, _ = file_document(
                        drive_file=drive_file,
                        group_name=group.name,
                        subgroup_name=subgroup.name,
                        filename_tag=subgroup.filename_tag,
                        custom_path=subgroup.custom_path,
                        include_group_in_path=group.include_in_path,
                        date_override=pending.date_override,
                        override_filename=pending.override_filename,
                    )

                    doc = Document(
                        drive_file_id=drive_file["id"],
                        file_name_when_imported=drive_file["name"],
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
                    await db.commit()
                    logger.info(f"Filed {drive_file['name']} → {folder_path}/{filename}")
                    try:
                        from main import _fetch_and_cache_thumbnail
                        await _fetch_and_cache_thumbnail(drive_file["id"])
                    except Exception:
                        pass
                except Exception as e:
                    logger.error(f"Failed to file {drive_file['name']}: {e}")
                    pending.status = "error"
                    await db.commit()
            else:
                # No matching pending scan within window.
                # If file is recent (still within window), skip — a pending scan may arrive soon.
                # Once the window has passed with no match, queue it for manual filing.
                age = datetime.utcnow() - file_created
                if age > MATCH_WINDOW:
                    scan_dt = parse_date_from_filename(drive_file["name"]) or file_created
                    doc = Document(
                        drive_file_id=drive_file["id"],
                        file_name_when_imported=drive_file["name"],
                        timestamp_from_filename=scan_dt,
                    )
                    db.add(doc)
                    await db.commit()
                    logger.info(f"No match within window for {drive_file['name']} — queued for manual filing")
                else:
                    logger.debug(f"No pending scan yet for {drive_file['name']} — will retry")


TOODLEDO_LOOKBACK_SECONDS = 6 * 3600  # tolerate downtime between polls without needing a persisted cursor


async def poll_toodledo_completions():
    if not toodledo.is_connected():
        return

    try:
        after_ts = int(time.time()) - TOODLEDO_LOOKBACK_SECONDS
        completed = await asyncio.to_thread(toodledo.get_completed_tasks, after_ts)
    except Exception as e:
        logger.error(f"Toodledo completion poll error: {e}")
        return
    if not completed:
        return

    completed_ids = {t["id"] for t in completed}

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Document).where(
                or_(
                    Document.toodledo_rob_task_id.in_(completed_ids),
                    Document.toodledo_beccy_task_id.in_(completed_ids),
                )
            )
        )
        docs = result.scalars().all()
        for doc in docs:
            if doc.toodledo_rob_task_id in completed_ids:
                doc.assigned_rob_task = False
                doc.toodledo_rob_task_id = None
            if doc.toodledo_beccy_task_id in completed_ids:
                doc.assigned_beccy_task = False
                doc.toodledo_beccy_task_id = None
        if docs:
            await db.commit()
            logger.info(f"Cleared {len(docs)} to-do(s) completed in 2Do")


def start_scheduler():
    scheduler.add_job(poll_drive, "interval", seconds=settings.poll_interval_seconds, id="drive_poll")
    scheduler.add_job(poll_toodledo_completions, "interval", seconds=settings.poll_interval_seconds, id="toodledo_poll")
    scheduler.start()


def stop_scheduler():
    scheduler.shutdown(wait=False)
