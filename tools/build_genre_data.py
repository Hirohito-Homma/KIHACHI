#!/usr/bin/env python3
"""Convert the Music Genre Master Database workbook into the shipped JSON.

Kept in the repo, and stdlib-only, so the data can be regenerated when the
workbook is versioned up without installing a spreadsheet library:

    python3 tools/build_genre_data.py Music_Genre_Master_Database_v0.2.xlsx

Only the fields the recognition layer needs are carried across. The workbook's
long prose columns (rhythm_character, production_traits, ...) are deliberately
left out: measured against v0.2 they hold ~51 distinct values across 1020 rows
because they are inherited per family, so copying them would multiply the data
file without adding information. Add them when they are individualised.
"""

from __future__ import annotations

import json
import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

NS = {
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}


def _column(ref: str) -> int:
    match = re.match(r"([A-Z]+)", ref or "")
    if not match:
        return 0
    number = 0
    for char in match.group(1):
        number = number * 26 + (ord(char) - 64)
    return number - 1


def read_sheet(path: Path, wanted: str) -> list[list[str]]:
    archive = zipfile.ZipFile(path)
    shared: list[str] = []
    if "xl/sharedStrings.xml" in archive.namelist():
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
        for item in root.findall("m:si", NS):
            shared.append("".join(t.text or "" for t in item.iter("{%s}t" % NS["m"])))
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    relations = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {rel.get("Id"): rel.get("Target") for rel in relations}
    part = None
    for sheet in workbook.find("m:sheets", NS):
        if sheet.get("name") == wanted:
            target = targets[sheet.get("{%s}id" % NS["r"])].lstrip("/")
            part = target if target.startswith("xl/") else "xl/" + target
    if part is None:
        raise SystemExit("sheet not found: %s" % wanted)

    grid: list[list[str]] = []
    for row in ET.fromstring(archive.read(part)).iter("{%s}row" % NS["m"]):
        cells: dict[int, str] = {}
        for cell in row.findall("m:c", NS):
            value = cell.find("m:v", NS)
            inline = cell.find("m:is", NS)
            if cell.get("t") == "s" and value is not None:
                text = shared[int(value.text)]
            elif inline is not None:
                text = "".join(t.text or "" for t in inline.iter("{%s}t" % NS["m"]))
            elif value is not None:
                text = value.text or ""
            else:
                continue
            if text.strip():
                cells[_column(cell.get("r"))] = text.strip()
        grid.append([cells.get(i, "") for i in range(max(cells) + 1 if cells else 0)])
    return grid


def slugify(name: str) -> str:
    """``Tech House`` -> ``tech_house``.

    The three genres KIHACHI recognised before this database existed
    (mutation_funk, dub, tech_house) slugify to exactly their old names, so the
    downstream behaviour keyed on them keeps working unchanged.
    """
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def build(workbook: Path, version: str) -> dict:
    grid = read_sheet(workbook, "Genre_Master")
    header = grid[0]
    index = {name: position for position, name in enumerate(header)}
    entries = []
    for row in grid[1:]:
        row = row + [""] * (len(header) - len(row))
        name = row[index["genre"]].strip()
        if not name:
            continue
        aliases = [a.strip() for a in row[index["aliases"]].split(";") if a.strip()]
        moods = [m.strip() for m in row[index["mood_tags"]].split(";") if m.strip()]

        def number(field: str):
            try:
                return float(row[index[field]])
            except (TypeError, ValueError):
                return None

        entries.append(
            {
                "id": row[index["genre_id"]],
                "slug": slugify(name),
                "name": name,
                "parent": row[index["parent_genre"]] or None,
                "level": row[index["level"]],
                "aliases": aliases,
                "bpm_min": number("bpm_min"),
                "bpm_max": number("bpm_max"),
                "meter": row[index["meter"]],
                "mood_tags": moods,
                "region": row[index["region"]],
            }
        )
    return {"version": version, "source": workbook.name, "genres": entries}


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    workbook = Path(sys.argv[1])
    match = re.search(r"v(\d+\.\d+)", workbook.name)
    data = build(workbook, match.group(1) if match else "unknown")
    out = Path(__file__).resolve().parent.parent / "src/kihachi_music_ai/data/genres.json"
    out.write_text(
        json.dumps(data, ensure_ascii=False, indent=1, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    print("%d genres -> %s" % (len(data["genres"]), out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
