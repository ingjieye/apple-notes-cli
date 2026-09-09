"""Search and browse notes straight from the live database.

Apple's store keeps note bodies as gzipped protobuf in ``ZICNOTEDATA.ZDATA``,
and there is no FTS index anywhere in the schema, so full-text search means
inflating every body and matching in memory. That sounds expensive and isn't:
a ~800-note store decodes end to end in well under a second, which is why this
does no caching and always reports the current state of the database.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import re
import sqlite3

from .parser import extract_note_markdown, extract_note_text
from .store import apple_date, query_folder_paths, trashed_folder_pks

SINCE_RE = re.compile(r"^(\d+)([smhdw])$")
SINCE_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
APPLE_EPOCH_OFFSET = 978307200.0


@dataclasses.dataclass(frozen=True)
class NoteSummary:
    """A note's metadata, read without inflating its body."""

    pk: int
    title: str
    folder: str
    created: str | None
    modified: str | None


@dataclasses.dataclass(frozen=True)
class SearchHit:
    note: NoteSummary
    match_count: int
    snippets: list[str]


def parse_since(value: str) -> dt.datetime:
    """Accept a relative offset (``30m``, ``2d``, ``1w``) or an ISO date."""
    match = SINCE_RE.match(value.strip())
    if match:
        amount, unit = int(match.group(1)), match.group(2)
        delta = dt.timedelta(**{SINCE_UNITS[unit]: amount})
        return dt.datetime.now(dt.timezone.utc) - delta
    text = value.strip()
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(
            f"cannot read {value!r} as a time; use 30m/6h/2d/1w or 2026-09-01"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def _to_apple_time(moment: dt.datetime) -> float:
    return moment.timestamp() - APPLE_EPOCH_OFFSET


def _build_query(
    *,
    since: dt.datetime | None,
    folder: str | None,
    include_password_protected: bool,
    with_body: bool,
    newest_first: bool,
    limit: int | None,
    trashed: set[int],
) -> tuple[str, list]:
    columns = [
        "n.Z_PK",
        "n.ZTITLE1",
        "n.ZFOLDER",
        # Which creation-date column is populated varies by how and when the
        # note was created, so every candidate is read and the first non-null
        # one wins. Matches the exporter's behaviour.
        "n.ZCREATIONDATE",
        "n.ZCREATIONDATE1",
        "n.ZCREATIONDATE2",
        "n.ZCREATIONDATE3",
        "n.ZMODIFICATIONDATE1",
    ]
    if with_body:
        columns.append("d.ZDATA")
    where = ["d.ZDATA IS NOT NULL", "IFNULL(n.ZMARKEDFORDELETION, 0) = 0"]
    params: list = []
    if not include_password_protected:
        where.append("IFNULL(n.ZISPASSWORDPROTECTED, 0) = 0")
    if trashed:
        placeholders = ", ".join(str(int(pk)) for pk in sorted(trashed))
        where.append(f"IFNULL(n.ZFOLDER, 0) NOT IN ({placeholders})")
    if since is not None:
        where.append("n.ZMODIFICATIONDATE1 >= ?")
        params.append(_to_apple_time(since))
    order = "DESC" if newest_first else "ASC"
    sql = f"""
        SELECT {", ".join(columns)}
        FROM ZICCLOUDSYNCINGOBJECT n
        JOIN ZICNOTEDATA d ON n.Z_PK = d.ZNOTE
        WHERE {" AND ".join(where)}
        ORDER BY n.ZMODIFICATIONDATE1 {order}, n.Z_PK {order}
    """
    # A folder filter cannot go in SQL: folder paths are assembled in Python
    # from the parent chain, so limiting rows here would drop nested matches.
    if limit is not None and folder is None:
        sql += " LIMIT ?"
        params.append(limit)
    return sql, params


def _created(row: sqlite3.Row) -> str | None:
    for column in (
        "ZCREATIONDATE1",
        "ZCREATIONDATE",
        "ZCREATIONDATE2",
        "ZCREATIONDATE3",
    ):
        with contextlib.suppress(IndexError):
            stamp = apple_date(row[column])
            if stamp:
                return stamp
    return None


def _summary(row: sqlite3.Row, folder_paths: dict[int, str]) -> NoteSummary:
    folder = folder_paths.get(row["ZFOLDER"], "Notes") if row["ZFOLDER"] else "Notes"
    return NoteSummary(
        pk=int(row["Z_PK"]),
        title=(row["ZTITLE1"] or "Untitled").strip() or "Untitled",
        folder=folder,
        created=_created(row),
        modified=apple_date(row["ZMODIFICATIONDATE1"]),
    )


def _folder_matches(candidate: str, wanted: str) -> bool:
    return wanted.lower() in candidate.lower()


def recent(
    conn: sqlite3.Connection,
    *,
    limit: int | None = 20,
    since: dt.datetime | None = None,
    folder: str | None = None,
    include_password_protected: bool = False,
    include_trashed: bool = False,
) -> list[NoteSummary]:
    """List notes newest-first. Reads metadata only — no body inflation."""
    folder_paths = query_folder_paths(conn)
    sql, params = _build_query(
        since=since,
        folder=folder,
        include_password_protected=include_password_protected,
        with_body=False,
        newest_first=True,
        limit=limit,
        trashed=set() if include_trashed else trashed_folder_pks(conn),
    )
    results = []
    for row in conn.execute(sql, params):
        summary = _summary(row, folder_paths)
        if folder and not _folder_matches(summary.folder, folder):
            continue
        results.append(summary)
        if limit is not None and len(results) >= limit:
            break
    return results


def _snippet(text: str, start: int, end: int, width: int) -> str:
    left = max(0, start - width)
    right = min(len(text), end + width)
    fragment = text[left:right].replace("\n", " ").strip()
    fragment = re.sub(r"\s+", " ", fragment)
    prefix = "…" if left > 0 else ""
    suffix = "…" if right < len(text) else ""
    return f"{prefix}{fragment}{suffix}"


def search(
    conn: sqlite3.Connection,
    query: str,
    *,
    regex: bool = False,
    case_sensitive: bool = False,
    limit: int | None = None,
    since: dt.datetime | None = None,
    folder: str | None = None,
    include_password_protected: bool = False,
    include_trashed: bool = False,
    snippet_width: int = 60,
    max_snippets: int = 3,
    search_titles: bool = True,
) -> list[SearchHit]:
    """Full-text search across every note body.

    Every candidate body is inflated and matched in memory; results are ranked
    by match count, then by recency.
    """
    flags = 0 if case_sensitive else re.IGNORECASE
    pattern = re.compile(query if regex else re.escape(query), flags)

    folder_paths = query_folder_paths(conn)
    sql, params = _build_query(
        since=since,
        folder=folder,
        include_password_protected=include_password_protected,
        with_body=True,
        newest_first=True,
        limit=None,
        trashed=set() if include_trashed else trashed_folder_pks(conn),
    )

    hits: list[SearchHit] = []
    for row in conn.execute(sql, params):
        summary = _summary(row, folder_paths)
        if folder and not _folder_matches(summary.folder, folder):
            continue
        try:
            body = extract_note_text(row["ZDATA"])
        except Exception:
            # A single unreadable note must not abort the whole search.
            continue
        haystack = f"{summary.title}\n{body}" if search_titles else body
        matches = list(pattern.finditer(haystack))
        if not matches:
            continue
        snippets = [
            _snippet(haystack, m.start(), m.end(), snippet_width)
            for m in matches[:max_snippets]
        ]
        hits.append(SearchHit(note=summary, match_count=len(matches), snippets=snippets))

    # ISO-8601 strings sort chronologically, so one pass ranks by match count
    # and breaks ties by recency.
    hits.sort(key=lambda h: (h.match_count, h.note.modified or ""), reverse=True)
    return hits[:limit] if limit is not None else hits


def show(
    conn: sqlite3.Connection,
    pk: int,
    *,
    markdown: bool = True,
) -> tuple[NoteSummary, str]:
    """Fetch one note's metadata and body by primary key."""
    row = conn.execute(
        """
        SELECT n.Z_PK, n.ZTITLE1, n.ZFOLDER, n.ZCREATIONDATE,
               n.ZCREATIONDATE1, n.ZCREATIONDATE2, n.ZCREATIONDATE3,
               n.ZMODIFICATIONDATE1, d.ZDATA
        FROM ZICCLOUDSYNCINGOBJECT n
        JOIN ZICNOTEDATA d ON n.Z_PK = d.ZNOTE
        WHERE n.Z_PK = ? AND d.ZDATA IS NOT NULL
        """,
        (pk,),
    ).fetchone()
    if row is None:
        raise KeyError(f"no note with id {pk}")
    summary = _summary(row, query_folder_paths(conn))
    body = (
        extract_note_markdown(row["ZDATA"]) if markdown else extract_note_text(row["ZDATA"])
    )
    return summary, body


def folders(
    conn: sqlite3.Connection, *, include_trashed: bool = False
) -> list[tuple[str, int]]:
    """Every folder path with its note count, ordered by path."""
    folder_paths = query_folder_paths(conn)
    trashed = set() if include_trashed else trashed_folder_pks(conn)
    counts: dict[str, int] = {}
    rows = conn.execute(
        """
        SELECT n.ZFOLDER
        FROM ZICCLOUDSYNCINGOBJECT n
        JOIN ZICNOTEDATA d ON n.Z_PK = d.ZNOTE
        WHERE d.ZDATA IS NOT NULL AND IFNULL(n.ZMARKEDFORDELETION, 0) = 0
        """
    )
    for row in rows:
        if row["ZFOLDER"] and int(row["ZFOLDER"]) in trashed:
            continue
        path = folder_paths.get(row["ZFOLDER"], "Notes") if row["ZFOLDER"] else "Notes"
        counts[path] = counts.get(path, 0) + 1
    return sorted(counts.items())
