"""
Walk the entire filed Drive folder tree and refresh drive_file_id on all
Document records — including ones with stale IDs. Matches by full path first,
then by filename alone as a fallback.
"""
import os, sys, sqlite3
sys.path.insert(0, os.path.dirname(__file__))

from services.drive import get_drive_service, get_credentials
from config import settings


def walk_drive(service, folder_id: str, path_prefix: str = "") -> tuple[dict, dict]:
    """
    Returns:
      path_map:  {'/Group/Subgroup/filename.pdf': file_id}
      name_map:  {'filename.pdf': [file_id, ...]}
    """
    path_map, name_map = {}, {}
    page_token = None
    while True:
        kwargs = dict(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="nextPageToken, files(id, name, mimeType)",
            pageSize=1000,
        )
        if page_token:
            kwargs["pageToken"] = page_token
        resp = service.files().list(**kwargs).execute()
        for f in resp.get("files", []):
            full_path = f"{path_prefix}/{f['name']}"
            if f["mimeType"] == "application/vnd.google-apps.folder":
                sp, sn = walk_drive(service, f["id"], full_path)
                path_map.update(sp)
                for k, v in sn.items():
                    name_map.setdefault(k, []).extend(v)
            else:
                path_map[full_path] = f["id"]
                name_map.setdefault(f["name"], []).append(f["id"])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return path_map, name_map


def validate_id(service, file_id: str) -> bool:
    try:
        service.files().get(fileId=file_id, fields="id").execute()
        return True
    except Exception:
        return False


def main():
    service = get_drive_service()

    print("Walking Drive folder tree from filed root…")
    path_map, name_map = walk_drive(service, settings.drive_filed_root_folder_id)
    print(f"  Filed root: {len(path_map)} files")

    # Also walk My Drive root to catch files outside the filed root folder
    print("Walking My Drive root for stray files…")
    root_path_map, root_name_map = walk_drive(service, "root")
    # Merge — filed root results take precedence; only add truly new file IDs from root walk
    known_fids = set(path_map.values())
    for p, fid in root_path_map.items():
        if p not in path_map:
            path_map[p] = fid
    for name, ids in root_name_map.items():
        for fid in ids:
            if fid not in known_fids:
                name_map.setdefault(name, []).append(fid)
                known_fids.add(fid)
    print(f"  Combined: {len(path_map)} files across {len(name_map)} unique filenames")

    dup_names = {k for k, v in name_map.items() if len(v) > 1}
    if dup_names:
        print(f"  {len(dup_names)} duplicate filenames (path-match only for these)")

    db = sqlite3.connect("scanny.db")
    docs = db.execute("""
        SELECT d.id, d.drive_file_id, d.current_full_path, d.desired_full_path,
               d.desired_filename, d.file_name_when_imported
        FROM documents d
    """).fetchall()
    print(f"{len(docs)} total documents in DB")

    # Pre-load which Drive IDs are already claimed in the DB so we don't double-assign
    claimed: dict[str, int] = {}  # drive_file_id -> doc_id
    for doc_id, fid, *_ in docs:
        if fid:
            claimed[fid] = doc_id

    refreshed = newly_found = cleared = no_match = 0

    MAC_PREFIX = "/Users/robmorgan/Library/CloudStorage/GoogleDrive-robcmorgan@gmail.com/My Drive"
    ROOT_NAME = "documents"  # name of DRIVE_FILED_ROOT_FOLDER_ID

    def normalise_paths(p: str) -> list[str]:
        """Return Drive-relative path variants for a stored (possibly Mac-prefixed) path."""
        p = p.strip()
        if p.startswith(MAC_PREFIX):
            p = p[len(MAC_PREFIX):]
        # p is now My-Drive-relative, e.g. '/Beccy/Pay/file.pdf' or '/documents/Domestic/file.pdf'
        variants = [p]
        # Also try without the leading root folder name (matches filed-root-relative paths)
        if p.startswith(f"/{ROOT_NAME}/"):
            variants.append(p[len(ROOT_NAME) + 1:])
        return variants

    for doc_id, current_fid, cur_path, des_path, des_fname, orig_fname in docs:
        # Build candidate lookup keys
        path_candidates, name_candidates = [], []
        for p in [cur_path, des_path]:
            if p:
                path_candidates.extend(normalise_paths(p))
                name_candidates.append(p.rstrip("/").split("/")[-1])
        for f in [des_fname, orig_fname]:
            if f:
                name_candidates.append(f.rstrip("/").split("/")[-1])

        new_id = None

        # 1. Try path match
        for p in path_candidates:
            if p in path_map:
                candidate = path_map[p]
                if candidate not in claimed or claimed[candidate] == doc_id:
                    new_id = candidate
                    break

        # 2. Fall back to filename (unique names only)
        if not new_id:
            for n in name_candidates:
                if n in name_map and n not in dup_names:
                    ids = [i for i in name_map[n] if i not in claimed or claimed[i] == doc_id]
                    if len(ids) == 1:
                        new_id = ids[0]
                        break

        if new_id:
            if new_id != current_fid:
                # Release old claim, take new one
                if current_fid and current_fid in claimed and claimed[current_fid] == doc_id:
                    del claimed[current_fid]
                db.execute("UPDATE documents SET drive_file_id=? WHERE id=?", (new_id, doc_id))
                claimed[new_id] = doc_id
                if current_fid:
                    refreshed += 1
                    print(f"  REFRESH doc={doc_id} {orig_fname}")
                else:
                    newly_found += 1
            # else: already correct, nothing to do
        else:
            # Validate existing ID if present
            if current_fid:
                if not validate_id(service, current_fid):
                    print(f"  CLEAR stale doc={doc_id} {orig_fname}")
                    db.execute("UPDATE documents SET drive_file_id=NULL WHERE id=?", (doc_id,))
                    del claimed[current_fid]
                    cleared += 1
            else:
                no_match += 1

    db.commit()
    db.close()

    print(f"\nResults:")
    print(f"  {newly_found} newly matched")
    print(f"  {refreshed} IDs refreshed")
    print(f"  {cleared} stale IDs cleared")
    print(f"  {no_match} still unmatched (no Drive file found)")


main()
