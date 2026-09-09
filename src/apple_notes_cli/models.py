"""Dataclasses shared across the parser, store, and exporter."""

from __future__ import annotations

import dataclasses
from pathlib import Path


@dataclasses.dataclass(frozen=True)
class ProtoField:
    number: int
    wire_type: int
    value: int | bytes


@dataclasses.dataclass(frozen=True)
class Attachment:
    identifier: str
    type_uti: str | None
    source_path: Path | None
    output_name: str

    @property
    def markdown_reference(self) -> str:
        reference = self.output_name
        alt = Path(reference).name.replace("[", "\\[").replace("]", "\\]")
        if reference.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
            return f"![{alt}]({reference})"
        return f"[{alt}]({reference})"


@dataclasses.dataclass(frozen=True)
class Note:
    pk: int
    title: str
    apple_notes_id: str
    folder_path: str
    created: str | None
    modified: str | None
    body: str
    attachments: list[Attachment]
    links: list[str]


@dataclasses.dataclass(frozen=True)
class ExportResult:
    note_path: Path
    attachment_paths: list[Path]


@dataclasses.dataclass(frozen=True)
class EmbeddedTable:
    identifier: str
    markdown: str


@dataclasses.dataclass(frozen=True)
class TextRun:
    start: int
    end: int
    style_type: int | None
    indent: int
    block_quote: int | None
    checklist_done: int | None
    font_weight: int | None
    underlined: int | None
    strikethrough: int | None
    superscript: int | None
    link: str | None
    attachment_identifier: str | None
    attachment_type: str | None

