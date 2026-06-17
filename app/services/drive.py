import os
from typing import Optional
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
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
        creds.refresh(Request())
        _save_credentials(creds)
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
