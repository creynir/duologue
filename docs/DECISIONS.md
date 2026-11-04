# duologue: decisions so far

Status: pre-alpha. The design below comes from a working prototype that ran on one agent fleet in
November 2026, kept a handful of real conversations in sync both ways, and was tested end to end
against the Claude Code builds that fleet ran (2.1.303 to 2.1.316) and Codex 0.163.0. This file records
what was decided and what was learned, so the package can be built from it.

## What duologue is

One AI coding conversation that lives in two places at once: Anthropic's Claude Code and OpenAI's
Codex. You talk to it in the Claude app (Claude Code Remote Control) or in the ChatGPT/Codex app
(`codex remote-control start`, which runs Codex's app-server daemon with remote control on) and
switch whenever you like. Each side's history shows the other side's work natively. Messages, shell
commands, file edits and tool calls appear as real tool blocks, not pasted text. Sync is continuous,
in both directions, appends to the same conversation on each side, never duplicates a turn, and keeps
each model's context within a budget. Older history stays in the files and readable on demand, though
an app may show only the recent part: Claude shows a long session from its last compaction onward.

Nobody else does exactly this (prior-art review, 2026-11-03): see "Prior art" below.

## Product decisions

| Topic | Decision |
|---|---|
| Name | `duologue`: PyPI `duologue`, CLI `duologue`, repo `creynir/duologue`. The npm name is taken and not needed. |
| Distribution | Pure-Python wheel on PyPI. Runtime stays standard-library only (the prototype is ~3,300 lines of stdlib Python). Install with `uv tool install duologue` or `pipx install duologue`. |
| Python | `>=3.11`, tested on 3.11, 3.12, 3.13 and 3.14. |
| License | Apache-2.0 (chosen when the repo was created). |
| CI/CD | GitHub Actions, modelled on `creynir/phalanx`. `ci.yml` runs on push and PR to main: ruff; pytest on Linux with Python 3.11 to 3.14 and on macOS with 3.14 only, because GitHub's macOS runners were often too busy to start queued jobs; a wheel build and smoke test. `publish.yml` runs on a `v*` tag: the tag must match the `pyproject.toml` version, then CI, `python -m build`, and PyPI via Trusted Publishing (`id-token: write`, no stored token). Actions are pinned to commit SHAs, kept current by Dependabot. |
| Default setup | One person, one machine: the same OS user runs Claude Code and Codex (macOS or Linux, both apps installed). |
| Advanced setup | Separate OS users per agent, a shared work folder, a notifier hook for alerts. Driven by config, not code. |
| Service | launchd (macOS) or a `systemd --user` timer (Linux). |
| CLI | `link`, `unlink`, `status`, `sync`, `rebuild`, `doctor`, `hold`/`release`, `log`. |
| Naming | No vendor trademarks in names or branding. |
| Scope | No company-specific code in the package. Deployment details live in the user's config. |

## Architecture: what the prototype proved

### Codex side

- **Two history modes.** `history_mode` lives in the rollout's first `session_meta` line (Codex
  decides by the file) and in `state_5.sqlite` `threads.history_mode`.
  - **legacy**: importer-made threads. They have no `ordinal`s and are rebuilt from text events
    (`user_message`/`agent_message`). `item_completed` records are ignored, so there are no native
    tool items.
  - **paginated**: all native threads. They render only from `~/.codex/thread_history_1.sqlite`
    (`thread_turns`, `thread_items`, `thread_history_projection_state`).
