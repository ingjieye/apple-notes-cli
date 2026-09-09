"""Command line entry point."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from . import search as search_mod
from .store import DEFAULT_NOTES_DIR, connect, default_database

EPILOG = """\
examples:
  apple-notes recent --since 2d
  apple-notes search "OpenTelemetry" --json
  apple-notes search "TODO|FIXME" --regex --folder Work
  apple-notes show 8554
  apple-notes export ~/Downloads/notes --incremental
"""


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--notes-dir",
        type=Path,
        default=DEFAULT_NOTES_DIR,
        help=f"Apple Notes group container (default: {DEFAULT_NOTES_DIR})",
    )
    parser.add_argument(
        "--database",
        type=Path,
        help="explicit NoteStore.sqlite path, overriding --notes-dir",
    )
    parser.add_argument(
        "--include-password-protected",
        action="store_true",
        help="include locked notes (their bodies stay encrypted and unreadable)",
    )
    parser.add_argument(
        "--include-trashed",
        action="store_true",
        help="include notes sitting in Recently Deleted",
    )


def add_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--since",
        help="only notes modified since this time (30m, 6h, 2d, 1w, or 2026-09-01)",
    )
    parser.add_argument("--folder", help="restrict to folder paths containing this text")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")


def local_time(value: str | None) -> str:
    """Render a stored UTC timestamp in the viewer's own timezone.

    Timestamps are kept as UTC ISO-8601 for machine consumers (``--json``,
    export frontmatter); only human-facing output is localized.
    """
    if not value:
        return "?"
    try:
        return dt.datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value


def open_store(args: argparse.Namespace):
    database = args.database or default_database(args.notes_dir)
    return connect(database)


def resolve_since(args: argparse.Namespace):
    if not getattr(args, "since", None):
        return None
    return search_mod.parse_since(args.since)


def cmd_recent(args: argparse.Namespace) -> int:
    with open_store(args) as conn:
        notes = search_mod.recent(
            conn,
            limit=args.limit,
            since=resolve_since(args),
            folder=args.folder,
            include_password_protected=args.include_password_protected,
            include_trashed=args.include_trashed,
        )
    if args.json:
        print(json.dumps([n.__dict__ for n in notes], ensure_ascii=False, indent=2))
        return 0
    if not notes:
        print("no notes matched")
        return 0
    for note in notes:
        print(f"{note.pk:>7}  {local_time(note.modified):<16}  {note.folder}/{note.title}")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    with open_store(args) as conn:
        hits = search_mod.search(
            conn,
            args.query,
            regex=args.regex,
            case_sensitive=args.case_sensitive,
            limit=args.limit,
            since=resolve_since(args),
            folder=args.folder,
            include_password_protected=args.include_password_protected,
            include_trashed=args.include_trashed,
            snippet_width=args.snippet_width,
            max_snippets=0 if args.no_snippets else args.max_snippets,
            search_titles=not args.body_only,
        )
    if args.json:
        payload = [
            {
                **hit.note.__dict__,
                "match_count": hit.match_count,
                "snippets": hit.snippets,
            }
            for hit in hits
        ]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if not hits:
        print(f"no notes matched {args.query!r}")
        return 1
    for hit in hits:
        note = hit.note
        print(
            f"{note.pk:>7}  {local_time(note.modified):<16}  "
            f"{note.folder}/{note.title}  ({hit.match_count}x)"
        )
        for snippet in hit.snippets:
            print(f"         {snippet}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    with open_store(args) as conn:
        try:
            note, body = search_mod.show(conn, args.id, markdown=not args.plain)
        except KeyError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    if args.json:
        print(json.dumps({**note.__dict__, "body": body}, ensure_ascii=False, indent=2))
        return 0
    print(f"# {note.title}\n")
    print(f"- id: {note.pk}")
    print(f"- folder: {note.folder}")
    print(f"- created: {local_time(note.created)}")
    print(f"- modified: {local_time(note.modified)}\n")
    print(body)
    return 0


def cmd_folders(args: argparse.Namespace) -> int:
    with open_store(args) as conn:
        rows = search_mod.folders(conn, include_trashed=args.include_trashed)
    if args.json:
        print(json.dumps([{"folder": f, "notes": c} for f, c in rows], ensure_ascii=False, indent=2))
        return 0
    for path, count in rows:
        print(f"{count:>6}  {path}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from .export import export_notes

    return export_notes(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apple-notes",
        description="Search, read, and export Apple Notes by reading the local database directly.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_recent = sub.add_parser("recent", help="list recently modified notes")
    p_recent.add_argument("-n", "--limit", type=int, default=20, help="max notes (default: 20)")
    add_filters(p_recent)
    add_common(p_recent)
    p_recent.set_defaults(func=cmd_recent)

    p_search = sub.add_parser("search", help="full-text search across note bodies")
    p_search.add_argument("query", help="text to find, or a regex with --regex")
    p_search.add_argument("-n", "--limit", type=int, help="max results (default: all)")
    p_search.add_argument("--regex", action="store_true", help="treat query as a regex")
    p_search.add_argument(
        "--case-sensitive", action="store_true", help="match case exactly"
    )
    p_search.add_argument(
        "--body-only", action="store_true", help="do not match against titles"
    )
    p_search.add_argument(
        "--max-snippets", type=int, default=3, help="context snippets per note (default: 3)"
    )
    p_search.add_argument(
        "--snippet-width", type=int, default=60, help="context chars each side (default: 60)"
    )
    p_search.add_argument("--no-snippets", action="store_true", help="omit context snippets")
    add_filters(p_search)
    add_common(p_search)
    p_search.set_defaults(func=cmd_search)

    p_show = sub.add_parser("show", help="print one note by id")
    p_show.add_argument("id", type=int, help="note id, as shown by search/recent")
    p_show.add_argument(
        "--plain", action="store_true", help="raw extracted text instead of Markdown"
    )
    p_show.add_argument("--json", action="store_true", help="emit JSON instead of text")
    add_common(p_show)
    p_show.set_defaults(func=cmd_show)

    p_folders = sub.add_parser("folders", help="list folders with note counts")
    p_folders.add_argument("--json", action="store_true", help="emit JSON instead of text")
    add_common(p_folders)
    p_folders.set_defaults(func=cmd_folders)

    p_export = sub.add_parser("export", help="export every note to Markdown files")
    p_export.add_argument(
        "output", nargs="?", type=Path, default=Path.cwd() / "exported-notes",
        help="output directory (default: ./exported-notes)",
    )
    p_export.add_argument("--limit", type=int, help="export at most this many notes")
    p_export.add_argument("--dry-run", action="store_true", help="report without writing")
    p_export.add_argument(
        "--incremental", action="store_true",
        help="skip unchanged notes and prune deleted ones via the manifest",
    )
    p_export.add_argument(
        "--keep-stale", action="store_true",
        help="with --incremental, do not delete exports of removed notes",
    )
    p_export.add_argument(
        "--no-attachments", action="store_true", help="do not copy attachment files"
    )
    p_export.add_argument(
        "--write-claude-md", action="store_true",
        help="also drop a CLAUDE.md into the output directory describing the layout",
    )
    p_export.add_argument(
        "--no-snapshot", action="store_true",
        help="read the live database instead of copying it to a temporary snapshot first",
    )
    add_common(p_export)
    p_export.set_defaults(func=cmd_export)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
