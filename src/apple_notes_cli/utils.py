"""Filename helpers shared by the store and the exporter."""

from __future__ import annotations

import re

FORBIDDEN_FILENAME_CHARS = re.compile(r"[/\\:*?\"<>|\x00-\x1f]")


def sanitize_filename(value: str, fallback: str = "Untitled", max_length: int = 180) -> str:
    value = value.replace("\n", " ").replace("\r", " ").strip()
    value = FORBIDDEN_FILENAME_CHARS.sub("-", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    if not value:
        value = fallback
    if len(value) > max_length:
        value = value[:max_length].rstrip(" .")
    return value or fallback


