# Qualification

Release targets: macOS arm64/x86-64 and Linux arm64/x86-64. See the CI run for the published commit. Pinned at the time of the test (2026-11-23): OpenAI Codex CLI **0.171.0**, Anthropic Claude Code **2.1.330**, Google Antigravity CLI **1.5.15**, txcript **0.14.4**. These are independent release lines; the operator's pre-existing global Codex/Claude executables were not upgraded.

| Destination | Produced native records | Seen in official registry/picker | Official resume and model-visible next turn |
| --- | --- | --- | --- |
| Codex CLI | Yes | `thread/list` and `thread/turns/list` | `codex exec resume` using the same managed ID; role and text checked in the loopback Responses request |
| Claude Code | Yes | Interactive official `claude --resume` picker in a PTY | `claude -p --resume ID`; role and text checked in the loopback Messages request |
| Antigravity CLI | Native SQLite trajectory + brain directory; typed text suffixes in a transaction | Official ACP `session/list` and `session/load` with replayed history | Official `agy --conversation` sent the expected user/model roles to a loopback Gemini-compatible endpoint and recorded the new reply under the same ID |
| Antigravity App | No application adapter | Unverified | Unverified |

The test generates real native turns with the vendor CLIs using synthetic credentials and a loopback model stub. It creates isolated homes, worktrees, metadata and transcripts. Both Codex-originated and Claude-originated tasks were continued in both directions. It does not use a paid model response or a real conversation. The Claude interactive picker is checked separately from noninteractive resume because SDK/print-created sessions may be filtered from the default picker.

## Native data flow

1. Discover Codex metadata via the official exact-worktree `thread/list`, state-index-only, with pagination. Claude discovery reads the project bucket, validates each matching working directory and native ID and excludes sidechains. Body parsing is deferred until selecting a session.
2. The Rust codec uses txcript to convert into and out of its common transcript representation. Check semantic role/content order and repetition after conversion. If the raw Codex rollout is unavailable but the active store exposes paginated history, request turns/items from the runtime.
3. First official Codex registration uses `externalAgentConfig/import` with a single `SESSIONS` item. A temporary, individually identified text projection is placed where the official importer validates Claude input. The exact staging file is removed afterward. Import completion, native ID, working directory, visible turns and native replay are checked.
4. The official importer cannot generally extend a transcript once Codex has added its own turn context. For subsequent fast-forwards, accept only a Threadway-originated, registered **legacy-format** managed target on the pinned release. Acquire the upstream coordination and exclusive thread-writer locks, verify native turns are exactly the previous text, back up the file, append completed import-style turns via atomic publication, then read back through the official turn API. No SQL writes or raw model-context injection. A changed format, live writer or unprovable prefix blocks the operation. See the pinned [writer lock](https://github.com/openai/codex/blob/rust-v0.171.0/codex-rs/rollout/src/writer_lock.rs) and [official import append conditions](https://github.com/openai/codex/blob/rust-v0.171.0/codex-rs/external-agent-migration/src/sessions/append.rs).
5. Claude native appends keep all original bytes, link new messages to the verified existing leaf, and validate the resulting transcript. Summary, origin and noninteractive entrypoint handling matter for picker visibility. The pinned file formats are private implementation details; requalification is required for another version.

## Reproduce

```sh
./build.sh
./setup-clients.sh
PYTHONPATH=. python3 -B -m unittest discover -s tests -p 'test_*.py'
python3 -B tests/native_e2e.py \
  --codex "$PWD/bin/clients/node_modules/.bin/codex" \
  --claude "$PWD/bin/clients/node_modules/.bin/claude" \
  --codec "$PWD/bin/threadway-codec"
```

A macOS login session is needed for the native picker/terminal acceptance. Use `--agy /absolute/path/to/antigravity` for the full three-agent acceptance suite. Set `THREADWAY_BROWSER_TEST=1` with Playwright and Chromium installed to exercise search, mobile overflow, branch previews and HTTP Host rejection. `THREADWAY_TEST_CHROMIUM` can select an installed Chromium executable.

The native suite checks all three agents over repeated edits, same-ID reused targets, 20 unchanged switches without extra sessions, exclusive access against an actual running Codex writer, an unchanged read-only original, divergent real turns, forks in both directions, idempotent branch selection and batch partial success across Git worktrees. Unit tests additionally cover interrupted registration, short writes, recovery with a newer source, cold-target checks, guarded undo, repeated equal text, malformed records and tool pairing. A local fixture with stubbed endpoints is not a cross-platform or all-accounts acceptance finding.

## Google native acceptance

The isolated home sets Google's documented `modelProvider=gemini`, a dummy fixture key, and `GOOGLE_GEMINI_BASE_URL` pointing at the loopback server. The harness verifies an exact role/marker sequence in the Gemini model request; it checks the real Google process's response and the next client's history. The route includes Codex → Google → Claude → Google → Codex → Google, plus a separate Google-originated task and independently edited Google/Claude branches. ACP is used only by tests to verify the official listing and replay. No Google credentials or live cloud model are required for this evidence. [Official authentication options](https://antigravity.google/docs/install/)

Google writes are limited to threadway-owned, verified 1.5.15 trajectories and complete text-step suffixes. Cold, append-only SQLite updates validate the source workspace, target ID, schema, preimage and individual step indices. Original native step and auxiliary tables stay intact. Native logs, provider context, external authorizations and attachments are not reconstructed as executable work. Standalone concurrent Antigravity CLI processes must be closed before an incremental write. Desktop App storage, live remote control and other CLI releases are unqualified.

## GUI

The optional browser suite checks mobile layout, search, divergent-branch preview, explicit branch preparation and origin/host rejection. Registering a handoff is a POST requiring a one-plan token and a current content checkpoint. The web server has no generic command endpoint. Local-only reviews without declaring an interactive terminal return a resume command.
