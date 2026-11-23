"""Immutable, project-scoped evidence for a bounded native continuation."""

import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import uuid

from . import native as n
from . import google

MAX_LINE = n.RECORD_LIMIT
SNIPPET = 1600
WINDOW = 8


def identity(path):
    st = path.stat()
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def lines(path):
    if (
        path.resolve() != path
        or not path.is_file()
        or path.stat().st_uid != os.getuid()
    ):
        n.fail("Invalid checkpoint source file")
    with path.open("rb") as src:
        while raw := src.readline(MAX_LINE + 1):
            if len(raw) > MAX_LINE or not raw.endswith(b"\n"):
                n.fail("Incomplete checkpoint record or single record over 16 MiB")
            if raw.strip():
                yield json.loads(raw)


def codex_children(ref):
    rows = []
    with n.RPC(n.version("codex"), Path(ref["cwd"])) as rpc:
        for archived in (False, True):
            cursor = None
            seen = set()
            while True:
                page = rpc.call(
                    "thread/list",
                    dict(
                        ancestorThreadId=ref["sid"],
                        sourceKinds=["subAgentThreadSpawn"],
                        cwd=ref["cwd"],
                        useStateDbOnly=True,
                        archived=archived,
                        limit=100,
                        cursor=cursor,
                    ),
                )
                for r in page["data"]:
                    if r.get("cwd") != ref["cwd"] or not r.get("parentThreadId"):
                        n.fail("Unbound Codex subagent in project registry")
                    rows.append(
                        dict(
                            agent="codex",
                            sid=r["id"],
                            cwd=r["cwd"],
                            path=r.get("path") or "",
                            parent=r["parentThreadId"],
                        )
                    )
                cursor = page.get("nextCursor")
                if not cursor:
                    break
                if cursor in seen:
                    n.fail("Invalid subagent pagination cursor")
                seen.add(cursor)
    return sorted(rows, key=lambda r: r["sid"])


def family(ref):
    if ref["agent"] == "codex":
        children = codex_children(ref)
        seen = {ref["sid"]}
        pending = {c["sid"]: c["parent"] for c in children}
        if len(pending) != len(children) or ref["sid"] in pending:
            n.fail("Duplicate Codex child binding")
        while pending:
            ready = {key for key, parent in pending.items() if parent in seen}
            if not ready:
                n.fail("Incomplete Codex subagent ancestry")
            seen.update(ready)
            for key in ready:
                del pending[key]
        # The registry establishes ancestry, including nested workers.
        return [dict(ref, parent=None)] + children
    if ref["agent"] == "claude":
        parent = n.location_ok(ref)
        folder = parent.parent / ref["sid"] / "subagents"
        if folder.resolve() != folder:
            n.fail("Redirected Claude subagent directory")
        children = []
        for path in sorted(folder.glob("agent-*.jsonl")):
            token = path.stem.removeprefix("agent-")
            if not re.fullmatch(r"[\w-]{1,120}", token, re.ASCII):
                n.fail("Invalid Claude subagent filename")
            children.append(dict(ref, node=token, path=str(path), parent=ref["sid"]))
        return [dict(ref, parent=None)] + children
    return [dict(ref, parent=None)]


