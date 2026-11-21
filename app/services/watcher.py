import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select, or_
from sqlalchemy.orm import joinedload
from database import AsyncSessionLocal
from models import PendingScan, PendingScanFile, Document, Subgroup, Group
from services.drive import list_files_in_folder, get_credentials
from services.filer import parse_date_from_filename, finalize_pending_scan
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

    async with AsyncSessionLocal() as db:
        # Phase 0 — idle-timeout sweep. Finalize any open session that's gone quiet
        # *before* matching this cycle's files, so a file arriving in the same cycle a
        # session times out can never be wrongly attached to it.
        stale = await db.execute(
            select(PendingScan.id)
            .where(PendingScan.status == "pending")
            .where(PendingScan.last_activity_at < datetime.utcnow() - MATCH_WINDOW)
        )
        for pending_id in stale.scalars().all():
            await finalize_pending_scan(db, pending_id)

        try:
            files = list_files_in_folder(settings.drive_incoming_folder_id)
        except Exception as e:
            logger.error(f"Drive list error: {e}")
            return

        if not files:
            return

        # Phase 1 — the single currently-open session, if any. Only one PendingScan can be
        # status=="pending" at a time (creating or finishing a session always closes the
        # rest), so matching is just "is there an open session, and is this file within its
        # idle window" rather than picking the best of several candidates.
        open_pending = (await db.execute(
            select(PendingScan)
            .options(joinedload(PendingScan.subgroup).joinedload(Subgroup.group))
            .where(PendingScan.status == "pending")
            .order_by(PendingScan.created_at.desc())
            .limit(1)
        )).scalar_one_or_none()

        # Phase 2 — buffer each new matching file against the open session.
        for drive_file in files:
            # Skip non-document file types
            if drive_file.get("mimeType") not in ACCEPTED_MIMETYPES:
                continue

            # Skip files that existed before this service started
            file_created_utc = datetime.fromisoformat(drive_file["createdTime"].replace("Z", "+00:00"))
            if file_created_utc < _service_start:
                continue

            existing_doc = await db.execute(select(Document).where(Document.drive_file_id == drive_file["id"]))
            if existing_doc.scalar_one_or_none():
                continue

            existing_buffered = await db.execute(
                select(PendingScanFile.id).where(PendingScanFile.drive_file_id == drive_file["id"])
            )
            if existing_buffered.scalar_one_or_none():
                continue

            file_created = file_created_utc.replace(tzinfo=None)

            if (
                open_pending and open_pending.subgroup
                and file_created >= open_pending.created_at
                and (file_created - open_pending.last_activity_at) <= MATCH_WINDOW
            ):
                db.add(PendingScanFile(
                    pending_scan_id=open_pending.id,
                    drive_file_id=drive_file["id"],
                    original_filename=drive_file["name"],
                    mime_type=drive_file.get("mimeType"),
                    drive_created_time=file_created,
                ))
                open_pending.last_activity_at = file_created
                await db.commit()
                logger.info(f"Buffered {drive_file['name']} into scan session {open_pending.id}")
            else:
                # No open session to attach to (or none with a subgroup).
                # If the file is still recent, a session may open/resume shortly — retry
                # next poll. Once its own age passes the window with nothing to attach to,
                # queue it for manual filing (Import/Phone picker), same as before.
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
                    logger.debug(f"No open scan session yet for {drive_file['name']} — will retry")


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
