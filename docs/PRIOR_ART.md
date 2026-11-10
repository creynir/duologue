# Prior art: what other tools do, and what duologue took from them

In November 2026 we read 45 open-source projects that move conversations between Claude Code and
Codex, or that read and write their files. This page records what we found, which of it we
checked against the apps themselves, and what duologue does about each finding.

## How the study was done

- **Search.** GitHub code and repository search for tools that touch both apps' session files:
  Codex rollouts (`rollout-*.jsonl`, `thread_history_1.sqlite`, `thread/resume`) and Claude Code
  transcripts (`~/.claude/projects`, `parentUuid`, `claude --resume`). Projects that only mention
  the apps were left out.
- **Reading, never running.** Every project was cloned and read as data. No code, script or
  test from them was run or imported.
- **Six groups, one research pass each.** Each pass answered the same questions for every project
  (purpose, mechanism, how it reads and writes each app, cursors, echo and conflict handling, live
  sessions, safety) and gave a file and line for every claim.
- **Checked again before use.** Every finding that changed duologue's code was checked once more
  against Codex 0.166.0's source, Claude Code 2.1.318, or both apps running in throwaway homes.
  The "Source" column below names where each one was confirmed.

## The landscape

Most tools copy a conversation once into a new session of the other app, or hand over a text
summary. Few append to an existing session. The closest to duologue is tandem, which maps tool
calls natively both ways but runs one command-line harness at a time, and whose tool activity does
not appear in the Codex app. None keeps one conversation going in both official apps, with each
side's tool calls shown natively in the other app.

### A. Live two-way sync

