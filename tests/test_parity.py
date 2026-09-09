from __future__ import annotations

import gzip
import tempfile
import types
import unittest
import datetime as dt
from pathlib import Path


from apple_notes_cli import export, models, parser, store, utils

# These tests were written against the original single-file exporter. The code
# is now split across modules, so this flattens the public names back into one
# namespace and the assertions below stay byte-for-byte identical -- which is
# exactly what makes them useful as a regression check on the split.
export_apple_notes = types.SimpleNamespace()
for _module in (models, parser, utils, store, export):
    for _name in dir(_module):
        if not _name.startswith("_"):
            setattr(export_apple_notes, _name, getattr(_module, _name))


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
        assert isinstance(payload, int)
        return key + varint(payload)
    assert isinstance(payload, bytes)
    return key + varint(len(payload)) + payload


def note_payload(text: str, attribute_runs: list[bytes] | None = None) -> bytes:
    content = field(2, 2, text.encode("utf-8"))
    for attribute_run in attribute_runs or []:
        content += field(5, 2, attribute_run)
    document = field(1, 0, 0) + field(2, 0, 0) + field(3, 2, content)
    top = field(1, 0, 0) + field(2, 2, document)
    return gzip.compress(top)


def checklist_attribute_run(length: int, done: int, indent: int = 0) -> bytes:
    checklist = field(1, 2, b"uuid") + field(2, 0, done)
    paragraph_style = field(1, 0, export_apple_notes.STYLE_TYPE_CHECKBOX)
    if indent:
        paragraph_style += field(4, 0, indent)
    paragraph_style += field(5, 2, checklist)
    return field(1, 0, length) + field(2, 2, paragraph_style)


def paragraph_attribute_run(
    length: int,
    style_type: int | None,
    indent: int = 0,
    block_quote: bool = False,
) -> bytes:
    paragraph_style = b""
    if style_type is not None:
        paragraph_style += field(1, 0, style_type)
    if indent:
        paragraph_style += field(4, 0, indent)
    if block_quote:
        paragraph_style += field(8, 0, export_apple_notes.STYLE_TYPE_BLOCK_QUOTE)
    return field(1, 0, length) + field(2, 2, paragraph_style)


def inline_attribute_run(
    length: int,
    *,
    font_weight: int | None = None,
    underlined: int | None = None,
    strikethrough: int | None = None,
    superscript: int | None = None,
    link: str | None = None,
) -> bytes:
    payload = field(1, 0, length)
    if font_weight is not None:
        payload += field(5, 0, font_weight)
    if underlined is not None:
        payload += field(6, 0, underlined)
    if strikethrough is not None:
        payload += field(7, 0, strikethrough)
    if superscript is not None:
        payload += field(8, 0, superscript)
    if link is not None:
        payload += field(9, 2, link.encode("utf-8"))
    return payload


