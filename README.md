# Threadway

Pick up a local coding task in **Codex, Claude Code or Google Antigravity CLI**. Threadway registers a native session, records where the last handoff ended and reuses the same destination on the next switch. Independently edited histories are shown as branches. No MCP server, cloud account or background synchronizer is needed for Threadway.

[中文指南](docs/zh-CN.md) · [Verification and supported scope](docs/verification.md) · [Releases](https://github.com/0xZoharHuang/threadway/releases)

## Start

Download the archive for your host, unpack it and run:

```sh
./setup-clients.sh        # Codex + Claude Code, scoped to this directory; needs npm
./setup-google.sh         # Google Antigravity CLI, checked against a pinned SHA-256
export PATH="$PWD/bin:$PATH"
cd /path/to/your/worktree
threadway doctor
threadway ui             # lightweight local GUI; Ctrl-C stops it
```

From source, build the codec first with `./build.sh` (Rust 1.96). Threadway uses Python 3.9+, Git, a POSIX local filesystem with file locking, and `lsof` for cold Claude appends. The release matrix covers macOS and Linux on arm64 and x86-64. Windows-native, the Antigravity desktop App and remote agent backends are outside the current matrix. WSL and other architectures need their own native acceptance runs.

Alternatively, point `THREADWAY_CODEX_BIN`, `THREADWAY_CLAUDE_BIN`, and `THREADWAY_AGY_BIN` at the pinned official executables. Your logins, API keys, sandbox, external connectors and billing remain under each native client's control. Threadway never runs old tool calls.

## CLI or browser

Run `threadway` for the interactive terminal picker, or use the scriptable commands:

```sh
threadway scan --project .
threadway list --json
threadway switch TASK --to agy --dry-run
threadway switch TASK --to agy                   # opens the official CLI in the original worktree
threadway switch TASK --from agy --to claude --no-launch
threadway batch --to codex --dry-run
threadway batch --to codex --apply              # registers sessions, does not open terminals
```

The GUI is a loopback-only, on-demand interface to the same engine. Select a task, review the source, destination and action, then prepare the handoff. With an interactive terminal you can also choose **Continue in terminal**. Otherwise, copy the verified resume command. Requests require a private URL, same-origin JSON and the specific preview token. The state and content hashes are checked again under lock. A stale preview must be refreshed. No arbitrary shell command is accepted from a browser.

The default project scope is the exact worktree; `--all-worktrees` also discovers other checked-out worktrees in that repository. Discovering a session reads local metadata. Selecting it can read conversation content. Code, uncommitted changes and test outputs stay in the worktree. Each identity remains responsible for permission to use that work when the target model receives your next turn.

## What gets created?

The original is read-only to Threadway. A first handoff creates a native counterpart. The first return that needs to extend the original creates a managed copy in that harness. Each additional harness has its own binding. Fast-forwards reuse the binding. If an original is edited later, a subsequent scan can expose that work separately.

Checkpoints are tracked per participant. A lagging third client can catch up without replaying the same messages twice. If both sides edited independently, choose a branch explicitly:

```sh
threadway status TASK
threadway switch TASK --from agy --to agy           # open the existing Google history
threadway switch TASK --from agy --to claude --branch
```

The second command that chooses the **same source head and target** reuses its previous branch. Where the automatic source is ambiguous, select one with `--from`. Text in different histories never becomes a chronological merge just because the timestamps or wording coincide.

## Long sessions, compaction and subagents

In the local UI, choose **History → Checkpoint with searchable history**, then **Prepare handoff**. From the terminal:

```sh
threadway checkpoint TASK --from codex --to claude
threadway history CHECKPOINT                       # list main and worker IDs
threadway history CHECKPOINT --find "test failure"
threadway history CHECKPOINT --node WORKER --after 20 --limit 10
threadway history CHECKPOINT --record 42 --offset 8192
threadway switch CHECKPOINT --to agy
```

The checkpoint is its own continuation task. Its first native message quotes the latest available parent summary, up to eight recent parent text records and excerpts from up to three workers. Earlier records stay in a private, searchable local archive. Each excerpt is bounded to 1,600 JSON-encoded bytes. The record ID opens the full text in pages. The next model request uses the target client's normal permissions and context window.

Codex child records are linked by the native project registry, including nested descendants. Claude's separate worker files are grouped under their owning session. Google can identify a nested trajectory without exposing a verified child ID; the archive marks that reference as unresolved. Compaction boundaries with no readable summary stay marked. A checkpoint captures persisted visible text. It cannot reconstruct data the original client has discarded, reattach a live worker, or resume an in-flight tool.

Repeat the checkpoint with the same source content to reuse its task and native ID. Continue using `switch CHECKPOINT` for subsequent work. Capturing an independently changed original creates a sibling checkpoint and preserves the existing continuation. Identical record bodies across checkpoints share one local copy. The ordinary text handoff remains available for linear sessions. [Adapter details and test limits](docs/verification.md)

## Can Grok Build already resume my session?

Yes. If you want to continue working in Grok Build, its built-in `/resume-codex` and `/resume-claude` skills can pick up recent sessions. Threadway is for a different workflow. For example, you can start in Codex, continue in the official Claude Code CLI, and take the new turns back to Codex. Later switches reuse the managed sessions. If both clients have new turns, you can open either branch. [Grok Build migration guide](https://github.com/xai-org/grok-build/blob/2bdd1d6a6369de0e8c68132ea4539e9abd9e14a8/crates/codegen/xai-grok-pager/docs/tutorial/01-coming-from-another-tool.md)

## Content, recovery and limits

By default, a handoff carries visible user and assistant text. To keep completed tool calls as structured history, choose `--tools` on the first handoff. The verified cross-agent mapping is a Codex `exec_command({"cmd":"…"})` and its plain-text result into a Claude Code `Bash({"command":"…"})` history entry. Threadway checks the original namespace, exact arguments, roles, ID and result before publishing. A Codex function named `Bash`, an MCP tool, an extra option such as `workdir`, or an unpaired result blocks the strict handoff. Historical commands are never executed by the converter. Codex and Google destinations currently support text handoffs only.

Compressed, rolled-back, nonlinear, oversized, active or unknown native records can require user action. Attachments, hidden provider context and in-flight commands are outside the text guarantee. The supported versions and known boundaries are documented in the [verification guide](docs/verification.md).

`~/.threadway` (or `THREADWAY_HOME`) is a private local index with provenance, transaction records and recovery points. Recovery files may contain prompt text. Rerun the same switch after interruption. Use `cancel TASK` only for a provably uncommitted intent. `undo TASK` can undo the last managed append while its verified postimage still matches. Google uses an SQLite transaction and a scoped inverse receipt for the inserted steps. Each client retains its own native history.

To uninstall, remove the distribution and its PATH entry. Native conversations and your private Threadway index remain. Back up the index before removing it if you want undo information. Do not upload transcripts, native databases, access tokens or `~/.threadway` in bug reports.

Apache License 2.0. The codec reuses [txcript](https://github.com/skillsynchq/txcript/tree/v0.14.4). Official clients are acquired separately under their vendors' licenses. Threadway is an independent project.
