#!/usr/bin/env python3
"""
Scanny → 2Do task sync agent.

Polls a Google Drive folder (synced locally by Drive for Desktop) for
JSON task files written by Scanny. For each file it creates a task in
2Do via AppleScript, then deletes the file.

Setup:
  1. Create a folder named '_scanny_tasks' in My Drive root.
  2. Copy its Drive folder ID into .env: DRIVE_TASKS_FOLDER_ID=...
  3. Restart Scanny.
  4. Copy this script somewhere permanent, e.g.:
       mkdir -p ~/Library/Application\ Support/Scanny
       cp scanny_todo_sync.py ~/Library/Application\ Support/Scanny/
  5. Edit com.scanny.todosync.plist — replace SCRIPT_PATH with the full
     path you chose above.
  6. Install the launch agent:
       cp com.scanny.todosync.plist ~/Library/LaunchAgents/
       launchctl load ~/Library/LaunchAgents/com.scanny.todosync.plist
  7. Test:
       python3 ~/Library/Application\ Support/Scanny/scanny_todo_sync.py

Logs: /tmp/scanny_todo_sync.out  /tmp/scanny_todo_sync.err
"""
import json
import os
import glob
import subprocess
import sys

TASKS_SUBDIR = "_scanny_tasks"


def find_tasks_dir():
    base = os.path.expanduser("~/Library/CloudStorage")
    # Modern Google Drive for Desktop
    matches = glob.glob(os.path.join(base, "GoogleDrive-*", "My Drive", TASKS_SUBDIR))
    if matches:
        return matches[0]
    # Older Backup and Sync
    candidate = os.path.expanduser(f"~/Google Drive/{TASKS_SUBDIR}")
    if os.path.isdir(candidate):
        return candidate
    return None


def esc(s: str) -> str:
    """Escape a string for embedding inside an AppleScript double-quoted literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def create_2do_task(title: str, note: str, list_name: str):
    script = (
        f'tell application "2Do"\n'
        f'  set taskList to first list whose name is "{esc(list_name)}"\n'
        f'  make new task at end of tasks of taskList'
        f' with properties {{title:"{esc(title)}", note:"{esc(note)}"}}\n'
        f'end tell'
    )
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"osascript exit {result.returncode}")


def main():
    tasks_dir = find_tasks_dir()
    if not tasks_dir:
        print(
            f"Tasks folder '{TASKS_SUBDIR}' not found in Google Drive. "
            "Is Drive for Desktop running and synced?",
            file=sys.stderr,
        )
        return

    files = sorted(glob.glob(os.path.join(tasks_dir, "scanny_*.json")))
    if not files:
        return

    for f in files:
        try:
            with open(f) as fp:
                task = json.load(fp)
            title = task.get("title") or "Scanny task"
            note = task.get("note") or ""
            list_name = task.get("list") or "go with Rob incoming"
            create_2do_task(title, note, list_name)
            os.remove(f)
            print(f"Created 2Do task: {title!r} → {list_name!r}")
        except Exception as e:
            print(f"Error processing {os.path.basename(f)}: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
