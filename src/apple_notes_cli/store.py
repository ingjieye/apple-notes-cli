"""Read-only access to the Apple Notes Core Data store.

Two ways in:

* ``connect()`` opens the live database with ``mode=ro``. SQLite's WAL mode
  supports concurrent readers, so this is safe while Notes.app is running and
  it always sees the newest committed data.
* ``snapshot()`` copies the database via ``backup()`` first. Slower, but gives
  a stable point-in-time view for long operations like a full export.

Never open the store with ``immutable=1``: that flag makes SQLite skip the
``-wal`` file entirely, silently serving a stale snapshot that is missing the
most recent notes.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import sqlite3
import tempfile
from pathlib import Path
from typing import Iterator

from .models import Attachment, EmbeddedTable, Note
from .parser import (
    URL_RE,
    extract_markdown_table,
    extract_note_markdown,
    summary_table_fallback,
)
from .utils import sanitize_filename

APPLE_EPOCH = dt.datetime(2001, 1, 1, tzinfo=dt.timezone.utc)
DEFAULT_NOTES_DIR = Path.home() / "Library/Group Containers/group.com.apple.notes"


def default_database(notes_dir: Path | None = None) -> Path:
    return (notes_dir or DEFAULT_NOTES_DIR) / "NoteStore.sqlite"


def connect(database: Path) -> sqlite3.Connection:
    """Open the live store read-only. Sees WAL content; does not block Notes.app."""
    if not database.exists():
        raise FileNotFoundError(
            f"Apple Notes database not found at {database}.\n"
            "Notes.app must have been launched at least once on this machine."
        )
    conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def apple_date(value: float | int | None) -> str | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if numeric <= 0:
        return None
    return (APPLE_EPOCH + dt.timedelta(seconds=numeric)).isoformat()


def parse_iso_timestamp(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    with contextlib.suppress(ValueError):
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed
    return None




def query_folder_paths(conn: sqlite3.Connection) -> dict[int, str]:
    rows = conn.execute(
        """
        SELECT Z_PK, ZTITLE2, ZPARENT
        FROM ZICCLOUDSYNCINGOBJECT
        WHERE ZTITLE2 IS NOT NULL
        """
    ).fetchall()
    titles = {int(row["Z_PK"]): row["ZTITLE2"] or "Notes" for row in rows}
    parents = {int(row["Z_PK"]): row["ZPARENT"] for row in rows}
    cache: dict[int, str] = {}

    def build(folder_id: int, seen: set[int] | None = None) -> str:
        if folder_id in cache:
            return cache[folder_id]
        if seen is None:
            seen = set()
        if folder_id in seen:
            return sanitize_filename(titles.get(folder_id, "Notes"))
        seen.add(folder_id)
        title = sanitize_filename(titles.get(folder_id, "Notes"))
        parent = parents.get(folder_id)
        if parent is None or int(parent) not in titles:
            cache[folder_id] = title
        else:
            cache[folder_id] = str(Path(build(int(parent), seen)) / title)
        return cache[folder_id]

    for folder_id in titles:
        build(folder_id)
    return cache


def trashed_folder_pks(conn: sqlite3.Connection) -> set[int]:
    """Folder primary keys for the trash, plus anything nested inside it.

    ``ZFOLDERTYPE == 1`` marks Recently Deleted independently of the UI
    language, so this beats matching a localized folder name. Notes sitting in
    the trash keep ``ZMARKEDFORDELETION = 0``, which is why filtering on that
    column alone still surfaces them.
    """
    rows = conn.execute(
        """
        SELECT Z_PK, ZPARENT, ZFOLDERTYPE
        FROM ZICCLOUDSYNCINGOBJECT
        WHERE ZTITLE2 IS NOT NULL
        """
    ).fetchall()
    parents = {int(row["Z_PK"]): row["ZPARENT"] for row in rows}
    roots = {int(row["Z_PK"]) for row in rows if row["ZFOLDERTYPE"] == 1}
    trashed = set(roots)
    for folder_pk in parents:
        chain: set[int] = set()
        current: int | None = folder_pk
        while current is not None and current not in chain:
            chain.add(current)
            if current in roots:
                trashed |= chain
                break
            parent = parents.get(current)
            current = int(parent) if parent is not None else None
    return trashed


def find_media_file(notes_dir: Path, media_id: str | None, generation: str | None, filename: str | None) -> Path | None:
    if not media_id:
        return None
    accounts_dir = notes_dir / "Accounts"
    if not accounts_dir.exists():
        return None
    for account_dir in accounts_dir.iterdir():
        media_dir = account_dir / "Media" / media_id
        if not media_dir.exists():
            continue
        candidates: list[Path] = []
        if generation and filename:
            candidates.append(media_dir / generation / filename)
        if filename:
            candidates.extend(media_dir.glob(f"*/{filename}"))
        candidates.extend(path for path in media_dir.glob("*/*") if path.is_file())
        for candidate in candidates:
            if candidate.is_file():
                return candidate
    return None



def query_embedded_tables(conn: sqlite3.Connection, note_pk: int) -> list[EmbeddedTable]:
    rows = conn.execute(
        """
        SELECT
            ZIDENTIFIER AS table_identifier,
            ZMERGEABLEDATA1 AS mergeable_data,
            ZMERGEABLEDATA AS legacy_mergeable_data,
            ZSUMMARY AS summary
        FROM ZICCLOUDSYNCINGOBJECT
        WHERE ZNOTE = ?
          AND ZTYPEUTI = 'com.apple.notes.table'
        ORDER BY Z_PK
        """,
        (note_pk,),
    ).fetchall()

    tables: list[EmbeddedTable] = []
    for row in rows:
        markdown = ""
        data = row["mergeable_data"] or row["legacy_mergeable_data"]
        if data:
            with contextlib.suppress(ValueError, OSError, UnicodeDecodeError):
                markdown = extract_markdown_table(data)
        if not markdown:
            markdown = summary_table_fallback(row["summary"])
        if markdown:
            tables.append(EmbeddedTable(identifier=row["table_identifier"] or "", markdown=markdown))
    return tables


def query_attachments(conn: sqlite3.Connection, notes_dir: Path, note_pk: int) -> list[Attachment]:
    rows = conn.execute(
        """
        SELECT
            a.ZIDENTIFIER AS attachment_identifier,
            a.ZTYPEUTI AS attachment_type,
            m.ZIDENTIFIER AS media_identifier,
            m.ZFILENAME AS media_filename,
            m.ZGENERATION1 AS media_generation
        FROM ZICCLOUDSYNCINGOBJECT a
        LEFT JOIN ZICCLOUDSYNCINGOBJECT m ON a.ZMEDIA = m.Z_PK
        WHERE a.ZNOTE = ?
          AND a.ZMEDIA IS NOT NULL
        ORDER BY a.Z_PK
        """,
        (note_pk,),
    ).fetchall()

    attachments: list[Attachment] = []
    for row in rows:
        attachment_id = row["attachment_identifier"] or row["media_identifier"] or "attachment"
        source = find_media_file(
            notes_dir,
            row["media_identifier"],
            row["media_generation"],
            row["media_filename"],
        )
        suffix = ""
        if row["media_filename"]:
            suffix = Path(row["media_filename"]).suffix
        output_name = sanitize_filename(f"{attachment_id.lower()}{suffix}", fallback="attachment")
        attachments.append(
            Attachment(
                identifier=attachment_id,
                type_uti=row["attachment_type"],
                source_path=source,
                output_name=output_name,
            )
        )
    return attachments


def attachment_reference_path(note_id: int, output_name: str) -> str:
    return str(Path("attachments") / str(note_id) / output_name)


def nest_attachments_under_note_id(attachments: list[Attachment], note_id: int) -> list[Attachment]:
    return [
        dataclasses.replace(
            attachment,
            output_name=attachment_reference_path(note_id, Path(attachment.output_name).name),
        )
        for attachment in attachments
    ]


def replace_attachment_placeholders(body: str, attachments: list[Attachment]) -> str:
    result = body
    inserted = 0
    for attachment in attachments:
        if "\ufffc" not in result:
            break
        result = result.replace("\ufffc", attachment.markdown_reference, 1)
        inserted += 1

    remaining = attachments[inserted:]
    if remaining:
        lines = ["", "## Attachments", ""]
        lines.extend(f"- {attachment.markdown_reference}" for attachment in remaining)
        result = result.rstrip() + "\n" + "\n".join(lines)
    return result


def replace_table_placeholders(body: str, tables: list[EmbeddedTable]) -> str:
    result = body
    inserted = 0
    for table in tables:
        if "\ufffc" not in result:
            break
        result = result.replace("\ufffc", "\n\n" + table.markdown + "\n\n", 1)
        inserted += 1

    remaining = tables[inserted:]
    if remaining:
        lines = ["", "## Tables", ""]
        for table in remaining:
            lines.extend([table.markdown, ""])
        result = result.rstrip() + "\n" + "\n".join(lines)
    return result


def extract_links(body: str) -> list[str]:
    seen: set[str] = set()
    links: list[str] = []
    for match in URL_RE.finditer(body):
        url = match.group(0).rstrip(".,;:!?)]}")
        if url and url not in seen:
            seen.add(url)
            links.append(url)
    return links




def copy_live_database(source: Path) -> Path:
    temp = tempfile.NamedTemporaryFile(prefix="apple-notes-", suffix=".sqlite", delete=False)
    temp_path = Path(temp.name)
    temp.close()

    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(temp_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return temp_path


def iter_notes(
    conn: sqlite3.Connection,
    notes_dir: Path,
    include_password_protected: bool = False,
    limit: int | None = None,
    include_trashed: bool = False,
) -> Iterator[Note]:
    folder_paths = query_folder_paths(conn)
    where = [
        "d.ZDATA IS NOT NULL",
        "IFNULL(n.ZMARKEDFORDELETION, 0) = 0",
    ]
    if not include_password_protected:
        where.append("IFNULL(n.ZISPASSWORDPROTECTED, 0) = 0")
    if not include_trashed:
        trashed = trashed_folder_pks(conn)
        if trashed:
            placeholders = ", ".join(str(int(pk)) for pk in sorted(trashed))
            where.append(f"IFNULL(n.ZFOLDER, 0) NOT IN ({placeholders})")
    sql = f"""
        SELECT
            n.Z_PK,
            n.ZTITLE1,
            n.ZFOLDER,
            n.ZIDENTIFIER,
            n.ZCREATIONDATE,
            n.ZCREATIONDATE1,
            n.ZCREATIONDATE2,
            n.ZCREATIONDATE3,
            n.ZMODIFICATIONDATE1,
            d.ZDATA
        FROM ZICCLOUDSYNCINGOBJECT n
        JOIN ZICNOTEDATA d ON n.Z_PK = d.ZNOTE
        WHERE {" AND ".join(where)}
        ORDER BY n.ZMODIFICATIONDATE1 ASC, n.Z_PK ASC
    """
    if limit is not None:
        sql += " LIMIT ?"
        rows = conn.execute(sql, (limit,))
    else:
        rows = conn.execute(sql)

    for row in rows:
        pk = int(row["Z_PK"])
        tables = query_embedded_tables(conn, pk)
        body = extract_note_markdown(row["ZDATA"])
        body = replace_table_placeholders(body, tables)
        attachments = query_attachments(conn, notes_dir, pk)
        title = note_title(row["ZTITLE1"], body)
        attachments = nest_attachments_under_note_id(attachments, pk)
        body = replace_attachment_placeholders(body, attachments)
        folder_path = folder_paths.get(row["ZFOLDER"], "Notes") if row["ZFOLDER"] else "Notes"
        yield Note(
            pk=pk,
            title=title,
            apple_notes_id=row["ZIDENTIFIER"] or str(pk),
            folder_path=folder_path,
            created=apple_date(row["ZCREATIONDATE1"])
            or apple_date(row["ZCREATIONDATE"])
            or apple_date(row["ZCREATIONDATE2"])
            or apple_date(row["ZCREATIONDATE3"]),
            modified=apple_date(row["ZMODIFICATIONDATE1"]),
            body=body,
            attachments=attachments,
            links=extract_links(body),
        )



def note_title(row_title: str | None, body: str) -> str:
    if row_title and row_title.strip():
        return row_title.strip()
    for line in body.splitlines():
        if line.strip():
            return line.strip()
    return "Untitled"


