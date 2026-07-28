import os
import time
import json as _json
from typing import Optional
from urllib.parse import urlencode

import httpx

from config import settings

AUTH_URL = "https://api.toodledo.com/3/account/authorize.php"
TOKEN_URL = "https://api.toodledo.com/3/account/token.php"
TASKS_ADD_URL = "https://api.toodledo.com/3/tasks/add.php"
FOLDERS_GET_URL = "https://api.toodledo.com/3/folders/get.php"
FOLDERS_ADD_URL = "https://api.toodledo.com/3/folders/add.php"
SCOPE = "basic tasks write"

_folder_cache: dict[str, int] = {}


def get_authorize_url(state: str) -> str:
    params = {
        "response_type": "code",
        "client_id": settings.toodledo_client_id,
        "state": state,
        "scope": SCOPE,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def _save_token(data: dict) -> None:
    data = dict(data)
    data["obtained_at"] = time.time()
    os.makedirs(os.path.dirname(settings.toodledo_token_file), exist_ok=True)
    with open(settings.toodledo_token_file, "w") as f:
        _json.dump(data, f)


def _load_token() -> Optional[dict]:
    if not os.path.exists(settings.toodledo_token_file):
        return None
    with open(settings.toodledo_token_file) as f:
        return _json.load(f)


def exchange_code(code: str) -> dict:
    resp = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": f"{settings.app_base_url}/toodledo/callback",
        },
        auth=(settings.toodledo_client_id, settings.toodledo_client_secret),
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    _save_token(data)
    return data


def _refresh(token: dict) -> dict:
    resp = httpx.post(
        TOKEN_URL,
        data={"grant_type": "refresh_token", "refresh_token": token["refresh_token"]},
        auth=(settings.toodledo_client_id, settings.toodledo_client_secret),
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    _save_token(data)
    return data


def get_access_token() -> Optional[str]:
    """Return a valid access token, transparently refreshing if it's expired.

    A refresh_token can only be used once (Toodledo invalidates it on use), so
    every refresh must persist the new refresh_token it comes back with.
    """
    token = _load_token()
    if not token:
        return None
    expires_in = token.get("expires_in", 3600)
    obtained_at = token.get("obtained_at", 0)
    if time.time() > obtained_at + expires_in - 60:
        try:
            token = _refresh(token)
        except Exception:
            return None
    return token.get("access_token")


def is_connected() -> bool:
    return get_access_token() is not None


def get_or_create_folder(name: str) -> int:
    """Return the id of a Toodledo folder by name, creating it if it doesn't exist yet.

    2Do maps Toodledo folders to its own Lists, so this is how a task ends up
    filed under e.g. "go with Rob incoming" instead of the default list.
    """
    if name in _folder_cache:
        return _folder_cache[name]
    access_token = get_access_token()
    if not access_token:
        raise ValueError("Not authenticated with Toodledo")

    resp = httpx.get(FOLDERS_GET_URL, params={"access_token": access_token}, timeout=15)
    resp.raise_for_status()
    folders = resp.json()
    if isinstance(folders, list):
        for f in folders:
            _folder_cache[f["name"]] = f["id"]
    if name in _folder_cache:
        return _folder_cache[name]

    resp = httpx.post(FOLDERS_ADD_URL, data={"access_token": access_token, "name": name}, timeout=15)
    resp.raise_for_status()
    result = resp.json()
    if isinstance(result, dict) and "errorDesc" in result:
        raise ValueError(f"Toodledo error: {result['errorDesc']}")
    folder_id = result[0]["id"]
    _folder_cache[name] = folder_id
    return folder_id


def create_task(title: str, note: str = "", folder_id: Optional[int] = None) -> dict:
    access_token = get_access_token()
    if not access_token:
        raise ValueError("Not authenticated with Toodledo")
    task = {"title": title, "note": note}
    if folder_id is not None:
        task["folder"] = folder_id
    resp = httpx.post(
        TASKS_ADD_URL,
        data={"access_token": access_token, "tasks": _json.dumps([task])},
        timeout=15,
    )
    resp.raise_for_status()
    result = resp.json()
    if isinstance(result, dict) and "errorDesc" in result:
        raise ValueError(f"Toodledo error: {result['errorDesc']}")
    return result
