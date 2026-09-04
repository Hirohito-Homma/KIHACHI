"""YouTube monetization ops team: roles, 24h shifts, packages, human publish gate.

KIHACHI can compose and review music. It does not upload to YouTube or claim
Partner Program eligibility. This module is the durable ops boundary between
those two claims:

- a roster of agent roles that cover a UTC day in fixed shifts
- release packages built from finished projects (title, description, tags, …)
- a monetization checklist that records evidence, never invents it
- a human authorize step before anything is treated as publish-ready

Pure and stdlib-only. Nothing here talks to the YouTube API.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

OPS_VERSION = "0.1"
DEFAULT_OPS_DIR = Path("ops/youtube")
SHIFT_HOURS = 4
SHIFTS_PER_DAY = 24 // SHIFT_HOURS
OPS_LOG_NAME = "ops_log.json"
CHANNEL_CONFIG_NAME = "channel.json"
CHECKLIST_NAME = "monetization_checklist.json"
QUEUE_DIR_NAME = "queue"
PACKAGES_DIR_NAME = "packages"
AUTHORIZED_DIR_NAME = "authorized"

ROLES: tuple[dict[str, str], ...] = (
    {
        "id": "strategy",
        "name": "Strategy Lead",
        "mission": "Pick the next brief themes and queue production priorities.",
    },
    {
        "id": "producer",
        "name": "Content Producer",
        "mission": "Drive KIHACHI compose / local-slice for queued briefs.",
    },
    {
        "id": "packager",
        "name": "Package Editor",
        "mission": "Build YouTube release packages from finished projects.",
    },
    {
        "id": "gate",
        "name": "Publish Gate",
        "mission": "Record human authorization; never auto-upload.",
    },
    {
        "id": "analyst",
        "name": "Analytics Watch",
        "mission": "Audit queue depth, package readiness, and checklist gaps.",
    },
    {
        "id": "community",
        "name": "Community Desk",
        "mission": "Draft community posts and reply templates for authorized drops.",
    },
)

# Fixed UTC rotation: one role owns each 4-hour block so the day is covered.
SHIFT_ROLE_ORDER: tuple[str, ...] = (
    "strategy",
    "producer",
    "packager",
    "gate",
    "analyst",
    "community",
)

MONETIZATION_ITEMS: tuple[dict[str, str], ...] = (
    {
        "id": "channel_created",
        "label": "YouTube channel exists and is owned by the operator",
    },
    {
        "id": "ypp_watch_hours",
        "label": "Public watch hours meet current YPP threshold (or Shorts alternative)",
    },
    {
        "id": "ypp_subscribers",
        "label": "Subscriber count meets current YPP threshold",
    },
    {
        "id": "original_content",
        "label": "Uploads are original / licensed; reused content policy reviewed",
    },
    {
        "id": "community_guidelines",
        "label": "No active Community Guidelines strikes blocking monetization",
    },
    {
        "id": "ad_friendly",
        "label": "Titles, thumbnails, and audio are advertiser-friendly",
    },
    {
        "id": "human_publish_gate",
        "label": "Publish requires an explicit human authorize record",
    },
)


@dataclass(frozen=True)
class ShiftManifest:
    ops_dir: Path
    log_file: Path
    entry: dict[str, Any]
    shift: dict[str, Any]


@dataclass(frozen=True)
class PackageManifest:
    ops_dir: Path
    package_dir: Path
    package: dict[str, Any]


@dataclass(frozen=True)
class AuthorizeManifest:
    ops_dir: Path
    record_file: Path
    record: dict[str, Any]


def role_by_id(role_id: str) -> dict[str, str]:
    for role in ROLES:
        if role["id"] == role_id:
            return role
    known = ", ".join(role["id"] for role in ROLES)
    raise ValueError(f"unknown role {role_id!r}; expected one of: {known}")


def roster() -> dict[str, Any]:
    """Return the standing team definition and UTC shift map."""

    shifts = []
    for index, role_id in enumerate(SHIFT_ROLE_ORDER):
        role = role_by_id(role_id)
        start = index * SHIFT_HOURS
        end = start + SHIFT_HOURS
        shifts.append(
            {
                "index": index,
                "utc_start_hour": start,
                "utc_end_hour": end,
                "label": f"{start:02d}:00-{end:02d}:00 UTC",
                "role": role,
            }
        )
    return {
        "ops_version": OPS_VERSION,
        "timezone": "UTC",
        "shift_hours": SHIFT_HOURS,
        "roles": [dict(role) for role in ROLES],
        "shifts": shifts,
        "boundary": {
            "uploads": False,
            "claims_ypp": False,
            "human_authorize_required": True,
        },
    }


def current_shift(now: datetime | None = None) -> dict[str, Any]:
    """Which 4-hour UTC block is live right now, and which role owns it."""

    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    else:
        moment = moment.astimezone(timezone.utc)
    index = moment.hour // SHIFT_HOURS
    role = role_by_id(SHIFT_ROLE_ORDER[index])
    start = index * SHIFT_HOURS
    end = start + SHIFT_HOURS
    return {
        "at": moment.isoformat(),
        "index": index,
        "utc_start_hour": start,
        "utc_end_hour": end,
        "label": f"{start:02d}:00-{end:02d}:00 UTC",
        "role": role,
    }


def ensure_ops_workspace(ops_dir: Path | None = None) -> Path:
    """Create the ops root and seed channel + checklist files if missing."""

    root = Path(ops_dir) if ops_dir is not None else DEFAULT_OPS_DIR
    root.mkdir(parents=True, exist_ok=True)
    (root / QUEUE_DIR_NAME).mkdir(exist_ok=True)
    (root / PACKAGES_DIR_NAME).mkdir(exist_ok=True)
    (root / AUTHORIZED_DIR_NAME).mkdir(exist_ok=True)

    channel_path = root / CHANNEL_CONFIG_NAME
    if not channel_path.is_file():
        _atomic_write_json(
            channel_path,
            {
                "ops_version": OPS_VERSION,
                "channel_name": "KIHACHI",
                "positioning": (
                    "Original electronic / dance music produced with the KIHACHI "
                    "Music Brain pipeline."
                ),
                "upload_cadence": "human-gated; packages queue until authorized",
                "default_language": "ja",
                "content_pillars": [
                    "full track premieres",
                    "mutation / revision process shorts",
                    "genre hybrid explainers",
                ],
            },
        )

    checklist_path = root / CHECKLIST_NAME
    if not checklist_path.is_file():
        _atomic_write_json(checklist_path, _fresh_checklist())

    log_path = root / OPS_LOG_NAME
    if not log_path.is_file():
        _atomic_write_json(
            log_path,
            {
                "ops_version": OPS_VERSION,
                "entries": [],
            },
        )
    return root


def run_shift(
    ops_dir: Path | None = None,
    *,
    role_id: str | None = None,
    note: str = "",
    now: datetime | None = None,
) -> ShiftManifest:
    """Append one shift entry. Role defaults to whoever owns the current UTC block."""

    root = ensure_ops_workspace(ops_dir)
    shift = current_shift(now)
    role = role_by_id(role_id) if role_id else shift["role"]
    queue = _list_json_names(root / QUEUE_DIR_NAME)
    packages = _list_dir_names(root / PACKAGES_DIR_NAME)
    authorized = _list_dir_names(root / AUTHORIZED_DIR_NAME)
    checklist = load_checklist(root)
    entry = {
        "index": None,  # filled after load
        "at": shift["at"],
        "shift": {
            "index": shift["index"],
            "label": shift["label"],
            "utc_start_hour": shift["utc_start_hour"],
            "utc_end_hour": shift["utc_end_hour"],
        },
        "role": role,
        "note": note.strip(),
        "snapshot": {
            "queue_items": len(queue),
            "packages": len(packages),
            "authorized": len(authorized),
            "checklist_done": checklist["done_count"],
            "checklist_total": checklist["total_count"],
            "monetization_ready": checklist["ready"],
        },
        "actions": _default_shift_actions(role["id"], queue, packages, authorized, checklist),
    }
    log_path = root / OPS_LOG_NAME
    log = _load_ops_log(log_path)
    entry["index"] = len(log["entries"])
    log["entries"].append(entry)
    log["current_shift"] = entry["index"]
    _atomic_write_json(log_path, log)
    return ShiftManifest(root, log_path, entry, shift)


def enqueue_brief(
    ops_dir: Path | None = None,
    *,
    brief: str,
    title: str | None = None,
    pillar: str | None = None,
) -> Path:
    """Drop a production brief into the ops queue (strategy / producer handoff)."""

    root = ensure_ops_workspace(ops_dir)
    text = brief.strip()
    if not text:
        raise ValueError("brief must not be blank")
    slug = _slug(title or text[:48])
    destination = root / QUEUE_DIR_NAME / f"{slug}.json"
    if destination.exists():
        raise FileExistsError(f"queue item already exists: {destination}")
    payload = {
        "ops_version": OPS_VERSION,
        "title": title or slug.replace("-", " "),
        "brief": text,
        "pillar": pillar,
        "status": "queued",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_write_json(destination, payload)
    return destination


def build_release_package(
    project_dir: Path,
    ops_dir: Path | None = None,
    *,
    overwrite: bool = False,
) -> PackageManifest:
    """Build a YouTube release package from a finished KIHACHI project.

    Reads whatever local artifacts exist; missing audio or review does not invent
    readiness. Publish authorization is a separate step.
    """

    root = ensure_ops_workspace(ops_dir)
    project_dir = Path(project_dir)
    if not project_dir.is_dir():
        raise FileNotFoundError(f"project not found: {project_dir}")

    spec = _load_json(project_dir / "song_spec.json")
    review = _load_json(project_dir / "generation_review.json")
    defects = _load_json(project_dir / "material_defects.json")
    lyrics = _read_text(project_dir / "lyrics.txt")
    prompt = _read_text(project_dir / "prompt.txt")
    audio = _find_audio(project_dir)

    title = _package_title(spec, project_dir)
    slug = _slug(title)
    package_dir = root / PACKAGES_DIR_NAME / slug
    if package_dir.exists() and not overwrite:
        raise FileExistsError(
            f"package already exists: {package_dir} (pass overwrite to replace)"
        )
    package_dir.mkdir(parents=True, exist_ok=True)

    blocking = 0
    if isinstance(defects, dict):
        blocking = int(defects.get("blocking") or 0)
    aligned = None
    grade = None
    if isinstance(review, dict):
        alignment = review.get("alignment") or {}
        if isinstance(alignment, dict):
            aligned = alignment.get("score")
            grade = alignment.get("grade")

    blockers: list[str] = []
    if audio is None:
        blockers.append("no render audio found under audio/")
    if blocking:
        blockers.append(f"{blocking} blocking material defect(s)")
    if review is None:
        blockers.append("generation_review.json missing")

    description = _build_description(
        title=title,
        spec=spec if isinstance(spec, dict) else {},
        lyrics=lyrics,
        prompt=prompt,
    )
    tags = _build_tags(spec if isinstance(spec, dict) else {})
    chapters = _build_chapters(spec if isinstance(spec, dict) else {})
    thumbnail_brief = _build_thumbnail_brief(title, spec if isinstance(spec, dict) else {})

    package = {
        "ops_version": OPS_VERSION,
        "slug": slug,
        "title": title,
        "project": str(project_dir),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "audio_relative": str(audio.relative_to(project_dir)) if audio else None,
        "review": {"alignment_score": aligned, "grade": grade, "blocking": blocking},
        "ready_for_authorize": not blockers,
        "blockers": blockers,
        "files": {
            "title": "youtube_title.txt",
            "description": "youtube_description.md",
            "tags": "youtube_tags.txt",
            "chapters": "youtube_chapters.txt",
            "thumbnail_brief": "thumbnail_brief.md",
            "manifest": "package.json",
        },
    }

    (package_dir / "youtube_title.txt").write_text(title + "\n", encoding="utf-8")
    (package_dir / "youtube_description.md").write_text(description, encoding="utf-8")
    (package_dir / "youtube_tags.txt").write_text("\n".join(tags) + "\n", encoding="utf-8")
    (package_dir / "youtube_chapters.txt").write_text(chapters, encoding="utf-8")
    (package_dir / "thumbnail_brief.md").write_text(thumbnail_brief, encoding="utf-8")
    _atomic_write_json(package_dir / "package.json", package)
    return PackageManifest(root, package_dir, package)


def authorize_package(
    package_slug: str,
    ops_dir: Path | None = None,
    *,
    reason: str,
    authorized_by: str = "human",
) -> AuthorizeManifest:
    """Human publish gate. Copies package metadata into authorized/; never uploads."""

    reason = reason.strip()
    if not reason:
        raise ValueError("authorize reason must not be blank")
    root = ensure_ops_workspace(ops_dir)
    package_dir = root / PACKAGES_DIR_NAME / package_slug
    package_path = package_dir / "package.json"
    if not package_path.is_file():
        raise FileNotFoundError(f"package not found: {package_dir}")
    package = json.loads(package_path.read_text(encoding="utf-8"))
    if not package.get("ready_for_authorize"):
        blockers = ", ".join(package.get("blockers") or []) or "unknown blockers"
        raise ValueError(f"package is not ready for authorize: {blockers}")

    dest = root / AUTHORIZED_DIR_NAME / package_slug
    dest.mkdir(parents=True, exist_ok=True)
    record = {
        "ops_version": OPS_VERSION,
        "package_slug": package_slug,
        "authorized_at": datetime.now(timezone.utc).isoformat(),
        "authorized_by": authorized_by,
        "reason": reason,
        "upload_performed": False,
        "package": package,
    }
    # Mirror the human-facing copy texts next to the record.
    for name in (
        "youtube_title.txt",
        "youtube_description.md",
        "youtube_tags.txt",
        "youtube_chapters.txt",
        "thumbnail_brief.md",
    ):
        source = package_dir / name
        if source.is_file():
            (dest / name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    record_path = dest / "authorize.json"
    _atomic_write_json(record_path, record)
    return AuthorizeManifest(root, record_path, record)


def load_checklist(ops_dir: Path | None = None) -> dict[str, Any]:
    root = ensure_ops_workspace(ops_dir)
    path = root / CHECKLIST_NAME
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data.get("items") or []
    done = sum(1 for item in items if item.get("status") == "done")
    data["done_count"] = done
    data["total_count"] = len(items)
    data["ready"] = done == len(items) and len(items) > 0
    return data


def update_checklist_item(
    item_id: str,
    ops_dir: Path | None = None,
    *,
    status: str,
    evidence: str = "",
) -> dict[str, Any]:
    """Set one monetization checklist item. Status is pending|done|blocked."""

    if status not in {"pending", "done", "blocked"}:
        raise ValueError("status must be pending, done, or blocked")
    root = ensure_ops_workspace(ops_dir)
    path = root / CHECKLIST_NAME
    data = json.loads(path.read_text(encoding="utf-8"))
    found = False
    for item in data["items"]:
        if item["id"] == item_id:
            item["status"] = status
            item["evidence"] = evidence.strip()
            item["updated_at"] = datetime.now(timezone.utc).isoformat()
            found = True
            break
    if not found:
        known = ", ".join(item["id"] for item in data["items"])
        raise ValueError(f"unknown checklist item {item_id!r}; expected one of: {known}")
    if status == "done" and item_id != "human_publish_gate" and not evidence.strip():
        raise ValueError("done items need evidence text (except when clearing)")
    # human_publish_gate is structural; mark done only when authorize path exists.
    if item_id == "human_publish_gate" and status == "done":
        item_ref = next(i for i in data["items"] if i["id"] == item_id)
        item_ref["evidence"] = evidence.strip() or "authorize_package is the publish gate"
    _atomic_write_json(path, data)
    return load_checklist(root)


def describe_status(ops_dir: Path | None = None, *, now: datetime | None = None) -> list[str]:
    root = ensure_ops_workspace(ops_dir)
    shift = current_shift(now)
    checklist = load_checklist(root)
    queue = _list_json_names(root / QUEUE_DIR_NAME)
    packages = _list_dir_names(root / PACKAGES_DIR_NAME)
    authorized = _list_dir_names(root / AUTHORIZED_DIR_NAME)
    log = _load_ops_log(root / OPS_LOG_NAME)
    lines = [
        f"YouTube ops workspace: {root}",
        f"live shift: {shift['label']} — {shift['role']['name']} ({shift['role']['id']})",
        f"mission: {shift['role']['mission']}",
        f"queue: {len(queue)} | packages: {len(packages)} | authorized: {len(authorized)}",
        (
            f"monetization checklist: {checklist['done_count']}/"
            f"{checklist['total_count']} "
            f"({'ready' if checklist['ready'] else 'not ready'})"
        ),
        f"shifts logged: {len(log['entries'])}",
        "boundary: no YouTube upload; human authorize required before publish",
    ]
    return lines


def describe_roster() -> list[str]:
    data = roster()
    lines = [
        f"YouTube ops team v{data['ops_version']} ({data['timezone']}, {data['shift_hours']}h shifts)",
        "roles:",
    ]
    for role in data["roles"]:
        lines.append(f"  - {role['id']}: {role['name']} — {role['mission']}")
    lines.append("UTC rotation:")
    for shift in data["shifts"]:
        role = shift["role"]
        lines.append(f"  - {shift['label']}: {role['name']} ({role['id']})")
    lines.append("boundary: packages only; uploads and YPP claims stay with the human operator")
    return lines


def describe_checklist(ops_dir: Path | None = None) -> list[str]:
    data = load_checklist(ops_dir)
    lines = [
        f"Monetization checklist ({data['done_count']}/{data['total_count']})",
    ]
    for item in data["items"]:
        mark = {"pending": "[ ]", "done": "[x]", "blocked": "[!]"}.get(
            item.get("status", "pending"), "[ ]"
        )
        evidence = f" — {item['evidence']}" if item.get("evidence") else ""
        lines.append(f"  {mark} {item['id']}: {item['label']}{evidence}")
    return lines


def _fresh_checklist() -> dict[str, Any]:
    return {
        "ops_version": OPS_VERSION,
        "items": [
            {
                "id": item["id"],
                "label": item["label"],
                "status": "pending",
                "evidence": "",
                "updated_at": None,
            }
            for item in MONETIZATION_ITEMS
        ],
    }


def _default_shift_actions(
    role_id: str,
    queue: Sequence[str],
    packages: Sequence[str],
    authorized: Sequence[str],
    checklist: dict[str, Any],
) -> list[str]:
    if role_id == "strategy":
        return [
            "review content pillars in channel.json",
            "enqueue briefs with youtube-ops enqueue when themes are clear",
            f"queue depth now: {len(queue)}",
        ]
    if role_id == "producer":
        return [
            "consume queued briefs via kihachi local-slice / audio-slice",
            "leave finished projects for the packager",
            f"queued briefs: {', '.join(queue) or '(none)'}",
        ]
    if role_id == "packager":
        return [
            "run youtube-ops package on finished projects",
            f"existing packages: {', '.join(packages) or '(none)'}",
        ]
    if role_id == "gate":
        return [
            "human listens, then youtube-ops authorize <slug> --reason ...",
            f"authorized so far: {', '.join(authorized) or '(none)'}",
            "this role never uploads",
        ]
    if role_id == "analyst":
        pending = [
            item["id"]
            for item in checklist.get("items", [])
            if item.get("status") != "done"
        ]
        return [
            "update checklist evidence with youtube-ops checklist-set",
            f"open checklist items: {', '.join(pending) or '(none)'}",
            f"packages awaiting authorize: {len(packages) - len(set(packages) & set(authorized))}",
        ]
    if role_id == "community":
        return [
            "draft community-tab copy for authorized packages",
            f"authorized drops: {', '.join(authorized) or '(none)'}",
        ]
    return []


def _package_title(spec: Any, project_dir: Path) -> str:
    if isinstance(spec, dict):
        meta = spec.get("meta") or {}
        if isinstance(meta, dict) and meta.get("title"):
            return str(meta["title"]).strip()
        song = spec.get("song") or {}
        if isinstance(song, dict) and song.get("title"):
            return str(song["title"]).strip()
    return project_dir.name


def _build_description(
    *,
    title: str,
    spec: dict[str, Any],
    lyrics: str | None,
    prompt: str | None,
) -> str:
    genres = _genre_labels(spec)
    bpm = None
    key = None
    song = spec.get("song") if isinstance(spec.get("song"), dict) else {}
    if song:
        bpm = song.get("bpm") or song.get("tempo")
        key = song.get("key")
    lines = [
        f"# {title}",
        "",
        "Original track prepared with KIHACHI Music AI.",
        "",
    ]
    if genres:
        lines.append(f"Genre blend: {', '.join(genres)}")
    if bpm is not None:
        lines.append(f"Tempo: {bpm} BPM")
    if key:
        lines.append(f"Key: {key}")
    lines.extend(["", "## Chapters", "", "See youtube_chapters.txt", ""])
    if lyrics:
        lines.extend(["## Lyrics", "", lyrics.strip(), ""])
    if prompt:
        lines.extend(
            [
                "## Production note",
                "",
                "Prompt compiled from SongSpec (excerpt):",
                "",
                "```",
                prompt.strip()[:600],
                "```",
                "",
            ]
        )
    lines.extend(
        [
            "---",
            "Upload is human-gated. This package does not publish itself.",
            "",
        ]
    )
    return "\n".join(lines)


def _build_tags(spec: dict[str, Any]) -> list[str]:
    tags = ["KIHACHI", "electronic", "original music", "AI music production"]
    for label in _genre_labels(spec):
        if label not in tags:
            tags.append(label)
    song = spec.get("song") if isinstance(spec.get("song"), dict) else {}
    if song.get("key"):
        tags.append(str(song["key"]))
    return tags[:20]


def _build_chapters(spec: dict[str, Any]) -> str:
    arrangement = spec.get("arrangement")
    sections = None
    if isinstance(arrangement, dict):
        sections = arrangement.get("sections")
    if not isinstance(sections, list) or not sections:
        return "0:00 Intro\n"
    bpm = 120.0
    song = spec.get("song") if isinstance(spec.get("song"), dict) else {}
    if song.get("bpm"):
        try:
            bpm = float(song["bpm"])
        except (TypeError, ValueError):
            pass
    sec_per_bar = (60.0 / bpm) * 4.0
    cursor_bars = 0.0
    lines: list[str] = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        name = str(section.get("name") or section.get("id") or "section")
        bars = float(section.get("bars") or section.get("length_bars") or 8)
        stamp = _timestamp(cursor_bars * sec_per_bar)
        lines.append(f"{stamp} {name.replace('_', ' ').title()}")
        cursor_bars += bars
    return "\n".join(lines) + "\n"


def _build_thumbnail_brief(title: str, spec: dict[str, Any]) -> str:
    genres = ", ".join(_genre_labels(spec)) or "electronic"
    return (
        f"# Thumbnail brief — {title}\n\n"
        f"- Mood: {genres}\n"
        f"- Text on image: {title}\n"
        "- Avoid clutter; one dominant visual + title only\n"
        "- No fake UI badges or engagement stickers\n"
        "- Safe area: keep title readable at mobile size\n"
    )


def _genre_labels(spec: dict[str, Any]) -> list[str]:
    genres = spec.get("genres")
    labels: list[str] = []
    if isinstance(genres, list):
        for item in genres:
            if isinstance(item, dict) and item.get("name"):
                labels.append(str(item["name"]))
            elif isinstance(item, str):
                labels.append(item)
    return labels


def _timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _find_audio(project_dir: Path) -> Path | None:
    audio_dir = project_dir / "audio"
    if not audio_dir.is_dir():
        return None
    preferred = (
        "ace-step-01.wav",
        "ace-step-01.mp3",
        "adopted.wav",
        "adopted.mp3",
    )
    for name in preferred:
        path = audio_dir / name
        if path.is_file():
            return path
    candidates = sorted(
        p for p in audio_dir.iterdir() if p.suffix.lower() in {".wav", ".mp3", ".m4a"}
    )
    return candidates[0] if candidates else None


def _slug(text: str) -> str:
    cleaned = []
    for char in text.lower().strip():
        if char.isalnum():
            cleaned.append(char)
        elif char in {" ", "-", "_", ".", "/"}:
            cleaned.append("-")
    slug = "".join(cleaned)
    while "--" in slug:
        slug = slug.replace("--", "-")
    slug = slug.strip("-")
    return slug or "untitled"


def _list_json_names(directory: Path) -> list[str]:
    if not directory.is_dir():
        return []
    return sorted(path.stem for path in directory.glob("*.json"))


def _list_dir_names(directory: Path) -> list[str]:
    if not directory.is_dir():
        return []
    return sorted(path.name for path in directory.iterdir() if path.is_dir())


def _load_ops_log(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"ops_version": OPS_VERSION, "entries": []}
    return json.loads(path.read_text(encoding="utf-8"))


def _load_json(path: Path) -> Any | None:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _read_text(path: Path) -> str | None:
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
