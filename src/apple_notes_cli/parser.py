"""Pure decoding of the Apple Notes protobuf payload into Markdown.

Nothing in here touches SQLite or the filesystem: every function takes bytes
or text and returns text. Lifted verbatim from the original exporter.
"""

from __future__ import annotations

import contextlib
import gzip
import re
from typing import Iterator

from .models import ProtoField, TextRun

URL_RE = re.compile(r"https?://[^\s<>()\[\]\"'，。！？；、]+")
STYLE_TYPE_TITLE = 0
STYLE_TYPE_HEADING = 1
STYLE_TYPE_SUBHEADING = 2
STYLE_TYPE_MONOSPACED = 4
STYLE_TYPE_DOTTED_LIST = 100
STYLE_TYPE_DASHED_LIST = 101
STYLE_TYPE_NUMBERED_LIST = 102
STYLE_TYPE_CHECKBOX = 103
STYLE_TYPE_BLOCK_QUOTE = 1
FONT_TYPE_BOLD = 1
FONT_TYPE_ITALIC = 2
FONT_TYPE_BOLD_ITALIC = 3


def read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data):
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if byte & 0x80 == 0:
            return value, offset
        shift += 7
        if shift > 70:
            raise ValueError("protobuf varint is too large")
    raise ValueError("unexpected end of protobuf varint")


def iter_proto_fields(data: bytes) -> Iterator[ProtoField]:
    offset = 0
    while offset < len(data):
        key, offset = read_varint(data, offset)
        number = key >> 3
        wire_type = key & 0x07

        if wire_type == 0:
            value, offset = read_varint(data, offset)
            yield ProtoField(number, wire_type, value)
        elif wire_type == 1:
            if offset + 8 > len(data):
                raise ValueError("unexpected end of fixed64 field")
            yield ProtoField(number, wire_type, data[offset : offset + 8])
            offset += 8
        elif wire_type == 2:
            length, offset = read_varint(data, offset)
            end = offset + length
            if end > len(data):
                raise ValueError("unexpected end of length-delimited field")
            yield ProtoField(number, wire_type, data[offset:end])
            offset = end
        elif wire_type == 5:
            if offset + 4 > len(data):
                raise ValueError("unexpected end of fixed32 field")
            yield ProtoField(number, wire_type, data[offset : offset + 4])
            offset += 4
        else:
            raise ValueError(f"unsupported protobuf wire type: {wire_type}")


def first_bytes_field(data: bytes, number: int) -> bytes:
    for field in iter_proto_fields(data):
        if field.number == number and field.wire_type == 2 and isinstance(field.value, bytes):
            return field.value
    raise ValueError(f"protobuf field {number} was not found")


def first_varint_field(data: bytes, number: int) -> int | None:
    for field in iter_proto_fields(data):
        if field.number == number and field.wire_type == 0 and isinstance(field.value, int):
            return field.value
    return None


def first_string_field(data: bytes, number: int) -> str | None:
    for field in iter_proto_fields(data):
        if field.number != number or field.wire_type != 2 or not isinstance(field.value, bytes):
            continue
        with contextlib.suppress(UnicodeDecodeError):
            return field.value.decode("utf-8")
    return None


def note_content_from_zdata(zdata: bytes) -> bytes:
    if zdata.startswith(b"\x1f\x8b"):
        zdata = gzip.decompress(zdata)
    document = first_bytes_field(zdata, 2)
    return first_bytes_field(document, 3)


def find_largest_utf8_field(data: bytes, depth: int = 0) -> str:
    best = ""
    if depth > 5:
        return best
    with contextlib.suppress(ValueError):
        for field in iter_proto_fields(data):
            if field.wire_type != 2 or not isinstance(field.value, bytes):
                continue
            with contextlib.suppress(UnicodeDecodeError):
                text = field.value.decode("utf-8")
                printable = sum(ch.isprintable() or ch in "\n\r\t" for ch in text)
                if text and printable / max(len(text), 1) > 0.9 and len(text) > len(best):
                    best = text
            nested = find_largest_utf8_field(field.value, depth + 1)
            if len(nested) > len(best):
                best = nested
    return best


def extract_note_text(zdata: bytes) -> str:
    try:
        content = note_content_from_zdata(zdata)
        text = first_bytes_field(content, 2).decode("utf-8")
    except (UnicodeDecodeError, ValueError, OSError):
        text = find_largest_utf8_field(zdata)

    return normalize_note_text(text)