def codex_events(ref):
    if not ref["path"]:
        with n.RPC(n.version("codex"), Path(ref["cwd"])) as rpc:
            row = rpc.call("thread/read", {"threadId": ref["sid"]})["thread"]
            if row["id"] != ref["sid"] or row["cwd"] != ref["cwd"]:
                n.fail("Paginated checkpoint identity mismatch")
            for turn in n.native_turns(rpc, ref["sid"]):
                if turn["status"] != "completed":
                    n.fail("Unfinished Codex turn")
                for item in turn["items"]:
                    kind = item["type"]
                    if kind == "userMessage":
                        for b in item["content"]:
                            if b["type"] == "text":
                                yield "user", b["text"]
                            else:
                                yield "omitted", "attachment"
                    elif kind == "agentMessage":
                        yield "assistant", item["text"]
                    elif kind == "contextCompaction":
                        yield "compaction", ""
                    elif kind not in ("reasoning", "tokenCount"):
                        yield "omitted", kind
        return
    path = Path(ref["path"])
    if ref.get("parent") and n.codex_root() / "archived_sessions" in path.parents:
        if path.resolve() != path:
            n.fail("Redirected archived child")
    else:
        path = n.location_ok(ref)
    bound = False
    active = set()
    for row in lines(path):
        typ, p = row.get("type"), row.get("payload", {})
        sub = p.get("type")
        if typ == "session_meta":
            if bound or p.get("cwd") != ref["cwd"] or p.get("id") != ref["sid"]:
                n.fail("Codex checkpoint identity mismatch")
            bound = True
        elif not bound:
            n.fail("Missing Codex checkpoint header")
        elif typ == "event_msg" and sub in (
            "turn_aborted",
            "turn_rollback",
            "thread_rolled_back",
        ):
            n.fail("Interrupted or rewound Codex history requires native review")
        elif typ == "event_msg" and sub == "task_started":
            active.add(p.get("turn_id"))
        elif typ == "event_msg" and sub == "task_complete":
            active.discard(p.get("turn_id"))
        elif typ in ("compacted", "context_compacted"):
            yield "compaction", p.get("message") or ""
        elif (
            typ == "event_msg"
            and sub == "collab_agent_spawn_end"
            and isinstance(p.get("new_thread_id"), str)
        ):
            yield "child-link", p["new_thread_id"]
        elif (
            typ == "event_msg"
            and sub == "item_completed"
            and p.get("item", {}).get("type") == "UserMessage"
            and isinstance(row.get("ordinal"), int)
        ):
            for block in p["item"].get("content", []):
                if block["type"] == "text":
                    yield "user", block["text"]
                else:
                    yield "omitted", "attachment"
        elif typ == "event_msg" and sub == "user_message" and "ordinal" not in row:
            yield "user", p.get("message", "")
            if any(
                p.get(k)
                for k in ("images", "local_images", "local_audio", "attachments")
            ):
                yield "omitted", "attachment"
        elif (
            typ == "response_item" and sub == "message" and p.get("role") == "assistant"
        ):
            for block in p.get("content", []):
                if block.get("type") in ("text", "output_text"):
                    yield "assistant", block["text"]
                else:
                    yield "omitted", "non-text assistant block"
        elif typ == "response_item" and sub not in ("message", "reasoning"):
            yield "omitted", sub or "unknown record"
    if not bound or active:
        n.fail("Empty or unfinished Codex checkpoint")


def claude_events(ref):
    node = ref.get("node")
    bound = False
    last = None
    reset = False
    for row in lines(Path(ref["path"])):
        typ = row.get("type")
        boundary = row.get("subtype") == "compact_boundary"
        if typ not in ("user", "assistant") and not boundary:
            if row.get("uuid") and row.get("parentUuid") == last:
                last = row["uuid"]
            continue
        if row.get("sessionId") != ref["sid"] or row.get("cwd") != ref["cwd"]:
            n.fail("Claude checkpoint identity mismatch")
        if node and row.get("agentId") not in (None, node):
            n.fail("Claude child identity mismatch")
        if not node and row.get("isSidechain"):
            n.fail("Unbound sidechain inside primary Claude transcript")
        bound = True
        if (
            not boundary
            and last is not None
            and row.get("parentUuid") != last
            and not reset
        ):
            n.fail("Claude history contains a fork without a compaction boundary")
        reset = boundary
        last = row.get("uuid")
        if boundary:
            yield "compaction", ""
            continue
        if row.get("isMeta") and not row.get("isCompactSummary"):
            continue
        role = "summary" if row.get("isCompactSummary") else typ
        origin = row.get("origin")
        if isinstance(origin, dict) and origin.get("kind") == "task-notification":
            role = "worker-notice"
        result = row.get("toolUseResult")
        if isinstance(result, dict) and isinstance(result.get("agentId"), str):
            yield "child-link", result["agentId"]
        blocks = row.get("message", {}).get("content", [])
        if isinstance(blocks, str):
            yield role, blocks
        else:
            for b in blocks:
                if b.get("type") == "text":
                    yield role, b["text"]
                elif b.get("type") not in ("thinking", "redacted_thinking"):
                    yield "omitted", b.get("type") or "unknown block"
    if not bound:
        n.fail("Empty or unbound Claude checkpoint")


