"""Local session graph. Native state is never the journal for another harness."""

import contextlib
import fcntl
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import uuid

from . import native as n


class Conflict(RuntimeError):
    pass


def new_id():
    return str(uuid.uuid4())


def ancestor(prefix, head):
    return head[: len(prefix)] == prefix


def outcome(checkpoint, source, target):
    if not ancestor(checkpoint, source) or not ancestor(checkpoint, target):
        return "diverged"
    if source == target:
        return "reuse" if source == checkpoint else "diverged"
    if source == checkpoint:
        return "reuse-ahead"
    if target == checkpoint:
        return "append"
    return "diverged"


def title_value(s):
    return "".join(c if c.isprintable() else " " for c in s)[:100]


class Store:
    def __init__(self, root=None):
        self.root = (
            Path(root or os.getenv("THREADWAY_HOME", Path.home() / ".threadway"))
            .expanduser()
            .absolute()
        )
        # macOS legitimately aliases /var and /tmp. Validate the private
        # directory itself under the canonical parent.
        self.root = self.root.parent.resolve() / self.root.name
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if (
            self.root.resolve() != self.root
            or self.root.stat().st_uid != os.getuid()
            or self.root.stat().st_mode & 0o077
        ):
            n.fail("Threadway state directory must be private (0700) and user-owned")
        self.db = sqlite3.connect(
            self.root / "index.sqlite", timeout=10, isolation_level=None
        )
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS tasks(
          id TEXT PRIMARY KEY,cwd TEXT NOT NULL,title TEXT NOT NULL, parent TEXT,active TEXT NOT NULL,
          base TEXT NOT NULL DEFAULT '[]', tool_mode INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS members(task TEXT REFERENCES tasks(id),agent TEXT,sid TEXT,path TEXT,
          owned INTEGER NOT NULL, PRIMARY KEY(task,agent));
        CREATE INDEX IF NOT EXISTS members_by_sid ON members(agent,sid);
        CREATE TABLE IF NOT EXISTS origins(agent TEXT,sid TEXT,task TEXT REFERENCES tasks(id),checkpoint TEXT,
          PRIMARY KEY(agent,sid));
        CREATE TABLE IF NOT EXISTS intents(task TEXT PRIMARY KEY REFERENCES tasks(id),data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS recovery(task TEXT PRIMARY KEY REFERENCES tasks(id), op TEXT NOT NULL,
            native_id TEXT NOT NULL, agent TEXT NOT NULL, after_hash TEXT NOT NULL, before_hash TEXT NOT NULL,
            old_base TEXT NOT NULL, old_active TEXT NOT NULL);
        """)

    @contextlib.contextmanager
    def lock(self):
        with (self.root / "lock").open("a") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("Another Threadway operation is running") from None
            yield

    @contextlib.contextmanager
    def transaction(self):
        self.db.execute("begin immediate")
        try:
            yield
            self.db.execute("commit")
        except BaseException:
            self.db.execute("rollback")
            raise

    def rows(self, q, *args):
        return [dict(r) for r in self.db.execute(q, args)]

    def tasks(self, paths):
        if not paths:
            return []
        return self.rows(
            "SELECT t.*,group_concat(m.agent) AS agents FROM tasks t JOIN members m ON m.task=t.id WHERE t.cwd IN ("
            + ",".join("?" for _ in paths)
            + ") GROUP BY t.id ORDER BY t.rowid DESC",
            *map(str, paths),
        )

    def adopt(self, ref, parent=None, task=None):
        tid = task or new_id()
        self.db.execute(
            "insert into tasks(id,cwd,title,parent,active) values(?,?,?,?,?)",
            (
                tid,
                ref["cwd"],
                title_value(ref.get("title") or ref["sid"]),
                parent,
                ref["agent"],
            ),
        )
        self.bind(tid, ref, False)
        return tid

    def bind(self, task, r, owned):
        self.db.execute(
            "insert or replace into members(task,agent,sid,path,owned) values(?,?,?,?,?)",
            (task, r["agent"], r["sid"], r["path"], int(owned)),
        )

    def discover(self, paths):
        counts = {"new": 0, "known": 0}
        with self.lock(), self.transaction():
            unfinished = [
                json.loads(r["data"]) for r in self.rows("select data from intents")
            ]
            reserved = {x.get("sid") for x in unfinished} | {
                x["op"] for x in unfinished
            }
            reserved.update(
                i
                for x in unfinished
                if x["to"] == "codex"
                for i in [
                    n.imported_sid(
                        n.claude_root()
                        / "projects"
                        / n.slug(x["source"]["cwd"])
                        / (x["op"] + ".jsonl")
                    )
                ]
                if i
            )
            for p in paths:
                for ref in n.codex_catalog(p) + n.claude_catalog(p):
                    if ref["sid"] in reserved:
                        continue
                    known = self.rows(
                        "select task from members where agent=? and sid=?",
                        ref["agent"],
                        ref["sid"],
                    )
                    if known:
                        counts["known"] += 1
                        continue
                    orig = self.rows(
                        "select task,checkpoint from origins where agent=? and sid=?",
                        ref["agent"],
                        ref["sid"],
                    )
                    if orig:
                        # A retired original remains discoverable if edited outside Threadway.
                        if orig[0]["checkpoint"] == self.stamp(ref):
                            continue
                        self.db.execute(
                            "delete from origins where agent=? and sid=?",
                            (ref["agent"], ref["sid"]),
                        )
                    task = self.adopt(ref, orig[0]["task"] if orig else None)
                    self.db.execute(
                        "insert into origins values(?,?,?,NULL)",
                        (ref["agent"], ref["sid"], task),
                    )
                    counts["new"] += 1
        return counts

    def task(self, ident):
        rows = self.rows(
            "select * from tasks where substr(id,1,?)=?", len(ident), ident
        )
        if len(rows) != 1:
            n.fail("No unique task; scan and select its displayed ID")
        return rows[0]

    def refs(self, t):
        return {
            m["agent"]: dict(m, cwd=t["cwd"])
            for m in self.rows("select * from members where task=?", t["id"])
        }

    def active_lease(self, ref):
        f = self.root / "leases" / ref["sid"]
        f.parent.mkdir(exist_ok=True, mode=0o700)
        fd = os.open(f, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            n.fail("Target session is active in Threadway")
        return fd

    def cold_claude(self, dest):
        # Official interactive clients publish per-process metadata in their config root.
        # Additional check for an open transcript FD catches other writers.
        sessions = n.claude_root() / "sessions"
        for f in sessions.glob("*.json"):
            try:
                pid = int(f.stem)
                os.kill(pid, 0)
            except (ValueError, ProcessLookupError):
                continue
            try:
                info = json.loads(n.file_bytes(f))
            except (ValueError, OSError):
                n.fail("Cannot establish whether Claude target is idle")
            if dest["sid"] in json.dumps(info) or info.get("cwd") == dest["cwd"]:
                n.fail(
                    "Claude has a live process for this session/worktree; close it before appending"
                )
        r = subprocess.run(
            ["/usr/sbin/lsof", "-t", "--", dest["path"]],
            capture_output=True,
            timeout=12,
        )
        if r.returncode not in (0, 1) or r.stdout.strip() or r.stderr.strip():
            n.fail("Cannot confirm Claude target is closed")

    def inspect(self, r, tool_mode):
        return n.inspect(r, include_tools=bool(tool_mode))

    def state(self, t, target, src=None):
        refs = self.refs(t)
        peer = "claude" if target == "codex" else "codex"
        src = src or (peer if peer in refs else t["active"])
        if src not in refs:
            n.fail("This task has no branch in the requested source agent")
        source = refs[src]
        messages, raw, _, omitted = self.inspect(source, t["tool_mode"])
        out = refs.get(target)
        base = json.loads(t["base"])
        if out is None:
            return "create", source, messages, raw, None, None, None, omitted
        other, stamp, leaf, skipped = self.inspect(out, t["tool_mode"])
        verdict = (
            outcome(base, n.fingerprints(messages), n.fingerprints(other))
            if src != target
            else "reuse"
        )
        if verdict == "append" and not out["owned"]:
            verdict = "managed-copy"
        return verdict, source, messages, raw, out, stamp, leaf, omitted + skipped

    def stamp(self, ref):
        return (
            n.digest(n.file_bytes(n.location_ok(ref)))
            if ref["path"]
            else self.inspect(ref, False)[1]
        )

    def check_source(self, ref, expected):
        if self.stamp(ref) != expected:
            n.fail("Source advanced; retry after it is idle")

    def commit_link(self, t, ref, base, intent, retired=None):
        prior = self.rows("select op from recovery where task=?", t["id"])
        folder = self.root / "transactions" / intent["op"]
        backed = (folder / "before.jsonl").is_file()
        with self.transaction():
            if retired:
                self.db.execute(
                    "update origins set checkpoint=? where agent=? and sid=?",
                    (intent["dest_stamp"], retired["agent"], retired["sid"]),
                )
            self.bind(t["id"], ref, True)
            self.db.execute(
                "update tasks set base=?, active=? where id=?",
                (json.dumps(base), ref["agent"], t["id"]),
            )
            self.db.execute("delete from intents where task=?", (t["id"],))
            self.db.execute("delete from recovery where task=?", (t["id"],))
            if backed:
                self.db.execute(
                    "insert into recovery values(?,?,?,?,?,?,?,?)",
                    (
                        t["id"],
                        intent["op"],
                        ref["sid"],
                        ref["agent"],
                        n.digest(n.file_bytes(ref["path"])),
                        intent["dest_stamp"],
                        t["base"],
                        t["active"],
                    ),
                )
        # Keep one guarded preimage per task, not a full transcript for each switch.
        for obj in ("candidate.jsonl", "import-input.jsonl"):
            (folder / obj).unlink(missing_ok=True)
        if not backed and folder.exists():
            folder.rmdir()
        for row in prior:
            if row["op"] != intent["op"]:
                shutil.rmtree(
                    self.root / "transactions" / str(uuid.UUID(row["op"])),
                    ignore_errors=True,
                )

    def _execute(self, t, intent):
        src = intent["source"]
        old = intent["dest"]
        to = intent["to"]
        cwd = Path(t["cwd"])
        expected = intent["newbase"]
        if len(expected) == 0:
            n.fail("Refusing an empty handoff")
        if to == "claude":
            ref = dict(
                agent="claude", sid=intent["sid"], path=intent["path"], cwd=t["cwd"]
            )
        else:
            mirror = (
                n.claude_root() / "projects" / n.slug(cwd) / (intent["op"] + ".jsonl")
            )
            mapped = old["sid"] if old and old["owned"] else n.imported_sid(mirror)
            existing = (
                next((x for x in n.codex_catalog(cwd) if x["sid"] == mapped), None)
                if mapped
                else None
            )
            if existing and old and old["owned"] and mapped != old["sid"]:
                n.fail("Unexpected Codex import identity")
            ref = existing
        if to == "codex" and old and old["owned"] and ref is None:
            n.fail("Managed Codex registration is missing or archived")
        # Recover exactly one already-published result after a crash.
        if (
            ref
            and Path(ref["path"]).exists()
            and (
                to == "codex"
                or n.digest(n.file_bytes(ref["path"])) == intent["after_hash"]
            )
        ):
            actual = self.inspect(ref, t["tool_mode"])[0]
            if n.fingerprints(actual) == expected:
                if to == "codex":
                    with n.RPC(n.binary("codex"), cwd) as rpc:
                        n.verify_codex(rpc, ref, actual)
                    if not (old and old["owned"]):
                        self.clean_mirror(intent, mirror)
                self.commit_link(
                    t, ref, expected, intent, old if old and not old["owned"] else None
                )
                return ref
        messages, sstamp, _, _ = self.inspect(src, t["tool_mode"])
        if n.fingerprints(messages) != expected or sstamp != intent["source_stamp"]:
            n.fail(
                "Source changed since preparation; cancel the unchanged intent, then retry"
            )
        stage = self.root / "transactions" / intent["op"]
        if old and old["owned"] and to == "claude":
            # A short write can leave an incomplete JSONL line. Complete ONLY a
            # byte-proven prefix of this exact journaled append, without parsing it.
            target = n.location_ok(old)
            current = n.file_bytes(target)
            if n.digest(current) != intent["dest_stamp"]:
                candidate = n.file_bytes(stage / "candidate.jsonl")
                backup = n.file_bytes(stage / "before.jsonl")
                if not (
                    n.digest(backup) == intent["dest_stamp"]
                    and n.digest(candidate) == intent["after_hash"]
                    and len(backup) < len(current) < len(candidate)
                    and current.startswith(backup)
                    and candidate.startswith(current)
                ):
                    n.fail("Unrecognized destination edit; both branches retained")
                lease = self.active_lease(old)
                try:
                    self.cold_claude(old)
                    n.append_claude_bytes(target, n.digest(current), candidate)
                finally:
                    os.close(lease)
                return self._execute(t, intent)
        if old:
            _, before, _, _ = self.inspect(old, t["tool_mode"])
            if before != intent["dest_stamp"]:
                n.fail("Destination changed since preparation; both histories retained")
        self.check_source(src, sstamp)
        if to == "claude":
            body = n.file_bytes(stage / "candidate.jsonl")
            if n.digest(body) != intent["after_hash"]:
                n.fail("Staged transcript failed integrity check")
            if old and old["owned"]:
                fd = self.active_lease(old)
                try:
                    self.cold_claude(old)
                    if n.digest(n.file_bytes(ref["path"])) != intent["dest_stamp"]:
                        n.fail("Concurrent Claude write; deferred")
                    # The private backup is available even after process loss.
                    n.append_claude_bytes(Path(ref["path"]), intent["dest_stamp"], body)
                finally:
                    os.close(fd)
            else:
                n.private_write(Path(ref["path"]), body, create=True)
            if not any(x["sid"] == ref["sid"] for x in n.claude_catalog(cwd)):
                n.fail("Claude store discovery failed")
        else:
            if not (old and old["owned"]) and mapped:
                n.fail(
                    "This source already has a separate Codex import; refusing to reuse an unowned native thread"
                )
            if old and old["owned"]:
                fd = self.active_lease(old)
                try:
                    prior = self.inspect(old, t["tool_mode"])[0]
                    ref = n.append_managed_codex(
                        old,
                        intent["dest_stamp"],
                        prior,
                        messages,
                        stage / "before.jsonl",
                    )
                finally:
                    os.close(fd)
            else:
                # Native Codex validates the selected importer source under
                # Claude's project store. Use a sanitized, uniquely owned input;
                # the original chat may contain private non-conversation records.
                if mirror.resolve() != mirror:
                    n.fail("Redirected importer directory")
                body = n.file_bytes(stage / "import-input.jsonl")
                if n.digest(body) != intent["mirror_hash"]:
                    n.fail("Invalid staged importer input")
                if mirror.exists():
                    if n.file_bytes(mirror) != body:
                        n.fail("Unexpected importer source collision")
                else:
                    n.private_write(mirror, body, create=True)
                tmp_ref = dict(
                    agent="claude", sid=intent["op"], cwd=t["cwd"], path=str(mirror)
                )
                ref = n.native_import(tmp_ref, cwd)
                self.clean_mirror(intent, mirror)
                with n.RPC(n.binary("codex"), cwd) as rpc:
                    n.verify_codex(rpc, ref, messages)
        got = self.inspect(ref, t["tool_mode"])[0]
        if n.fingerprints(got) != expected:
            n.fail("Native readback differs; transaction retained for inspection")
        self.commit_link(
            t, ref, expected, intent, old if old and not old["owned"] else None
        )
        return ref

    @staticmethod
    def clean_mirror(intent, path):
        if path.exists():
            if n.digest(n.file_bytes(path)) != intent["mirror_hash"]:
                n.fail("Import staging session changed; retaining it")
            path.unlink()

    def switch(self, key, to, source=None, dry=False, branch=False, tools=False):
        with self.lock():
            t = self.task(key)
            tid = t["id"]
            pending = self.rows("select data from intents where task=?", tid)
            if pending:
                obj = json.loads(pending[0]["data"])
                if dry:
                    return {"action": "pending", "task": tid, "to": obj["to"]}
                if obj["to"] != to:
                    n.fail("Finish the pending handoff to " + obj["to"] + " first")
                n.version(to)
                return self.result("recovered", tid, self._execute(t, obj))
            if branch and (not source or source == to):
                n.fail("Choose a source and a different target for a branch")
            if tools and not t["tool_mode"]:
                if t["base"] != "[]":
                    n.fail("Content policy is fixed after the first handoff")
                t["tool_mode"] = 1
            verdict, src, msgs, sstamp, out, dstamp, leaf, omitted = self.state(
                t, to, source
            )
            report = {
                "action": verdict,
                "task": tid,
                "from": src["agent"],
                "to": to,
                "messages": len(msgs),
                "tool_records": omitted,
            }
            if verdict == "diverged":
                checkpoint = json.loads(t["base"])
                report["branches"] = {
                    r["agent"]: {
                        "native_id": r["sid"],
                        "common_messages": len(checkpoint),
                        "delta_messages": len(m) - len(checkpoint)
                        if ancestor(checkpoint, n.fingerprints(m))
                        else None,
                        "rewritten": not ancestor(checkpoint, n.fingerprints(m)),
                    }
                    for r, m in (
                        (src, msgs),
                        (out, self.inspect(out, t["tool_mode"])[0]),
                    )
                }
            if dry:
                return report
            if verdict == "diverged":
                if not branch:
                    raise Conflict(
                        "Both branches retained. Inspect status; use --from AGENT --branch to create a separate continuation."
                    )
                if not source:
                    raise Conflict("Choose a source with --from")
                # Same source head and destination always identify the same branch.
                fork = str(
                    uuid.uuid5(
                        uuid.UUID(tid),
                        source + to + n.digest(n.packed(n.fingerprints(msgs))),
                    )
                )
                if not self.rows("select id from tasks where id=?", fork):
                    with self.transaction():
                        self.adopt(src, parent=tid, task=fork)
                        self.db.execute(
                            "update tasks set tool_mode=? where id=?",
                            (t["tool_mode"], fork),
                        )
                t = self.task(fork)
                tid = fork
                verdict, src, msgs, sstamp, out, dstamp, leaf, omitted = self.state(
                    t, to, source
                )
                if verdict == "diverged":
                    raise Conflict("The previously created branch has itself diverged")
            n.version(src["agent"])
            if verdict.startswith("reuse"):
                n.version(to)
                with self.transaction():
                    # Keep the last common head; the target may still be ahead.
                    self.db.execute("update tasks set active=? where id=?", (to, tid))
                return self.result(verdict, tid, out)
            n.version(to)
            if not Path(t["cwd"]).is_dir():
                n.fail("Original worktree is unavailable")
            if to == "codex" and any(
                m["role"] == "assistant"
                and m["content"] == "<EXTERNAL SESSION IMPORTED>"
                for m in msgs
            ):
                n.fail("User content collides with native Codex import marker")
            if (
                to == "codex"
                and t["tool_mode"]
                and any(isinstance(m["content"], list) for m in msgs)
            ):
                n.fail(
                    "Pinned Codex session importer cannot preserve typed tool history; no write performed"
                )
            op = new_id()
            sid = out["sid"] if out and out["owned"] else new_id()
            folder = self.root / "transactions" / op
            folder.mkdir(parents=True, mode=0o700)
            intent = dict(
                op=op,
                source=src,
                source_stamp=sstamp,
                dest=out,
                dest_stamp=dstamp,
                to=to,
                newbase=n.fingerprints(msgs),
                sid=sid,
            )
            if to == "claude":
                target = (
                    n.claude_root() / "projects" / n.slug(t["cwd"]) / (sid + ".jsonl")
                )
                if target.resolve() != target:
                    n.fail("Claude store path is redirected")
                intent["path"] = str(target)
                if out and out["owned"]:
                    self.cold_claude(out)
                    original = n.file_bytes(target)
                    if n.digest(original) != dstamp:
                        n.fail("Target changed during handoff")
                    n.private_write(folder / "before.jsonl", original)
                    tail = msgs[len(self.inspect(out, t["tool_mode"])[0]) :]
                    fragment = n.claude_suffix(tail, sid, t["cwd"], leaf)
                    candidate = original + fragment
                else:
                    candidate = n.encode_claude(msgs, sid, t["cwd"])
                probe = dict(agent="claude", cwd=t["cwd"], sid=sid)
                staged_msgs = n.decode_records(probe, candidate, bool(t["tool_mode"]))[
                    0
                ]
                if staged_msgs != msgs:
                    n.fail("Converted history changed meaning; target was not written")
                n.private_write(folder / "candidate.jsonl", candidate)
                intent["after_hash"] = n.digest(candidate)
            elif not (out and out["owned"]):
                body = n.encode_claude(msgs, op, t["cwd"])
                n.private_write(folder / "import-input.jsonl", body)
                intent["mirror_hash"] = n.digest(body)
            with self.transaction():
                self.db.execute(
                    "insert into intents values(?,?)", (tid, json.dumps(intent))
                )
                self.db.execute(
                    "update tasks set tool_mode=? where id=?", (t["tool_mode"], tid)
                )
            ref = self._execute(t, intent)
            return dict(self.result(verdict, tid, ref), tool_records=omitted)

    def cancel(self, key):
        with self.lock():
            t = self.task(key)
            rows = self.rows("select data from intents where task=?", t["id"])
            if not rows:
                return {"action": "nothing-pending"}
            i = json.loads(rows[0]["data"])
            old = i["dest"]
            if old and self.stamp(old) != i["dest_stamp"]:
                n.fail("Destination changed; cannot discard recovery evidence")
            if (
                i["to"] == "claude"
                and (not old or not old["owned"])
                and Path(i["path"]).exists()
            ):
                n.fail("New target already exists; retry this handoff")
            if i["to"] == "codex" and (not old or not old["owned"]):
                mirror = (
                    n.claude_root()
                    / "projects"
                    / n.slug(t["cwd"])
                    / (i["op"] + ".jsonl")
                )
                if n.imported_sid(mirror):
                    n.fail("Codex already committed; retry this handoff")
                self.clean_mirror(i, mirror)
            self.db.execute("delete from intents where task=?", (t["id"],))
            shutil.rmtree(
                self.root / "transactions" / str(uuid.UUID(i["op"])), ignore_errors=True
            )
            return {"action": "cancelled", "task": t["id"]}

    def undo(self, key):
        # Never restore by time or title: only revert the exact bytes we wrote.
        with self.lock():
            t = self.task(key)
            if self.rows("select task from intents where task=?", t["id"]):
                n.fail("Resolve pending transaction first")
            rows = self.rows("select * from recovery where task=?", t["id"])
            if not rows:
                n.fail("No reversible append for this task")
            rec = rows[0]
            ref = self.refs(t)[rec["agent"]]
            if not ref["owned"] or ref["sid"] != rec["native_id"]:
                n.fail("Original binding changed")
            fd = self.active_lease(ref)
            try:
                guard = (
                    n.codex_writer(ref["sid"])
                    if ref["agent"] == "codex"
                    else contextlib.nullcontext()
                )
                with guard:
                    p = n.location_ok(ref)
                    if ref["agent"] == "claude":
                        self.cold_claude(ref)
                    before = n.file_bytes(
                        self.root / "transactions" / rec["op"] / "before.jsonl"
                    )
                    actual = n.digest(n.file_bytes(p))
                    if n.digest(before) != rec["before_hash"] or actual not in (
                        rec["before_hash"],
                        rec["after_hash"],
                    ):
                        n.fail("Target or backup changed; refusing undo")
                    if ref["agent"] == "codex":
                        with n.RPC(n.version("codex"), Path(t["cwd"])) as rpc:
                            n.verify_codex(
                                rpc, ref, self.inspect(ref, t["tool_mode"])[0]
                            )
                            if actual != rec["before_hash"]:
                                n.private_write(p, before)
                            n.verify_codex(
                                rpc,
                                ref,
                                n.decode_records(ref, before, bool(t["tool_mode"]))[0],
                            )
                    elif actual != rec["before_hash"]:
                        n.private_write(p, before)
                    if n.file_bytes(p) != before:
                        n.fail("Undo readback failed")
                    with self.transaction():
                        self.db.execute(
                            "update tasks set base=?,active=? where id=?",
                            (rec["old_base"], rec["old_active"], t["id"]),
                        )
                        self.db.execute("delete from recovery where task=?", (t["id"],))
            finally:
                os.close(fd)
            shutil.rmtree(
                self.root / "transactions" / str(uuid.UUID(rec["op"])),
                ignore_errors=True,
            )
            return {"action": "reverted-append", "task": t["id"]}

    @staticmethod
    def result(action, tid, r):
        return {
            "action": action,
            "task": tid,
            "to": r["agent"],
            "native_id": r["sid"],
            "cwd": r["cwd"],
        }

    def launch(self, info):
        agent = info["to"]
        program = n.version(agent)
        fd = self.active_lease({"sid": info["native_id"]})
        args = (
            [program, "resume", info["native_id"]]
            if agent == "codex"
            else [program, "--resume", info["native_id"]]
        )
        try:
            return subprocess.call(args, cwd=info["cwd"], pass_fds=(fd,))
        finally:
            os.close(fd)
