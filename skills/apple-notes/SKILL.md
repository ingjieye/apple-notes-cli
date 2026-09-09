---
name: apple-notes
description: Search, read, and export the user's local Apple Notes via the apple-notes CLI, which reads the Notes SQLite store directly. Use whenever the user asks about their own notes — 查笔记、搜索笔记、我的笔记里、备忘录、Apple Notes、我记过什么、找一下我写的、导出笔记 — or asks to find, recall, summarize, or cross-reference something they wrote down. Also use when the user asks what they noted about a topic, person, meeting, or project, or wants notes exported to Markdown.
---

# Apple Notes

Query the user's Apple Notes through the `apple-notes` CLI. It reads
`~/Library/Group Containers/group.com.apple.notes/NoteStore.sqlite` read-only
and always reflects the live store — there is no cache and no staleness.

## Check availability first

```bash
command -v apple-notes || echo "not installed"
```

If missing, install it:

```bash
uv tool install git+https://github.com/ingjieye/apple-notes-cli
```

## Commands

```bash
apple-notes search "<query>" [-n N] [--regex] [--case-sensitive] [--folder F] [--since 2d] [--json]
apple-notes recent [-n N] [--since 6h] [--folder F] [--json]
apple-notes show <id> [--plain] [--json]
apple-notes folders [--json]
apple-notes export <dir> [--incremental]
```

## How to use it

**Always use `--json` when you need to reason over results.** The text output
is for humans; JSON gives you `pk`, `title`, `folder`, `created`, `modified`,
`match_count`, and `snippets`.

```bash
apple-notes search "kubernetes" --json
```

**Two-step reads.** `search` and `recent` return metadata and snippets, not
full bodies. To read a note, take its `pk` and call `show`:

```bash
apple-notes search "release process" --json   # -> pk 4821
apple-notes show 4821                          # full Markdown body
```

**Searching is cheap — under a second even for a full store.** Don't hesitate
to run several queries with different wording. The user's notes may be in
Chinese, English, or both, so try both when the first attempt comes up empty:

```bash
apple-notes search "会议" --json
apple-notes search "meeting" --json
```

**Narrow with filters rather than post-processing.** `--folder` matches any
folder path containing the text; `--since` takes `30m`, `6h`, `2d`, `1w`, or an
ISO date.

```bash
apple-notes recent --since 1w --folder Work --json
apple-notes search "invoice" --since 2026-01-01 --json
```

**`--regex` for alternatives.** The query is a literal string by default.

```bash
apple-notes search "TODO|FIXME|待办" --regex --json
```

## Exit codes

`search` follows `grep`: `0` matched, `1` nothing matched, `2` bad argument or
unreadable database. Exit `1` is a normal empty result, not an error — report
that nothing was found and consider retrying with different wording.

## Reporting back

Cite notes by title and folder, not by `pk` — the id is an internal key that
means nothing to the user. Include the modified date when recency matters.
Note bodies can be opened in Notes.app via `notes://showNote?identifier=<uuid>`,
which `export` writes into frontmatter.

## Scope and limits

- **Read-only.** This never writes to the Notes store; doing so would corrupt
  CloudKit sync. If the user wants to create or edit a note, say so and offer
  AppleScript (`osascript -e 'tell application "Notes" ...'`) instead.
- **Recently Deleted is excluded** by default. Add `--include-trashed` if the
  user is looking for something they deleted.
- **Password-protected notes** stay encrypted and unreadable; they are skipped.
- Attachment files are not searched — only note text. `export` copies them.

## Privacy

These are the user's personal notes. Read only what the current request needs,
quote no more than answers the question, and never send note content to an
external service unless the user explicitly asks for that.
