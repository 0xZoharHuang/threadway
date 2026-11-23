"""Google Antigravity CLI trajectory storage; scoped discovery and transactional appends."""

import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import struct
import subprocess
import tempfile
from urllib.parse import unquote, urlsplit
import uuid
from . import native as n

TABLES = {
    "trajectory_meta",
    "steps",
    "gen_metadata",
    "executor_metadata",
    "parent_references",
    "trajectory_metadata_blob",
    "battle_mode_infos",
}
COLUMNS = (
    "idx",
    "step_type",
    "status",
    "has_subtrajectory",
    "metadata",
    "error_details",
    "permissions",
    "task_details",
    "render_info",
    "step_payload",
    "step_format",
)
MARK = ".threadway-owner"


def root():
    return Path.home() / ".gemini" / "antigravity-cli"


def db_path(sid):
    path = root() / "conversations" / (str(uuid.UUID(sid)) + ".db")
    if path.resolve() != path:
        n.fail("Redirected Antigravity conversation")
    return path


def narrative(ref):
    p = db_path(ref["sid"])
    if p != Path(ref["path"]):
        n.fail("Antigravity session binding mismatch")
    return p


def connect(path, write=False):
    if (
        path.resolve() != path
        or not path.is_file()
        or path.stat().st_uid != os.getuid()
        or path.stat().st_size > n.LIMIT
    ):
        n.fail("Unsupported Antigravity database file")
    wal, shm = [Path(str(path) + s) for s in ("-wal", "-shm")]
    if any(x.is_symlink() for x in (wal, shm)):
        n.fail("Redirected Antigravity index")
    if wal.exists() and wal.stat().st_size and not shm.exists():
        n.fail("Incomplete Antigravity WAL")
    opts = (
        "mode=rw"
        if write
        else ("mode=ro" if wal.exists() and shm.exists() else "mode=ro&immutable=1")
    )
    db = sqlite3.connect(
        path.as_uri() + "?" + opts, uri=True, timeout=0.25, isolation_level=None
    )
    try:
        db.execute("pragma trusted_schema=OFF")
        if not write:
            db.execute("pragma query_only=ON")
        return db
    except BaseException:
        db.close()
        raise


def fields(data):
    """Read only protobuf fields needed for workspace metadata."""
    pos = 0

    def integer():
        nonlocal pos
        value = 0
        for shift in range(0, 70, 7):
            if pos >= len(data):
                break
            byte = data[pos]
            pos += 1
            value |= (byte & 127) << shift
            if byte < 128:
                return value
        raise ValueError("Invalid Antigravity metadata")

    while pos < len(data):
        tag = integer()
        wire = tag & 7
        if wire == 0:
            integer()
            continue
        if wire == 2:
            size = integer()
        elif wire in (1, 5):
            size = 8 if wire == 1 else 4
        else:
            raise ValueError("Unknown Antigravity metadata wire type")
        end = pos + size
        if end > len(data):
            raise ValueError("Truncated Antigravity metadata")
        v = data[pos:end]
        pos = end
        if wire == 2:
            yield tag >> 3, v


def workspace(db):
    row = db.execute(
        "select data from trajectory_metadata_blob where id='main' and length(data)<=1048576"
    ).fetchone()
    if not row:
        return None
    main = dict(fields(row[0]))
    ws = dict(fields(main.get(1, b"")))
    for blob in (ws.get(1), main.get(7)):
        if blob:
            uri = urlsplit(blob.decode("utf-8"))
            if uri.scheme == "file" and uri.netloc in ("", "localhost"):
                return Path(unquote(uri.path))
    return None


def catalog(cwd):
    refs = []
    for path in (root() / "conversations").glob("*.db"):
        try:
            if path != db_path(path.stem):
                continue
            with contextlib.closing(connect(path)) as db:
                db.execute("begin")
                if workspace(db) != cwd or db.execute(
                    "select cascade_id from trajectory_meta"
                ).fetchall() != [(path.stem,)]:
                    continue
                refs.append(
                    dict(
                        agent="agy",
                        sid=path.stem,
                        path=str(path),
                        cwd=str(cwd),
                        title="Antigravity " + path.stem[:8],
                        updated=int(path.stat().st_mtime),
                    )
                )
        except (ValueError, UnicodeError, sqlite3.Error, OSError):
            continue
    return refs


