# apple-notes-cli

Search, read, and export Apple Notes from the terminal by reading the local
Notes database directly — no AppleScript, no UI automation, no cloud API.

Built for the common case: **you mostly want to look things up, not write
them.** Recalling what you noted about a project, finding the meeting where a
decision was made, pulling every note that mentions a library — that is
read-heavy work, and it is exactly where the existing tooling is weakest.

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

Almost every Apple Notes integration drives Notes.app through AppleScript
(`osascript`): the [`memo`](https://github.com/antoniorodr/memo) CLI does — its
`get_memo.py` and `id_search_memo.py` shell out to it — as do the
[`apple-notes` skill in
OpenClaw](https://github.com/openclaw/openclaw/blob/main/skills/apple-notes/SKILL.md)
and Hermes, which wrap `memo`, and Claude's own desktop [Notes
connector](https://support.claude.com/en/articles/11176164-use-connectors-to-extend-claude-s-capabilities).

AppleScript is the right choice for *writing* — it is Apple's supported
interface and it keeps CloudKit sync intact. But for reading it carries costs
that are easy to miss. Measured on a real 818-note store:

| Operation | AppleScript | apple-notes-cli |
| --- | --- | --- |
| Enumerate folders and count notes | 1.85 s | — |
| Fetch one note body | 0.325 s | — |
| **Full-text search every note** | **~266 s** (extrapolated) | **0.11 s** |

**Full-text search is impractical, so tools quietly stop doing it.**
`first note whose id is "…"` is an unindexed linear scan inside Notes.app, so
each body costs about a third of a second. Reading all 818 would take over four
minutes. Faced with that, `memo` searches **titles only** — its
`search_memo.py` builds a `{title: id}` map and fuzzy-matches against the keys,
never the bodies. So `memo notes -s "kubernetes"` finds notes *called*
"kubernetes", not the notes that discuss it. The OpenClaw skill exposes this as
"fuzzy search" with no indication that note contents are not searched.

**Caching trades freshness for speed, usually silently.** `memo` caches its
note list to `~/.cache/memo/notes_cache.json` with a 300-second TTL, so a note
you just wrote can be invisible for five minutes. Neither the skill
documentation nor the CLI surfaces this, so an agent asking "what did I note
today?" can be handed a confidently stale answer.

**Other friction that comes with the AppleScript path:**

- **It needs Notes.app.** The app gets launched or scripted in the background,
  and if it hangs or shows a modal, the integration hangs with it.
- **It needs Automation permission** (System Settings → Privacy & Security →
  Automation), granted interactively per client app. That makes unattended use
  — cron, CI, an SSH session — awkward or impossible.
- **Bodies arrive as HTML**, so every tool bolts on an HTML-to-Markdown
  converter and inherits its fidelity bugs.
- **macOS only**, and it cannot run from the web or iOS at all.
- **Per-note round trips.** Reading N notes means N `osascript` invocations,
  each paying process startup plus Apple Events IPC.

Reading the SQLite store sidesteps all of it: one query, then in-memory
decoding. Full-text search over everything takes ~0.11 s, which is why this
tool needs no cache and always reflects the live database.

The trade-off is that this only reads; AppleScript remains the right way to
create and edit notes. The two compose well — see
[Reading is all it does](#reading-is-all-it-does).

## Install

Requires macOS and Python 3.10+.

### Just ask your agent

If you use Claude Code, Claude Desktop, or any coding agent, point it at this
file and it has everything it needs:

> Read https://github.com/ingjieye/apple-notes-cli/blob/main/README.md and set
> it up, including the skill.

### The CLI

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

### The agent skill

[`skills/apple-notes/SKILL.md`](skills/apple-notes/SKILL.md) teaches an agent
to use this CLI — when to reach for it, how to page from search results to full
note bodies, and what its limits are. Drop it in place:

```bash
mkdir -p ~/.claude/skills/apple-notes
curl -fsSL https://raw.githubusercontent.com/ingjieye/apple-notes-cli/main/skills/apple-notes/SKILL.md \
  -o ~/.claude/skills/apple-notes/SKILL.md
```

Use `.claude/skills/` inside a project instead of `~/.claude/skills/` to scope
it to that project. Then ask your agent things like "what did I note about the
cluster migration?" or "找一下我记过的会议决定".

<details>
<summary>Setup steps, if you are an agent reading this</summary>

```bash
# 1. macOS only; bail out early elsewhere.
[ "$(uname)" = "Darwin" ] || { echo "macOS required"; exit 1; }

# 2. Install the CLI (uv preferred, pipx as fallback).
if command -v uv >/dev/null; then
  uv tool install git+https://github.com/ingjieye/apple-notes-cli
else
  pipx install git+https://github.com/ingjieye/apple-notes-cli
fi

# 3. Install the skill.
mkdir -p ~/.claude/skills/apple-notes
curl -fsSL https://raw.githubusercontent.com/ingjieye/apple-notes-cli/main/skills/apple-notes/SKILL.md \
  -o ~/.claude/skills/apple-notes/SKILL.md

# 4. Verify against the real store. Exit 1 from `search` just means no match.
apple-notes folders | head -5
apple-notes recent -n 3
```

If step 4 reports the database is missing, Notes.app has never been launched on
this machine. If it raises a permission error, the terminal needs access to the
Notes container — that is a macOS prompt the user has to accept, not something
you can grant.

</details>

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