def parse_paragraph_style(data: bytes) -> tuple[int | None, int, int | None, int | None]:
    style_type = first_varint_field(data, 1)
    indent = first_varint_field(data, 4) or 0
    block_quote = first_varint_field(data, 8)
    checklist_done = None
    for field in iter_proto_fields(data):
        if field.number != 5 or field.wire_type != 2 or not isinstance(field.value, bytes):
            continue
        checklist_done = first_varint_field(field.value, 2)
        break
    return style_type, indent, block_quote, checklist_done


def parse_text_runs(content: bytes) -> list[TextRun]:
    runs: list[TextRun] = []
    offset = 0
    for field in iter_proto_fields(content):
        if field.number != 5 or field.wire_type != 2 or not isinstance(field.value, bytes):
            continue

        length = first_varint_field(field.value, 1)
        if length is None:
            continue

        style_type = None
        indent = 0
        block_quote = None
        checklist_done = None
        font_weight = first_varint_field(field.value, 5)
        underlined = first_varint_field(field.value, 6)
        strikethrough = first_varint_field(field.value, 7)
        superscript = first_varint_field(field.value, 8)
        link = first_string_field(field.value, 9)
        attachment_identifier = None
        attachment_type = None
        for run_field in iter_proto_fields(field.value):
            if run_field.wire_type != 2 or not isinstance(run_field.value, bytes):
                continue
            if run_field.number == 2:
                style_type, indent, block_quote, checklist_done = parse_paragraph_style(run_field.value)
            elif run_field.number == 12:
                attachment_identifier = first_string_field(run_field.value, 1)
                attachment_type = first_string_field(run_field.value, 2)

        runs.append(
            TextRun(
                offset,
                offset + length,
                style_type,
                indent,
                block_quote,
                checklist_done,
                font_weight,
                underlined,
                strikethrough,
                superscript,
                link,
                attachment_identifier,
                attachment_type,
            )
        )
        offset += length
    return runs


def run_at_offset(runs: list[TextRun], offset: int) -> TextRun | None:
    for run in runs:
        if run.start <= offset < run.end:
            return run
    return None


def line_ranges(text: str) -> list[tuple[int, str]]:
    ranges: list[tuple[int, str]] = []
    offset = 0
    for line in text.split("\n"):
        ranges.append((offset, line))
        offset += len(line) + 1
    while ranges and ranges[0][1] == "":
        ranges.pop(0)
    while ranges and ranges[-1][1] == "":
        ranges.pop()
    return ranges


def overlapping_runs(runs: list[TextRun], start: int, end: int) -> list[TextRun]:
    return [run for run in runs if run.start < end and run.end > start]


def block_style_key(run: TextRun) -> tuple[int | None, int, int | None, int | None]:
    return (run.style_type, run.indent, run.block_quote, run.checklist_done)


def dominant_block_run(runs: list[TextRun], start: int, end: int) -> TextRun | None:
    scores: dict[tuple[int | None, int, int | None, int | None], tuple[int, int, TextRun]] = {}
    for run in overlapping_runs(runs, start, end):
        overlap = min(end, run.end) - max(start, run.start)
        if overlap <= 0:
            continue
        key = block_style_key(run)
        total, _last_start, _representative = scores.get(key, (0, run.start, run))
        scores[key] = (total + overlap, run.start, run)
    if not scores:
        return None
    return max(scores.values(), key=lambda item: (item[0], item[1]))[2]


def apply_inline_markdown(text: str, run: TextRun, object_replacements: dict[str, str] | None = None) -> str:
    if not text:
        return text

    match = re.match(r"^(\s*)(.*?)(\s*)$", text.replace("\x00", "\u2400"), re.DOTALL)
    if not match:
        return text
    leading, result, trailing = match.groups()
    if not result:
        return leading + trailing

    if "\ufffc" in result and run.attachment_identifier and object_replacements:
        replacement = object_replacements.get(run.attachment_identifier)
        if replacement:
            result = result.replace("\ufffc", replacement)

    if run.link:
        label = result.replace("[", "\\[").replace("]", "\\]")
        url = run.link.replace(")", "%29")
        result = f"[{label}]({url})"

    if run.font_weight == FONT_TYPE_BOLD:
        result = f"**{result}**"
    elif run.font_weight == FONT_TYPE_ITALIC:
        result = f"*{result}*"
    elif run.font_weight == FONT_TYPE_BOLD_ITALIC:
        result = f"***{result}***"

    if run.strikethrough == 1:
        result = f"~~{result}~~"
    if run.underlined == 1:
        result = f"<u>{result}</u>"
    if run.superscript == 1:
        result = f"<sup>{result}</sup>"
    elif run.superscript == -1:
        result = f"<sub>{result}</sub>"
    return leading + result + trailing