def shape(db):
    schema = db.execute(
        "select type,name,sql from sqlite_master where name not like 'sqlite_%' order by type,name"
    ).fetchall()
    if {n for t, n, _ in schema if t == "table"} != TABLES or any(
        t not in ("table", "index") for t, _, _ in schema
    ):
        n.fail("Unqualified Antigravity database schema")
    if db.execute("pragma user_version").fetchone()[0] != 1:
        n.fail("Unqualified Antigravity schema version")
    if tuple(x[1] for x in db.execute("pragma table_info(steps)")) != COLUMNS:
        n.fail("Unqualified Antigravity step columns")
    if (
        db.execute("pragma page_count").fetchone()[0]
        * db.execute("pragma page_size").fetchone()[0]
        > n.LIMIT
    ):
        n.fail("Antigravity snapshot exceeds the qualified size")
    return schema


def stamp(db, before_step=None):
    h = hashlib.sha256(n.packed(shape(db)))
    for table in sorted(TABLES):
        h.update(table.encode())
        where = " where idx < ?" if table == "steps" and before_step is not None else ""
        for row in db.execute(
            f"select * from {table}{where} order by rowid",
            (before_step,) if where else (),
        ):
            for value in row:
                b = value if isinstance(value, bytes) else n.packed(value)
                h.update(b"B" if isinstance(value, bytes) else b"J")
                h.update(struct.pack(">Q", len(b)))
                h.update(b)
    return h.hexdigest()


def verify_id(db, ref):
    if workspace(db) != Path(ref["cwd"]) or db.execute(
        "select cascade_id from trajectory_meta"
    ).fetchall() != [(ref["sid"],)]:
        n.fail("Antigravity identity/workspace mismatch")


def inspect(ref, typed=False):
    return inspect_path(narrative(ref), ref, typed)


def inspect_path(path, ref, typed=False):
    with tempfile.TemporaryDirectory(prefix="threadway-agy-") as tmp:
        snap = Path(tmp) / "conversations" / path.name
        snap.parent.mkdir()
        with (
            contextlib.closing(connect(path)) as src,
            contextlib.closing(sqlite3.connect(snap)) as dst,
        ):
            src.execute("begin")
            verify_id(src, ref)
            signature = stamp(src)
            info = src.execute(
                "select step_type,status,has_subtrajectory from steps order by idx"
            ).fetchall()
            tools = 0
            for typ, status, child in info:
                if status != 3 or child:
                    n.fail("Incomplete or nested Antigravity trajectory")
                if typ in (5, 7, 8, 9, 21, 38, 132):
                    tools += 1
                elif typ not in (14, 15, 90, 101):
                    n.fail("Unqualified Antigravity step type; native session retained")
            if typed and tools:
                n.fail("Typed Antigravity tools are not qualified")
            src.backup(dst)
            dst.execute("pragma journal_mode=delete")
        doc = json.loads(n.codec("read-agy", b"", snap))
        if doc.get("id") != ref["sid"]:
            n.fail("Antigravity codec identity mismatch")
        messages = []
        for item in doc["messages"]:
            body = item["content"]
            if isinstance(body, list):
                if any(
                    x.get("type") not in ("text", "thinking", "tool_use", "tool_result")
                    for x in body
                ):
                    n.fail("Cannot transfer Antigravity attachment")
                body = "".join(x["text"] for x in body if x["type"] == "text")
            if body:
                messages.append({"role": item["role"], "content": body})
        if not messages:
            n.fail("No visible Antigravity text")
        return messages, signature, None, tools


