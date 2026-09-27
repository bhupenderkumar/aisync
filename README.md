# aisync

One command to see and continue conversations across Claude Code and OpenCode.

- Claude Code stores sessions in `~/.claude/projects/<dir-slug>/<uuid>.jsonl`.
- OpenCode stores sessions in SQLite (`~/.local/share/opencode/opencode.db`) and ships `opencode export` / `opencode import`.

aisync reads both, lists them together, and copies messages across. Copies stay linked, so later syncs only add new messages, in either direction.

## Requirements

- macOS or Linux, Python 3.9 or newer (standard library only)
- [Claude Code](https://claude.com/claude-code) (`claude`) and [OpenCode](https://opencode.ai) (`opencode`) on your `PATH`

## Install

```
git clone https://github.com/bhupenderkumar/aisync.git
cd aisync
chmod +x aisync.py
ln -sf "$PWD/aisync.py" ~/.local/bin/aisync   # any directory on your PATH works
```

## Commands

```
aisync                         # interactive: pick a session, then continue in claude or opencode
aisync list [--all]            # sessions from both tools for this dir (or every dir)
aisync show <id> [--last N]    # print a transcript (id prefix is fine)
aisync sync [<id>] [--all] [--since DAYS]   # two-way sync of one session, or every session in this dir
aisync resume <id> claude|opencode          # sync, then open it in the chosen tool
aisync context <id> [--last N] # markdown handoff to paste into any AI tool
```

Typical flow when Claude Code stops working:

```
aisync resume <claude-session-id> opencode
# ...work in OpenCode...
aisync resume <opencode-session-id> claude   # back to Claude, OpenCode turns included
```

## How it works

- Tool calls and results are copied as readable text (`[tool call: bash] ...`), with long output truncated to 1500 chars. Thinking blocks are not copied. This keeps the history valid for both APIs.
- Copied turns start with `[from claude]` or `[from opencode]`.
- Pairing state is in `~/.aisync/state.json`. Before aisync rewrites an OpenCode session, it saves a backup to `~/.aisync/backups/`.
- Close the session in the other tool before you sync it. A running TUI won't show messages added while it's open.

## Configuration

- `OPENCODE_DB`: path to the OpenCode database (default `~/.local/share/opencode/opencode.db`)
- `AISYNC_HOME`: where aisync keeps its state and backups (default `~/.aisync`)
