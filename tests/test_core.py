import json
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

    def raw_tool_pair(self, call_id="x", name="exec_command", args=None, **extra):
        call = dict(
            type="function_call",
            call_id=call_id,
            name=name,
            arguments=json.dumps({"cmd": "echo test"} if args is None else args),
        )
        call.update(extra)
        result = dict(type="function_call_output", call_id=call_id, output="test")
        return [call, result]

    def append_tools(self, items):
        with self.source.open("ab") as f:
            for item in items:
                f.write(
                    n.packed(
                        dict(
                            timestamp="2026-11-22T12:00:00Z",
                            type="response_item",
                            payload=item,
                        )
                    )
                    + b"\n"
                )

    def test_tool_pair_is_typed_only_when_requested(self):
        self.append_tools(self.raw_tool_pair(namespace="functions"))
        self.assertEqual(self.st.inspect(self.ref, False)[0], self.text)
        self.st.switch(self.tid, "claude", tools=True)
        dst = self.st.refs(self.st.task(self.tid))["claude"]
        msgs = self.st.inspect(dst, True)[0]
        self.assertEqual(msgs, self.st.inspect(self.ref, True)[0])
        self.assertEqual(
            msgs[-2:],
            [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "x",
                            "name": "Bash",
                            "input": {"command": "echo test"},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "x", "content": "test"}
                    ],
                },
            ],
        )
        self.assertEqual(self.st.switch(self.tid, "claude")["action"], "reuse")

    def test_unqualified_raw_tool_blocks_entire_strict_handoff(self):
        cases = [
            {"name": "Bash", "args": {"command": "echo test"}},
            {"name": "Bash", "args": {"vendorOnly": "opaque"}},
            {"name": "vendor_lookup"},
            {"namespace": "mcp__vendor"},
            {"args": {"cmd": "echo test", "workdir": "/elsewhere"}},
            {"args": {"cmd": ["echo", "test"]}},
            {"arguments": '{"cmd":"a","cmd":"b"}'},
            {"type": "custom_tool_call"},
            {"name": "shell"},
            {"arguments": "broken"},
        ]
        for case in cases:
            with self.subTest(case=case):
                self.write_source(self.text)
                self.append_tools(
                    self.raw_tool_pair() + self.raw_tool_pair("y", **case)
                )
                before = self.source.read_bytes()
                for dry in (True, False):
                    with self.assertRaisesRegex(RuntimeError, "tool|strict"):
                        self.st.switch(self.tid, "claude", dry=dry, tools=True)
                self.assertEqual(self.source.read_bytes(), before)
                self.assertEqual(self.st.inspect(self.ref, False)[0], self.text)
                self.assertFalse(self.st.rows("select 1 from intents"))
                self.assertFalse(list(n.claude_root().rglob("*.jsonl")))
        self.created()

    def test_strict_requires_paired_text_results_and_supported_target(self):
        call, result = self.raw_tool_pair()
        for items in (
            [call],
            [result, call],
            [call, result, result],
            [call, call, result],
            [call, dict(result, output=[{"type": "text", "text": "test"}])],
        ):
            with self.subTest(items=items):
                self.write_source(self.text)
                self.append_tools(items)
                with self.assertRaises(RuntimeError):
                    self.st.switch(self.tid, "claude", tools=True)
        self.write_source(self.text)
        self.append_tools([call, result])
        for dry in (True, False):
            with self.assertRaisesRegex(RuntimeError, "cannot preserve"):
                self.st.switch(self.tid, "agy", dry=dry, tools=True)

    def test_claude_native_tool_contract(self):
        call = {
            "type": "tool_use",
            "name": "Bash",
            "input": {"command": "echo test"},
            "id": "x",
        }
        res = {
            "type": "tool_result",
            "tool_use_id": "x",
            "content": "failure",
            "is_error": True,
        }
        ref = dict(self.ref, agent="claude")
        messages = self.text + [
            {"role": "assistant", "content": [call]},
            {"role": "user", "content": [res]},
        ]
        raw = n.encode_claude(messages, self.sid, self.cwd)
        self.assertEqual(n.decode_records(ref, raw, True)[0], messages)
        for changed in (
            dict(call, name="mcp__vendor__Bash"),
            dict(call, input={"command": "a", "extra": 1}),
            dict(res, content=[{"type": "text", "text": "failure"}]),
        ):
            original = res if changed["type"] == "tool_result" else call
            rows = [json.loads(line) for line in raw.splitlines()]
            for row in rows:
                blocks = row.get("message", {}).get("content", [])
                if original in blocks:
                    blocks[blocks.index(original)] = changed
            modified = b"".join(n.packed(row) + b"\n" for row in rows)
            with self.assertRaisesRegex(RuntimeError, "verified mapping"):
                n.decode_records(ref, modified, True)

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