def add_spacing_after_bold_labels(text: str) -> str:
    return re.sub(r"(\*\*[^*\n]{1,40}[：:]\*\*)(?=\S)", r"\1 ", text)


def fence_for_lines(lines: list[str]) -> str:
    longest = max((len(match.group(0)) for line in lines for match in re.finditer(r"`+", line)), default=0)
    return "`" * max(3, longest + 1)


def should_preserve_plain_block(lines: list[str]) -> bool:
    if len(lines) < 2:
        return False
    if any(is_markdown_block_line(line) or has_markdown_inline_markup(line) for line in lines):
        return False
    if any(line.startswith("\t") for line in lines):
        return True
    return len(lines) >= 3 and any(line.strip() == "↓" for line in lines)


def has_markdown_inline_markup(line: str) -> bool:
    return "](" in line or "<u>" in line or "</u>" in line


def is_markdown_block_line(line: str) -> bool:
    return bool(re.match(r"^\s*(#{1,6}\s|[-*+]\s|\d+\.\s|>\s|```|\|)", line))


def replace_leading_tabs(line: str) -> str:
    tabs = len(line) - len(line.lstrip("\t"))
    if tabs == 0:
        return line
    return "&emsp;" * tabs + line[tabs:]


def with_hard_breaks(lines: list[str]) -> list[str]:
    rendered: list[str] = []
    for index, line in enumerate(lines):
        line = replace_leading_tabs(line)
        next_line = lines[index + 1] if index + 1 < len(lines) else None
        if next_line is not None and not is_markdown_block_line(line) and not is_markdown_block_line(next_line):
            rendered.append(line.rstrip() + "  ")
        else:
            rendered.append(line)
    return rendered


def preserve_plain_structure_blocks(text: str) -> str:
    groups: list[list[str]] = []
    current: list[str] = []
    for line in text.split("\n"):
        if line == "":
            if current:
                groups.append(current)
                current = []
            groups.append([])
        else:
            current.append(line)
    if current:
        groups.append(current)

    rendered: list[str] = []
    for group in groups:
        if not group:
            rendered.append("")
            continue
        if should_preserve_plain_block(group):
            fence = fence_for_lines(group)
            rendered.extend([fence, *group, fence])
        else:
            rendered.extend(with_hard_breaks(group))
    return "\n".join(rendered)


def inline_style_key(run: TextRun) -> tuple[int | None, int | None, int | None, int | None, str | None, str | None]:
    return (
        run.font_weight,
        run.underlined,
        run.strikethrough,
        run.superscript,
        run.link,
        run.attachment_identifier,
    )


def is_plain_inline_key(key: tuple | None) -> bool:
    return key is None or all(value is None for value in key)


def is_plain_segment(key: tuple | None, text: str) -> bool:
    return is_plain_inline_key(key) and len(text) == 1 and bool(text.strip())


def merge_leading_style_fragments(
    segments: list[tuple[tuple | None, TextRun | None, str]],
) -> list[tuple[tuple | None, TextRun | None, str]]:
    merged: list[tuple[tuple | None, TextRun | None, str]] = []
    index = 0
    while index < len(segments):
        key, run, text = segments[index]
        if (
            is_plain_segment(key, text)
            and index + 1 < len(segments)
            and not is_plain_inline_key(segments[index + 1][0])
            and segments[index + 1][2]
            and not segments[index + 1][2][0].isspace()
        ):
            next_key, next_run, next_text = segments[index + 1]
            merged.append((next_key, next_run, text + next_text))
            index += 2
            continue
        merged.append((key, run, text))
        index += 1
    return merged


