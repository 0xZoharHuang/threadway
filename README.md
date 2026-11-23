# Threadway

Continue the same local coding task in the official Codex or Claude Code CLI. Threadway tracks the last handoff, checks which side has new messages, and preserves divergent histories. It runs locally on macOS. [中文使用说明](docs/zh-CN.md) · [Compatibility and verification](docs/verification.md)

## Run

The macOS arm64 archive includes the Rust codec. From a source checkout, build it with `./build.sh` (Rust 1.96.0). Run `./setup-clients.sh` to install the two pinned official clients in this checkout, or provide their paths using `THREADWAY_CODEX_BIN` and `THREADWAY_CLAUDE_BIN`. The installer needs npm. Your existing global clients retain their versions. Threadway uses Python 3.9+ and the pinned Apache-2.0 [txcript](https://github.com/skillsynchq/txcript/tree/v0.14.4) library, compiled into its small codec executable.

```sh
export PATH="$PWD/bin:$PATH"    # from the unpacked Threadway directory
cd /path/to/your/actual/worktree
threadway                     # index this project and choose a task interactively
threadway ui                  # optional loopback dashboard; Ctrl-C closes it
```

The first **cross-agent** switch creates a native destination session. The original session is read-only to the synchronizer. When changes need to travel back to it, Threadway creates one managed counterpart in the original harness. Subsequent compatible handoffs reuse those IDs. A user who opens an original session directly can still edit it. A later scan links new work on a retired original as a separate task.

Default scope is the exact worktree. Pass `--all-worktrees` to discover other checked-out worktrees of the same Git repository. The target CLI opens in the source worktree. Code, uncommitted changes and test artifacts are used there in place; the conversation transfer includes no working-tree copy.

## Commands

```sh
threadway scan --project /path/to/worktree
threadway list --json
threadway status TASK
threadway switch TASK --to claude --dry-run
threadway switch TASK --to claude              # launch the native CLI
threadway switch TASK --to codex --no-launch   # register and verify only
threadway batch --to claude --dry-run
threadway batch --to claude --apply            # register, never launch a batch of terminals
```

Task IDs may use an unambiguous prefix. Preview and status show create, reuse, target-ahead, append, first managed copy or divergence. `scan` writes local metadata; a batch preview refreshes that index but does not write any native sessions. One failed handoff does not abort unrelated batch tasks. JSON batch output includes the result for every selected task.

If both sides changed, normal switching stops before writing. Inspect the two heads in the dashboard, continue an existing branch in its own harness, or explicitly create a cross-harness continuation:

```sh
threadway switch TASK --from claude --to claude
threadway switch TASK --from claude --to codex --branch
```

The branch is tied to its source head and target. Repeating the same choice reopens the same continuation. No automatic synthesis orders independent turns as if either agent had seen the other's changes.

## Scope

- By default the transferred conversation contains visible user and assistant text. The report counts encountered tool records. A first switch can request `--tools`, preserving representable, paired tool blocks where the native path can be verified. Codex's session import currently flattens tool records, so a strict typed-tool handoff into Codex stops. The tool setting stays fixed after the first handoff.
- Some native rows are harness bookkeeping, hidden context or reasoning. They do not become instructions in the new session by design. An unknown attachment, ambiguous parent chain, observed compaction/rollback or unsupported history shape can block a handoff. No suppressed failure is turned into a Markdown success.
- Native files, local prompts and eligible tool outputs can contain sensitive data. After native resume, the selected client uses its own account, model, tools and permissions. Check your destination before sending a new turn. No pending command is executed during conversion.
- Current qualification is for local, completed, linear sessions and the pinned client/codec versions in the verification matrix. `doctor` checks executable versions. A missing original worktree or an unsupported Claude long-path encoding produces a diagnostic.

## Storage and recovery

Threadway keeps its SQLite index, operation journal, leases and private backups under `~/.threadway` (or `THREADWAY_HOME`). The index contains titles, paths, native IDs and signed-off message fingerprints. Recovery files may contain actual conversation content. The state directory is private to the current OS user.

A native write is staged and journaled, then verified before advancing the common checkpoint. Rerun the same switch after interruption. An already-published result is adopted once. A byte-proven short Claude append can be finished; if the destination changed unexpectedly, the tool retains the evidence and stops. Use `threadway cancel TASK` for an intent whose target is still unchanged. `threadway undo TASK` restores the latest reversible managed append when its destination still has the exact expected bytes. A later native edit prevents undo. First imports and original chats are not automatically deleted by undo.

A local process lock coordinates Threadway instances; per-session leases cover official CLI processes launched by Threadway. For managed Codex legacy imports the adapter also holds Codex's native writer lock. The Claude adapter checks registered running sessions and open file descriptors, fingerprints the destination, appends, and verifies the result. Claude clients started outside Threadway do not participate in its lease. Close an independently launched target before writing to it.

The optional dashboard binds only to loopback, uses an unguessable URL, rejects other Host headers and serves read-only handoff previews. It loads message excerpts only when you request branch details and only supplies terminal commands to copy. It has no external assets, login, telemetry or background server.

To uninstall, remove the distribution and its PATH entry. Native conversations remain in their own clients, and `~/.threadway` continues to hold your index/backups. Back that directory up before removing it if you need recovery. Never include it or native transcripts in a public bug report.

Apache-2.0. Independent project. The official clients retain their respective licenses and are acquired separately.
