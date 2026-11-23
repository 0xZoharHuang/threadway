import contextlib
import json
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from threadway import checkpoint as cp, native as n
from threadway.store import Store


class Checkpoints(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        self.cwd = root / "project"
        self.cwd.mkdir()
        env = patch.dict(
            "os.environ",
            dict(
                HOME=str(root),
                CODEX_HOME=str(root / "codex"),
                CLAUDE_CONFIG_DIR=str(root / "claude"),
                THREADWAY_HOME=str(root / "tw"),
            ),
        )
        env.start()
        self.addCleanup(env.stop)
        self.st = Store()
        self.addCleanup(self.st.db.close)
        check = patch.object(n, "version", return_value="fixture")
        check.start()
        self.addCleanup(check.stop)
        self.sid = str(uuid.uuid4())

    def adopt(self, agent, path):
        ref = dict(
            agent=agent,
            sid=self.sid,
            cwd=str(self.cwd),
            path=str(path),
            title="fixture",
        )
        with self.st.transaction():
            return self.st.task(self.st.adopt(ref))

    def test_large_nested_codex_repeated_compaction(self):
        def source(sid, parent=None):
            path = n.codex_root() / "sessions" / (sid + ".jsonl")
            ref = dict(
                agent="codex", sid=sid, cwd=str(self.cwd), path=str(path), parent=parent
            )
            n.private_write(
                path,
                n.packed(
                    dict(type="session_meta", payload=dict(id=sid, cwd=str(self.cwd)))
                )
                + b"\n",
            )
            return ref

        def add(f, typ, **payload):
            f.write(n.packed(dict(type=typ, payload=payload)) + b"\n")

        main = source(self.sid)
        kid = source(str(uuid.uuid4()), self.sid)
        grandkid = source(str(uuid.uuid4()), kid["sid"])
        for child in (kid, grandkid):
            with open(child["path"], "ab") as stream:
                add(
                    stream,
                    "response_item",
                    type="message",
                    role="assistant",
                    content=[dict(type="output_text", text="child report")],
                )
        path = Path(main["path"])
        with path.open("ab") as stream:
            # Exceeds the legacy whole-file limit with bounded per-record memory.
            row = (
                n.packed(
                    dict(
                        type="response_item",
                        payload=dict(type="reasoning", encrypted_content="X" * 262144),
                    )
                )
                + b"\n"
            )
            for _ in range(257):
                stream.write(row)
            for i in range(2000):
                add(
                    stream,
                    "event_msg",
                    type="user_message",
                    message="project history " + str(i),
                )
            add(
                stream,
                "compacted",
                message="first summary",
                replacement_history=[{"role": "developer", "content": "PRIVATE"}],
            )
            add(stream, "event_msg", type="user_message", message="superseded choice")
            add(stream, "compacted", message="latest decision")
            add(stream, "event_msg", type="user_message", message="recent follow-up")
        task = self.adopt("codex", path)
        with patch.object(cp, "codex_children", return_value=[kid, grandkid]):
            ident = self.st.checkpoint(task)
            derived = self.st.task(ident)
            nodes = cp.history(self.st, derived)
            self.assertEqual(
                {r["id"]: r["parent"] for r in nodes},
                {self.sid: None, kid["sid"]: self.sid, grandkid["sid"]: kid["sid"]},
            )
            seed = cp.seed(self.st, self.st.refs(derived)["checkpoint"])[0][0][
                "content"
            ]
            self.assertIn("latest decision", seed)
            self.assertNotIn("superseded choice", seed)
            self.assertNotIn("first summary", seed)
            self.assertIn("recent follow-up", seed)
            self.assertLess(len(seed.encode()), 24576)
            self.assertTrue(cp.history(self.st, derived, find="first summary"))
            self.assertFalse(cp.history(self.st, derived, find="PRIVATE"))
            self.assertEqual(
                len(cp.history(self.st, derived, node=self.sid, after=10, limit=18)), 18
            )
            first = self.st.switch(ident, "claude")
            with patch.object(cp, "events", side_effect=AssertionError("cache missed")):
                self.assertEqual(self.st.checkpoint(task), ident)
            self.assertEqual(
                first["native_id"], self.st.switch(ident, "claude")["native_id"]
            )
            with path.open("ab") as stream:
                add(stream, "event_msg", type="user_message", message="separate branch")
            with self.assertRaisesRegex(RuntimeError, "changed since preview"):
                self.st.checkpoint(task, expected=ident)
            library = self.st.root / "checkpoints" / "content.sqlite"
            with contextlib.closing(sqlite3.connect(library)) as db:
                prior = db.execute("select count(*) from bodies").fetchone()[0]
            new = self.st.checkpoint(task)
            with contextlib.closing(sqlite3.connect(library)) as db:
                self.assertEqual(
                    prior + 1, db.execute("select count(*) from bodies").fetchone()[0]
                )
            self.assertNotEqual(ident, new)
            self.assertFalse(cp.history(self.st, derived, find="separate branch"))
            self.assertTrue(
                cp.history(self.st, self.st.task(new), find="separate branch")
            )

    def test_large_visible_history_can_be_read_beyond_seed_budget(self):
        path = n.claude_root() / "projects" / n.slug(self.cwd) / (self.sid + ".jsonl")
        path.parent.mkdir(parents=True)
        chunk = "long visible text " * 2000
        with path.open("w") as stream:
            for i in range(2100):
                row = dict(
                    type="assistant",
                    sessionId=self.sid,
                    cwd=str(self.cwd),
                    uuid=str(i),
                    parentUuid=str(i - 1) if i else None,
                    message=dict(role="assistant", content=chunk + f"row-{i}-end"),
                )
                print(json.dumps(row), file=stream)
        self.assertGreater(path.stat().st_size, 64 * 1024 * 1024)
        derived = self.st.task(self.st.checkpoint(self.adopt("claude", path)))
        payload = cp.seed(self.st, self.st.refs(derived)["checkpoint"])[0]
        self.assertLess(len(n.packed(payload)), 24576)
        page = cp.history(self.st, derived, record=2100, offset=len(chunk))
        self.assertEqual(page["text"], "row-2099-end")
        self.assertEqual(cp.history(self.st, derived)[0]["records"], 2100)

    def test_claude_worker_and_snapshot_preview(self):
        main = n.claude_root() / "projects" / n.slug(self.cwd) / (self.sid + ".jsonl")
        worker = main.with_suffix("") / "subagents" / "agent-fixture.jsonl"

        def row(typ, parent, uid, text, **extra):
            return dict(
                type=typ,
                sessionId=self.sid,
                cwd=str(self.cwd),
                uuid=uid,
                parentUuid=parent,
                message=dict(role=typ, content=text),
                **extra,
            )

        rows = [
            row("user", None, "one", "old"),
            row("system", None, "b1", "", subtype="compact_boundary"),
            row("user", "b1", "s1", "summary 1", isCompactSummary=True),
            row("system", None, "b2", "", subtype="compact_boundary"),
            row("user", "b2", "s2", "summary 2", isCompactSummary=True),
            row("assistant", "s2", "end", "answer"),
        ]
        n.private_write(main, b"".join(n.packed(r) + b"\n" for r in rows))
        n.private_write(
            worker,
            n.packed(
                row(
                    "assistant",
                    None,
                    "child",
                    "a" * 10000 + "TAIL",
                    agentId="fixture",
                    isSidechain=True,
                )
            )
            + b"\n",
        )
        task = self.adopt("claude", main)
        preview = self.st.checkpoint(task, preview=True)
        self.assertFalse(
            self.st.rows("select id from tasks where parent=?", task["id"])
        )
        ident = self.st.checkpoint(task, expected=preview)
        derived = self.st.task(ident)
        self.assertEqual(cp.history(self.st, derived)[1]["relation"], "session-file")
        child = cp.history(self.st, derived, node="fixture")[0]
        self.assertIn(
            "TAIL",
            cp.history(self.st, derived, record=child["seq"], offset=8192)["text"],
        )
        self.assertEqual(len(cp.history(self.st, derived, find="summary ")), 2)
        worker.unlink()
        worker.symlink_to(main)
        with self.assertRaises(RuntimeError):
            self.st.checkpoint(task)

    def test_google_checkpoint_markers_and_unresolved_child(self):
        from threadway import google

        folder = self.cwd / "staging"
        messages = [
            {"role": "user", "content": "google question"},
            {"role": "assistant", "content": "google answer"},
        ]
        google.stage(folder, self.sid, self.cwd, messages)
        target = google.db_path(self.sid)
        target.parent.mkdir(parents=True)
        (folder / "conversations" / (self.sid + ".db")).rename(target)

        with contextlib.closing(sqlite3.connect(target)) as db, db:
            db.execute("update steps set has_subtrajectory=1 where step_type=15")
            db.execute(
                "insert into steps(idx,step_type,status,step_payload) values(100,23,3,X'')"
            )
        t = self.st.task(self.st.checkpoint(self.adopt("agy", target)))
        self.assertTrue(cp.history(self.st, t, find="google answer"))
        self.assertTrue(cp.history(self.st, t, find="nested trajectory"))
        records = cp.history(self.st, t, node=self.sid)
        self.assertIn("checkpoint-marker", [r["kind"] for r in records])