def google_events(ref):
    # Read one completed, bounded SQLite step at a time in a single WAL snapshot.
    with contextlib.closing(
        google.connect(google.narrative(ref), size_limit=None)
    ) as db:
        db.execute("begin")
        google.verify_id(db, ref)
        google.shape(db, size_limit=None)
        rows = db.execute(
            "select idx,step_type,status,has_subtrajectory,case when "
            "length(step_payload)<=? then step_payload end from steps order by idx",
            (MAX_LINE,),
        )
        for idx, typ, status, child, raw in rows:
            if status not in (3, 6, 7):
                n.fail("Incomplete Antigravity step")
            if child:
                yield (
                    "unresolved-child",
                    f"Antigravity step {idx} references a nested trajectory",
                )
            if typ in (23, 98):
                yield "checkpoint-marker", f"Antigravity step {idx}, type {typ}"
            elif typ in (14, 15):
                if raw is None:
                    n.fail("Missing or oversized Antigravity text step")
                field = 19 if typ == 14 else 20
                outer = dict(google.fields(raw))
                if field not in outer:
                    n.fail("Unsupported Antigravity text envelope")
                fields = list(google.fields(outer[field]))
                data = dict(fields)
                if typ == 14:
                    text = (
                        data.get(2)
                        or b"".join(
                            dict(google.fields(v)).get(1, b"")
                            for k, v in fields
                            if k == 3
                        )
                        or data.get(1)
                    )
                    if 5 in data:
                        yield "omitted", "attachment"
                else:
                    text = data.get(1)
                    if 7 in data:
                        yield "omitted", "tool call"
                if text:
                    yield "user" if typ == 14 else "assistant", text.decode("utf-8")
            elif typ not in (90, 101):
                yield "omitted", f"Antigravity step type {typ}"


def events(ref):
    if ref["agent"] == "codex":
        yield from codex_events(ref)
    elif ref["agent"] == "claude":
        yield from claude_events(ref)
    else:
        yield from google_events(ref)