| Project | What it does |
|---|---|
| [Bhavya6187/tandem](https://github.com/Bhavya6187/tandem) | Runs the real `claude`, `codex` and `opencode` on a terminal and keeps one session across them, one at a time. Native tool mapping both ways and a write-ahead cursor. The most mature tool in the set. |
| [Chamotrans/codex-claude-session-sync](https://github.com/Chamotrans/codex-claude-session-sync) | macOS app that pairs a Claude session with a Codex thread and appends both ways. Counts turns as its cursor and writes Codex's `state_5.sqlite` itself. |
| [Maxaccurate/claude-codex-sync](https://github.com/Maxaccurate/claude-codex-sync) | Windows tool that keeps Claude and Codex sessions in step. Byte-offset cursors with a hash of the prefix, Codex's writer lock, backups and a verified write. |
| [faratech/agent-session-sync](https://github.com/faratech/agent-session-sync) | Hands sessions between five harnesses every two minutes, as new sessions and prompt-hook text. Tracks what it wrote in a ledger. |
| [AaronWong1999/ccx](https://github.com/AaronWong1999/ccx) | Menu-bar app that pairs a Claude session with a Codex thread; the other side arrives as text injected by a hook. Marks its own records with a field. |
| [AaronWong1999/cloudcli-plugin-ccx](https://github.com/AaronWong1999/cloudcli-plugin-ccx) | A UI tab that drives ccx. |
| [Chendestiny/agent-session-sync](https://github.com/Chendestiny/agent-session-sync) | Exports 21 agents' histories through a neutral format into 12 targets. Recognises its own records by the shape of their ids. |

### B. Converters: one session into a new native session of the other app

| Project | What it does |
|---|---|
| [Dicklesworthstone/cross_agent_session_resumer](https://github.com/Dicklesworthstone/cross_agent_session_resumer) | `casr`: resumes a session of one of 15 providers in another, as a new session. |
| [liwala/session-porter](https://github.com/liwala/session-porter) | Transfers a session in raw, full or summary form; writes Codex's database; edits become comments. |
| [1va7/opal-bridge](https://github.com/1va7/opal-bridge) | Two-way translator through its own session format, with real diffs and compaction both ways. Re-renders whole twin files. |
| [nerln/claude-codex-bridge](https://github.com/nerln/claude-codex-bridge) | Moves a conversation into a new session of the other agent, within a head and tail budget, with a format drift check. |
| [Agentryx-ai/pass-the-thread](https://github.com/Agentryx-ai/pass-the-thread) | Imports Codex desktop threads into the Claude desktop app (the other way is experimental). Tells authored lines from bookkeeping when it looks for conflicts. |
| [mirkobrombin/AgentTranscriptConverter](https://github.com/mirkobrombin/AgentTranscriptConverter) | One-shot converter both ways that aims to lose nothing. Documents how `claude --resume` reads a transcript's first and last 64 KiB. |
| [FredrikAhman/claudeimportfromcodex](https://github.com/FredrikAhman/claudeimportfromcodex) | Imports Codex threads into Claude Code's CLI and desktop list; filters the context Codex injects; recognises threads made by Codex's own importer. |

### C. Handoff and resume

| Project | What it does |
|---|---|
| [kastner/codex-claude-sessions](https://github.com/kastner/codex-claude-sessions) | Drives Codex's own importer (`externalAgentConfig/detect` and `/import`) over the app-server. Claude to Codex, once. |
| [AndrewAvery7/claude-codex-bridge](https://github.com/AndrewAvery7/claude-codex-bridge) | A `/to-codex` command that hands a Claude session to Codex through its importer and opens the thread. |
| [awithi-co/acdc](https://github.com/awithi-co/acdc) | Plugins for both apps: cross-agent resume, recall, and a live relay through a shared Codex app-server. Detects the history a fork replays. |
| [Amal-David/claude-to-codex](https://github.com/Amal-David/claude-to-codex) | A `/handoff` command that packages the active Claude session for a Codex terminal. Follows the active branch of a rewound transcript. |
| [neeschit/ripcord](https://github.com/neeschit/ripcord) | Recovers a bounded summary of the other tool's last session. |
| [ralskwo/codex-claude-session-bridge](https://github.com/ralskwo/codex-claude-session-bridge) | An MCP server that lists and reads sessions and prepares a handoff, reading Codex newest first within a budget. |
| [gitgoready/claude-codex-switch](https://github.com/gitgoready/claude-codex-switch) | Converts whole sessions both ways into new sessions and inserts rows into Codex's database. |
| [TooonyChen/synctx](https://github.com/TooonyChen/synctx) | A terminal list of sessions from three agents that copies one into another as a new session. |
| [redareda9/claude-codex-switch](https://github.com/redareda9/claude-codex-switch) | A shell wrapper around an external converter that opens the other agent in a new tab. |
| [thearyanag/claude-to-codex](https://github.com/thearyanag/claude-to-codex) | One-way converter with deterministic ids. |
| [hacktivist123/agent-session-resume](https://github.com/hacktivist123/agent-session-resume) | A skill that has the receiving agent read the prior session and write a checkpoint first. |

### D. Tools that read or write Codex's rollouts and index

| Project | What it does |
|---|---|
| [extracurricular-ai/codex-rewind](https://github.com/extracurricular-ai/codex-rewind) | A fork of Codex 0.166.0 that adds `/rewind` and `/redo`. We used its source as the reference for how Codex keeps rollouts and its history index consistent. |
| [ryfineZ/codex-session-patcher](https://github.com/ryfineZ/codex-session-patcher) | Rewrites replies inside sessions, then has to repair Codex's index offsets. |
| [ANGELBERRIOS23/conversation-reclaim](https://github.com/ANGELBERRIOS23/conversation-reclaim) | Frees disk space by stripping images from old rollouts. |
| [BigStrongSun/ccswitchmulti](https://github.com/BigStrongSun/ccswitchmulti) | A provider switcher with Codex repair tools; ran into duplicate ordinals written while Codex had a thread open. |
| [ccpopy/cc-sessions](https://github.com/ccpopy/cc-sessions) | A session manager (browse, edit, move, back up) that takes Codex's writer lock and writes through a transaction file. |
| [AgentWorkforce/relayhistory](https://github.com/AgentWorkforce/relayhistory) | A local search index across agents, reading Codex's database read-only. |
| [org2AI/ORG2](https://github.com/org2AI/ORG2) | A desktop record of agent work. Changes a Codex prefix only by writing a new rollout file and switching to it. |

### E. Codex app-server clients and live bridges

| Project | What it does |
|---|---|
| [nwverse/claude-codex](https://github.com/nwverse/claude-codex) | Makes the Codex desktop app talk to Claude Code by standing in for Codex's backend. The history lives in its own database. |
| [jamestut/codex-multiplexer](https://github.com/jamestut/codex-multiplexer) | Lets several Codex clients share one app-server. |
| [careless10/claude-codex-bridge](https://github.com/careless10/claude-codex-bridge) | Sends text between a running Claude session and a Codex thread (`codex queue`, Claude's messaging socket). |
| [SeemSeam/claude_codex_bridge](https://github.com/SeemSeam/claude_codex_bridge) | A tmux workspace that passes questions between many command-line agents. |
| [happier-dev/happier](https://github.com/happier-dev/happier) | A hosted app that runs agents and can switch agent inside a session, with text briefs. |
| [Sunwood-ai-labs/codex-remote-control-lab](https://github.com/Sunwood-ai-labs/codex-remote-control-lab) | A phone browser in front of a Codex app-server. |
| [asheshgoplani/agent-deck](https://github.com/asheshgoplani/agent-deck) | A tmux control panel for many agent sessions, with a search index. |

### F. Readers, indexers and cross-device sync

| Project | What it does |
|---|---|
| [zippoxer/recall](https://github.com/zippoxer/recall) | Full-text search and resume across agents. |
| [spacering-net/codeg](https://github.com/spacering-net/codeg) | A multi-agent workspace whose parsers model Codex's paginated history in full: `history_base`, ordinals, rewound files. |
| [westsource/agentctxsync](https://github.com/westsource/agentctxsync) | Pools sessions across devices; supports neither Claude Code nor Codex. |
| [Gregor-von-Vitek/agent-sessions-sync](https://github.com/Gregor-von-Vitek/agent-sessions-sync) | Syncs whole session files between machines through GitHub. |
| [pirate/iterm-agent-fork](https://github.com/pirate/iterm-agent-fork) | An iTerm2 script that forks an agent session or hands it to another harness. |
| [mgreau/zen](https://github.com/mgreau/zen) | A worktree manager that migrates a session to the other agent when only one has it. |

Codex also has its own importer (`externalAgentConfig`), which copies Claude sessions into new
Codex threads once, with tool calls as text.

## What we learned, and what duologue does now

Each finding was confirmed in the source named, then built into duologue with a test that fails
without it.

| Finding | Source | duologue now |
|---|---|---|
| Codex numbers every line of a paginated rollout. It continues from the last line's number, and its history index accepts only the next number. | codex-rs `rollout/src/ordinal.rs`, `thread-store/src/local/thread_history.rs`; ccswitchmulti | Before appending, the file's last ordinal must be the one before the index's next, or the pair blocks with both numbers. |
| A thread rewound in Codex (`thread/revert`) continues in a new file, `rollout-<time>-<thread>_<rollout>.jsonl`, whose `session_meta` has `history_base` (thread and end ordinal). Codex's state database then points at it. | codex-rs `thread-store/src/local/revert_thread.rs`; codeg | Finds that file by name, and blocks with "rewound in Codex" instead of a generic message. `release` resumes from where the new file stands. |
| Editing an earlier message or undoing in Codex appends `thread_rolled_back` with `num_turns`. Codex ends the open turn, then drops the last `num_turns` turns. | codex-rs `app-server-protocol/src/protocol/thread_history.rs` (`handle_thread_rollback`) | Turns rolled back before they synced never reach Claude. When turns Claude already has are rolled back, a note says how many, with Codex's next turns or on its own. A Claude turn on top of a rollback is a conflict. |
| Codex may keep a rollout compressed (`.jsonl.zst`), and writes it out plain when the thread is resumed. | codex-rs `rollout/src/recorder.rs`; checked with Codex 0.166.0 | A compressed thread stays compressed while neither side has anything to copy (Codex's index ends at the cursor), and is resumed when one does, instead of waiting until someone opens it in Codex. Archived threads are left alone. |
| A message typed while Claude works is stored as an `attachment` of type `queued_command` (`commandMode` "prompt", origin `human`) and folded into the running turn. Task notifications and messages from other agents (origin `peer`, marked `isMeta`) use the same record. | codeg; checked in about 1,000 such records written by Claude Code 2.1.220 to 2.1.316 | Travels to Codex as a second user message in the same turn, as Codex records a message sent mid-turn. Task notifications and other agents' messages do not. |
| Claude's session list reads the first and last 64 KiB of a transcript. It shows the prompt in the last `last-prompt` line (`lastPrompt`, `leafUuid`, `sessionId`), cut to 200 characters. | mirkobrombin, FredrikAhman, Maxaccurate; checked in transcripts Claude Code wrote | Every append from Codex that holds a message typed in Codex ends with a `last-prompt` line naming the last one. |
| The Claude app draws a diff from the result record of its own `Edit` and `Write` tools (`structuredPatch`, `oldString`, `newString`). | tandem, opal-bridge; the fields checked in records Claude Code wrote | A Codex change to one file becomes Claude's own `Write` (new file) or `Edit` (one hunk) with that record. Anything else stays one `apply_patch` call. Claude Code 2.1.318 resumes a session holding them (an end-to-end test). |
| Above 5,242,880 bytes, Claude Code loads a transcript only from its last compaction on. | zen; checked in Claude Code 2.1.318's transcript loader | `doctor` notes a transcript past that size. |
| Cursors alone do not stop echoes after a cursor moves back. The robust tools also recognise their own records: by id shape, a marker field or a ledger. | Chendestiny, ccx, Maxaccurate, faratech | Records duologue writes to Claude carry `duologueSource`; Codex turns it writes have name-based (version 5) ids. A sync pass never sends either back. A new pair's history copy still includes them. |
| Before a write, check the structure the other app will rebuild from. | tandem, Maxaccurate | Turns for Codex are checked before the write: each turn starts and ends, every item sits in its turn and thread, no id repeats. |
| Handoff tools tell the model that the history is real, that tool outputs describe the past, and to check the repository before acting. | acdc, Amal-David, thearyanag, ralskwo | The lead note before Codex's turns says their tool outputs show files as they were then. |
| `doctor` is where index trouble shows first: an index ahead of its file means the file was cut. | relayhistory, cc-sessions, agent-deck | `doctor` compares each pair's Codex index with its file. |

## Decisions

### Already in duologue's design, confirmed by others

- Byte-offset cursors with a hash of everything before them (Maxaccurate); a write-ahead journal
  (tandem, cc-sessions, ORG2).
- Codex's writer lock, taken the way codex-rs takes it, for the whole append (Maxaccurate,
  cc-sessions, ORG2).
- Let Codex index its own file on `thread/resume`, then verify the index; never write Codex's
  databases. Projects that wrote `state_5.sqlite` or `INSERT`ed thread rows (Chamotrans,
  Chendestiny, opal-bridge, gitgoready, synctx) broke on schema changes.
- Native tool records on both sides. Every converter that flattened tool calls to text lost
  structure the next model needed.
- A gate on Codex's item format, deterministic ids, and no forged thinking.

### Later

- Waking a running app instead of waiting: `codex queue --thread` for Codex and the messaging
  socket of a running Claude Code (careless10). Opt-in only.
- Using Codex's importer to create the Codex side in `link` (kastner, AndrewAvery7).
- A second check of Codex's index through `thread/turns/list` (Maxaccurate, ccx).
- Following a rewound thread's `history_base` across files, so a rewind needs no `release`
  (codeg).
- Detecting the replayed history at the start of a forked thread (acdc).
- Reading only the active branch of a Claude transcript rewound with `/rewind` (Amal-David).
- More live-session signals: open files (`lsof`) and the process's folder (iterm-agent-fork, zen).
- Carrying compaction both ways (opal-bridge, pass-the-thread), `TodoWrite` as `update_plan`
  (tandem), and title records (gitgoready).

### Rejected

- **Writing Codex's databases.** Codex owns them; schema changes break writers.
- **Text or summary handoffs, and prompt injection.** The other model loses the tool structure,
  and the other app does not show the turns.
- **Standing in for Codex's backend** (nwverse). The conversation then exists only in that
  backend, not in Codex's own threads.
- **Typing into a terminal app with tmux.** It depends on the layout of the input box.
- **Re-rendering whole files.** It destroys turns added on the other side (opal-bridge and
  pass-the-thread both added guards after losing messages).
- **Turn counts as cursors.** They go wrong after compaction or an edit.

## Known trade-offs

- **Claude Code treats a file it sees in a native `Edit` or `Write` as read.** When it resumes a
  session, Claude Code 2.1.318 seeds its read-before-write check from those records, so after a
  Codex change arrives as `Edit`, Claude may overwrite that file without reading it first. Claude
  Code's own edits do the same; the lead note asks Claude to check the repository first. We keep
  the native records for the diff they show.

- **A cursor that stops after an undo is anchored on that undo.** If Codex later converts a
  legacy thread, the conversion drops undo lines (codex-rs `rollout_migration/rollback_plan.rs`),
  so the anchor is not found and the pair blocks until `release`.

## Open questions

- pass-the-thread reports that the Claude desktop app may rewrite a transcript when it opens it.
  If it does, duologue sees the Claude side change before its cursor and blocks the pair until
  `release`. This needs a test with the desktop app.
