import asyncio
import io
import os
from typing import Optional
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload
from config import settings

SCOPES = ["https://www.googleapis.com/auth/drive"]


def get_flow() -> Flow:
    return Flow.from_client_secrets_file(
        settings.google_client_secret_file,
        scopes=SCOPES,
        redirect_uri=f"{settings.app_base_url}/drive/callback",
    )


def get_credentials() -> Optional[Credentials]:
    if not os.path.exists(settings.google_token_file):
        return None
    creds = Credentials.from_authorized_user_file(settings.google_token_file, SCOPES)
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_credentials(creds)
        except Exception:
            return None
    return creds if creds and creds.valid else None


def _save_credentials(creds: Credentials):
    os.makedirs(os.path.dirname(settings.google_token_file), exist_ok=True)
    with open(settings.google_token_file, "w") as f:
        f.write(creds.to_json())


def save_credentials_from_flow(flow: Flow, code: str) -> Credentials:
    flow.fetch_token(code=code)
    _save_credentials(flow.credentials)
    return flow.credentials


def get_drive_service():
    creds = get_credentials()
    if not creds:
        raise ValueError("Not authenticated with Google Drive")
    return build("drive", "v3", credentials=creds)


def list_files_in_folder(folder_id: str) -> list[dict]:
    service = get_drive_service()
    query = f"'{folder_id}' in parents and trashed=false and mimeType!='application/vnd.google-apps.folder'"
    result = service.files().list(
        q=query,
        fields="files(id, name, createdTime, mimeType)",
        orderBy="createdTime",
    ).execute()
    return result.get("files", [])


def get_or_create_folder(parent_id: str, name: str) -> str:
    service = get_drive_service()
    query = (
        f"'{parent_id}' in parents and name='{name}' "
        f"and mimeType='application/vnd.google-apps.folder' and trashed=false"
    )
    result = service.files().list(q=query, fields="files(id)").execute()
    files = result.get("files", [])
    if files:
        return files[0]["id"]
    folder = service.files().create(
        body={"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]},
        fields="id",
    ).execute()
    return folder["id"]


def move_and_rename_file(file_id: str, new_name: str, new_parent_id: str, old_parent_id: str):
    service = get_drive_service()
    service.files().update(
        fileId=file_id,
        addParents=new_parent_id,
        removeParents=old_parent_id,
        body={"name": new_name},
        fields="id, name",
    ).execute()


async def download_file_bytes(drive_file_id: str) -> tuple[bytes, str]:
    """Fetch a Drive file's raw content. Raises HTTPException(404) if the file doesn't exist."""
    import httpx
    from fastapi import HTTPException
    creds = get_credentials()
    url = f"https://www.googleapis.com/drive/v3/files/{drive_file_id}?alt=media"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers={"Authorization": f"Bearer {creds.token}"}, follow_redirects=True)
    if resp.status_code == 404:
        raise HTTPException(404)
    return resp.content, resp.headers.get("content-type", "application/pdf")


def upload_file_bytes(name: str, content: bytes, mime_type: str, parent_id: str) -> dict:
    """Upload new file content into a Drive folder. Returns the created file resource dict."""
    service = get_drive_service()
    media = MediaIoBaseUpload(io.BytesIO(content), mimetype=mime_type, resumable=False)
    return service.files().create(
        body={"name": name, "parents": [parent_id]},
        media_body=media,
        fields="id, name, createdTime, mimeType",
    ).execute()


async def trash_or_move_to_can_delete(drive_file_id: str):
    """Trash a Drive file. If not owner (403), move it to a root-level 'can delete' folder instead."""
    from fastapi import HTTPException
    service = get_drive_service()
    try:
        await asyncio.to_thread(lambda: service.files().update(fileId=drive_file_id, body={"trashed": True}).execute())
    except HttpError as e:
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
