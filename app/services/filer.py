import re
from datetime import datetime, date
from typing import Optional
from services.drive import get_or_create_folder, move_and_rename_file, list_files_in_folder
from config import settings


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
