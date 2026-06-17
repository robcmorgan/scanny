import re
from datetime import datetime, date
from typing import Optional
from services.drive import get_or_create_folder, move_and_rename_file
from config import settings


_DATE_PATTERNS = [
    r'(\d{4})[_\-](\d{2})[_\-](\d{2})[_\-T](\d{2})(\d{2})',  # 2026-12-01_1149
    r'(\d{4})(\d{2})(\d{2})[_\-T](\d{2})(\d{2})(\d{2})',       # 20261201_114900
    r'(\d{4})(\d{2})(\d{2})[_\-T](\d{2})(\d{2})',               # 20261201_1149
]


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
    tag = filename_tag or subgroup_name

    if override_filename:
        filename = override_filename if "." in override_filename else f"{override_filename}.{ext}"
    else:
        date_str = scan_datetime.strftime("%Y-%m-%d")
        time_str = scan_datetime.strftime("%H%M")
        label = f"{tag} {group_name}" if include_group_in_path else tag
        filename = f"{date_str} {time_str} {label}.{ext}"

    if custom_path:
        folder = custom_path.rstrip("/")
    else:
        folder = f"/{group_name}/{subgroup_name}"

    return folder, filename


def file_document(
    drive_file: dict,
    group_name: str,
    subgroup_name: str,
    filename_tag: Optional[str],
    custom_path: Optional[str],
    include_group_in_path: bool,
    date_override: Optional[date],
    override_filename: Optional[str],
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

    move_and_rename_file(drive_file["id"], filename, parent_id, settings.drive_incoming_folder_id)
    return folder_path, filename, parent_id