def render_inline_markdown(
    text: str,
    runs: list[TextRun],
    start: int,
    object_replacements: dict[str, str] | None = None,
) -> str:
    if not text:
        return text

    end = start + len(text)
    segments: list[tuple[tuple | None, TextRun | None, str]] = []
    cursor = start
    for run in overlapping_runs(runs, start, end):
        if run.start > cursor:
            segments.append((None, None, text[cursor - start : run.start - start]))
        segment_start = max(cursor, run.start)
        segment_end = min(end, run.end)
        if segment_start < segment_end:
            segment = text[segment_start - start : segment_end - start]
            key = inline_style_key(run)
            if segments and segments[-1][0] == key:
                previous_key, previous_run, previous_text = segments[-1]
                segments[-1] = (previous_key, previous_run, previous_text + segment)
            else:
                segments.append((key, run, segment))
        cursor = max(cursor, segment_end)
    if cursor < end:
        segments.append((None, None, text[cursor - start :]))
    segments = merge_leading_style_fragments(segments)

    parts: list[str] = []
    for _key, run, segment in segments:
        if run is None:
            parts.append(segment)
        else:
            parts.append(apply_inline_markdown(segment, run, object_replacements))
    return add_spacing_after_bold_labels("".join(parts))


def line_prefix(style_type: int | None, run: TextRun | None, line_number: int) -> tuple[str, bool]:
    indent = "  " * (run.indent if run else 0)
    if style_type == STYLE_TYPE_CHECKBOX:
        checked = "x" if run and run.checklist_done == 1 else " "
        return f"{indent}- [{checked}] ", True
    if style_type in {STYLE_TYPE_DOTTED_LIST, STYLE_TYPE_DASHED_LIST}:
        return f"{indent}- ", True
    if style_type == STYLE_TYPE_NUMBERED_LIST:
        return f"{indent}{line_number}. ", True
    if run and run.block_quote == STYLE_TYPE_BLOCK_QUOTE:
        return "> " * max(run.indent, 1), False
    if run and run.indent > 0:
        return "> " * run.indent, False
    if style_type == STYLE_TYPE_TITLE:
        return "### ", True
    if style_type == STYLE_TYPE_HEADING:
        return "#### ", True
    if style_type == STYLE_TYPE_SUBHEADING:
        return "##### ", True
    if style_type == STYLE_TYPE_MONOSPACED:
        return "    ", False
    return "", False


def render_note_text_as_markdown(
    text: str,
    runs: list[TextRun],
    object_replacements: dict[str, str] | None = None,
) -> str:
    if not runs:
        return preserve_plain_structure_blocks(normalize_note_text(text))

    rendered: list[str] = []
    ordered_numbers: dict[int, int] = {}
    for start, line in line_ranges(text):
        if not line:
            rendered.append("")
            ordered_numbers.clear()
            continue

        first_content = len(line) - len(line.lstrip())
        line_start = start + first_content
        line_end = start + len(line)
        run = dominant_block_run(runs, line_start, line_end)
        style_type = run.style_type if run else None
        if style_type in {STYLE_TYPE_TITLE, STYLE_TYPE_HEADING, STYLE_TYPE_SUBHEADING} and len(line.strip()) > 80:
            style_type = None
        if style_type == STYLE_TYPE_NUMBERED_LIST:
            indent = run.indent if run else 0
            line_number = ordered_numbers.get(indent, 1)
            prefix, strip_line = line_prefix(style_type, run, line_number)
            ordered_numbers[indent] = line_number + 1
            for deeper_indent in [key for key in ordered_numbers if key > indent]:
                del ordered_numbers[deeper_indent]
        else:
            prefix, strip_line = line_prefix(style_type, run, 1)
            if style_type not in {STYLE_TYPE_CHECKBOX, STYLE_TYPE_DOTTED_LIST, STYLE_TYPE_DASHED_LIST}:
                ordered_numbers.clear()

        content = line.lstrip() if strip_line else line
        content_start = start + (first_content if strip_line else 0)
        rendered.append(prefix + render_inline_markdown(content, runs, content_start, object_replacements))
    return preserve_plain_structure_blocks("\n".join(rendered))


def extract_note_markdown(zdata: bytes, object_replacements: dict[str, str] | None = None) -> str:
    try:
        content = note_content_from_zdata(zdata)
        text = first_bytes_field(content, 2).decode("utf-8")
        runs = parse_text_runs(content)
        return render_note_text_as_markdown(text, runs, object_replacements)
    except (UnicodeDecodeError, ValueError, OSError):
        return extract_note_text(zdata)


def normalize_note_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u2028", "\n").replace("\u2029", "\n")
    return text.strip("\n")




def object_id(data: bytes) -> dict[str, int | str]:
    result: dict[str, int | str] = {}
    unsigned = first_varint_field(data, 2)
    string = first_string_field(data, 4)
    index = first_varint_field(data, 6)
    if unsigned is not None:
        result["unsigned"] = unsigned
    if string is not None:
        result["string"] = string
    if index is not None:
        result["index"] = index
    return result