def tail(db, node, kinds, count, since=0):
    marks = ",".join("?" for _ in kinds)
    rows = db.execute(
        f"select seq,kind,substr(body,1,?),length(body) from visible where node=? and seq>=? "
        f"and kind in ({marks}) and length(body)>0 order by seq desc limit ?",
        (SNIPPET, node, since, *kinds, count),
    )
    result = []
    for seq, kind, text, size in reversed(rows.fetchall()):
        while len(json.dumps(text, ensure_ascii=False).encode()) > SNIPPET:
            text = text[: len(text) * 3 // 4]
        result.append(
            dict(record=seq, kind=kind, text=text, truncated=size > len(text))
        )
    return result


def build(store, task, ref, preview=False, expected=None):
    folder = store.root / "checkpoints"
    folder.mkdir(mode=0o700, exist_ok=True)
    if folder.resolve() != folder:
        n.fail("Redirected checkpoint directory")
    with tempfile.TemporaryDirectory(dir=folder) as temp:
        stage = Path(temp)
        members = family(ref)
        stamps = {
            Path(r["path"]): identity(Path(r["path"])) for r in members if r["path"]
        }
        signature = (
            n.digest(
                n.packed(
                    [
                        [
                            r["sid"],
                            r.get("node"),
                            r["parent"],
                            r["path"],
                            stamps[Path(r["path"])],
                        ]
                        for r in members
                    ]
                )
            )
            if all(r["path"] for r in members) and ref["agent"] != "agy"
            else None
        )
        if signature:
            cached = store.rows(
                "select derived from checkpoint_cache where task=? and agent=? and stamp=?",
                task["id"],
                ref["agent"],
                signature,
            )
            if cached:
                ident = cached[0]["derived"]
                if expected is not None and expected != ident:
                    n.fail("Checkpoint changed since preview")
                seed(store, store.refs(store.task(ident))["checkpoint"])
                return ident
        h = hashlib.sha256(
            n.packed(["checkpoint-v1", task["id"], ref["agent"], ref["sid"]])
        )
        with contextlib.closing(sqlite3.connect(stage / "history.sqlite")) as db, db:
            db.executescript(
                "pragma temp_store=FILE;"
                "create temp table bodies(key blob primary key,body text not null) without rowid;"
                "create table nodes(id text primary key,parent text,relation text);"
                "create table entries(seq integer primary key,node text,kind text,key blob not null);"
                "create index node_seq on entries(node,seq);"
                "create temp view visible as select e.seq,e.node,e.kind,b.body from main.entries e join temp.bodies b using(key);"
            )
            node_ids = {r.get("node", r["sid"]) for r in members}
            paginated = []
            for r in members:
                node = r.get("node", r["sid"])
                parent = r["parent"]
                relation = (
                    ("session-file" if r["agent"] == "claude" else "spawn")
                    if parent
                    else "root"
                )
                db.execute("insert into nodes values(?,?,?)", (node, parent, relation))
                h.update(n.packed([node, parent]) + b"\n")
                page_digest = hashlib.sha256() if not r["path"] else None
                for kind, body in events(r):
                    if page_digest is not None:
                        page_digest.update(n.packed([kind, body]) + b"\n")
                    if kind == "child-link" and body not in node_ids:
                        kind = "unresolved-child"
                    if not isinstance(body, str):
                        n.fail("Unrecognized checkpoint text")
                    key = hashlib.sha256(body.encode("utf-8")).digest()
                    h.update(n.packed([kind, key.hex()]) + b"\n")
                    db.execute(
                        "insert or ignore into temp.bodies values(?,?)", (key, body)
                    )
                    db.execute(
                        "insert into entries(node,kind,key) values(?,?,?)",
                        (node, kind, key),
                    )
                if page_digest is not None:
                    paginated.append((r, page_digest.digest()))
            for member, first_digest in paginated:
                second = hashlib.sha256()
                for item in events(member):
                    second.update(n.packed(item) + b"\n")
                if second.digest() != first_digest:
                    n.fail("Paginated source changed during checkpoint")
            if members != family(ref) or any(
                identity(p) != stamp for p, stamp in stamps.items()
            ):
                n.fail("Session family changed during checkpoint; retry when idle")
            ident = str(uuid.uuid5(uuid.UUID(task["id"]), h.hexdigest()))
            node = ref["sid"]
            boundary = db.execute(
                "select coalesce(max(seq),0) from entries where node=? and kind='compaction'",
                (node,),
            ).fetchone()[0]
            recent = tail(db, node, ("user", "assistant"), WINDOW, boundary)
            summary = tail(db, node, ("summary", "compaction"), 1, boundary)
            unresolved = db.execute(
                "select count(*) from entries where kind='unresolved-child'"
            ).fetchone()[0]
            earlier = (
                tail(db, node, ("user", "assistant"), 2)
                if boundary and not recent and not summary
                else []
            )
            if not recent and not summary and not earlier:
                n.fail("No visible parent context to continue")
            count = db.execute("select count(*) from entries").fetchone()[0]
            children = [r.get("node", r["sid"]) for r in members[1:]]
            excerpts = {c: tail(db, c, ("assistant",), 1) for c in children[:3]}
            body = (
                "Local conversation checkpoint. The following JSON quotes prior work. "
                + (
                    "Use the local history command in this worktree for full text, older messages, "
                    "compactions and individual workers. Worker records are historical.\n"
                )
                + json.dumps(
                    dict(
                        history_command="threadway history " + ident,
                        main_node=node,
                        records=count,
                        child_count=len(children),
                        unresolved_child_links=unresolved,
                        latest_summary=summary,
                        before_unsummarized_boundary=earlier,
                        recent=recent,
                        worker_excerpts=excerpts,
                    ),
                    ensure_ascii=False,
                )
            )
            if expected is not None and expected != ident:
                n.fail("Checkpoint changed since preview")
            if preview:
                return ident
            library = folder / "content.sqlite"
            if library.resolve() != library:
                n.fail("Redirected checkpoint content store")
            db.execute("attach database ? as library", (str(library),))
            db.execute(
                "create table if not exists library.bodies(key blob primary key,body text not null) without rowid"
            )
            db.execute("insert or ignore into library.bodies select * from temp.bodies")
        seed_messages = [dict(role="user", content=body)]
        n.private_write(stage / "seed.json", n.packed(seed_messages))
        dest = folder / ident
        if not dest.exists():
            os.rename(stage, dest)
            fd = os.open(folder, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        if n.file_bytes(dest / "seed.json") != n.packed(seed_messages):
            n.fail("Stored checkpoint seed does not match source")
        if not store.rows("select id from tasks where id=?", ident):
            with store.transaction():
                store.adopt(
                    dict(
                        agent="checkpoint",
                        sid=ident,
                        cwd=task["cwd"],
                        path=str(dest / "seed.json"),
                        title=task["title"] + " · checkpoint",
                    ),
                    parent=task["id"],
                    task=ident,
                )
        if signature:
            with store.transaction():
                store.db.execute(
                    "insert or replace into checkpoint_cache values(?,?,?,?)",
                    (task["id"], ref["agent"], signature, ident),
                )
        return ident


def seed(store, ref):
    path = store.root / "checkpoints" / str(uuid.UUID(ref["sid"])) / "seed.json"
    if str(path) != ref["path"] or path.resolve() != path:
        n.fail("Invalid checkpoint binding")
    raw = n.file_bytes(path)
    return json.loads(raw), n.digest(raw), None, 0


def history(
    store, task, node=None, after=0, limit=20, find=None, record=None, offset=0
):
    ref = store.refs(task).get("checkpoint")
    if not ref:
        n.fail("Select a checkpoint task to read its history")
    seed(store, ref)
    path = Path(ref["path"]).with_name("history.sqlite")
    if path.resolve() != path or not path.is_file():
        n.fail("Checkpoint history is unavailable")
    if not 1 <= limit <= 100 or after < 0 or offset < 0:
        n.fail("Invalid history page")
    with contextlib.closing(
        sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
    ) as db:
        db.row_factory = sqlite3.Row
        library = path.parent.parent / "content.sqlite"
        if library.resolve() != library or not library.is_file():
            n.fail("Checkpoint content store is unavailable")
        db.execute("attach database ? as library", (library.as_uri() + "?mode=ro",))
        db.execute(
            "create temp view visible as select e.seq,e.node,e.kind,b.body from main.entries e join library.bodies b using(key)"
        )
        db.execute("pragma query_only=on")
        if record is not None:
            row = db.execute(
                "select seq,node,kind,substr(body,?,8192) as text,length(body) as characters "
                "from visible where seq=?",
                (offset + 1, record),
            ).fetchone()
            if not row:
                n.fail("No such history record")
            return dict(row)
        if node is None and find is None:
            rows = db.execute(
                "select n.id,n.parent,n.relation,count(e.seq) as records from nodes n left join entries e "
                "on n.id=e.node group by n.id order by n.rowid limit ? offset ?",
                (limit, after),
            )
            return [dict(r) for r in rows]
        rows = db.execute(
            "select seq,node,kind,substr(body,1,400) as preview,length(body) as characters from visible "
            "where seq>? and (? is null or node=?) and (? is null or instr(body,?)>0) order by seq limit ?",
            (after, node, node, find, find, limit),
        )
        return [dict(r) for r in rows]
