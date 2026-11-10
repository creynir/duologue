# Changelog

## Unreleased

What a study of 45 similar tools taught us, checked against Codex 0.166.0 and Claude Code
2.1.318 (see [docs/PRIOR_ART.md](docs/PRIOR_ART.md)).

- **Diffs in Claude.** A Codex change to one file arrives as Claude's own `Write` or `Edit`, so
  the Claude app shows the diff.
- **Messages typed while Claude works** reach Codex in the turn they joined.
- **Undo and edits in Codex.** Rolled-back turns that had not synced are left out; when Claude
  already has them, the next Codex turns arrive with a note saying how many were undone.
- **Rewound and compressed Codex threads.** A rewound thread blocks with that reason. A thread
  Codex compressed while idle is written out again when Claude has turns for it, instead of
  waiting until it is opened in Codex.
- **Claude's session list** shows the last prompt from Codex.
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