def ensure_cold():
    processes = subprocess.check_output(
        ["/bin/ps", "-ww", "-U", str(os.getuid()), "-o", "comm="], text=True, timeout=6
    )
    if any(
        Path(x.strip()).name.lower() in ("agy", "antigravity")
        for x in processes.splitlines()
    ):
        n.fail("Close the running Antigravity CLI before updating a managed trajectory")


def stage(folder, sid, cwd, messages):
    folder.mkdir(mode=0o700)
    n.codec(
        "stage-agy",
        n.packed(
            {
                "id": sid,
                "cwd": str(cwd),
                "messages": messages,
                "timestamp": n.timestamp(),
            }
        ),
        folder,
    )


def create(ref, folder, op):
    path = narrative(ref)
    brain = root() / "brain" / ref["sid"]
    if brain.resolve() != brain:
        n.fail("Redirected Antigravity brain directory")
    brain.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    marker = brain / MARK
    if brain.exists():
        if not marker.is_file() or n.file_bytes(marker) != op.encode():
            n.fail("Unowned Antigravity directory collision")
    else:
        with tempfile.TemporaryDirectory(dir=brain.parent) as temp:
            staged = Path(temp) / "brain"
            shutil.copytree(folder / "brain" / ref["sid"], staged)
            n.private_write(staged / MARK, op.encode(), create=True)
            staged.rename(brain)
    n.private_write(
        path, n.file_bytes(folder / "conversations" / path.name), create=True
    )


def recoverable(ref, op):
    marker = root() / "brain" / ref["sid"] / MARK
    return marker.is_file() and n.file_bytes(marker) == op.encode()


def append(ref, folder, expected_stamp, prior, desired, backup):
    ensure_cold()
    p = narrative(ref)
    if not (root() / "brain" / ref["sid"] / MARK).is_file():
        n.fail("Missing Antigravity managed origin")
    if desired[: len(prior)] != prior or desired == prior:
        n.fail("Unproven Google fast-forward")
    # Use only a fully prepared staged suffix from the journal's preparation.
    extra = folder / "google" / "conversations" / p.name
    with contextlib.closing(connect(extra)) as src:
        entries = src.execute("select * from steps order by idx").fetchall()
    if len(entries) != len(desired) - len(prior) or any(
        r[1] not in (14, 15) for r in entries
    ):
        n.fail("Unsupported Google suffix")
    with contextlib.closing(connect(p, True)) as db:
        db.execute("begin immediate")
        try:
            verify_id(db, ref)
            if stamp(db) != expected_stamp:
                n.fail("Concurrent Google edit; preserving native session")
            last = db.execute("select coalesce(max(idx),-1)+1 from steps").fetchone()[0]
            if not backup.exists():
                n.private_write(
                    backup,
                    n.packed({"first": last, "before": expected_stamp}),
                    create=True,
                )
            elif json.loads(n.file_bytes(backup)) != {
                "first": last,
                "before": expected_stamp,
            }:
                n.fail("Google backup differs")
            db.executemany(
                "insert into steps values (" + ",".join("?" * len(COLUMNS)) + ")",
                [(last + i, *row[1:]) for i, row in enumerate(entries)],
            )
            n.private_write(backup.with_name("postimage.sha256"), stamp(db).encode())
            db.execute("commit")
        except BaseException:
            if db.in_transaction:
                db.execute("rollback")
            raise


def undo(ref, backup, expected_hash):
    ensure_cold()
    saved = json.loads(n.file_bytes(backup))
    p = narrative(ref)
    with contextlib.closing(connect(p, True)) as db:
        db.execute("begin immediate")
        try:
            verify_id(db, ref)
            now = stamp(db)
            if now == saved["before"]:
                db.execute("rollback")
                return
            written = n.file_bytes(backup.with_name("postimage.sha256")).decode()
            if (
                now != expected_hash
                or now != written
                or stamp(db, saved["first"]) != saved["before"]
            ):
                n.fail("Google target changed; cannot undo")
            db.execute("delete from steps where idx>=?", (saved["first"],))
            db.execute("commit")
        except BaseException:
            if db.in_transaction:
                db.execute("rollback")
            raise
