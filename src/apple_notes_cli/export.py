"""Write notes to disk as Markdown, with incremental-manifest bookkeeping."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sqlite3
import sys
from pathlib import Path

from .models import ExportResult, Note
from .store import copy_live_database, iter_notes, parse_iso_timestamp
from .utils import sanitize_filename

MANIFEST_FILENAME = ".apple_notes_export_manifest.json"
MANIFEST_VERSION = 1
IGNORED_PRUNE_FILENAMES = {".DS_Store"}


def set_macos_creation_time(path: Path, created: dt.datetime) -> None:
    setfile = shutil.which("SetFile")
    if not setfile:
        return
    local = created.astimezone()
    formatted = local.strftime("%m/%d/%Y %H:%M:%S")
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            [setfile, "-d", formatted, str(path)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def apply_file_timestamps(path: Path, *, created: str | None, modified: str | None) -> None:
    created_dt = parse_iso_timestamp(created)
    modified_dt = parse_iso_timestamp(modified) or created_dt
    if modified_dt is not None:
        timestamp = modified_dt.timestamp()
        with contextlib.suppress(OSError):
            os.utime(path, (timestamp, timestamp))
    if created_dt is not None and sys.platform == "darwin":
        set_macos_creation_time(path, created_dt)




def yaml_scalar(value: str | int | None) -> str:
    if value is None:
        return '""'
    if isinstance(value, int):
        return str(value)
    return json.dumps(value, ensure_ascii=False)




def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    counter = 2
    while True:
        candidate = parent / f"{stem}-{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def note_filename_stem(title: str, pk: int) -> str:
    return f"{sanitize_filename(title)}-{pk}"




def build_markdown(note: Note) -> str:
    lines = [
        "---",
        f"title: {yaml_scalar(note.title)}",
        f"id: {note.pk}",
        f"apple_notes_id: {yaml_scalar(note.apple_notes_id)}",
        f"created: {yaml_scalar(note.created)}",
        f"modified: {yaml_scalar(note.modified)}",
        f"folder: {yaml_scalar(note.folder_path)}",
    ]

    if note.attachments:
        lines.append("attachments:")
        for attachment in note.attachments:
            lines.append(f"  - filename: {yaml_scalar(attachment.output_name)}")
            lines.append(f"    identifier: {yaml_scalar(attachment.identifier)}")
            lines.append(f"    type: {yaml_scalar(attachment.type_uti)}")
    else:
        lines.append("attachments: []")

    if note.links:
        lines.append("links:")
        for url in note.links:
            lines.append(f"  - url: {yaml_scalar(url)}")
            lines.append(f"    title: {yaml_scalar(url)}")
    else:
        lines.append("links: []")

    lines.append(f"source: notes://showNote?identifier={note.apple_notes_id}")
    lines.append("---")
    lines.append("")
    lines.append(note.body.rstrip())
    lines.append("")
    return "\n".join(lines)


def file_fingerprint(path: Path | None) -> dict[str, int | str] | None:
    if path is None or not path.exists():
        return None
    stat = path.stat()
    return {
        "path": str(path),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def note_signature(note: Note) -> str:
    payload = {
        "markdown": build_markdown(note),
        "attachments": [
            {
                "identifier": attachment.identifier,
                "type": attachment.type_uti,
                "output_name": attachment.output_name,
                "source": file_fingerprint(attachment.source_path),
            }
            for attachment in note.attachments
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def path_relative_to_output(output_root: Path, path: Path) -> str:
    return str(path.resolve().relative_to(output_root.resolve()))


def manifest_file(output_root: Path) -> Path:
    return output_root / MANIFEST_FILENAME


def load_manifest(output_root: Path) -> dict:
    path = manifest_file(output_root)
    if not path.exists():
        return {"version": MANIFEST_VERSION, "notes": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"version": MANIFEST_VERSION, "notes": {}}
    if not isinstance(data, dict) or not isinstance(data.get("notes"), dict):
        return {"version": MANIFEST_VERSION, "notes": {}}
    return data


def manifest_entry(output_root: Path, note: Note, result: ExportResult, signature: str) -> dict:
    return {
        "id": note.pk,
        "apple_notes_id": note.apple_notes_id,
        "title": note.title,
        "folder": note.folder_path,
        "modified": note.modified,
        "signature": signature,
        "path": path_relative_to_output(output_root, result.note_path),
        "attachments": [path_relative_to_output(output_root, path) for path in result.attachment_paths],
    }


def safe_output_path(output_root: Path, relative_path: str) -> Path | None:
    if not relative_path or os.path.isabs(relative_path):
        return None
    root = output_root.resolve()
    path = (root / relative_path).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    return path


def manifest_paths_exist(output_root: Path, entry: dict) -> bool:
    paths = [entry.get("path"), *entry.get("attachments", [])]
    for relative_path in paths:
        if not isinstance(relative_path, str):
            return False
        path = safe_output_path(output_root, relative_path)
        if path is None or not path.exists():
            return False
    return True


def path_has_mtime(path: Path, modified: str | None) -> bool:
    modified_dt = parse_iso_timestamp(modified)
    if modified_dt is None:
        return True
    with contextlib.suppress(OSError):
        return abs(path.stat().st_mtime - modified_dt.timestamp()) < 1.0
    return False


def manifest_entry_timestamps_match(output_root: Path, entry: dict) -> bool:
    modified = entry.get("modified")
    if not isinstance(modified, str):
        return True
    paths = [entry.get("path"), *entry.get("attachments", [])]
    for relative_path in paths:
        if not isinstance(relative_path, str):
            return False
        path = safe_output_path(output_root, relative_path)
        if path is None or not path.exists() or not path_has_mtime(path, modified):
            return False
    return True


def manifest_referenced_paths(notes: dict[str, dict]) -> set[str]:
    paths: set[str] = set()
    for entry in notes.values():
        if not isinstance(entry, dict):
            continue
        note_path = entry.get("path")
        if isinstance(note_path, str):
            paths.add(note_path)
        for attachment_path in entry.get("attachments", []):
            if isinstance(attachment_path, str):
                paths.add(attachment_path)
    return paths


def prune_empty_dirs(output_root: Path, start: Path) -> None:
    root = output_root.resolve()
    current = start.resolve()
    while current != root:
        try:
            current.relative_to(root)
        except ValueError:
            return
        entries = list(current.iterdir()) if current.exists() else []
        if entries and all(entry.name in IGNORED_PRUNE_FILENAMES and entry.is_file() for entry in entries):
            for entry in entries:
                entry.unlink()
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def prune_all_empty_dirs(output_root: Path) -> None:
    if not output_root.exists():
        return
    directories = [path for path in output_root.rglob("*") if path.is_dir()]
    directories.sort(key=lambda path: len(path.relative_to(output_root).parts), reverse=True)
    for directory in directories:
        prune_empty_dirs(output_root, directory)


def remove_manifest_entry_paths(output_root: Path, entry: dict, keep: set[str] | None = None) -> int:
    keep = keep or set()
    deleted = 0
    paths = [entry.get("path"), *entry.get("attachments", [])]
    for relative_path in paths:
        if not isinstance(relative_path, str) or relative_path in keep:
            continue
        path = safe_output_path(output_root, relative_path)
        if path is None or not path.exists() or not path.is_file():
            continue
        path.unlink()
        deleted += 1
        prune_empty_dirs(output_root, path.parent)
    return deleted


def exported_note_attachment_names(markdown: str) -> list[str]:
    lines = markdown.splitlines()
    if not lines or lines[0] != "---":
        return []

    names: list[str] = []
    for line in lines[1:]:
        if line == "---":
            break
        stripped = line.strip()
        if not stripped.startswith("- filename: "):
            continue
        raw = stripped.removeprefix("- filename: ").strip()
        with contextlib.suppress(json.JSONDecodeError):
            value = json.loads(raw)
            if isinstance(value, str) and value:
                names.append(value)
    return names


def is_exported_note_markdown(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return text.startswith("---\n") and "\nsource: notes://showNote?identifier=" in text.split("\n---", 1)[0]


def remove_orphaned_export_files(output_root: Path, referenced_paths: set[str]) -> int:
    if not output_root.exists():
        return 0

    deleted = 0
    for note_path in sorted(output_root.rglob("*.md")):
        relative_note_path = path_relative_to_output(output_root, note_path)
        if relative_note_path in referenced_paths or not is_exported_note_markdown(note_path):
            continue

        text = note_path.read_text(encoding="utf-8", errors="replace")
        attachment_names = exported_note_attachment_names(text)
        note_path.unlink()
        deleted += 1

        for attachment_name in attachment_names:
            attachment_path = safe_output_path(output_root, str(Path(relative_note_path).parent / attachment_name))
            if attachment_path is None or not attachment_path.exists() or not attachment_path.is_file():
                continue
            relative_attachment_path = path_relative_to_output(output_root, attachment_path)
            if relative_attachment_path in referenced_paths:
                continue
            attachment_path.unlink()
            deleted += 1

        prune_empty_dirs(output_root, note_path.parent)
    return deleted


def write_manifest(output_root: Path, notes: dict[str, dict]) -> None:
    output_root.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": MANIFEST_VERSION,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "notes": notes,
    }
    manifest_file(output_root).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )




def expected_note_path(output_root: Path, note: Note) -> Path:
    filename = f"{note_filename_stem(note.title, note.pk)}.md"
    return output_root / Path(note.folder_path) / filename


def write_note(output_root: Path, note: Note, copy_attachments: bool) -> ExportResult:
    note_dir = output_root / Path(note.folder_path)
    note_dir.mkdir(parents=True, exist_ok=True)

    attachment_paths: list[Path] = []
    if copy_attachments:
        used_paths: set[str] = set()
        for attachment in note.attachments:
            if attachment.source_path is None:
                continue
            destination = note_dir / attachment.output_name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if attachment.output_name in used_paths:
                destination = unique_path(destination)
            used_paths.add(path_relative_to_output(note_dir, destination))
            shutil.copy2(attachment.source_path, destination)
            apply_file_timestamps(destination, created=note.created, modified=note.modified)
            attachment_paths.append(destination)

    path = expected_note_path(output_root, note)
    path.write_text(build_markdown(note), encoding="utf-8")
    apply_file_timestamps(path, created=note.created, modified=note.modified)
    return ExportResult(note_path=path, attachment_paths=attachment_paths)


def write_claude_files(output_root: Path, exported_count: int) -> None:
    output_root.mkdir(parents=True, exist_ok=True)
    generated_at = dt.datetime.now().strftime("%Y-%m-%d, %H:%M")
    content = f"""# Apple Notes Export

