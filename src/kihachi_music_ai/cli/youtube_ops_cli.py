"""CLI surface for the YouTube monetization ops team."""

from __future__ import annotations

import argparse

from ..youtube_ops import (
    authorize_package,
    build_release_package,
    describe_checklist,
    describe_roster,
    describe_status,
    enqueue_brief,
    run_shift,
    update_checklist_item,
)


def youtube_ops(args: argparse.Namespace) -> int:
    command = args.youtube_ops_command
    if command == "roster":
        for line in describe_roster():
            print(line)
        return 0
    if command == "status":
        for line in describe_status(args.ops_dir):
            print(line)
        return 0
    if command == "shift":
        manifest = run_shift(args.ops_dir, role_id=args.role, note=args.note)
        entry = manifest.entry
        role = entry["role"]
        print(f"Logged shift #{entry['index']}: {entry['shift']['label']}")
        print(f"- role: {role['name']} ({role['id']})")
        print(f"- mission: {role['mission']}")
        snap = entry["snapshot"]
        print(
            f"- snapshot: queue={snap['queue_items']} packages={snap['packages']} "
            f"authorized={snap['authorized']} checklist={snap['checklist_done']}/"
            f"{snap['checklist_total']}"
        )
        for action in entry["actions"]:
            print(f"- action: {action}")
        if entry["note"]:
            print(f"- note: {entry['note']}")
        print(f"- log: {manifest.log_file}")
        return 0
    if command == "enqueue":
        path = enqueue_brief(
            args.ops_dir,
            brief=args.brief,
            title=args.title,
            pillar=args.pillar,
        )
        print(f"Queued brief: {path}")
        return 0
    if command == "package":
        manifest = build_release_package(
            args.project,
            args.ops_dir,
            overwrite=args.overwrite,
            title=args.title,
        )
        package = manifest.package
        print(f"Built release package: {manifest.package_dir}")
        print(f"- title: {package['title']}")
        print(f"- ready for authorize: {package['ready_for_authorize']}")
        for blocker in package["blockers"]:
            print(f"- blocker: {blocker}")
        return 0
    if command == "checklist":
        for line in describe_checklist(args.ops_dir):
            print(line)
        return 0
    if command == "checklist-set":
        data = update_checklist_item(
            args.item_id,
            args.ops_dir,
            status=args.status,
            evidence=args.evidence,
        )
        print(
            f"Checklist updated: {args.item_id} -> {args.status} "
            f"({data['done_count']}/{data['total_count']})"
        )
        return 0
    if command == "authorize":
        manifest = authorize_package(
            args.package_slug,
            args.ops_dir,
            reason=args.reason,
            authorized_by=args.by,
        )
        print(f"Authorized package: {manifest.record_file}")
        print("- upload_performed: false (human must publish outside KIHACHI)")
        return 0
    raise ValueError(f"unknown youtube-ops command: {command}")