- **Lines appended from outside never show by themselves.** `thread/inject_items` is model-only and
  never rendered (openai/codex#49328).
- **Append path (works, no database writes).**
  1. If the thread is loaded, close it with `thread/archive` + `thread/unarchive`. Archiving moves the
     rollout to `archived_sessions/`, so re-resolve the path afterwards.
  2. If the thread is legacy, convert it first: `codex migrate-rollouts --thread <id> --apply --json`.
     `skipped_busy` means retry later.
  3. Take the writer lock `~/.codex/thread-writer-locks/<id>.lock` (flock, non-blocking). If it is held,
     do not write.
  4. Require the projection checkpoint `next_rollout_byte_offset` to equal the file size. A longer
     file means an earlier append stopped before Codex indexed it (the prototype stopped here with an
     error and needed a person). Recover first: when the extra lines are exactly the ones duologue
     recorded before writing them (steal-list item 2), run steps 6 and 7 for them, then start again;
     otherwise block the pair and report it. Never append after lines Codex has not indexed.
  5. Append canonical lines with contiguous `ordinal`s starting at `next_rollout_ordinal`.
  6. Call `thread/resume` with no overrides: overrides persist on the thread. This is what makes Codex
     index the tail itself (codex-rs `thread_processor`: "Paginated JSONL is canonical, but its SQLite
     projection can lag").
  7. Verify that the turns and the checkpoint advanced, then call `thread/unsubscribe`.
  8. If the app had the thread open, reopen it so the app refetches.
- **Turn ids must be unique.** Codex silently drops a duplicate turn id. Its importer restarts at
  `external-import-turn-1` on every import. Codex's own turns use UUIDv7; ours are deterministic uuid5.
- **Item records** (rollout `event_msg/item_completed`; the variant name is PascalCase, fields are
  snake_case):
  - `CommandExecution`: `command` argv, `cwd` as a `file://` URI, `parsed_cmd` with type
    `read|list_files|search|unknown`, `source: unified_exec_startup`, `status`, `aggregated_output`,
    `exit_code`, `duration`.
  - `McpToolCall`: camelCase fields.
  - `FileChange`: `changes` maps each path to `{type: add|delete|update, ...}`.
  - `AgentMessage`: `content: [{type: "Text"}]`, `phase`.
  - `UserMessage`: `content: [{type: "text", text_elements: []}]`.
  - `Reasoning`.

  Enum values in the index are camelCase (`unifiedExecStartup`); convert them to snake_case for
  rollouts. The model-visible `response_item`s must carry **no `id`**: the model API rejects
  user-supplied item ids.
- **Codex's own importer** (`externalAgentConfig/import`) creates legacy, text-only threads. Its
  append mode refuses any thread with native turns.
- **Compression.** Rollouts idle for 7 days are compressed to `.jsonl.zst`. Wait, or decompress,
  before appending.
- **Managed developer instructions** reach a chat once, when it starts. Policy changes do not reach
  chats that are already running.

### Claude side

- **Appending.** Write `tool_use` and its `tool_result` as strictly alternating user and assistant
  records, with `sourceToolAssistantUUID` on results and deterministic uuids. Add a lead note saying
  the turns happened in Codex; without it Claude doubts tool calls it did not make.
- **The `model` field is mandatory.** Every assistant record needs `model`, or `--resume` hangs.
  Use the last real model in the session, or `<synthetic>` before Claude has run.
- **Never forge `thinking` blocks**: signatures are checked.
- **Tool names.** Claude's API accepts tool names it does not have in history. Name Codex tools
  `mcp__codex__*`, which Claude renders as tool rows. Plain shell commands map to `Bash`. Hide
  Claude's own `ToolSearch` calls from Codex.
- **Write only while the copy is not running**, then relaunch it with
  `claude --resume <id> --remote-control "<title>"`.
- **Large transcripts.** Over 5,242,880 bytes, Claude loads only what follows the last
  `compact_boundary`. Claude shows long sessions from their last compaction onward, so older
  history stays in the file but leaves the view.
- **Removed tools.** Current Claude Code has no Grep, Glob or TodoWrite tools. They only appear in
  old transcripts, but keep their mappings.
- **Codex app noise.** Filter `<external_codex_apps_open_page>…` user events: the Codex app injects
  them, the user did not type them.

### Sync engine

- **Cursors.**
  - Claude: the last synced record uuid.
  - Codex: a byte offset anchored to the id of the last finished turn. It is found again after Codex
    rewrites the file, for example during migration, so a restart never replays old turns.
- **Deterministic ids** (uuid5) in both directions, so a retry can recognise what it already wrote.
  Before writing, each side checks whether its ids are already there (Codex: the turn ids in the
  history index; Claude: the uuid of the first record to write) and writes nothing if they are. That
  covers a crash after a complete write. A write cut off partway needs steal-list items 2 and 3: the
  ids recorded before writing, read back, and an accepted cursor that moves only after verification.
- **Quiet and open-turn checks.** Sync only finished turns. If the user typed on both sides, stop and
  report a conflict.
- **Output caps.** The model sees 4,000 characters per tool output, the app shows 20,000. The rest
  goes to a pointer file.
- **History rebuilds.**
  - Merge every source, native first: original native threads, older transcripts, rescued index rows.
  - Dedupe by turn id. Then, between different sources (copies of the same conversation), by the
    opening message; never within one source, where a repeat is a real new turn ("status?" asked
    twice). Text alone can still merge two genuine turns from different sources, so the rebuild's plan
    step reports how many turns each source adds before anything is applied.
  - Skip copies that fall within the original's own time period, and drop app noise.
  - Give the model the newest ~400k characters. Older turns are shown in the app only, plus a pointer
    file the model can read on demand.
- **Text-to-native parsing.** Earlier converters (Codex's importer's `[external_agent_tool_call: …]`
  and our own `[ran …] / [result — …]`) can be turned back into native items for display.

## Steal list (planned, from studying prior art)

Ranked, each with its source:

1. **Rewrite detection** (Maxaccurate/claude-codex-sync). Store `sha256(file[:offset])` next to every
   cursor. On mismatch, block the pair and never auto-resync it.
2. **Write-ahead intent plus acceptance cursor** (tandem, Happier, ccx). Record the uuids about to be
   written before writing. Keep separate `written` and `accepted` cursors; `accepted` moves only after
   read-back. On restart, write only the missing ids.
3. **Persisted verification** (Maxaccurate). A pair with an unverified write gets no new writes, and
   that holds across restarts.
4. **Hold the Codex writer lock for the whole append** (Maxaccurate).
5. **Claude busy/idle** from `~/.claude/sessions/<pid>.json` `status` (tandem, Chamotrans), with quiet
   time only as a fallback. Long tool calls write nothing for minutes.
6. **Honest native tool mapping** (tandem).
   - Single-file Codex `apply_patch` becomes `Edit`/`Write` with a `structuredPatch`, so it renders as
     a diff.
   - Claude `Read`/`Grep`/`Glob` become `cat -n`/`rg` equivalents.
   - `TodoWrite` maps to `update_plan`.
   - Fall back verbatim when a mapping fails.
7. **Pair tool calls by id** whatever the file order. Close dangling calls with "(tool result not
   recorded)" (tandem).
8. **Pre-flight structure check** before `thread/resume` and `claude --resume`. Every parentUuid must
   resolve, there must be no unanswered `tool_use`, roles must alternate, and ordinals must be
   contiguous (tandem).
9. **Version and format gate.** Pin the hash of Codex's v2 `ThreadItem` schema
   (`codex app-server generate-json-schema`) and keep a version floor and ceiling. On drift, stop
   writing and alert. Fail closed only on unknown record types that carry a message (tandem, ORG2).
10. **Quarantine** for untranslatable entries: at most one placeholder per turn, listed by `doctor`
    (tandem).
11. **Native provenance markers instead of text tags.** Codex `turn/start.clientUserMessageId` comes
    back as `clientId` (ORG2). On Claude, use a top-level mirror field (ccx).
12. **Replay framing that cannot impersonate.** "Recording of past messages, not a live turn". Escape
    container tags, and state exact inlined ranges in pointers (Happier).
13. **Budget refinements** (Happier, ccx). Pin the last user ask and keep a digest of earlier asks.
    Add an "interrupted mid-step, do not restart finished work" note. Warn about cost when the prompt
    cache has expired on a large idle side.
14. **Canary after upgrades** (tandem live gate, ORG2 hop tests). Relay secret words both ways and
    assert each turn appears exactly once.
15. **Packaging lessons** (ccx). `doctor`/`setup`/`update`/`repair`/`uninstall`, a "paste this into
    your agent to install" prompt, and a CI check that the tag matches the version.

Avoid writing `state_5.sqlite` directly, writing fake tool calls the model would read as real ones,
whole-session last-write-wins, and mapping unknown tools to Bash.

## Prior art (2026-11-03)

| Project | What it does | What it lacks |
|---|---|---|
| tandem (Bhavya6187) | Most mature. Native tool mapping both ways, write-ahead cursor. | CLI only, one harness at a time; tool activity never appears in the Codex UI. |
| Chamotrans/codex-claude-session-sync, Maxaccurate/claude-codex-sync | Two-way append into existing sessions. | Skip or wait on open sessions; Codex tool calls flattened to text. |
| ccx (aaronwong1999) | Pairs a Claude session with a Codex thread. | The other side arrives only as hook-injected text. |
| Happier "Continue with another Agent" | Switches agents in one session. | Only in its own app; text briefs. |
| opal-bridge | Native blocks in twin files. | Re-renders whole files; not one live thread. |
| ORG2 | Hub design with prefix-append. | — |
| OpenAI `/codex:transfer` and Codex's importer | Official, Claude → Codex. | One way, text, new threads. |

None does one live conversation in both official apps with native blocks on both sides.

## Risks

- **OpenAI considers edits to rollouts from outside the harness unsupported**
  (openai/codex#49328). A Codex release can break the append path. Mitigation: the version and
  schema gate, the canary, and failing closed.
- **Neither vendor documents its transcript format.** Claude 2.1.316 added uuid-less `api-request*`
  records. Translate only `user`/`assistant` records.

## Testing plan

- **Unit tests.** Translators both ways against golden fixtures recorded from real CLIs and
  sanitized: tool mapping, caps, deterministic ids, cursor anchoring after a rewrite, the text-tool
  parser, and the history merge rules.
- **Integration tests.**
  - Hermetic homes (`CODEX_HOME`, `CLAUDE_CONFIG_DIR`).
  - A scripted fake app-server for the append, resume and verify flow, including
    "thread already has an active writer".
  - Mock Responses and Messages servers to assert what each model actually saw.
- **End-to-end tests** (manual or canary). Real CLIs: a real Claude session with every tool type
  goes to Codex; a real Codex turn with command, patch and MCP calls goes to Claude. Each side's model
  must answer from the other's turns, retries must add nothing, and a thread open in the app must be
  handled.

## Open questions

- Should Codex's model context get real `function_call` pairs (tandem shows they are accepted)
  instead of prose notes?
- Should big Claude-side rebuilds use a native `compact_boundary` and summary?
- Pairing UX: link from inside a conversation (`duologue link` run by the agent), from the CLI, or
  automatically?
- Which default budgets fit a single-user setup?
