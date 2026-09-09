# apple-notes-cli

Search, read, and export Apple Notes from the terminal by reading the local
Notes database directly — no AppleScript, no UI automation, no cloud API.

```console
$ apple-notes search "kubernetes" -n 3
   4821  2026-09-08 14:02   Work/Infra/Cluster upgrade notes  (7x)
         …drain the node before the kubernetes control plane restarts…
   3907  2026-08-30 09:15   Work/Runbooks/On-call  (2x)
         …kubernetes events are the fastest way to see why a pod is pending…

$ apple-notes recent --since 2d
   4830  2026-09-09 15:59   Work/Meeting notes
   4829  2026-09-09 13:54   Personal/Reading list
```

## Why not AppleScript

The usual approach is `osascript` against Notes.app. It works, but the cost
lands squarely on search. Measured on a real 818-note store:

| Operation | AppleScript | apple-notes-cli |
| --- | --- | --- |
| Enumerate folders and count notes | 1.85 s | — |
| Fetch one note body | 0.325 s | — |
| **Full-text search every note** | **~266 s** (extrapolated) | **0.11 s** |

`first note whose id is "…"` is a linear scan inside Notes.app with no index
behind it, so reading every body costs a third of a second each. That is why
AppleScript-based tools either search titles only or cache aggressively and
serve you stale results. Reading the SQLite store sidesteps the whole problem:
one query, then in-memory decoding of every body.

## Install

Requires macOS and Python 3.10+.

```bash
uv tool install git+https://github.com/ingjieye/apple-notes-cli
# or
pipx install git+https://github.com/ingjieye/apple-notes-cli
```

Run it straight from a clone without installing:

```bash
git clone https://github.com/ingjieye/apple-notes-cli && cd apple-notes-cli
uv run python -m apple_notes_cli recent
```

macOS may ask you to grant your terminal access to the Notes container the
first time. Nothing here needs Full Disk Access on its own.

## Commands

```
apple-notes search <query>   full-text search across every note body
apple-notes recent           list recently modified notes
apple-notes show <id>        print one note as Markdown
apple-notes folders          list folders with note counts
apple-notes export <dir>     export everything to Markdown files
```

### search

```bash
apple-notes search "OpenTelemetry"
apple-notes search "TODO|FIXME" --regex
apple-notes search "invoice" --folder Finance --since 1w
apple-notes search "postgres" --json | jq -r '.[].title'
apple-notes search "draft" --body-only --no-snippets
```

Results rank by match count, then by recency. Titles are searched alongside
bodies unless you pass `--body-only`. `--json` emits structured records, which
is the useful shape for piping into an LLM or another tool.

### recent

```bash
apple-notes recent -n 50
apple-notes recent --since 6h --folder Work
```

Metadata only — bodies are never inflated, so this stays fast regardless of
store size.

### export

```bash
apple-notes export ~/Downloads/notes
apple-notes export ~/Downloads/notes --incremental
```

One Markdown file per note, mirroring your folder tree, with YAML frontmatter
(title, ids, dates, folder, links, attachments, and a `notes://` link back to
the note). Attachments are copied next to the Markdown under
`attachments/<id>/`. `--incremental` keeps a manifest so unchanged notes are
skipped and exports of deleted notes are pruned.

## How it works

```
NoteStore.sqlite
  └── ZICNOTEDATA.ZDATA         gzip → protobuf → text runs → Markdown
  └── ZICCLOUDSYNCINGOBJECT     titles, dates, folder tree, attachments
```

| Module | Role |
| --- | --- |
| `store.py` | read-only SQLite access, folder tree, attachments |
| `parser.py` | gzip + protobuf decoding, Markdown rendering |
| `search.py` | search, recency listing, single-note reads |
| `export.py` | Markdown output and the incremental manifest |

Three details are worth knowing if you build something similar:

**Open the database with `mode=ro`, never `immutable=1`.** The `immutable`
flag makes SQLite skip the `-wal` file, which silently serves a stale snapshot
missing your most recent notes. On a live store the `-wal` file routinely holds
megabytes of un-checkpointed data. WAL mode allows concurrent readers, so
`mode=ro` is safe while Notes.app is running and never blocks it. Export uses
SQLite's `backup()` API instead, for a stable point-in-time copy.

**There is no FTS index.** The schema has no full-text tables, and bodies are
gzipped protobuf blobs, so `WHERE body LIKE …` is not available. Search means
inflating every candidate body and matching in memory. At ~800 notes that is
about 30 ms, so this tool does no caching and always reflects the live store.

**Recently Deleted is `ZFOLDERTYPE = 1`.** Notes in the trash keep
`ZMARKEDFORDELETION = 0`, so filtering on that column alone still surfaces
them. Matching on the folder type is also language-independent, unlike
comparing against a localized "Recently Deleted" string. They are excluded by
default; pass `--include-trashed` to include them.

## Exit codes

`search` follows the `grep` convention: `0` when something matched, `1` when
nothing did, `2` on a bad argument or an unreadable database. That makes it
usable in a conditional:

```bash
if apple-notes search "release checklist" --no-snippets >/dev/null; then
  echo "found it"
fi
```

## Reading is all it does

This tool only reads. It never writes to `NoteStore.sqlite` — modifying the
store directly would corrupt CloudKit's sync state and risk losing notes. If
you need to create or edit notes, drive Notes.app through AppleScript for the
write path and use this for reading.

Password-protected notes are listed but their bodies stay encrypted and
unreadable; they are skipped unless you pass `--include-password-protected`.

## Development

```bash
uv run python -m unittest discover -s tests
```

`tests/test_parity.py` is the original single-file exporter's suite, kept
unchanged apart from neutralized example URLs, so it guards the protobuf
decoding against regressions.
`tests/test_search.py` builds a synthetic Core Data store to cover the query
layer, trash handling, and time filters.

## License

MIT
