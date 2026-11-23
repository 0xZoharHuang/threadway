"""Native-format Antigravity fixtures in a private test home."""

import contextlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid
from unittest.mock import patch
from threadway import google
from threadway.store import Store, outcome


class Trajectories(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name).resolve()
        self.cwd = self.path / "project with spaces"
        self.cwd.mkdir()
        env = patch.dict(
            os.environ,
            {
                "HOME": str(self.path),
                "THREADWAY_HOME": str(self.path / "index"),
                "CLAUDE_CONFIG_DIR": str(self.path / "claude"),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        self.sid = str(uuid.uuid4())
        folder = self.path / "stage"
        self.msg = [
            {"role": "user", "content": "A"},
            {"role": "assistant", "content": "B"},
        ]
        google.stage(folder, self.sid, self.cwd, self.msg)
        self.ref = {
            "sid": self.sid,
            "cwd": str(self.cwd),
            "agent": "agy",
            "path": str(google.db_path(self.sid)),
        }
        google.create(self.ref, folder, "fixture-owner")
        self.orig = google.inspect(self.ref)[1]
        self.op = self.path / "op"
        self.op.mkdir()
        self.end = [
            {"role": "user", "content": "C"},
            {"role": "assistant", "content": "D"},
        ]
        google.stage(self.op / "google", self.sid, self.cwd, self.end)
        self.backup = self.op / "receipt"

    def append(self):
        with patch.object(google, "ensure_cold"):
            google.append(
                self.ref, self.op, self.orig, self.msg, self.msg + self.end, self.backup
            )

    def test_query_and_guarded_undo(self):
        self.assertEqual([r["sid"] for r in google.catalog(self.cwd)], [self.sid])
        self.assertEqual(google.catalog(self.path), [])
        self.append()
        messages, after, _, _ = google.inspect(self.ref)
        self.assertEqual(messages, self.msg + self.end)
        with patch.object(google, "ensure_cold"):
            google.undo(self.ref, self.backup, after)
        self.assertEqual(google.inspect(self.ref)[:2], (self.msg, self.orig))

    def test_contention_and_preserve_later_native_edits(self):
        with contextlib.closing(google.connect(google.db_path(self.sid), True)) as db:
            db.execute("begin immediate")
            with self.assertRaises(sqlite3.OperationalError):
                self.append()
            db.rollback()
        self.assertEqual(google.inspect(self.ref)[1], self.orig)
        self.append()
        after = google.inspect(self.ref)[1]
        with contextlib.closing(google.connect(google.db_path(self.sid), True)) as db:
            db.execute(
                "insert into battle_mode_infos(idx,data) values (?,?)",
                (999, b"fixture"),
            )
        with (
            patch.object(google, "ensure_cold"),
            self.assertRaisesRegex(RuntimeError, "changed"),
        ):
            google.undo(self.ref, self.backup, after)

    def test_unknown_format_and_collision(self):
        with self.assertRaises(FileExistsError):
            google.create(self.ref, self.path / "stage", "fixture-owner")
        with contextlib.closing(google.connect(google.db_path(self.sid), True)) as db:
            db.execute("pragma user_version=999")
        with self.assertRaisesRegex(RuntimeError, "version"):
            google.inspect(self.ref)

    def test_migrate_old_checkpoints_and_compare_two_of_three(self):
        store = Store()
        self.addCleanup(store.db.close)
        refs = {"codex": {}, "claude": {}, "agy": {}}
        t = {"base": json.dumps(["a"])}
        self.assertEqual(store.common_base(t, refs, "claude", "codex"), ["a"])
        t["base"] = json.dumps(
            {"codex": ["a", "b"], "agy": ["a", "b"], "claude": ["a"]}
        )
        self.assertEqual(store.common_base(t, refs, "claude", "agy"), ["a"])
        self.assertEqual(
            outcome(
                store.common_base(t, refs, "claude", "agy"), ["a", "c"], ["a", "b"]
            ),
            "diverged",
        )

    def test_stale_gui_preview_no_intent(self):
        store = Store()
        self.addCleanup(store.db.close)
        tid = store.adopt(dict(self.ref, title="fixture"))
        preview = store.switch(tid, "claude", dry=True)
        self.append()
        with self.assertRaisesRegex(RuntimeError, "preview"):
            store.switch(tid, "claude", expected_preview=preview["plan_id"])
        self.assertEqual(store.rows("select task from intents"), [])
