"""One-time script: populate Subgroup.filename_tag from 'String from tag' in Subgroups.xlsx."""
import sqlite3
import openpyxl

XLSX = "../filemaker-import/export filemaker/Subgroups.xlsx"
DB = "scanny.db"

wb = openpyxl.load_workbook(XLSX)
ws = wb.active
headers = [c.value for c in ws[1]]

rows = [dict(zip(headers, r)) for r in ws.iter_rows(min_row=2, values_only=True)]

# Build lookup: (group_name, subgroup_name) -> string_from_tag
lookup = {}
for d in rows:
    gname = d.get("Groups::Group name")
    sname = d.get("Subgroup name")
    sft = d.get("String from tag")
    if gname and sname and sft:
        lookup[(gname, sname)] = sft

conn = sqlite3.connect(DB)
cur = conn.cursor()

subgroups = cur.execute("""
    SELECT s.id, g.name, s.name, s.filename_tag
    FROM subgroups s JOIN groups g ON g.id = s.group_id
""").fetchall()

updated = 0
skipped = 0
for sid, gname, sname, current_tag in subgroups:
    sft = lookup.get((gname, sname))
    if sft and sft != current_tag:
        cur.execute("UPDATE subgroups SET filename_tag=? WHERE id=?", (sft, sid))
        print(f"  SET {gname!r}/{sname!r} -> {sft!r}")
        updated += 1
    elif not sft:
        skipped += 1

conn.commit()
conn.close()
print(f"\nDone: {updated} updated, {skipped} had no match in xlsx.")