This directory contains Apple Notes exported as markdown files.

## Structure

- Each Notes folder is represented as a subdirectory.
- Each note is saved as `{{note-title}}-{{id}}.md`.
- Attachments are saved in `attachments/{{id}}/` next to the markdown file.
- Frontmatter includes title, Apple Notes ID, dates, folder, links, attachments, and the `notes://` source URL.

## Working With Notes

1. Preserve `source` links when editing or referencing notes.
2. Keep the `attachments/` folder next to the markdown files that reference it.
3. Keep folder structure intact when moving exported notes.

---
Generated by `apple-notes export` • Notes exported: {exported_count} • Last updated: {generated_at}
"""
    (output_root / "CLAUDE.md").write_text(content, encoding="utf-8")


def export_notes(args: argparse.Namespace) -> int:
    notes_dir = args.notes_dir.expanduser().resolve()
    db_path = args.database.expanduser().resolve() if args.database else notes_dir / "NoteStore.sqlite"
    if not db_path.exists():
        raise FileNotFoundError(f"Notes database not found: {db_path}")

    temp_db: Path | None = None
    if args.no_snapshot:
        read_db = db_path
    else:
        temp_db = copy_live_database(db_path)
        read_db = temp_db

    output_root = args.output.expanduser().resolve()
    old_manifest = load_manifest(output_root) if args.incremental else {"notes": {}}
    old_notes: dict[str, dict] = old_manifest.get("notes", {})
    current_manifest_notes: dict[str, dict] = {}
    exported = 0
    written = 0
    skipped = 0
    deleted_notes = 0
    deleted_files = 0
    try:
        conn = sqlite3.connect(read_db)
        conn.row_factory = sqlite3.Row
        try:
            notes = iter_notes(
                conn,
                notes_dir,
                include_password_protected=args.include_password_protected,
                limit=args.limit,
                include_trashed=getattr(args, "include_trashed", False),
            )
            for note in notes:
                exported += 1
                key = str(note.pk)
                signature = note_signature(note)
                expected_relpath = path_relative_to_output(output_root, expected_note_path(output_root, note))
                old_entry = old_notes.get(key)
                unchanged = (
                    args.incremental
                    and isinstance(old_entry, dict)
                    and old_entry.get("signature") == signature
                    and old_entry.get("path") == expected_relpath
                    and manifest_paths_exist(output_root, old_entry)
                    and manifest_entry_timestamps_match(output_root, old_entry)
                )

                if unchanged:
                    skipped += 1
                    current_manifest_notes[key] = old_entry
                    continue

                written += 1
                if args.dry_run:
                    current_manifest_notes[key] = {
                        "id": note.pk,
                        "apple_notes_id": note.apple_notes_id,
                        "title": note.title,
                        "folder": note.folder_path,
                        "modified": note.modified,
                        "signature": signature,
                        "path": expected_relpath,
                        "attachments": [],
                    }
                    continue
                if args.incremental and isinstance(old_entry, dict):
                    deleted_files += remove_manifest_entry_paths(
                        output_root,
                        old_entry,
                        keep={expected_relpath},
                    )
                result = write_note(output_root, note, copy_attachments=not args.no_attachments)
                new_entry = manifest_entry(output_root, note, result, signature)
                current_manifest_notes[key] = new_entry
                if args.incremental and isinstance(old_entry, dict):
                    new_paths = {new_entry["path"], *new_entry.get("attachments", [])}
                    deleted_files += remove_manifest_entry_paths(output_root, old_entry, keep=new_paths)
        finally:
            conn.close()
    finally:
        if temp_db is not None:
            temp_db.unlink(missing_ok=True)

    if args.incremental and not args.keep_stale and args.limit is None:
        stale_keys = set(old_notes) - set(current_manifest_notes)
        deleted_notes = len(stale_keys)
        if not args.dry_run:
            for key in stale_keys:
                entry = old_notes.get(key)
                if isinstance(entry, dict):
                    deleted_files += remove_manifest_entry_paths(output_root, entry)

    if not args.dry_run:
        if args.incremental and not args.keep_stale and args.limit is None:
            deleted_files += remove_orphaned_export_files(
                output_root,
                manifest_referenced_paths(current_manifest_notes),
            )
        if getattr(args, "write_claude_md", False):
            write_claude_files(output_root, exported)
        write_manifest(output_root, current_manifest_notes)
        if args.incremental:
            prune_all_empty_dirs(output_root)
    print(f"exported_notes={exported}")
    if args.incremental:
        print(f"notes_written={written}")
        print(f"notes_skipped={skipped}")
        print(f"notes_deleted={deleted_notes}")
        print(f"files_deleted={deleted_files}")
    if not args.dry_run:
        print(f"output={output_root}")
    return 0


