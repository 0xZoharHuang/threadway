"""Read-only discovery, strict public-history projection, and native registration."""

import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone

from .rpc import RPC

PIN = {"codex": "0.171.0", "claude": "2.1.330", "agy": "1.5.15"}
AGENTS = tuple(PIN)
LIMIT = 64 * 1024 * 1024


def fail(reason):
    raise RuntimeError(reason)


def digest(b):
    return hashlib.sha256(b).hexdigest()


def packed(x):
    return json.dumps(
        x, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()


def file_bytes(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        fail("Session file absent or redirected")
    with path.open("rb") as f:
        s = os.fstat(f.fileno())
        if s.st_uid != os.getuid():
            fail("Native file belongs to another OS account")
        if s.st_size > LIMIT:
            fail("Session exceeds the validated 64 MiB import limit")
        b = f.read(LIMIT + 1)
        t = path.stat()
        if (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns) != (
            t.st_dev,
            t.st_ino,
            len(b),
            t.st_mtime_ns,
        ):
            fail("Session changed during read")
    return b


def private_write(path, contents, create=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(contents)
            f.flush()
            os.fsync(f.fileno())
        if create:
            os.link(tmp, path)
        else:
            os.replace(tmp, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        Path(tmp).unlink(missing_ok=True)


def timestamp():
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def slug(path):
    value = re.sub("[^A-Za-z0-9]", "-", str(path))
    if len(value) > 200:
        fail("Claude long-path hashing is not qualified in this version")
    return value


def binary(agent):
    dedicated = (
        Path(__file__).resolve().parents[1]
        / "bin"
        / "clients"
        / "node_modules"
        / ".bin"
        / agent
    )
    if agent == "agy":
        dedicated = (
            Path(__file__).resolve().parents[1] / "bin" / "google" / "antigravity"
        )
    default = str(dedicated) if dedicated.is_file() else agent
    v = os.environ.get("THREADWAY_" + agent.upper() + "_BIN", default)
    p = shutil.which(v)
    if not p:
        fail(f"{agent} CLI unavailable")
    return p


def version(agent):
    command = binary(agent)
    if not re.search(
        r"(?<![\d.])" + re.escape(PIN[agent]) + r"(?![\d.])",
        subprocess.check_output([command, "--version"], text=True, timeout=12),
    ):
        fail(f"Untested {agent} CLI; this build is qualified for {PIN[agent]}")
    return command


def codec(mode, body, folder=None):
    p = Path(
        os.environ.get(
            "THREADWAY_CODEC",
            Path(__file__).resolve().parents[1] / "bin/threadway-codec",
        )
    )
    cmd = [str(p), mode] + ([str(folder)] if folder else [])
    r = subprocess.run(cmd, input=body, capture_output=True, timeout=45)
    if r.returncode:
        fail(f"{mode} rejected this transcript")
    return r.stdout


def claude_root():
    return (
        Path(os.getenv("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
        .expanduser()
        .resolve()
    )


def codex_root():
    return Path(os.getenv("CODEX_HOME", Path.home() / ".codex")).expanduser().resolve()


def native_project_rows(rpc, cwd):
    cursor = None
    seen = set()
    while True:
        page = rpc.call(
            "thread/list",
            {
                "cwd": str(cwd),
                "limit": 100,
                "cursor": cursor,
                "archived": False,
                "useStateDbOnly": True,
                "sourceKinds": ["cli", "vscode", "exec", "appServer"],
            },
        )
        for row in page["data"]:
            if (
                row["cwd"] == str(cwd)
                and not row.get("parentThreadId")
                and not row.get("attachedToThreadId")
            ):
                yield row
        cursor = page.get("nextCursor")
        if not cursor:
            return
        if cursor in seen:
            fail("Invalid native project cursor")
        seen.add(cursor)


def codex_catalog(cwd):
    root = codex_root()
    if not root.is_dir():
        return []
    items = []
    with RPC(version("codex"), cwd) as rpc:
        for row in native_project_rows(rpc, cwd):
            path = Path(row["path"]) if row.get("path") else None
            if path and (
                root / "sessions" not in path.parents or path.resolve() != path
            ):
                continue
            if not path or not path.is_file():
                if row.get("historyMode") != "paginated":
                    continue
                path = None
            title = row.get("name") or row.get("preview") or row["id"]
            items.append(
                dict(
                    agent="codex",
                    sid=row["id"],
                    path=str(path) if path else "",
                    cwd=str(cwd),
                    title=title.splitlines()[0][:100],
                    updated=row["updatedAt"],
                )
            )
    return items


def claude_catalog(cwd):
    folder = claude_root() / "projects" / slug(cwd)
    if not folder.exists():
        return []
    result = []
    for f in folder.glob("*.jsonl"):
        try:
            sid = str(uuid.UUID(f.stem))
            if f.resolve() != f or sid != f.stem:
                continue
            with f.open("rb") as stream:
                # Metadata discovery never indexes full message bodies.
                for _ in range(48):
                    line = stream.readline(256 * 1024)
                    if not line or not line.endswith(b"\n"):
                        break
                    obj = json.loads(line)
                    if obj.get("sessionId") != sid or not obj.get("cwd"):
                        continue
                    if obj["cwd"] == str(cwd) and not obj.get("isSidechain"):
                        body = obj.get("message", {}).get("content", "")
                        title = (
                            body
                            if isinstance(body, str) and obj.get("type") == "user"
                            else ""
                        )
                        result.append(
                            dict(
                                agent="claude",
                                sid=sid,
                                path=str(f),
                                cwd=str(cwd),
                                title=(title.strip() or sid).splitlines()[0][:100],
                                updated=int(f.stat().st_mtime),
                            )
                        )
                    break
        except (ValueError, OSError):
            continue
    return result


def list_worktrees(cwd, all_worktrees):
    if not all_worktrees:
        return [cwd]
    p = subprocess.run(
        ["git", "-C", str(cwd), "worktree", "list", "--porcelain", "-z"],
        capture_output=True,
    )
    if p.returncode:
        return [cwd]
    return [
        x
        for part in p.stdout.split(b"\0")
        if part.startswith(b"worktree ")
        for x in [Path(os.fsdecode(part[9:])).resolve()]
        if x.is_dir()
    ]


def location_ok(ref):
    if ref["agent"] == "agy":
        from . import google

        return google.narrative(ref)
    p = Path(ref["path"])
    cwd = Path(ref["cwd"])
    if p.resolve() != p or not p.is_file():
        fail("Native location moved; rescan the original worktree")
    if ref["agent"] == "claude":
        if p != claude_root() / "projects" / slug(cwd) / (ref["sid"] + ".jsonl"):
            fail("Invalid Claude project binding")
    elif codex_root() / "sessions" not in p.parents:
        fail("Invalid Codex project binding")
    return p


def inspect(ref, include_tools=False):
    """Compare complete user-visible text, failing closed on unsupported branch/content."""
    if ref["agent"] == "agy":
        from . import google

        return google.inspect(ref, include_tools)
    if ref["agent"] == "codex" and not ref["path"]:
        with RPC(version("codex"), Path(ref["cwd"])) as rpc:
            native = rpc.call("thread/read", {"threadId": ref["sid"]})["thread"]
            if native["cwd"] != ref["cwd"] or native["id"] != ref["sid"]:
                fail("Wrong native thread binding")
            visible, tools = turn_history(rpc, ref["sid"])
            if include_tools and tools:
                fail("Typed tools in paginated-only history are unqualified")
            if not visible:
                fail("Empty native turn history")
            return visible, digest(packed(visible)), None, tools
    return decode_records(ref, file_bytes(location_ok(ref)), include_tools)


def unique_fields(pairs):
    obj = dict(pairs)
    if len(obj) != len(pairs):
        fail("Duplicate field in strict tool history")
    return obj


def codex_tool_block(item):
    """Qualify the original protocol fields before txcript normalizes tool names."""
    common = {"type", "call_id", "id"}
    call_id = item.get("call_id")
    if isinstance(call_id, str) and call_id:
        if (
            item.get("type") == "function_call"
            and item.get("name") == "exec_command"
            and item.get("namespace") in (None, "functions")
            and item.keys() <= common | {"name", "namespace", "arguments"}
            and isinstance(item.get("arguments"), str)
        ):
            try:
                args = json.loads(item["arguments"], object_pairs_hook=unique_fields)
            except ValueError:
                fail("Malformed Codex tool arguments")
            if (
                isinstance(args, dict)
                and set(args) == {"cmd"}
                and isinstance(args["cmd"], str)
            ):
                return "assistant", {
                    "type": "tool_use",
                    "id": call_id,
                    "name": "Bash",
                    "input": {"command": args["cmd"]},
                }
        if (
            item.get("type") == "function_call_output"
            and item.keys() <= common | {"output"}
            and isinstance(item.get("output"), str)
        ):
            return "user", {
                "type": "tool_result",
                "tool_use_id": call_id,
                "content": item["output"],
            }
    fail("Codex tool name, namespace, arguments or result has no verified mapping")


def claude_tool_block(role, block):
    if block["type"] == "tool_use":
        args = block.get("input")
        if (
            role == "assistant"
            and block.keys() == {"type", "id", "name", "input"}
            and isinstance(block["id"], str)
            and block["id"]
            and block["name"] == "Bash"
            and isinstance(args, dict)
            and args.keys() == {"command"}
            and isinstance(args["command"], str)
        ):
            return role, block
    elif (
        role == "user"
        and block.keys() <= {"type", "tool_use_id", "content", "is_error"}
        and isinstance(block.get("tool_use_id"), str)
        and block["tool_use_id"]
        and isinstance(block.get("content"), str)
        and isinstance(block.get("is_error", False), bool)
    ):
        # Simple omits the optional false flag.
        return role, {k: v for k, v in block.items() if k != "is_error" or v}
    fail("Claude tool name, arguments or result has no verified mapping")


def decode_records(ref, raw, include_tools=False):
    if raw and not raw.endswith(b"\n"):
        fail("Incomplete last native record")
    obj = [
        json.loads(x, object_pairs_hook=unique_fields if include_tools else None)
        for x in raw.splitlines()
        if x.strip()
    ]
    agent = ref["agent"]
    if agent == "codex":
        headers = [r["payload"] for r in obj if r.get("type") == "session_meta"]
        if (
            not headers
            or headers[0].get("id") != ref["sid"]
            or headers[0].get("cwd") != ref["cwd"]
        ):
            fail("Codex ID/worktree mismatch")
    visible = []
    leaf = None
    ids = set()
    tools = 0
    expected_tools = []
    decoded_tools = []
    call_ids = set()
    result_ids = set()
    active_turns = set()
    selected = []
    for r in obj:
        if agent == "codex":
            selected.append(r)
            typ = r.get("type")
            p = r.get("payload", {})
            sub = p.get("type")
            if (
                typ == "event_msg"
                and sub == "task_started"
                and not str(p.get("turn_id", "")).startswith("external-import-turn-")
            ):
                active_turns.add(p.get("turn_id"))
            if typ == "event_msg" and sub == "task_complete":
                active_turns.discard(p.get("turn_id"))
            if typ == "event_msg" and sub in ("turn_aborted", "turn_rollback"):
                fail("Interrupted or rewound native turn; handoff deferred")
            if typ in ("compacted", "context_compacted") or sub in (
                "context_compacted",
                "thread_rolled_back",
            ):
                fail(
                    "Compacted or rolled-back Codex session: native round trip not qualified"
                )
            if (
                typ == "event_msg"
                and sub == "user_message"
                and any(
                    p.get(k)
                    for k in ("images", "local_images", "local_audio", "attachments")
                )
            ):
                fail("Native attachment cannot be carried losslessly")
            if typ == "response_item":
                if (
                    sub == "message"
                    and p.get("role") in ("user", "assistant")
                    and any(
                        b.get("type") not in ("input_text", "output_text", "text")
                        for b in p.get("content", [])
                    )
                ):
                    fail("Attachment cannot be carried losslessly")
                if sub not in ("reasoning", "message"):
                    tools += 1
                    if include_tools:
                        expected_tools.append(codex_tool_block(p))
        else:
            if r.get("type") == "attachment" and r.get("attachment", {}).get(
                "type"
            ) not in ("date", "session_context"):
                fail("Unqualified Claude attachment record; no transfer")
            if not r.get("isMeta") and r.get("type") in (
                "user",
                "assistant",
                "summary",
            ):
                selected.append(r)
            if r.get("isSidechain") or r.get("subtype") == "compact_boundary":
                fail(
                    "Branched or compacted Claude history requires explicit separation"
                )
            if r.get("type") not in ("user", "assistant") and not (
                "uuid" in r and "parentUuid" in r
            ):
                continue
            if r.get("sessionId") != ref["sid"] or r.get("cwd") != ref["cwd"]:
                fail("Claude ID/worktree mismatch")
            u = r.get("uuid")
            if not u or u in ids:
                fail("Invalid native message ID")
            if r.get("parentUuid") != leaf:
                fail("Nonlinear Claude parent chain")
            ids.add(u)
            leaf = u
            if r.get("type") not in ("user", "assistant"):
                continue
            blocks = r.get("message", {}).get("content", "")
            if isinstance(blocks, list):
                for b in blocks:
                    if b.get("type") in ("tool_use", "tool_result"):
                        tools += 1
                        if include_tools:
                            if r.get("isMeta") or r["message"].get("role") != r["type"]:
                                fail(
                                    "Unexpected role or metadata in Claude tool history"
                                )
                            expected_tools.append(claude_tool_block(r["type"], b))
                    elif b.get("type") not in ("text", "thinking", "redacted_thinking"):
                        fail("Unsupported Claude attachment or content block")
    if active_turns:
        fail("Codex turn has not finished")
    converted_input = b"".join(packed(r) + b"\n" for r in selected)
    doc = json.loads(codec("decode-" + agent, converted_input))
    for m in doc["messages"]:
        content = m["content"]
        if isinstance(content, list):
            if any(
                b.get("type") not in ("thinking", "text", "tool_use", "tool_result")
                for b in content
            ):
                fail("Unsupported transcript content")
            if include_tools:
                for b in content:
                    if b.get("type") in ("tool_use", "tool_result"):
                        decoded_tools.append((m["role"], b))
                    if b.get("type") == "tool_use":
                        if not b.get("id") or b["id"] in call_ids:
                            fail("Missing or duplicate tool-call ID")
                        call_ids.add(b["id"])
                    if b.get("type") == "tool_result":
                        i = b.get("tool_use_id")
                        if i not in call_ids or i in result_ids:
                            fail("Unpaired tool result")
                        result_ids.add(i)
            if include_tools and any(
                b.get("type") in ("tool_use", "tool_result") for b in content
            ):
                visible.append(
                    {
                        "role": m["role"],
                        "content": [b for b in content if b.get("type") != "thinking"],
                    }
                )
                continue
            if any(
                b.get("type") not in ("thinking", "text", "tool_use", "tool_result")
                for b in content
            ):
                fail("Unsupported transcript content")
            content = "".join(
                b.get("text", "") for b in content if b.get("type") == "text"
            )
        if content:
            visible.append({"role": m["role"], "content": content})
    if include_tools and (call_ids != result_ids or decoded_tools != expected_tools):
        fail("Unrepresentable tool call/result history")
    if not visible:
        fail("No supported visible messages in this session")
    return visible, digest(raw), leaf, tools


def fingerprints(messages):
    return [digest(packed(m)) for m in messages]


def encode_claude(messages, sid, cwd):
    doc = dict(id=sid, cwd=str(cwd), timestamp=timestamp(), messages=messages)
    encoded = []
    for line in codec("encode-claude", packed(doc)).splitlines():
        row = json.loads(line)
        if row.get("type") in ("assistant", "user"):
            row.update(
                isSidechain=False,
                userType="external",
                entrypoint="cli",
                version=PIN["claude"],
            )
            row.setdefault("parentUuid", None)
        encoded.append(row)
    label = next(
        (
            m["content"]
            for m in messages
            if m["role"] == "user" and isinstance(m["content"], str)
        ),
        "Local task",
    )
    label = " ".join(label.split())[:90]
    encoded.append(
        {
            "type": "custom-title",
            "sessionId": sid,
            "customTitle": "Threadway · " + label,
        }
    )
    return b"".join(packed(r) + b"\n" for r in encoded)


def list_codex(rpc, cwd, sid):
    return next(
        (row for row in native_project_rows(rpc, cwd) if row["id"] == sid), None
    )


def native_import(claude_ref, cwd, expected_sid=None):
    """Delegate registration/append to OpenAI; consume item-level success, not just RPC success."""
    path = location_ok(claude_ref)
    with RPC(binary("codex"), cwd) as rpc:
        item = {
            "itemType": "SESSIONS",
            "cwd": str(cwd),
            "description": "Threadway local handoff",
            "details": {
                "sessions": [{"path": str(path), "cwd": str(cwd), "title": None}]
            },
        }
        response = rpc.call(
            "externalAgentConfig/import",
            {
                "source": "threadway",
                "migrationSource": "claude-code",
                "migrationItems": [item],
            },
        )
        done = rpc.wait(
            "externalAgentConfig/import/completed",
            lambda p: p.get("importId") == response["importId"],
        )
        results = [
            r
            for part in done["itemTypeResults"]
            for r in part.get("successes", [])
            if r.get("source") == str(path)
        ]
        if len(results) != 1:
            fail(
                "Codex refused native import or safe append; both original histories were retained"
            )
        sid = str(uuid.UUID(results[0]["target"]))
        if expected_sid and sid != expected_sid:
            fail("Unexpected native target; transaction needs review")
        row = list_codex(rpc, cwd, sid)
        if not row or not row.get("path") or Path(row["cwd"]).resolve() != cwd:
            fail("Native Codex list readback did not confirm the worktree and ID")
        return dict(agent="codex", sid=sid, cwd=str(cwd), path=row["path"])


def imported_sid(source):
    f = codex_root() / "external_agent_session_imports.json"
    if not f.exists():
        return None
    d = json.loads(file_bytes(f))
    found = {
        r.get("imported_thread_id")
        for r in d["records"]
        if r.get("source_path") == str(source)
    }
    if len(found) > 1:
        fail("Ambiguous native import receipt")
    return next(iter(found), None)


def claude_suffix(messages, sid, cwd, leaf):
    if not leaf:
        fail("Claude destination has no validated leaf")
    rows = []
    # txcript generates deterministic UUIDs relative to an input transcript. Mint
    # new UUIDs for suffixes so they cannot collide with earlier native records.
    for line in encode_claude(messages, sid, cwd).splitlines():
        row = json.loads(line)
        if row.get("type") not in ("user", "assistant"):
            continue
        row.update(uuid=str(uuid.uuid4()), parentUuid=leaf)
        leaf = row["uuid"]
        rows.append(packed(row) + b"\n")
    if not rows:
        fail("Empty converted suffix")
    return b"".join(rows)


@contextlib.contextmanager
def codex_writer(sid):
    """Use the local writer namespace of Codex 0.171.0; never remove native locks."""
    import fcntl

    sid = str(uuid.UUID(sid))
    locks = codex_root() / "thread-writer-locks"
    if locks.resolve() != locks or not locks.is_dir():
        fail("Native Codex writer coordination unavailable")

    def opened(path):
        return os.fdopen(
            os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600), "r+b"
        )

    with opened(locks / ".coordination.lock") as coord:
        try:
            fcntl.flock(coord, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            fail("Native Codex writer coordination busy")
        lock = opened(locks / (sid + ".lock"))
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            lock.close()
            raise
    try:
        yield
    finally:
        lock.close()


def codex_suffix(messages):
    # Emit only completed import-style turns into a Threadway-owned, registered
    # legacy rollout. OpenAI's ThreadStore remains the sole database writer.
    buf = []
    turn = None

    def row(kind, payload):
        buf.append(
            packed({"type": kind, "timestamp": timestamp(), "payload": payload}) + b"\n"
        )

    def end():
        if turn:
            row(
                "event_msg",
                {"type": "task_complete", "turn_id": turn, "last_agent_message": None},
            )

    for m in messages:
        role, text = m["role"], m["content"]
        if not isinstance(text, str):
            fail("Typed tool addition to a Codex rollout is unqualified")
        if role == "user" or turn is None:
            end()
            turn = str(uuid.uuid4())
            row(
                "event_msg",
                {
                    "type": "task_started",
                    "turn_id": turn,
                    "started_at": int(datetime.now().timestamp()),
                },
            )
        if role == "user":
            row(
                "event_msg",
                {
                    "type": "user_message",
                    "message": text,
                    "local_images": [],
                    "local_audio": [],
                    "text_elements": [],
                },
            )
        elif role == "assistant":
            row(
                "event_msg",
                {
                    "type": "agent_message",
                    "message": text,
                    "phase": None,
                    "memory_citation": None,
                },
            )
        else:
            fail("Unsupported role")
        row(
            "response_item",
            {
                "type": "message",
                "role": role,
                "content": [
                    {
                        "type": "input_text" if role == "user" else "output_text",
                        "text": text,
                    }
                ],
            },
        )
    end()
    return b"".join(buf)


def turn_history(rpc, sid):
    actual = []
    cursor = None
    seen = set()
    tools = 0
    while True:
        page = rpc.call(
            "thread/turns/list",
            {
                "threadId": sid,
                "itemsView": "full",
                "sortDirection": "asc",
                "limit": 100,
                "cursor": cursor,
            },
        )
        for turn in page["data"]:
            if turn.get("status") != "completed":
                fail("Thread contains a non-completed turn; cannot safely hand off")
            for item in turn["items"]:
                typ = item["type"]
                if typ == "userMessage":
                    if any(b["type"] != "text" for b in item["content"]):
                        fail("Unsupported user attachment")
                    actual.append(
                        {
                            "role": "user",
                            "content": "".join(b["text"] for b in item["content"]),
                        }
                    )
                elif typ == "agentMessage":
                    if item["text"] != "<EXTERNAL SESSION IMPORTED>":
                        actual.append({"role": "assistant", "content": item["text"]})
                elif typ in ("reasoning", "plan", "tokenCount"):
                    continue
                elif typ in ("contextCompaction", "imageView", "imageGeneration"):
                    fail("Unsupported compacted or media history")
                else:
                    tools += 1
        cursor = page.get("nextCursor")
        if not cursor:
            break
        if cursor in seen:
            fail("Invalid native pagination cursor")
        seen.add(cursor)
    return actual, tools


def verify_codex(rpc, ref, messages):
    row = list_codex(rpc, Path(ref["cwd"]), ref["sid"])
    if not row or row["path"] != ref["path"] or row["cwd"] != ref["cwd"]:
        fail("Unexpected Codex registry binding")
    full = rpc.call("thread/read", {"threadId": ref["sid"]})["thread"]
    if (
        full.get("historyMode") != "legacy"
        or full.get("originator") != "threadway"
        or full.get("path") != ref["path"]
    ):
        fail(
            f"Unsupported managed Codex registration: history mode={full.get('historyMode')}, matching origin={full.get('originator') == 'threadway'}"
        )
    actual, _ = turn_history(rpc, ref["sid"])
    if actual != messages:
        fail("Native Codex turn history differs from the proposed visible transcript")
    return row


def append_managed_codex(ref, previous_hash, prior, desired, backup):
    path = location_ok(ref)
    with codex_writer(ref["sid"]), RPC(binary("codex"), Path(ref["cwd"])) as rpc:
        verify_codex(rpc, ref, prior)
        original = file_bytes(path)
        if digest(original) != previous_hash:
            fail("Managed Codex destination changed")
        header = json.loads(original.splitlines()[0])["payload"]
        if (
            header.get("originator") != "threadway"
            or header.get("id") != ref["sid"]
            or header.get("cwd") != ref["cwd"]
        ):
            fail("Managed target origin mismatch")
        suffix = codex_suffix(desired[len(prior) :])
        if not suffix or desired[: len(prior)] != prior:
            fail("Cannot prove strict fast-forward")
        if not backup.exists():
            private_write(backup, original, create=True)
        if file_bytes(backup) != original:
            fail("Recovery preimage differs")
        candidate = original + suffix
        private_write(backup.with_name("postimage.sha256"), digest(candidate).encode())
        private_write(path, candidate)
        try:
            verify_codex(rpc, ref, desired)
        except BaseException:
            if file_bytes(path) == candidate:
                private_write(path, original)
            raise
    return ref


def append_claude_bytes(path, expected_hash, candidate):
    # O_APPEND means a coincident uncooperative native append cannot be silently
    # overwritten. The exact postimage is checked before claiming the handoff.
    import fcntl

    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW)
    with os.fdopen(fd, "r+b", buffering=0) as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        prior = f.read(LIMIT + 1)
        if digest(prior) != expected_hash or not candidate.startswith(prior):
            fail("Incompatible Claude append")
        tail = candidate[len(prior) :]
        if os.write(f.fileno(), tail) != len(tail):
            fail("Partial append; retained exact preimage and journal")
        os.fsync(f.fileno())
        if file_bytes(path) != candidate:
            fail("Concurrent write after append; retained native data and journal")
