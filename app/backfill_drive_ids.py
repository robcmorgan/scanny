"""
One-time script: walk the filed root Drive folder recursively and
update drive_file_id on Document records that are missing it,
matching by the current filename (last segment of current_full_path).
"""
import asyncio
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))

from sqlalchemy import select
from database import AsyncSessionLocal, init_db
from models import Document
from services.drive import get_drive_service
from config import settings


def list_all_files_recursive(service, folder_id: str) -> dict[str, str]:
    """Return {filename: file_id} for every non-folder file under folder_id."""
    result = {}
    page_token = None
    while True:
        params = dict(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="nextPageToken, files(id, name, mimeType)",
            pageSize=1000,
        )
        if page_token:
            params["pageToken"] = page_token
        resp = service.files().list(**params).execute()
        for f in resp.get("files", []):
            if f["mimeType"] == "application/vnd.google-apps.folder":
                # Recurse into subfolder
                sub = list_all_files_recursive(service, f["id"])
                result.update(sub)
            else:
                name = f["name"]
                if name in result:
                    print(f"  ⚠ duplicate filename in Drive: {name}")
                else:
                    result[name] = f["id"]
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return result


async def main():
    await init_db()
    print("Walking Drive folder tree…")
    service = get_drive_service()
    drive_map = list_all_files_recursive(service, settings.drive_filed_root_folder_id)
    print(f"Found {len(drive_map)} files in Drive")

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Document).where(Document.drive_file_id.is_(None))
        )
        missing = result.scalars().all()
        print(f"{len(missing)} documents missing drive_file_id")

        updated = 0
        not_found = 0
        for doc in missing:
            # The filed filename is the last segment of current_full_path
            if not doc.current_full_path:
                not_found += 1
                continue
            filed_name = doc.current_full_path.rstrip("/").split("/")[-1]
            if filed_name in drive_map:
                doc.drive_file_id = drive_map[filed_name]
                updated += 1
            else:
                not_found += 1
                if not_found <= 10:
                    print(f"  Not found in Drive: {filed_name}")

        await db.commit()
        print(f"\nUpdated {updated} documents")
        print(f"Still missing: {not_found}")


asyncio.run(main())
