# duologue: handoff for the next build session

Start here when picking the project up. [DECISIONS.md](DECISIONS.md) holds the design and the reasons
behind it. This file says what state the project is in, what to build next and in which order, and
the format details learned the hard way that the code must respect.

## State (2026-11-03)

- **This repo:** design notes only, no package code yet.
- **The working prototype:** a single-file, standard-library Python bridge, about 3,300 lines. It
  ran for a week on a private agent fleet: native turns both ways, an append path that Codex itself
  indexes, anchored cursors, deterministic ids, full-history rebuilds, and output and model-context
  budgets.
  - It is **not** in this repo: it carries deployment specifics (OS users, folders, hosts) that
    must not be published. The maintainer has it.
  - Port its logic, not its deployment.
- **Tests it passed:** a 26-check suite on throwaway threads, run against the real Codex app-server
  and the real Claude Code. A real-session end-to-end run both ways: a real Claude session with 11
  tools went to Codex, and a real Codex turn with command, patch and MCP calls went to Claude. In
  both directions the other side's model answered correctly from the translated turns. A visual
  check confirmed both apps render the translated blocks natively.

## Build plan, in order

1. **Scaffold, modelled on `creynir/phalanx`.**
   - `pyproject.toml`: setuptools, console script `duologue`, `requires-python >=3.11`,
     Apache-2.0, a `dev` extra with pytest and ruff.
   - Layout: `src/duologue/`, `tests/`, `.pre-commit-config.yaml`.
   - CI in `.github/workflows/`:
     - `ci.yml`: push and PR to main; ruff and pytest on Linux and macOS, Python 3.11/3.12/3.13.
     - `publish.yml`: on `v*` tags, test, `python -m build`, then PyPI via Trusted Publishing
       (`id-token: write`), plus a check that the tag matches the version.
2. **Port the core into modules.**
   - `codex/`: JSON-RPC client over the app-server socket or stdio; rollout reading and writing;
     history index reads; migration; writer lock.
   - `claude/`: transcript reading and appending; relaunch under Remote Control.
   - `translate/`: Claude→Codex items, Codex→Claude tool blocks, and the text-to-native parser for
     older transcripts.
   - `sync/`: cursors, the tick and engine, conflicts.
   - `rebuild/`: full-history merge from several sources, with a model budget.
   - Also `config.py` and `cli.py`.
   - Defaults: one OS user, `~/.claude` and `~/.codex`. Everything else is config.
3. **Improvements 1–9 from the steal list in DECISIONS.md**, before any feature work. They are the
   safety net:
   1. rewrite hash;
   2. write-ahead and accepted cursors;
   3. persisted verification;
   4. the writer lock held across the write;
   5. Claude busy/idle from `~/.claude/sessions/<pid>.json`;
   6. native Edit/Write diffs;
   7. pairing tool calls by id;
   8. a pre-flight structure check;
   9. the version and schema gate.
4. **Tests.**
   - Unit tests with golden fixtures recorded from real CLIs, sanitized: no secrets, no private data.
   - Integration tests:
     - Hermetic `CODEX_HOME` and `CLAUDE_CONFIG_DIR`.
     - A scripted fake app-server, modelled on `codex-rs/app-server/tests/suite/v2`, including the
       "thread already has an active writer" error.
     - Mock model servers, to assert what each model saw. For Codex, a small Responses SSE mock
       with `base_url=http://127.0.0.1:PORT` and `requires_openai_auth=false`; for Claude,
       `ANTHROPIC_BASE_URL`.
   - A manual end-to-end canary with real CLIs: code words relayed both ways.
5. **CLI and service.**
   - Commands:
     - `doctor`: versions, gates, quarantine.
     - `link`/`unlink`, `status`, `sync`, `rebuild`, `hold`/`release`, `log`.
   - The service: launchd on macOS, a `systemd --user` timer on Linux.
