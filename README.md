# duologue

One conversation, two coding agents. Switch sides whenever you like.

duologue keeps one AI coding conversation in both Claude Code and OpenAI Codex. Work on it in
Claude, carry on in Codex, come back to Claude: each side shows what happened on the other as its
own native history. Messages are messages, shell commands are commands, file edits are edits, and
each model is shown the other's work on its next turn.

**Status: alpha.** It works end to end with Claude Code 2.1.318 and 2.1.322 and Codex 0.166.0
and 0.167 (the ChatGPT app's own Codex included), and the tests run both real CLIs on every
change. It writes into files that both apps treat as internal. OpenAI
calls edits to Codex's session files from outside Codex unsupported
([openai/codex#49328](https://github.com/openai/codex/issues/49328)), so a Codex update can
break it. duologue checks the item format of the Codex it runs with, and refuses to write to one
it has not been tested with.

## Requirements

- macOS or Linux, Python 3.11 or later.
- Claude Code and the Codex CLI, used by the same OS user.

## Install

```sh
pipx install duologue        # or: uv tool install duologue
duologue doctor              # checks both apps, their versions and their folders
```

Until the first release is on PyPI: `pipx install git+https://github.com/creynir/duologue`.

## Quick start

```sh
duologue service install     # sync every 30 seconds in the background
```

That is all. On each pass duologue pairs the Claude Code sessions and Codex threads that changed
in the last 7 days and have no other side yet, newest first and up to 10 a pass: it creates that
side in the same folder, then keeps the two in sync. `duologue sync` runs a pass now; `duologue
status` lists the pairs, and what is waiting to be paired or was left out, and why.

A new side gets the newest turns that fit the copy budget (400,000 characters by default), with a
note telling the model where the older ones are. A Claude session that Codex's own importer
already copied is paired with that thread instead of getting a second one; what Claude added
since the import follows, within the same budget. (If the session changed since in another way,
the two are paired from now on only if you carried on in Codex's copy; otherwise the session
gets a thread of its own, and Codex's copy stays as it is.)

Left out: scripted runs (`claude -p`, the Agent SDK, `codex exec`) unless `auto_scripted = true`,
Claude sessions that continue in another session, Codex subagent threads, threads rewound in
Codex, archived threads, threads Codex compressed while idle (until you open them again),
conversations whose folder is gone, ones with nothing typed or no finished turn yet, and pairs you
unlinked. When a conversation has been deleted for an hour (while its app's folder of
conversations is there; a folder that is not, such as an unmounted drive, blocks the pair
instead), its pair goes, and the other side is paired again only once you carry on in it. To pair
only what you choose, set `auto = false` and link by hand, in the folder you work in:

```sh
duologue link --claude last --codex last   # pair the newest Claude session and Codex thread here
duologue link --claude last                # a Codex thread with this Claude session's history
duologue link --codex last                 # a Claude session with this Codex thread's history
```

## How it works

- **Claude to Codex.** Each Claude turn becomes a Codex turn made of Codex's own items: a user
  message, commands (`Read`, `Grep` and `Glob` become the `cat -n` and `rg` commands that do the
  same), file changes and MCP calls, so the Codex app shows them natively. The Codex model gets a
  note for each tool call instead, so it never takes Claude's calls for its own. duologue appends
  the turns while no Codex process has the thread loaded, holding Codex's own writer lock. Then
  it lets Codex index them, and checks that it did.
- **Codex to Claude.** Codex's commands, file changes and MCP calls become tool calls and results
  in the Claude transcript, after a note saying they happened in Codex. A new file, or a change
  to one place in one file, becomes Claude's own `Write` or `Edit`, so the Claude app shows the
  diff; Claude Code then treats that file as read, and may overwrite it without reading it first.
  Other changes arrive as one `apply_patch` call. (Turns that Codex's own
  importer made hold only text, and arrive as text.) duologue writes them
  only while no Claude Code process has the session open, so it carries on from them when it is
  next opened.
- **Only finished turns travel.** A turn still running on either side waits for its end. A
  message you type into Claude while it works travels with the turn it joined.
- **Undo in Codex.** Turns you edit away or undo in Codex before they synced never reach Claude;
  if Claude already has them, a note tells Claude which were undone.
- **Nothing is copied twice.** Every id duologue writes derives from the source record. Each
  side has a cursor, and duologue's own records never travel back.

## When it waits, and when it stops

`duologue status` says what each pair is doing.

- **Waiting** clears by itself once the app lets go: the Claude session is open, a Codex app has
  the thread loaded, or a turn is running. Some waits need you: the thread is archived
  (unarchive it); a Codex turn never finished because Codex stopped (send the thread a new
  message); and a Codex whose item format duologue has not been tested with (update duologue;
  `notify` tells you once).
- **Written in both apps.** You wrote in both since the last sync. Both are kept: Claude's new
  turns go to Codex after Codex's, then Codex's go to Claude after Claude's, each with a note
  telling the model the two happened at the same time. With `on_conflict = "stop"` the pair
  stops instead, as a **conflict**, until you keep one side's new turns:
  `duologue resolve <pair> --keep claude` (or `codex`). The other side's new turns stay where
  they are.
- **Blocked** needs you. A file changed before the point already synced (rewinding a thread in
  Codex does this too), or an app did not take a write the way it should. Check the
  conversation, then run `duologue release <pair>`: the pair syncs on from where each side
  stands.

## Safety

- **Write-ahead journal.** Every write, including the side `link` creates, is recorded before
  it happens. After a crash, the next run works out from the file whether the write is
  missing, cut off or complete.
- **Rewrite detection.** Each cursor keeps a hash of the file up to it, so a file rewritten under
  duologue stops the pair instead of being guessed at.
- **Undo only its own bytes.** When an app does not take a write, duologue removes only what it
  wrote itself, and only while nothing was written after it.
- **No writes to Codex's databases.** Codex indexes appended lines itself.
- **Structure check.** Records for Claude are checked before they are written: links, roles,
  tool calls answered, a model on every answer, no forged thinking.

## Commands

| Command | What it does |
|---|---|
| `link` | Pair a Claude session with a Codex thread (`--claude`, `--codex`: an id, or `last`), or create the missing one |
| `sync` | One sync pass now: pairs new conversations, then syncs every pair (or only the ones named) |
| `status` | Every pair, its state and its last sync (`--json` for all of it) |
| `hold` / `release` | Pause a pair, or resume a held or blocked one |
| `resolve` | With `on_conflict = "stop"`: after you wrote in both apps, keep one side's new turns |
| `log` | Recent activity |
| `doctor` | Check both apps, their versions, Codex's item format and every pair |
| `unlink` | Stop syncing a pair; both conversations stay as they are, and are not paired again unless you link them |
| `service` | `install`, `uninstall` or `status` of the background sync (launchd or `systemd --user`); its output goes to `service.log` in duologue's folder (`$DUOLOGUE_HOME`, by default `~/.duologue`) |

## Configuration

Everything is optional. `~/.duologue/config.toml` (or `$DUOLOGUE_HOME/config.toml`):

```toml
claude_home = "~/.claude"          # default: $CLAUDE_CONFIG_DIR, else ~/.claude
codex_home = "~/.codex"            # default: $CODEX_HOME, else ~/.codex
claude_bin = "claude"
codex_bin = "codex"
quiet_seconds = 15                 # a finished Claude turn waits this long before it is copied
copy_budget = 400000               # characters copied into a side duologue creates
notify = ["notify-send", "duologue"]   # run with a message when a pair stops
allow_untested_codex = false       # write to a Codex whose item format is untested
auto = true                        # pair new conversations by itself; false: only what you link
auto_days = 7                      # ... the ones that changed within this many days
auto_scripted = false              # ... and scripted runs too
on_conflict = "keep-both"          # after you wrote in both apps; "stop" waits for resolve
desktop_cards = true               # list the sessions duologue creates in the Claude desktop app
claude_desktop_dir = "~/Library/Application Support/Claude"   # that app's folder
```

## Where conversations show up

- **Claude Code** (the CLI's `--resume` list and the VS Code extension) lists a session duologue
  created with the others from its folder.
- **The Codex app** lists a thread duologue created under its folder, as it lists its own.
- **The Claude desktop app** lists a session duologue created, under its folder, the next time
  the app starts (macOS, with Claude Code's default folder; `desktop_cards = false` stops new
  cards, and the cards already there are still followed). When you carry on in it there, the app
  copies the session into one of its own; duologue follows the copy, and the old session is
  titled "(continued in the Claude desktop app)". Not yet checked with the real desktop app: the
  card format is the app's own, undocumented, and may change.
- **Remote Control** shows a Claude session while a Claude Code process runs it with remote
  control on; duologue does not start one.

## Limits

- Claude Code shows a long session only from its last compaction. Older turns stay in the file.
- duologue writes a Codex turn into a Claude session only while no Claude Code process has it
  open (a terminal, VS Code, the desktop app or Remote Control). Those turns wait until it is
  closed; Claude's turns still go to Codex meanwhile.
- A Codex app that keeps a thread loaded keeps duologue waiting. With a shared app-server daemon
  (`codex remote-control start`), duologue closes an idle thread briefly to write to it.
- One user on one machine. Rebuilding a whole history from several copies is planned for a
  later release.

## Development

```sh
pip install -e ".[dev]"
pytest                 # unit and integration tests (a fake Codex app-server)
pytest -m e2e          # the real claude and codex CLIs, against local model stand-ins
```

The design, what the prototype proved and the decisions behind it are in
[docs/DECISIONS.md](docs/DECISIONS.md).

## License

Apache-2.0, see [LICENSE](LICENSE).
