"""Tests for the query layer, built on a synthetic Core Data store."""

from __future__ import annotations

import datetime as dt
import gzip
import sqlite3
import tempfile
import unittest
from pathlib import Path

from apple_notes_cli import search as search_mod
from apple_notes_cli.store import trashed_folder_pks

APPLE_EPOCH_OFFSET = 978307200.0


def varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def field(number: int, wire_type: int, payload: bytes | int) -> bytes:
    key = varint((number << 3) | wire_type)
    if wire_type == 0:
        return key + varint(payload)
    return key + varint(len(payload)) + payload


def note_payload(text: str) -> bytes:
    content = field(2, 2, text.encode("utf-8"))
    document = field(1, 0, 0) + field(2, 0, 0) + field(3, 2, content)
    return gzip.compress(field(1, 0, 0) + field(2, 2, document))


def apple_time(moment: dt.datetime) -> float:
    return moment.timestamp() - APPLE_EPOCH_OFFSET


SCHEMA = """
CREATE TABLE ZICCLOUDSYNCINGOBJECT (
    Z_PK INTEGER PRIMARY KEY,
    ZTITLE1 VARCHAR,
    ZTITLE2 VARCHAR,
    ZFOLDER INTEGER,
    ZPARENT INTEGER,
    ZFOLDERTYPE INTEGER,
    ZMARKEDFORDELETION INTEGER,
    ZISPASSWORDPROTECTED INTEGER,
    ZIDENTIFIER VARCHAR,
    ZCREATIONDATE TIMESTAMP,
    ZCREATIONDATE1 TIMESTAMP,
    ZCREATIONDATE2 TIMESTAMP,
    ZCREATIONDATE3 TIMESTAMP,
    ZMODIFICATIONDATE1 TIMESTAMP,
    ZNOTEDATA INTEGER
);
CREATE TABLE ZICNOTEDATA (
    Z_PK INTEGER PRIMARY KEY,
    ZNOTE INTEGER,
    ZDATA BLOB
);
"""


class QueryLayerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "NoteStore.sqlite"
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

        now = dt.datetime.now(dt.timezone.utc)
        # Folders: 10 = Work, 11 = Work/Deep, 20 = trash, 21 = a folder nested
        # inside the trash (Notes does not create these itself, but the parent
        # walk has to survive one).
        self._folder(10, "Work", None, 0)
        self._folder(11, "Deep", 10, 0)
        self._folder(20, "Recently Deleted", None, 1)
        self._folder(21, "Nested", 20, 0)

        self._note(1, "Alpha", 10, "telemetry is here", now - dt.timedelta(hours=1))
        self._note(2, "Beta", 11, "telemetry telemetry twice", now - dt.timedelta(days=5))
        self._note(3, "Gamma", 10, "nothing to see", now - dt.timedelta(minutes=10))
        self._note(4, "Trashed", 20, "telemetry in the bin", now - dt.timedelta(minutes=5))
        self._note(5, "NestedTrash", 21, "telemetry nested", now - dt.timedelta(minutes=5))
        self._note(6, "Locked", 10, "telemetry locked", now, protected=1)
        self._note(7, "Deleted", 10, "telemetry deleted", now, marked=1)
        self.conn.commit()

    def _folder(self, pk: int, title: str, parent: int | None, folder_type: int) -> None:
        self.conn.execute(
            "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, ZTITLE2, ZPARENT, ZFOLDERTYPE)"
            " VALUES (?, ?, ?, ?)",
            (pk, title, parent, folder_type),
        )

    def _note(
        self,
        pk: int,
        title: str,
        folder: int,
        body: str,
        modified: dt.datetime,
        *,
        protected: int = 0,
        marked: int = 0,
    ) -> None:
        note_pk = 1000 + pk
        self.conn.execute(
            "INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, ZTITLE1, ZFOLDER, ZMARKEDFORDELETION,"
            " ZISPASSWORDPROTECTED, ZIDENTIFIER, ZCREATIONDATE1, ZMODIFICATIONDATE1, ZNOTEDATA)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                note_pk,
                title,
                folder,
                marked,
                protected,
                f"uuid-{pk}",
                apple_time(modified),
                apple_time(modified),
                pk,
            ),
        )
        self.conn.execute(
            "INSERT INTO ZICNOTEDATA (Z_PK, ZNOTE, ZDATA) VALUES (?, ?, ?)",
            (pk, note_pk, note_payload(body)),
        )

    # --- trash handling -------------------------------------------------

    def test_trashed_folder_pks_includes_nested_folders(self):
        self.assertEqual(trashed_folder_pks(self.conn), {20, 21})

    def test_search_excludes_trashed_notes_by_default(self):
        titles = {hit.note.title for hit in search_mod.search(self.conn, "telemetry")}
        self.assertEqual(titles, {"Alpha", "Beta"})

    def test_search_can_opt_into_trashed_notes(self):
        titles = {
            hit.note.title
            for hit in search_mod.search(self.conn, "telemetry", include_trashed=True)
        }
        self.assertEqual(titles, {"Alpha", "Beta", "Trashed", "NestedTrash"})

    def test_recent_excludes_trashed_notes(self):
        self.assertEqual(
            [n.title for n in search_mod.recent(self.conn)], ["Gamma", "Alpha", "Beta"]
        )

    def test_locked_and_deleted_notes_are_excluded(self):
        titles = {
            hit.note.title
            for hit in search_mod.search(self.conn, "telemetry", include_trashed=True)
        }
        self.assertNotIn("Locked", titles)
        self.assertNotIn("Deleted", titles)

    # --- search behaviour -----------------------------------------------

    def test_hits_rank_by_match_count(self):
        hits = search_mod.search(self.conn, "telemetry")
        self.assertEqual([h.note.title for h in hits], ["Beta", "Alpha"])
        self.assertEqual([h.match_count for h in hits], [2, 1])

    def test_title_is_searched_unless_body_only(self):
        self.assertEqual(len(search_mod.search(self.conn, "Gamma")), 1)
        self.assertEqual(
            len(search_mod.search(self.conn, "Gamma", search_titles=False)), 0
        )

    def test_regex_search(self):
        hits = search_mod.search(self.conn, r"tele\w+try", regex=True)
        self.assertEqual({h.note.title for h in hits}, {"Alpha", "Beta"})

    def test_plain_query_is_not_treated_as_regex(self):
        self.assertEqual(search_mod.search(self.conn, "tele.metry"), [])

    def test_case_sensitivity(self):
        self.assertEqual(len(search_mod.search(self.conn, "TELEMETRY")), 2)
        self.assertEqual(
            len(search_mod.search(self.conn, "TELEMETRY", case_sensitive=True)), 0
        )

    def test_since_filters_older_notes(self):
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
        titles = {h.note.title for h in search_mod.search(self.conn, "telemetry", since=since)}
        self.assertEqual(titles, {"Alpha"})

    def test_folder_filter_matches_nested_paths(self):
        hits = search_mod.search(self.conn, "telemetry", folder="Work/Deep")
        self.assertEqual([h.note.title for h in hits], ["Beta"])

    def test_snippets_carry_context_and_respect_cap(self):
        hit = search_mod.search(self.conn, "telemetry", max_snippets=1)[0]
        self.assertEqual(len(hit.snippets), 1)
        self.assertIn("telemetry", hit.snippets[0])

    # --- folders and show ------------------------------------------------

    def test_folders_reports_counts_without_trash(self):
        self.assertEqual(
            search_mod.folders(self.conn), [("Work", 3), ("Work/Deep", 1)]
        )

    def test_show_returns_body_and_metadata(self):
        note, body = search_mod.show(self.conn, 1001)
        self.assertEqual(note.title, "Alpha")
        self.assertEqual(note.folder, "Work")
        self.assertIn("telemetry", body)

    def test_show_raises_for_unknown_id(self):
        with self.assertRaises(KeyError):
            search_mod.show(self.conn, 999999)


class ParseSinceTests(unittest.TestCase):
    def test_relative_offsets(self):
        now = dt.datetime.now(dt.timezone.utc)
        self.assertAlmostEqual(
            (now - search_mod.parse_since("2d")).total_seconds(), 172800, delta=5
        )
        self.assertAlmostEqual(
            (now - search_mod.parse_since("30m")).total_seconds(), 1800, delta=5
        )
        self.assertAlmostEqual(
            (now - search_mod.parse_since("1w")).total_seconds(), 604800, delta=5
        )

    def test_iso_date_is_localized(self):
        parsed = search_mod.parse_since("2026-09-01")
        self.assertEqual((parsed.year, parsed.month, parsed.day), (2026, 9, 1))
        self.assertIsNotNone(parsed.tzinfo)

    def test_invalid_value_raises_value_error(self):
        with self.assertRaises(ValueError):
            search_mod.parse_since("last tuesday")


if __name__ == "__main__":
    unittest.main()
