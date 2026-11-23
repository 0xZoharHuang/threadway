import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from threadway import native as n
from threadway.store import Store, Conflict, outcome


class CounterpartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.cwd = self.root / "project with spaces"
        self.cwd.mkdir()
        top = Path(__file__).resolve().parents[1]
        codec = os.environ.get("THREADWAY_CODEC", str(top / "bin/threadway-codec"))
        env = {
            "THREADWAY_HOME": str(self.root / "helper"),
            "CLAUDE_CONFIG_DIR": str(self.root / "claude"),
            "CODEX_HOME": str(self.root / "codex"),
            "THREADWAY_CODEC": codec,
        }
        self.environ = patch.dict(os.environ, env)
        self.environ.start()
        self.addCleanup(self.environ.stop)
        self.st = Store()
        self.addCleanup(self.st.db.close)
        self.check = patch.object(n, "version", return_value="unused")
        self.check.start()
        self.addCleanup(self.check.stop)
        self.sid = str(uuid.uuid4())
        self.text = [
            {"role": "user", "content": "synthetic only"},
            {"role": "assistant", "content": "ready"},
        ]
        self.source = (
            n.codex_root() / "sessions" / ("rollout-fixture-" + self.sid + ".jsonl")
        )
        self.write_source(self.text)
        self.ref = dict(
            agent="codex",
            path=str(self.source),
            sid=self.sid,
            cwd=str(self.cwd),
            title="fixture",
        )
        with self.st.transaction():
            self.tid = self.st.adopt(self.ref)

    def write_source(self, messages):
        doc = {
            "id": self.sid,
            "cwd": str(self.cwd),
            "timestamp": "2026-11-22T12:00:00Z",
            "messages": messages,
        }
        n.private_write(self.source, n.codec("encode-codex", n.packed(doc)))

    def created(self):
        out = self.st.switch(self.tid, "claude")
        self.assertEqual(out["action"], "create")
        return self.st.refs(self.st.task(self.tid))["claude"]

    def test_reuse_and_undo(self):
        dst = self.created()
        before = n.file_bytes(dst["path"])
        self.write_source(
            self.text
            + [
                {"role": "user", "content": "more"},
                {"role": "assistant", "content": "yes"},
            ]
        )
        appended = self.st.switch(self.tid, "claude")
        self.assertEqual(appended["native_id"], dst["sid"])
        self.assertEqual(self.st.switch(self.tid, "claude")["action"], "reuse")
        self.st.undo(self.tid)
        self.assertEqual(n.file_bytes(dst["path"]), before)
        self.assertEqual(
            self.st.switch(self.tid, "claude", dry=True)["action"], "append"
        )

    def test_protect_later_native_edits(self):
        dst = self.created()
        self.write_source(self.text + [{"role": "user", "content": "again"}])
        self.st.switch(self.tid, "claude")
        with open(dst["path"], "ab") as f:
            f.write(b'{"type":"fixture-metadata"}\n')
        with self.assertRaisesRegex(RuntimeError, "changed"):
            self.st.undo(self.tid)

    def test_crash_after_publication_and_source_advances(self):
        with patch.object(
            self.st, "commit_link", side_effect=OSError("simulated crash")
        ):
            with self.assertRaises(OSError):
                self.st.switch(self.tid, "claude")
        self.write_source(self.text + [{"role": "user", "content": "later"}])
        self.assertEqual(self.st.switch(self.tid, "claude")["action"], "recovered")
        self.assertEqual(
            self.st.switch(self.tid, "claude", dry=True)["action"], "append"
        )
        self.assertEqual(len(n.claude_catalog(self.cwd)), 1)

    def test_parallel_writer_and_cancel_before_write(self):
        dst = self.created()
        original = n.file_bytes(dst["path"])
        self.write_source(self.text + [{"role": "user", "content": "yes"}])
        hold = self.st.active_lease(dst)
        try:
            with self.assertRaisesRegex(RuntimeError, "active"):
                self.st.switch(self.tid, "claude")
        finally:
            os.close(hold)
        self.assertEqual(n.file_bytes(dst["path"]), original)
        self.assertEqual(self.st.cancel(self.tid)["action"], "cancelled")

    def test_short_write_recovery(self):
        dst = self.created()
        original = n.file_bytes(dst["path"])
        self.write_source(self.text + [{"role": "user", "content": "append this"}])

        def short(p, expected, body):
            with p.open("ab") as f:
                f.write(body[len(original) : len(original) + 12])
            raise OSError("simulated full disk")

        with patch.object(n, "append_claude_bytes", side_effect=short):
            with self.assertRaises(OSError):
                self.st.switch(self.tid, "claude")
        self.assertEqual(self.st.switch(self.tid, "claude")["action"], "recovered")
        self.assertEqual(self.st.switch(self.tid, "claude")["action"], "reuse")

    def test_reject_source_rewrite_and_guarded_cancel(self):
        self.created()
        self.write_source([{"role": "user", "content": "unrelated"}])
        self.assertEqual(
            self.st.switch(self.tid, "claude", dry=True)["action"], "diverged"
        )
        with self.assertRaises(Conflict):
            self.st.switch(self.tid, "claude")

    def test_missing_pairs_and_truncated_records(self):
        with self.source.open("ab") as f:
            f.write(
                b'{"type":"response_item","payload":{"type":"function_call","name":"exec","arguments":"{}","call_id":"abc"}}\n'
            )
        with self.assertRaises(RuntimeError):
            n.inspect(self.ref, include_tools=True)
        with self.source.open("ab") as f:
            f.write(b'{"unfinished"')
        with self.assertRaisesRegex(RuntimeError, "Incomplete"):
            n.inspect(self.ref)

    def test_active_native_lock_cannot_be_taken(self):
        (n.codex_root() / "thread-writer-locks").mkdir()
        with n.codex_writer(self.sid):
            with self.assertRaises(BlockingIOError):
                with n.codex_writer(self.sid):
                    pass

    def test_role_order_and_duplicates(self):
        same = {"role": "user", "content": "again"}
        self.write_source([same, same, {"role": "assistant", "content": "ok"}])
        dst = self.created()
        self.assertEqual(
            self.st.inspect(dst, False)[0],
            [same, same, {"role": "assistant", "content": "ok"}],
        )

    def test_tool_pair_is_typed_only_when_requested(self):
        use = {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "x",
                    "name": "Bash",
                    "input": {"command": "echo test"},
                }
            ],
        }
        res = {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "x", "content": "test"}],
        }
        self.write_source(self.text[:1] + [use, res] + self.text[1:])
        self.assertTrue(self.st.inspect(self.ref, True)[3])
        self.st.switch(self.tid, "claude", tools=True)
        dst = self.st.refs(self.st.task(self.tid))["claude"]
        self.assertEqual(
            self.st.inspect(dst, True)[0], self.st.inspect(self.ref, True)[0]
        )

    def test_graph(self):
        self.assertEqual(outcome(["a"], ["a", "b"], ["a", "c"]), "diverged")
        self.assertEqual(outcome(["a"], ["a"], ["a", "c"]), "reuse-ahead")

    def test_identical_parallel_edits_are_still_independent(self):
        self.assertEqual(outcome(["a"], ["a", "yes"], ["a", "yes"]), "diverged")

    def test_native_rewrite_does_not_publish(self):
        self.created()
        source_before = self.source.read_bytes()
        with self.source.open("ab") as f:
            f.write(b'{"type":"compacted","payload":{}}\n')
        with self.assertRaisesRegex(RuntimeError, "compacted|Compacted"):
            self.st.switch(self.tid, "claude")
        self.assertTrue(self.source.read_bytes().startswith(source_before))
        self.assertFalse(self.st.rows("select 1 from intents"))

    def test_paginated_source_keeps_roles_and_refuses_missing_tools(self):
        rows = {
            "data": [
                {
                    "status": "completed",
                    "items": [
                        {
                            "type": "userMessage",
                            "content": [{"type": "text", "text": "a"}],
                        },
                        {"type": "commandExecution"},
                        {"type": "agentMessage", "text": "b"},
                    ],
                }
            ],
            "nextCursor": None,
        }
        ref = dict(self.ref, path="")
        with patch.object(n, "RPC") as client:
            rpc = client.return_value.__enter__.return_value
            rpc.call.side_effect = lambda method, _: (
                {"thread": {"cwd": ref["cwd"], "id": ref["sid"]}}
                if method == "thread/read"
                else rows
            )
            got, _, _, tools = n.inspect(ref)
            self.assertEqual([m["content"] for m in got], ["a", "b"])
            self.assertEqual(tools, 1)
            with self.assertRaisesRegex(RuntimeError, "Typed tools"):
                n.inspect(ref, True)

    def test_project_scoping(self):
        self.created()
        other = self.root / "another project"
        other.mkdir()
        self.assertEqual(n.claude_catalog(other), [])
        self.assertEqual(self.st.tasks([other]), [])