def custom_map(data: bytes) -> tuple[int | None, list[tuple[int, dict[str, int | str]]]]:
    map_type = first_varint_field(data, 1)
    entries: list[tuple[int, dict[str, int | str]]] = []
    for field in iter_proto_fields(data):
        if field.number != 3 or field.wire_type != 2 or not isinstance(field.value, bytes):
            continue
        key = first_varint_field(field.value, 1)
        value = None
        for entry_field in iter_proto_fields(field.value):
            if entry_field.number == 2 and entry_field.wire_type == 2 and isinstance(entry_field.value, bytes):
                value = object_id(entry_field.value)
                break
        if key is not None and value is not None:
            entries.append((key, value))
    return map_type, entries


def dictionary_elements(data: bytes) -> list[tuple[dict[str, int | str], dict[str, int | str]]]:
    elements: list[tuple[dict[str, int | str], dict[str, int | str]]] = []
    for field in iter_proto_fields(data):
        if field.number != 1 or field.wire_type != 2 or not isinstance(field.value, bytes):
            continue
        key = None
        value = None
        for element_field in iter_proto_fields(field.value):
            if element_field.wire_type != 2 or not isinstance(element_field.value, bytes):
                continue
            if element_field.number == 1:
                key = object_id(element_field.value)
            elif element_field.number == 2:
                value = object_id(element_field.value)
        if key is not None and value is not None:
            elements.append((key, value))
    return elements


def entry_field(entry: bytes, number: int) -> bytes | None:
    for field in iter_proto_fields(entry):
        if field.number == number and field.wire_type == 2 and isinstance(field.value, bytes):
            return field.value
    return None


def entry_custom_map(entry: bytes) -> tuple[int | None, list[tuple[int, dict[str, int | str]]]] | None:
    data = entry_field(entry, 13)
    if data is None:
        return None
    return custom_map(data)


def entry_type(entry: bytes, type_items: list[str]) -> str | None:
    decoded = entry_custom_map(entry)
    if decoded is None:
        return None
    type_index, _entries = decoded
    if type_index is None or type_index >= len(type_items):
        return None
    return type_items[type_index]


def target_uuid_index(entry: bytes) -> int | None:
    decoded = entry_custom_map(entry)
    if decoded is None:
        return None
    _type_index, entries = decoded
    if not entries:
        return None
    value = entries[0][1]
    unsigned = value.get("unsigned")
    return unsigned if isinstance(unsigned, int) else None


def ordered_set_parts(entry: bytes) -> tuple[list[bytes], list[tuple[dict[str, int | str], dict[str, int | str]]]]:
    ordered_set = entry_field(entry, 16)
    if ordered_set is None:
        return [], []

    ordering = entry_field(ordered_set, 1)
    if ordering is None:
        return [], []

    attachments: list[bytes] = []
    elements: list[tuple[dict[str, int | str], dict[str, int | str]]] = []
    for field in iter_proto_fields(ordering):
        if field.wire_type != 2 or not isinstance(field.value, bytes):
            continue
        if field.number == 1:
            for array_field in iter_proto_fields(field.value):
                if array_field.number != 2 or array_field.wire_type != 2 or not isinstance(array_field.value, bytes):
                    continue
                for attachment_field in iter_proto_fields(array_field.value):
                    if attachment_field.number == 2 and attachment_field.wire_type == 2 and isinstance(
                        attachment_field.value, bytes
                    ):
                        attachments.append(attachment_field.value)
        elif field.number == 2:
            elements = dictionary_elements(field.value)
    return attachments, elements


def index_from_object_id(value: dict[str, int | str]) -> int | None:
    index = value.get("index")
    return index if isinstance(index, int) else None


def table_axis_indices(entry: bytes, table_objects: list[bytes], uuid_items: list[bytes]) -> dict[int, int]:
    attachments, elements = ordered_set_parts(entry)
    indices: dict[int, int] = {}
    for position, uuid in enumerate(attachments):
        with contextlib.suppress(ValueError):
            indices[uuid_items.index(uuid)] = position

    for key, value in elements:
        key_index = index_from_object_id(key)
        value_index = index_from_object_id(value)
        if key_index is None or value_index is None:
            continue
        if key_index >= len(table_objects) or value_index >= len(table_objects):
            continue
        key_uuid = target_uuid_index(table_objects[key_index])
        value_uuid = target_uuid_index(table_objects[value_index])
        if key_uuid in indices and value_uuid is not None:
            indices[value_uuid] = indices[key_uuid]
    return indices


