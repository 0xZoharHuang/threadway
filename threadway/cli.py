"""Threadway command line."""

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys

from . import native as n
from .store import Store, Conflict


def emit(value, machine=False):
    if machine:
        print(json.dumps(value, ensure_ascii=False))
    elif isinstance(value, list):
        for x in value:
            emit(x)
    elif isinstance(value, dict):
        print("   ".join(f"{k}: {v}" for k, v in value.items()))
    else:
        print(value)


def scoped(t, paths):
    if Path(t["cwd"]) not in paths:
        n.fail("Task belongs to a different worktree; select its project")


def parser():
    p = argparse.ArgumentParser(
        prog="threadway",
        description="Continue local coding tasks across official agent CLIs.",
    )
    p.add_argument(
        "--project",
        default=".",
        help="Git worktree or project directory (default: current)",
    )
    p.add_argument(
        "--all-worktrees",
        action="store_true",
        help="Include other checked-out worktrees of this repository",
    )
    p.add_argument("--json", action="store_true")
    sp = p.add_subparsers(dest="cmd")

    def sub(name):
        q = sp.add_parser(name)
        for option, kwargs in [
            ("--project", {}),
            ("--json", {"action": "store_true"}),
            ("--all-worktrees", {"action": "store_true"}),
        ]:
            q.add_argument(option, default=argparse.SUPPRESS, **kwargs)
        return q

    sub("scan")
    sub("list")
    q = sub("status")
    q.add_argument("task")
    q = sub("switch")
    q.add_argument("task")
    q.add_argument("--to", required=True, choices=n.AGENTS)
    q.add_argument("--from", dest="source", choices=n.AGENTS)
    q.add_argument("--branch", action="store_true")
    q.add_argument("--dry-run", action="store_true")
    q.add_argument("--no-launch", action="store_true")
    q.add_argument(
        "--tools", action="store_true", help="Require typed tool pairs on every handoff"
    )
    q = sub("batch")
    q.add_argument("--to", choices=n.AGENTS, required=True)
    g = q.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--apply", action="store_true")
    q = sub("ui")
    q.add_argument("--no-browser", action="store_true")
    sub("doctor")
    for verb in ("undo", "cancel"):
        q = sub(verb)
        q.add_argument("task")
    q = sub("stage-google")
    q.add_argument("task")
    q.add_argument("--out", required=True, type=Path)
    return p


def run(argv=None):
    a = parser().parse_args(argv)
    os.umask(0o077)
    cwd = Path(a.project).expanduser().resolve(strict=True)
    if not cwd.is_dir():
        n.fail("Project must be a directory")
    paths = n.list_worktrees(cwd, a.all_worktrees)
    st = Store()
    if a.cmd == "doctor":
        for agent in n.AGENTS:
            try:
                version = n.version(agent)
                emit(
                    {"agent": agent, "version": n.PIN[agent], "executable": version},
                    a.json,
                )
            except RuntimeError as e:
                emit({"agent": agent, "error": str(e)}, a.json)
        return 0
    if a.cmd in (None, "scan", "batch", "ui"):
        report = st.discover(paths)
        if a.cmd == "scan":
            emit(report, a.json)
            return 0
    tasks = st.tasks(paths)
    if a.cmd == "list":
        emit(
            [
                {k: t[k] for k in ("id", "title", "active", "agents", "cwd", "parent")}
                for t in tasks
            ],
            a.json,
        )
        return 0
    if a.cmd == "ui":
        from .ui import serve

        serve(st, paths, cwd, not a.no_browser)
        return 0
    if a.cmd == "batch":
        result = []
        for t in tasks:
            try:
                result.append(st.switch(t["id"], a.to, dry=a.dry_run))
            except (ValueError, RuntimeError, OSError, sqlite3.Error) as e:
                result.append({"task": t["id"], "action": "blocked", "reason": str(e)})
        emit(result, a.json)
        return int(
            any(r["action"] in ("blocked", "diverged", "pending") for r in result)
        )
    if not a.cmd:
        if not sys.stdin.isatty():
            n.fail("Use scan/list/switch in scripts")
        if not tasks:
            print("No eligible local sessions in this project.")
            return 0
        for i, t in enumerate(tasks, 1):
            targets = st.refs(t)
            try:
                peer = "claude" if t["active"] == "codex" else "codex"
                hint = (
                    st.switch(t["id"], peer, dry=True)["action"]
                    if len(targets) > 1
                    else "new handoff"
                )
            except (ValueError, RuntimeError, OSError):
                hint = "needs inspection"
            print(f"{i:3}  {t['active']:7} {t['title']}  [{t['id'][:8]} · {hint}]")
        choice = int(input("Task number: "))
        if not 1 <= choice <= len(tasks):
            n.fail("Invalid task")
        a.task = tasks[choice - 1]["id"]
        a.to = input("Continue in codex or claude: ").strip()
        if a.to not in n.AGENTS:
            n.fail("Unknown CLI")
        a.cmd = "switch"
        a.source = None
        a.dry_run = False
        a.no_launch = False
        a.branch = False
        a.tools = False
        decision = st.switch(a.task, a.to, dry=True)
        if decision["action"] == "diverged":
            print("Both native branches contain independent edits.")
            for name, info in decision["branches"].items():
                print(name, info["delta_messages"], "messages beyond checkpoint")
            keep = input(
                f"Follow which history in {a.to}? [codex/claude, Enter cancels] "
            ).strip()
            if not keep:
                return 0
            if keep not in n.AGENTS:
                n.fail("Invalid source branch")
            a.source = keep
            a.branch = keep != a.to
    t = st.task(a.task)
    scoped(t, paths)
    if a.cmd in ("undo", "cancel"):
        emit(getattr(st, a.cmd)(t["id"]), a.json)
        return 0
    if a.cmd == "status":
        refs = st.refs(t)
        result = {k: t[k] for k in ("id", "cwd", "title", "parent", "active")}
        result["sessions"] = {k: r["sid"] for k, r in refs.items()}
        result["next"] = {}
        for target in n.AGENTS:
            try:
                result["next"][target] = st.switch(t["id"], target, dry=True)
            except (ValueError, RuntimeError, OSError) as e:
                result["next"][target] = str(e)
        emit(result, a.json)
        return 0
    if a.cmd == "stage-google":
        ref = st.refs(t)[t["active"]]
        messages, _, _, tools = st.inspect(ref, t["tool_mode"])
        import uuid

        out = a.out.expanduser().resolve()
        real = (Path.home() / ".gemini").resolve()
        if real == out or real in out.parents:
            n.fail("Use a staging directory outside the live Google store")
        out.mkdir(parents=True, exist_ok=False)
        n.codec(
            "stage-agy",
            n.packed({"id": str(uuid.uuid4()), "cwd": t["cwd"], "messages": messages}),
            out,
        )
        emit(
            {
                "action": "staged-only",
                "out": str(out),
                "messages": len(messages),
                "tool_records": tools,
            },
            a.json,
        )
        return 0
    info = st.switch(
        t["id"], a.to, a.source, dry=a.dry_run, branch=a.branch, tools=a.tools
    )
    emit(info, a.json)
    sys.stdout.flush()
    return 0 if a.dry_run or a.no_launch else st.launch(info)


def main():
    try:
        return run()
    except KeyboardInterrupt:
        return 130
    except (OSError, RuntimeError, ValueError, KeyError, sqlite3.Error) as e:
        print(str(e), file=sys.stderr)
        return 3 if isinstance(e, Conflict) else 2
