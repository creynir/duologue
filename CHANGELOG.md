# Changelog

## Unreleased

Every conversation syncs by itself.

- **Automatic pairing.** Each sync pass pairs the Claude Code sessions and Codex threads that
  changed in the last 7 days (`auto_days`) and have no other side yet, newest first and up to 10
  a pass, creating that side in the same folder. Scripted runs, Codex subagent, rewound,
  archived and compressed threads, conversations whose folder is gone and ones with nothing
  typed yet are left out, and so is a pair you unlinked until you link it again. `duologue
  status` says what waits or was left out, and why. A try that failed is made again ten minutes
  later. `auto = false` turns it off.
- **A deleted conversation removes its pair**, and the other side is not paired again.
- **`sync --quiet`** (the service) prints a stop once, not on every pass, and a pass that
  changed nothing does not rewrite the pairs file.
- **Codex's own importer.** A Claude session Codex already imported is paired with that thread
  instead of getting a second copy. What Claude added since the import follows, the newest turns
  within the copy budget, after a note saying where the others are. A session that no longer
  starts with what Codex copied syncs with that thread from now on when you carried on in it in
  Codex, and gets a thread of its own otherwise. The importer's turns never travel back to
  Claude.
- **Writing in both apps keeps both.** Claude's new turns go to Codex after Codex's, and Codex's
  to Claude after Claude's, each with a note that they happened at the same time. An undo in
  Codex of a Claude turn copied this way is told to Claude. `on_conflict = "stop"` keeps the
  old behaviour: the pair waits for `duologue resolve`.

What a study of 45 similar tools taught us, checked against Codex 0.166.0 and Claude Code
2.1.318 (see [docs/PRIOR_ART.md](docs/PRIOR_ART.md)).

- **Diffs in Claude.** A new file, or a change to one place in one file, arrives from Codex as
  Claude's own `Write` or `Edit`, so the Claude app shows the diff. Other changes stay one
  `apply_patch` call.
- **Messages typed while Claude works** reach Codex in the turn they joined.
- **Undo and edits in Codex.** Rolled-back turns that had not synced are left out. When Claude
  already has them, a note tells Claude how many were undone, with the next Codex turns or on
  its own. A Claude turn written on top of an undo is a conflict.
- **Rewound and compressed Codex threads.** A rewound thread blocks with that reason, and `link`
  will not copy its history. A thread Codex compressed while idle is written out again when
  either side has turns to copy (and by `release` and `resolve`), instead of waiting until it
  is opened in Codex. An archived thread, or one whose last write by duologue is not settled
  yet, is still left alone.
- **Claude's session list** shows the last prompt from Codex.
- **Task notifications** that reach Claude as prompts are not copied to Codex as the user's.
- **Echo guard.** Records duologue wrote are recognised by a field (Claude) or their id (Codex)
  and never sent back, even if a cursor moves behind them.
- **Checks before writing to Codex:** whole turns, and line numbers that continue Codex's own.
- **`doctor`** notes a Claude transcript over 5 MiB and a Codex index that lags or overruns its
  file.
- The note before Codex's turns says their tool outputs show files as they were then.

## 0.1.0

The first release: one AI coding conversation in both Claude Code and Codex.

- **Link** a Claude Code session and a Codex thread, the newest of each in a folder with
  `--claude last --codex last`, or link one and let duologue create the other from its history
  (the newest turns within the copy budget, with a note saying where the older ones are).
- **Sync both ways.**
  - Claude's turns reach Codex as Codex's own items: commands, file changes and MCP calls the
    Codex app shows natively.
  - Codex's turns reach Claude as tool calls and results.
  - Each model is shown the other side's work on its next turn.
  - Only finished turns travel, nothing is copied twice, and duologue waits while either app has
    the conversation open.
- **Commands:**
  - `link`, `sync`, `status`, `log`, `doctor`, `hold`, `release`, `resolve` and `unlink`;
  - `service` runs the sync every 30 seconds under launchd or `systemd --user`.
- **Safety:**
  - every write is journaled before it happens and settled after a crash;
  - cursors detect files rewritten under duologue;
  - only duologue's own bytes are ever undone;
  - Codex's databases are never written;
  - records for Claude are checked before they are written;
  - writes wait for a Codex whose item format duologue has not been tested with.
- Tested with Claude Code 2.1.318 and Codex 0.166.0, on Python 3.11 to 3.14, Linux and macOS.
  CI runs the real CLIs against local model stand-ins on every change.