6. **README and release.**
   - The README covers install, a quickstart and safety notes. State that OpenAI calls edits to
     rollouts from outside the harness unsupported (openai/codex#49328).
   - Then tag v0.1.0.

## Format facts the code must respect

### Codex (verified on 0.163)

- **Two history modes.**
  - `history_mode` comes from the rollout's first `session_meta` line (Codex decides by the file)
    and from `threads.history_mode` in `state_5.sqlite`.
  - **Legacy** threads render from text events only, so their `item_completed` records are ignored.
  - **Paginated** threads render only from `thread_history_1.sqlite`.
  - `codex migrate-rollouts --thread <id> --apply --json` converts a legacy thread; `skipped_busy`
    means it is loaded.
- **Appending is safe only with the thread closed.**
  1. Close it if it is loaded: `thread/archive` + `thread/unarchive`. The rollout moves while it
     is archived, so re-resolve its path.
  2. Take `~/.codex/thread-writer-locks/<id>.lock` (flock, non-blocking).
  3. Require the checkpoint `next_rollout_byte_offset` to equal the file size.
  4. Append canonical lines with contiguous `ordinal`s starting at `next_rollout_ordinal`.
  5. Call `thread/resume` with **no overrides** (they persist on the thread); this makes Codex index
     the tail.
  6. Verify with `thread/turns/list`, then call `thread/unsubscribe`. The writer lock stays held
     while the thread is loaded; `unsubscribe` does not release it.
  7. If the app had the thread open, reopen it so the app fetches the new turns.
- **Never write the history database.** Lines appended to a paginated rollout show up only after
  that resume. `thread/inject_items` is model-only and never rendered (openai/codex#49328).
- **Turn ids must be unique** in a thread: Codex silently drops duplicates. Its importer restarts at
  `external-import-turn-1`. Codex's own turns use UUIDv7; use deterministic uuid5 for ours.
- **Rollout item records** (`event_msg/item_completed`; PascalCase variant names, snake_case fields):
  - `CommandExecution`: `command` argv, `cwd` as a `file://` URI (anything else is skipped),
    `parsed_cmd` with type `read|list_files|search|unknown`, `source`, `status`,
    `aggregated_output`, `exit_code`, `duration`.
  - `McpToolCall`: camelCase fields.
  - `FileChange`: `changes` maps each path to `add|delete|update`. Without `status` it shows as in
    progress.
  - `AgentMessage`: `content: [{type: "Text"}]`, `phase`.
  - `UserMessage`: `content: [{type: "text", text_elements: []}]`.
  - `Reasoning`.
  - The history index stores enum values in camelCase; convert them to snake_case for rollouts.
- **No ids on `response_item`s:** the model-visible ones must carry no `id`, or the model API
  answers "user-supplied input item IDs" not supported.
- **App-server API.**
  - `thread/turns/list {threadId, itemsView: "full", sortDirection, limit, cursor}` returns `data`,
    `nextCursor` and `backwardsCursor`.
  - `thread/read includeTurns` leaves items `notLoaded`.
  - `thread/items/list` is not supported on legacy threads. On paginated threads it fails with
    "invalid paginated history lineage" when the index does not match the file.
- **Code mode.** `custom_tool_call name:"exec"` carries JavaScript with nested
  `tools.exec_command({cmd:…})`. In legacy threads Codex records no command items for those, so
  recover the commands from the script.
- **Turns can run back to back** (goal-driven work: the next `task_started` comes in the same second
  as the previous `task_complete`). Sync finished turns while a new one runs; never close a thread
  mid-turn.
- **Rollouts idle for 7 days** are compressed to `.jsonl.zst`.
- **Managed developer instructions** reach a chat once, at its start. Policy changes do not reach
  chats that are already running.
- **Not the user's words:** injected user-role context (`<environment_context>`, AGENTS.md,
  `<recommended_plugins>`, the app's `<external_codex_apps_open_page>` events). The real prompt is
  `event_msg/user_message` or a `UserMessage` item.

### Claude Code (verified on 2.1.3xx)

- **Appending records.**
  - Every assistant record needs `model`, or `--resume` hangs. Use the session's last real model,
    or `<synthetic>`.
  - `sessionId` must equal the file name, and the project directory is the cwd with every
    non-alphanumeric character replaced by `-`.
  - Roles strictly alternate.
  - Each `tool_use` is followed by its `tool_result` with `sourceToolAssistantUUID`.
  - Never forge `thinking` blocks.
- **Tool names.** Unknown tool names are accepted in history; Codex tools show best as
  `mcp__codex__*`.
- **Large files.** Above 5,242,880 bytes, Claude loads only what follows the last `compact_boundary`,
  and it shows long sessions from their last compaction onward.
- **Compaction records.** The boundary is `{type: "system", subtype: "compact_boundary",
  parentUuid: null, logicalParentUuid, compactMetadata: {trigger, preTokens, postTokens}}`, followed
  by a user record with `isCompactSummary: true` and `isVisibleInTranscriptOnly: true`. Copy any
  kept tail physically after it.
- **Records to drop or filter.** Translate only `type in {user, assistant}`: 2.1.316
  `api-request-blob` records duplicate messages. Drop `{type: "fallback"}` blocks. A headless resume
  may start a turn on its own ("Continue from where you left off."); filter it.
- **Signals and locations.**
  - `~/.claude/sessions/<pid>.json` holds `status: busy|idle|waiting`. Claude writes housekeeping
    records minutes after Stop, so file mtime is not a quiet signal.
  - `EnterWorktree` moves the transcript; look it up as `projects/*/<sid>.jsonl`, newest first.
  - Claude Code no longer has Grep, Glob or TodoWrite tools. They only appear in old transcripts.

## Rules for this repo

- **Nothing deployment- or company-specific in commits:** no hostnames, private paths, account
  names, or recorded data that isn't sanitized.
- **Work on branches with pull requests.** The design notes are the only bootstrap.
- **Test against real CLIs** on throwaway threads before claiming a format works. Both formats are
  undocumented and change between releases.