class AppleNotesExportTests(unittest.TestCase):
    def test_extract_note_text_from_gzipped_notestore_proto(self):
        payload = note_payload("Title\n\nbody line")

        self.assertEqual(export_apple_notes.extract_note_text(payload), "Title\n\nbody line")

    def test_extract_note_markdown_renders_checklists(self):
        payload = note_payload(
            "\nTask\nDone\nNested\n",
            [
                checklist_attribute_run(1, 0),
                checklist_attribute_run(5, 0),
                checklist_attribute_run(5, 1),
                checklist_attribute_run(7, 0, indent=1),
            ],
        )

        self.assertEqual(
            export_apple_notes.extract_note_markdown(payload),
            "- [ ] Task\n- [x] Done\n  - [ ] Nested",
        )

    def test_extract_note_markdown_renders_block_styles(self):
        payload = note_payload(
            "\nTitle\nHeading\nSub\nCode\nQuote\nBullet\nOne\nTwo\n",
            [
                paragraph_attribute_run(1, export_apple_notes.STYLE_TYPE_TITLE),
                paragraph_attribute_run(6, export_apple_notes.STYLE_TYPE_TITLE),
                paragraph_attribute_run(8, export_apple_notes.STYLE_TYPE_HEADING),
                paragraph_attribute_run(4, export_apple_notes.STYLE_TYPE_SUBHEADING),
                paragraph_attribute_run(5, export_apple_notes.STYLE_TYPE_MONOSPACED),
                paragraph_attribute_run(6, None, indent=1, block_quote=True),
                paragraph_attribute_run(7, export_apple_notes.STYLE_TYPE_DOTTED_LIST),
                paragraph_attribute_run(4, export_apple_notes.STYLE_TYPE_NUMBERED_LIST),
                paragraph_attribute_run(4, export_apple_notes.STYLE_TYPE_NUMBERED_LIST),
            ],
        )

        self.assertEqual(
            export_apple_notes.extract_note_markdown(payload),
            "### Title\n#### Heading\n##### Sub\n    Code\n> Quote\n- Bullet\n1. One\n2. Two",
        )

    def test_extract_note_markdown_does_not_wrap_trailing_space_in_bold(self):
        payload = note_payload(
            "产量： 约占全球咖啡产量",
            [
                inline_attribute_run(4, font_weight=export_apple_notes.FONT_TYPE_BOLD),
            ],
        )

        self.assertEqual(
            export_apple_notes.extract_note_markdown(payload),
            "**产量：** 约占全球咖啡产量",
        )

    def test_extract_note_markdown_adds_space_after_bold_label(self):
        payload = note_payload(
            "产量：约占全球咖啡产量",
            [
                inline_attribute_run(3, font_weight=export_apple_notes.FONT_TYPE_BOLD),
            ],
        )

        self.assertEqual(
            export_apple_notes.extract_note_markdown(payload),
            "**产量：** 约占全球咖啡产量",
        )

    def test_extract_note_markdown_preserves_tab_indented_plain_blocks(self):
        payload = note_payload("RC Account\n\t0:N WABA\n\t\t1:N Phone")

        self.assertEqual(
            export_apple_notes.extract_note_markdown(payload),
            "```\nRC Account\n\t0:N WABA\n\t\t1:N Phone\n```",
        )

    def test_extract_note_markdown_preserves_arrow_flow_blocks(self):
        payload = note_payload("Meta\n↓\nTIG -> Verification\n↓\nNECA")

        self.assertEqual(
            export_apple_notes.extract_note_markdown(payload),
            "```\nMeta\n↓\nTIG -> Verification\n↓\nNECA\n```",
        )

    def test_extract_note_markdown_preserves_plain_line_breaks(self):
        payload = note_payload("SW: https://example.com\nBUS: test/test\nLogInsight: http://log")

        self.assertEqual(
            export_apple_notes.extract_note_markdown(payload),
            "SW: https://example.com  \nBUS: test/test  \nLogInsight: http://log",
        )

    def test_extract_note_markdown_does_not_fence_mixed_markdown_blocks(self):
        payload = note_payload("Heading\n[https://example.com](https://example.com)\n\tIndented")

        self.assertEqual(
            export_apple_notes.extract_note_markdown(payload),
            "Heading  \n[https://example.com](https://example.com)  \n&emsp;Indented",
        )

    def test_extract_note_markdown_merges_leading_plain_style_fragment(self):
        payload = note_payload(
            "主要产地： 巴西",
            [
                paragraph_attribute_run(1, None),
                inline_attribute_run(4, font_weight=export_apple_notes.FONT_TYPE_BOLD),
            ],
        )

        self.assertEqual(
            export_apple_notes.extract_note_markdown(payload),
            "**主要产地：** 巴西",
        )

    def test_extract_note_markdown_uses_dominant_block_style(self):
        payload = note_payload(
            "产量：约",
            [
                paragraph_attribute_run(1, export_apple_notes.STYLE_TYPE_HEADING),
                paragraph_attribute_run(4, export_apple_notes.STYLE_TYPE_DOTTED_LIST),
            ],
        )

        self.assertEqual(export_apple_notes.extract_note_markdown(payload), "- 产量：约")

    def test_extract_note_markdown_preserves_numbering_across_nested_bullets(self):
        payload = note_payload(
            "南美洲\n巴西豆\n中美洲\n哥斯达\n非洲\n埃塞",
            [
                paragraph_attribute_run(1, export_apple_notes.STYLE_TYPE_HEADING),
                paragraph_attribute_run(3, export_apple_notes.STYLE_TYPE_NUMBERED_LIST),
                paragraph_attribute_run(1, export_apple_notes.STYLE_TYPE_NUMBERED_LIST),
                paragraph_attribute_run(3, export_apple_notes.STYLE_TYPE_DOTTED_LIST, indent=1),
                paragraph_attribute_run(1, export_apple_notes.STYLE_TYPE_DOTTED_LIST, indent=1),
                paragraph_attribute_run(3, export_apple_notes.STYLE_TYPE_NUMBERED_LIST),
                paragraph_attribute_run(1, export_apple_notes.STYLE_TYPE_NUMBERED_LIST),
                paragraph_attribute_run(2, export_apple_notes.STYLE_TYPE_DOTTED_LIST, indent=1),
                paragraph_attribute_run(1, export_apple_notes.STYLE_TYPE_DOTTED_LIST, indent=1),
                paragraph_attribute_run(2, export_apple_notes.STYLE_TYPE_NUMBERED_LIST),
                paragraph_attribute_run(1, export_apple_notes.STYLE_TYPE_NUMBERED_LIST),
                paragraph_attribute_run(2, export_apple_notes.STYLE_TYPE_DOTTED_LIST, indent=1),
            ],
        )

        self.assertEqual(
            export_apple_notes.extract_note_markdown(payload),
            "1. 南美洲\n  - 巴西豆\n2. 中美洲\n  - 哥斯达\n3. 非洲\n  - 埃塞",
        )

    def test_extract_note_markdown_renders_inline_styles(self):
        payload = note_payload(
            "BoldItalicBothUnderStrikeSupLink",
            [
                inline_attribute_run(4, font_weight=export_apple_notes.FONT_TYPE_BOLD),
                inline_attribute_run(6, font_weight=export_apple_notes.FONT_TYPE_ITALIC),
                inline_attribute_run(4, font_weight=export_apple_notes.FONT_TYPE_BOLD_ITALIC),
                inline_attribute_run(5, underlined=1),
                inline_attribute_run(6, strikethrough=1),
                inline_attribute_run(3, superscript=1),
                inline_attribute_run(4, link="https://example.com"),
            ],
        )

        self.assertEqual(
            export_apple_notes.extract_note_markdown(payload),
            "**Bold***Italic****Both***<u>Under</u>~~Strike~~<sup>Sup</sup>[Link](https://example.com)",
        )

    def test_markdown_table_escapes_cells(self):
        table = export_apple_notes.markdown_table([["A|B", "C"], ["one\ntwo", "three"]])

        self.assertEqual(table, "| A\\|B | C |\n| --- | --- |\n| one<br>two | three |")

    def test_replace_table_placeholders_in_order(self):
        tables = [
            export_apple_notes.EmbeddedTable("T1", "| A |\n| --- |\n| 1 |"),
            export_apple_notes.EmbeddedTable("T2", "| B |\n| --- |\n| 2 |"),
        ]

        body = export_apple_notes.replace_table_placeholders("before\n\ufffc\nmiddle\n\ufffc\nafter", tables)

        self.assertIn("| A |\n| --- |\n| 1 |", body)
        self.assertIn("| B |\n| --- |\n| 2 |", body)

    def test_extract_links_matches_urls_before_chinese_punctuation(self):
        body = "https://wiki.example.com/a 账号\nhttps://git.example.com/x?ref_type=heads，/ai"

        self.assertEqual(
            export_apple_notes.extract_links(body),
            [
                "https://wiki.example.com/a",
                "https://git.example.com/x?ref_type=heads",
            ],
        )

    def test_sanitize_filename_matches_alto_style_for_url_like_titles(self):
        title = "https://platform.example.com/external-channels"

        self.assertEqual(
            export_apple_notes.sanitize_filename(title),
            "https---platform.example.com-external-channels",
        )

    def test_build_markdown_frontmatter_shape(self):
        note = export_apple_notes.Note(
            pk=42,
            title="Example",
            apple_notes_id="ABC-123",
            folder_path="Notes",
            created="2026-06-10T00:00:00+00:00",
            modified="2026-06-10T01:00:00+00:00",
            body="Example\n\nbody",
            attachments=[],
            links=["https://example.com"],
        )

        markdown = export_apple_notes.build_markdown(note)

        self.assertTrue(markdown.startswith("---\n"))
        self.assertIn("title: \"Example\"\n", markdown)
        self.assertIn("id: 42\n", markdown)
        self.assertIn("apple_notes_id: \"ABC-123\"\n", markdown)
        self.assertIn("attachments: []\n", markdown)
        self.assertIn("source: notes://showNote?identifier=ABC-123\n", markdown)
        self.assertTrue(markdown.endswith("Example\n\nbody\n"))

    def test_note_signature_changes_when_body_changes(self):
        note = export_apple_notes.Note(
            pk=1,
            title="Example",
            apple_notes_id="A",
            folder_path="Notes",
            created=None,
            modified=None,
            body="old",
            attachments=[],
            links=[],
        )
        changed = export_apple_notes.dataclasses.replace(note, body="new")

        self.assertNotEqual(export_apple_notes.note_signature(note), export_apple_notes.note_signature(changed))

    def test_manifest_roundtrip_and_safe_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            note_file = root / "Notes" / "Example-1.md"
            attachment_file = root / "Notes" / "image.png"
            outside_file = root.parent / "outside-apple-notes-export-test.txt"
            note_file.parent.mkdir()
            note_file.write_text("note", encoding="utf-8")
            attachment_file.write_text("attachment", encoding="utf-8")
            outside_file.write_text("outside", encoding="utf-8")
            self.addCleanup(lambda: outside_file.unlink(missing_ok=True))

            entry = {
                "path": "Notes/Example-1.md",
                "attachments": ["Notes/image.png", "../outside-apple-notes-export-test.txt"],
            }

            deleted = export_apple_notes.remove_manifest_entry_paths(root, entry)

            self.assertEqual(deleted, 2)
            self.assertFalse(note_file.exists())
            self.assertFalse(attachment_file.exists())
            self.assertTrue(outside_file.exists())

    def test_write_and_load_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            export_apple_notes.write_manifest(root, {"1": {"path": "Notes/Example-1.md"}})

            manifest = export_apple_notes.load_manifest(root)

            self.assertEqual(manifest["version"], export_apple_notes.MANIFEST_VERSION)
            self.assertEqual(manifest["notes"]["1"]["path"], "Notes/Example-1.md")

    def test_prune_empty_dirs_removes_ds_store_only_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_dir = root / "Old Folder" / "Nested"
            old_dir.mkdir(parents=True)
            (old_dir / ".DS_Store").write_text("metadata", encoding="utf-8")

            export_apple_notes.prune_all_empty_dirs(root)

            self.assertFalse((root / "Old Folder").exists())

    def test_prune_empty_dirs_keeps_directories_with_regular_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kept_dir = root / "Old Folder"
            kept_dir.mkdir()
            (kept_dir / ".DS_Store").write_text("metadata", encoding="utf-8")
            (kept_dir / "manual.md").write_text("manual", encoding="utf-8")

            export_apple_notes.prune_all_empty_dirs(root)

            self.assertTrue(kept_dir.exists())
            self.assertTrue((kept_dir / "manual.md").exists())

    def test_remove_orphaned_export_files_deletes_stale_note_and_attachment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_dir = root / "10 - 工作" / "12 - RC"
            old_dir.mkdir(parents=True)
            stale_note = old_dir / "Example-1.md"
            stale_attachment = old_dir / "image.png"
            stale_note.write_text(
                "\n".join([
                    "---",
                    'title: "Example"',
                    "id: 1",
                    'apple_notes_id: "A"',
                    'folder: "10 - 工作/12 - RC"',
                    "attachments:",
                    '  - filename: "image.png"',
                    '    identifier: "ATTACHMENT"',
                    '    type: "public.png"',
                    "links: []",
                    "source: notes://showNote?identifier=A",
                    "---",
                    "",
                    "body",
                    "",
                ]),
                encoding="utf-8",
            )
            stale_attachment.write_text("attachment", encoding="utf-8")

            deleted = export_apple_notes.remove_orphaned_export_files(root, set())

            self.assertEqual(deleted, 2)
            self.assertFalse((root / "10 - 工作").exists())

    def test_remove_orphaned_export_files_keeps_manual_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_dir = root / "10 - 工作"
            old_dir.mkdir()
            manual = old_dir / "manual.md"
            manual.write_text("# manual\n", encoding="utf-8")

            deleted = export_apple_notes.remove_orphaned_export_files(root, set())

            self.assertEqual(deleted, 0)
            self.assertTrue(manual.exists())

    def test_write_note_applies_modified_time_to_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            modified = "2026-06-10T01:00:00+00:00"
            note = export_apple_notes.Note(
                pk=7,
                title="Timestamped",
                apple_notes_id="T",
                folder_path="Notes",
                created="2026-06-09T01:00:00+00:00",
                modified=modified,
                body="body",
                attachments=[],
                links=[],
            )

            result = export_apple_notes.write_note(root, note, copy_attachments=True)

            expected = dt.datetime.fromisoformat(modified).timestamp()
            self.assertAlmostEqual(result.note_path.stat().st_mtime, expected, delta=1.0)

    def test_manifest_entry_timestamps_match_detects_stale_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            note_path = root / "Notes" / "Example-1.md"
            note_path.parent.mkdir()
            note_path.write_text("note", encoding="utf-8")
            modified = "2026-06-10T01:00:00+00:00"

            self.assertFalse(
                export_apple_notes.manifest_entry_timestamps_match(
                    root,
                    {"path": "Notes/Example-1.md", "attachments": [], "modified": modified},
                )
            )

            export_apple_notes.apply_file_timestamps(note_path, created=None, modified=modified)

            self.assertTrue(
                export_apple_notes.manifest_entry_timestamps_match(
                    root,
                    {"path": "Notes/Example-1.md", "attachments": [], "modified": modified},
                )
            )

    def test_attachment_references_are_nested_under_note_id(self):
        attachment = export_apple_notes.Attachment(
            identifier="ATTACHMENT",
            type_uti="public.png",
            source_path=None,
            output_name="image.png",
        )

        nested = export_apple_notes.nest_attachments_under_note_id([attachment], 1)
        body = export_apple_notes.replace_attachment_placeholders("before\n\ufffc\nafter", nested)

        self.assertEqual(nested[0].output_name, "attachments/1/image.png")
        self.assertIn("![image.png](attachments/1/image.png)", body)

    def test_write_note_copies_attachments_to_same_named_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.png"
            source.write_text("attachment", encoding="utf-8")
            attachment = export_apple_notes.Attachment(
                identifier="ATTACHMENT",
                type_uti="public.png",
                source_path=source,
                output_name="attachments/7/image.png",
            )
            note = export_apple_notes.Note(
                pk=7,
                title="Timestamped",
                apple_notes_id="T",
                folder_path="Notes",
                created=None,
                modified=None,
                body="![image.png](attachments/7/image.png)",
                attachments=[attachment],
                links=[],
            )

            result = export_apple_notes.write_note(root, note, copy_attachments=True)
            markdown = result.note_path.read_text(encoding="utf-8")

            self.assertTrue((root / "Notes" / "Timestamped-7.md").exists())
            self.assertTrue((root / "Notes" / "attachments" / "7" / "image.png").exists())
            self.assertIn('filename: "attachments/7/image.png"', markdown)


if __name__ == "__main__":
    unittest.main()