def note_message_to_markdown(note_message: bytes) -> str:
    text = first_bytes_field(note_message, 2).decode("utf-8")
    return render_note_text_as_markdown(text, parse_text_runs(note_message)).strip()


def markdown_table_cell(value: str) -> str:
    value = value.replace("|", "\\|")
    value = re.sub(r"\n{2,}", "<br><br>", value)
    return value.replace("\n", "<br>").strip()


def markdown_table(rows: list[list[str]]) -> str:
    rows = [[markdown_table_cell(cell) for cell in row] for row in rows if row]
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    normalized = [row + [""] * (width - len(row)) for row in rows]
    header = normalized[0]
    separator = ["---"] * width
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(separator) + " |",
    ]
    for row in normalized[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def extract_markdown_table(mergeable_data: bytes) -> str:
    if mergeable_data.startswith(b"\x1f\x8b"):
        mergeable_data = gzip.decompress(mergeable_data)

    data = first_bytes_field(first_bytes_field(mergeable_data, 2), 3)
    table_objects: list[bytes] = []
    key_items: list[str] = []
    type_items: list[str] = []
    uuid_items: list[bytes] = []
    for field in iter_proto_fields(data):
        if field.number == 3 and field.wire_type == 2 and isinstance(field.value, bytes):
            table_objects.append(field.value)
        elif field.number == 4 and field.wire_type == 2 and isinstance(field.value, bytes):
            key_items.append(field.value.decode("utf-8"))
        elif field.number == 5 and field.wire_type == 2 and isinstance(field.value, bytes):
            type_items.append(field.value.decode("utf-8"))
        elif field.number == 6 and field.wire_type == 2 and isinstance(field.value, bytes):
            uuid_items.append(field.value)

    for table_entry in table_objects:
        if entry_type(table_entry, type_items) != "com.apple.notes.ICTable":
            continue

        decoded = entry_custom_map(table_entry)
        if decoded is None:
            continue
        _type_index, map_entries = decoded
        row_indices: dict[int, int] = {}
        column_indices: dict[int, int] = {}
        cell_columns_entry: bytes | None = None

        for key_index, value in map_entries:
            if key_index >= len(key_items):
                continue
            object_index = index_from_object_id(value)
            if object_index is None or object_index >= len(table_objects):
                continue
            key = key_items[key_index]
            if key == "crRows":
                row_indices = table_axis_indices(table_objects[object_index], table_objects, uuid_items)
            elif key == "crColumns":
                column_indices = table_axis_indices(table_objects[object_index], table_objects, uuid_items)
            elif key == "cellColumns":
                cell_columns_entry = table_objects[object_index]

        if not row_indices or not column_indices or cell_columns_entry is None:
            continue

        rows = [["" for _ in range(max(column_indices.values()) + 1)] for _ in range(max(row_indices.values()) + 1)]
        dictionary = entry_field(cell_columns_entry, 6)
        if dictionary is None:
            continue
        for column_key, column_value in dictionary_elements(dictionary):
            column_object_index = index_from_object_id(column_key)
            row_dictionary_index = index_from_object_id(column_value)
            if column_object_index is None or row_dictionary_index is None:
                continue
            if column_object_index >= len(table_objects) or row_dictionary_index >= len(table_objects):
                continue
            column_uuid = target_uuid_index(table_objects[column_object_index])
            if column_uuid not in column_indices:
                continue
            row_dictionary = entry_field(table_objects[row_dictionary_index], 6)
            if row_dictionary is None:
                continue
            for row_key, row_value in dictionary_elements(row_dictionary):
                row_object_index = index_from_object_id(row_key)
                cell_index = index_from_object_id(row_value)
                if row_object_index is None or cell_index is None:
                    continue
                if row_object_index >= len(table_objects) or cell_index >= len(table_objects):
                    continue
                row_uuid = target_uuid_index(table_objects[row_object_index])
                if row_uuid not in row_indices:
                    continue
                note_message = entry_field(table_objects[cell_index], 10)
                if note_message is None:
                    continue
                rows[row_indices[row_uuid]][column_indices[column_uuid]] = note_message_to_markdown(note_message)
        table = markdown_table(rows)
        if table:
            return table
    return ""


def summary_table_fallback(summary: str | None) -> str:
    if not summary:
        return ""
    lines = [line for line in normalize_note_text(summary).splitlines()]
    if not lines:
        return ""
    return "\n".join(f"> {line}" if line else ">" for line in lines)


