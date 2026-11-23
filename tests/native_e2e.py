"""Run against pinned official clients in disposable homes with a loopback model."""

import argparse
import contextlib
import fcntl
import http.server
import json
import os
import pty
import re
import select
import shlex
import sqlite3
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


class Stub(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        d = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        path = self.path
        if "generatecontent" in path.lower():
            self.server.calls.append(("agy", d["contents"]))
            chunk = {
                "candidates": [
                    {
                        "index": 0,
                        "content": {
                            "role": "model",
                            "parts": [{"text": self.server.answer}],
                        },
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 8,
                    "candidatesTokenCount": 3,
                    "totalTokenCount": 11,
                },
            }
            data = ("data: " + json.dumps(chunk) + "\n\n").encode()
            typ = "text/event-stream"
        elif "messages" in path:
            self.server.calls.append(("claude", d.get("messages", [])))
            data = json.dumps(
                {
                    "id": "msg_fixture",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-sonnet-4-6",
                    "content": [{"type": "text", "text": self.server.answer}],
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 2, "output_tokens": 1},
                }
            ).encode()
            typ = "application/json"
        elif path.endswith("/responses"):
            self.server.calls.append(("codex", d.get("input", [])))
            out = {
                "id": "msg_" + str(len(self.server.calls)),
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": self.server.answer}],
                "status": "completed",
            }
            rid = "resp_" + str(len(self.server.calls))
            events = [
                (
                    "response.created",
                    {"response": {"id": rid, "status": "in_progress"}},
                ),
                (
                    "response.output_item.added",
                    {
                        "output_index": 0,
                        "item": dict(out, content=[], status="in_progress"),
                    },
                ),
                (
                    "response.output_text.delta",
                    {
                        "item_id": out["id"],
                        "output_index": 0,
                        "content_index": 0,
                        "delta": self.server.answer,
                    },
                ),
                ("response.output_item.done", {"output_index": 0, "item": out}),
                (
                    "response.completed",
                    {
                        "response": {
                            "id": rid,
                            "object": "response",
                            "model": "gpt-5.4",
                            "status": "completed",
                            "output": [out],
                            "usage": {
                                "input_tokens": 2,
                                "output_tokens": 1,
                                "total_tokens": 3,
                            },
                        }
                    },
                ),
            ]
            data = "".join(
                "event: " + t + "\ndata: " + json.dumps(dict(type=t, **v)) + "\n\n"
                for t, v in events
            ).encode()
            typ = "text/event-stream"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", typ)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def setup(root):
    home = root / "home"
    home.mkdir()
    cod = home / ".codex"
    cod.mkdir()
    cwd = root / "project"
    cwd.mkdir()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    server.answer = "Synthetic A ready"
    server.calls = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/v1"
    env = {
        "HOME": str(home),
        "PATH": os.environ["PATH"],
        "TMPDIR": str(root),
        "CODEX_HOME": str(cod),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "OPENAI_API_KEY": "synthetic",
        "CODEX_API_KEY": "synthetic",
        "ANTHROPIC_API_KEY": "synthetic",
        "ANTHROPIC_BASE_URL": url[:-3],
        "OPENAI_BASE_URL": url,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_TELEMETRY": "1",
        "DISABLE_ERROR_REPORTING": "1",
        "DISABLE_AUTOUPDATER": "1",
        "NO_COLOR": "1",
        "TERM": "xterm-256color",
    }
    (cod / "config.toml").write_text(
        f'model = "gpt-5.4"\nmodel_provider = "fixture"\n[model_providers.fixture]\nname = "Fixture"\nbase_url = "{url}"\nwire_api = "responses"\nenv_key = "OPENAI_API_KEY"\nsupports_websockets = false\nrequest_max_retries = 0\nstream_max_retries = 0\n'
    )
    return cwd, env, server


def pick(binary, env, cwd, needle):
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 130, 0, 0))
    p = subprocess.Popen(
        [
            str(binary),
            "--bare",
            "--setting-sources",
            "",
            "--strict-mcp-config",
            "--resume",
        ],
        env=env,
        cwd=cwd,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
    )
    os.close(slave)
    try:
        data = b""
        end = time.monotonic() + 12
        events = []
        while time.monotonic() < end:
            if select.select([master], [], [], 0.2)[0]:
                try:
                    b = os.read(master, 65536)
                except OSError:
                    break
                data += b
                if b"\x1b[6n" in b:
                    os.write(master, b"\x1b[1;1R")
                clean = (
                    re.sub(rb"\x1b\[[0-?]*[ -/]*[@-~]", b"", data)
                    .lower()
                    .replace(b" ", b"")
                )
                if needle.lower().encode() in clean:
                    return True, data
                for marker in [
                    b"trust this folder",
                    b"yes, i trust",
                    b"choose the text style",
                    b"confirm you want to proceed",
                    b"welcome to claude",
                    b"use this api key",
                    b"press enter to",
                    b"--bare requires",
                ]:
                    if marker.replace(b" ", b"") in clean and marker not in events:
                        events.append(marker)
                        os.write(master, b"\r")
                        break
        return False, data
    finally:
        p.terminate()
        try:
            p.wait(timeout=2)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(timeout=3)
        os.close(master)


def isolated_wrapper(root, label, program, profile):
    if sys.platform != "darwin":
        return program  # CI has a fresh disposable home and uses only loopback model endpoints.
    dest = root / ("official-" + label)
    dest.write_text(
        "#!/bin/sh\nexec /usr/bin/sandbox-exec -f "
        + shlex.quote(str(profile))
        + " "
        + shlex.quote(str(program))
        + ' "$@"\n'
    )
    dest.chmod(0o700)
    return dest


def test(root, programs, codec):
    cwd, env, server = setup(root)
    # Force any actual model exchange onto localhost and deny reads of the
    # operator's real agent stores, including when a CLI tries OS defaults.
    user = Path.home()
    denied = [
        user / k
        for k in (
            ".codex",
            ".claude",
            ".gemini",
            ".ssh",
            ".aws",
            "Library/Keychains",
            "Library/Application Support/Codex",
        )
    ]
    profile = root / "fixture.sb"
    profile.write_text(
        '(version 1)(allow default)(deny network-outbound)(allow network-outbound (remote ip "localhost:*"))'
        + "(deny file-read* file-write* "
        + " ".join("(subpath " + json.dumps(str(p)) + ")" for p in denied)
        + ")"
    )
    cx = isolated_wrapper(root, "codex", programs["codex"], profile)
    cl = isolated_wrapper(root, "claude", programs["claude"], profile)
    env.update(
        THREADWAY_HOME=str(root / "tw"),
        THREADWAY_CODEC=str(codec),
        THREADWAY_CODEX_BIN=str(cx),
        THREADWAY_CLAUDE_BIN=str(cl),
        PYTHONDONTWRITEBYTECODE="1",
    )
    if programs.get("agy"):
        agy = isolated_wrapper(root, "agy", programs["agy"], profile)
        g_root = Path(env["HOME"]) / ".gemini/antigravity-cli"
        g_root.mkdir(parents=True)
        (g_root / "settings.json").write_text(json.dumps({"modelProvider": "gemini"}))
        env.update(
            GEMINI_API_KEY="dummy-local-only",
            GOOGLE_GEMINI_BASE_URL=env["ANTHROPIC_BASE_URL"],
            THREADWAY_AGY_BIN=str(agy),
        )
    cfg = Path(env["CLAUDE_CONFIG_DIR"])
    cfg.mkdir(exist_ok=True)
    (cfg / ".claude.json").write_text(
        json.dumps(
            {
                "hasCompletedOnboarding": True,
                "lastOnboardingVersion": "2.1.330",
                "theme": "dark",
                "customApiKeyResponses": {"approved": ["synthetic"], "rejected": []},
                "projects": {str(cwd): {"hasTrustDialogAccepted": True}},
            }
        )
    )

    def invoke(argv, ok=True):
        p = subprocess.run(
            list(map(str, argv)),
            cwd=cwd,
            env=env,
            input="",
            text=True,
            capture_output=True,
            timeout=60,
        )
        if ok and p.returncode:
            raise RuntimeError((p.stdout + p.stderr)[-1500:])
        return p

    def tw(*args):
        return json.loads(invoke([REPO / "threadway-cli", "--json", *args]).stdout)

    def turn(agent, sid, num, prior, claude_tools=""):
        marker = f"TW-FIX-Q{num}."
        answer = f"TW-FIX-A{num}."
        server.answer = answer
        server.calls.clear()
        if agent == "agy":
            argv = [
                agy,
                "--output-format",
                "json",
                "--print-timeout",
                "15s",
                *(["--conversation", sid] if sid else []),
                "-p",
                marker,
            ]
        elif agent == "codex":
            argv = [
                cx,
                "exec",
                *(["resume"] if sid else []),
                "--skip-git-repo-check",
                "--json",
                *([sid] if sid else ["--sandbox", "read-only"]),
                marker,
            ]
        else:
            argv = [
                cl,
                "--bare",
                "-p",
                "--model",
                "claude-sonnet-4-6",
                "--setting-sources",
                "",
                "--strict-mcp-config",
                "--tools",
                claude_tools,
                "--max-turns",
                "1",
                "--output-format",
                "json",
                *(["--resume", sid] if sid else []),
                marker,
            ]
        p = invoke(argv)
        assert answer in p.stdout
        got = next(
            msgs
            for name, msgs in server.calls
            if name == agent and marker in json.dumps(msgs)
        )
        expected = [
            (role, f"TW-FIX-{key}{i}.")
            for i in prior
            for role, key in [("user", "Q"), ("assistant", "A")]
        ] + [("user", marker)]
        subset = [
            ("assistant" if r["role"] == "model" else r["role"], s)
            for r in got
            for role, s in expected
            if s in json.dumps(r)
        ]
        assert subset == expected, (agent, subset, expected)
        return p.stdout

    turn("codex", None, 0, [])
    print("Official Codex source created", flush=True)
    assert tw("scan")["new"] == 1
    task = tw("list")[0]["id"]
    srcfile = next(Path(env["CODEX_HOME"]).rglob("rollout-*.jsonl"))
    original = srcfile.read_bytes()

    def switch(to, *args):
        return tw("switch", task, "--to", to, "--no-launch", *args)

    b = switch("claude")
    cid = b["native_id"]
    found, _ = pick(programs["claude"], env, cwd, "TW-FIX-Q0.")
    assert found, "Imported Claude session not visible in the official resume picker"
    print("Official Claude resume picker verified", flush=True)
    turn("claude", cid, 1, [0])
    a = switch("codex")
    aid = a["native_id"]
    turn("codex", aid, 2, [0, 1])
    assert switch("claude")["native_id"] == cid
    turn("claude", cid, 3, [0, 1, 2])
    assert switch("codex")["native_id"] == aid
    turn("codex", aid, 4, [0, 1, 2, 3])
    assert switch("claude")["native_id"] == cid
    print(
        "PASS: official Claude picker, Codex registry and 5 turns with exact model-visible role/text order",
        flush=True,
    )
    for i in range(20):
        data = switch("codex" if i % 2 == 0 else "claude")
        assert data["native_id"] in (cid, aid) and data["action"] == "reuse"
    assert len(list(cfg.glob("projects/*/*.jsonl"))) == 1
    assert len(list(Path(env["CODEX_HOME"]).rglob("rollout-*.jsonl"))) == 2
    assert srcfile.read_bytes() == original
    # Verify interoperability with the official writer, not just two Python locks.
    sys.path.insert(0, str(REPO))
    from threadway.rpc import RPC

    with RPC(cx, cwd, env) as live:
        live.call("thread/resume", {"threadId": aid, "excludeTurns": True})
        code = "from threadway.native import codex_writer; import sys; guard=codex_writer(sys.argv[1]); guard.__enter__()"
        childenv = dict(env, PYTHONPATH=str(REPO))
        collision = subprocess.run(
            [sys.executable, "-B", "-c", code, aid],
            env=childenv,
            cwd=cwd,
            capture_output=True,
            text=True,
        )
        assert collision.returncode and "BlockingIOError" in collision.stderr, (
            "Official writer lock was not respected"
        )
    print("PASS: official active Codex writer excludes Threadway", flush=True)
    # Real native turns on both sides since the common checkpoint.
    turn("claude", cid, 5, [0, 1, 2, 3, 4])
    turn("codex", aid, 6, [0, 1, 2, 3, 4])
    assert switch("codex", "--dry-run")["action"] == "diverged"
    denied = invoke(
        [REPO / "threadway-cli", "switch", task, "--to", "codex", "--no-launch"], False
    )
    assert denied.returncode == 3
    chosen = switch("codex", "--from", "claude", "--branch")
    assert chosen["task"] != task and chosen["native_id"] != aid
    again = switch("codex", "--from", "claude", "--branch")
    assert again["native_id"] == chosen["native_id"]
    turn("codex", chosen["native_id"], 7, [0, 1, 2, 3, 4, 5])
    assert tw("status", task)["next"]["claude"]["action"] == "diverged"
    alt = tw(
        "switch", task, "--to", "claude", "--from", "codex", "--branch", "--no-launch"
    )
    assert alt["native_id"] != cid and alt["task"] != chosen["task"]
    turn("claude", alt["native_id"], 8, [0, 1, 2, 3, 4, 6])
    print(
        "PASS: 20 unchanged switches, original preserved, both real divergent continuations retained; explicit fork reused",
        flush=True,
    )
    # Second repository worktree discovery and partial batch verification.
    invoke(["git", "init", "-q"])
    invoke(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=f@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "fixture",
        ]
    )
    other = root / "second"
    invoke(["git", "worktree", "add", "-q", "-b", "fixture-second", other])
    invoke(
        [cx, "exec", "--cd", str(other), "--skip-git-repo-check", "--json", "TW-batch"]
    )
    assert tw("scan", "--all-worktrees")["new"] == 1
    batch = invoke(
        [
            REPO / "threadway-cli",
            "--json",
            "batch",
            "--all-worktrees",
            "--to",
            "claude",
            "--apply",
        ],
        False,
    )
    report = json.loads(batch.stdout)
    assert any(r["action"] == "blocked" for r in report) and any(
        r["action"] == "create" and r["cwd"] == str(other) for r in report
    ), report
    assert srcfile.read_bytes() == original
    print(
        "PASS: project batch keeps conflict isolated and completes separate worktree",
        flush=True,
    )
    # Repeat starting from a genuinely Claude-created, SDK/print-mode thread.
    first = json.loads(turn("claude", None, 10, []))["session_id"]
    tw("scan")
    matches = [
        t
        for t in tw("list")
        if t["agents"] == "claude"
        and tw("status", t["id"])["sessions"]["claude"] == first
    ]
    assert len(matches) == 1
    reverse = matches[0]["id"]
    initial_claude = next(cfg.glob("projects/*/" + first + ".jsonl"))
    keep = initial_claude.read_bytes()
    imported = tw("switch", reverse, "--to", "codex", "--no-launch")["native_id"]
    turn("codex", imported, 11, [10])
    copied = tw("switch", reverse, "--to", "claude", "--no-launch")["native_id"]
    assert copied != first and keep == initial_claude.read_bytes()
    turn("claude", copied, 12, [10, 11])
    back = tw("switch", reverse, "--to", "codex", "--no-launch")
    assert back["native_id"] == imported
    turn("codex", imported, 13, [10, 11, 12])
    print(
        "PASS: independently Claude-originated task round trip, original bytes intact.",
        flush=True,
    )
    # Google is evaluated separately; conversion does not assert native resume.
    # Use an idle fixture task for stage-only Google evidence.
    out = root / "google-stage"
    tw("stage-google", chosen["task"], "--out", str(out))
    (db,) = (out / "conversations").glob("*.db")
    with contextlib.closing(
        sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
    ) as connection:
        steps = connection.execute("select count(*) from steps").fetchone()[0]
    assert steps >= 2 and (out / "brain" / db.stem).is_dir()
    if os.environ.get("THREADWAY_BROWSER_TEST") == "1":
        from browser_smoke import exercise

        exercise(REPO, cwd, env)
    print("PASS: Google CLI-format SQLite + brain directory, steps:", steps, flush=True)
    if programs.get("agy"):
        # Extend an existing task with a third native endpoint. Preserve pair-specific checkpoints.
        def hand(task, to, src=None):
            return tw(
                "switch",
                task,
                "--to",
                to,
                "--no-launch",
                *(["--from", src] if src else []),
            )

        g = hand(reverse, "agy", "codex")["native_id"]
        from google_acp import verify as verify_acp

        verify_acp(
            agy,
            cwd,
            env,
            g,
            [f"TW-FIX-{r}{i}." for i in (10, 11, 12, 13) for r in ("Q", "A")],
        )
        print(
            "PASS: official Google ACP session/list and session/load replay under the native ID.",
            flush=True,
        )
        turn("agy", g, 14, [10, 11, 12, 13])
        assert hand(reverse, "claude", "agy")["native_id"] == copied
        turn("claude", copied, 15, [10, 11, 12, 13, 14])
        assert hand(reverse, "agy", "claude")["native_id"] == g
        verify_acp(agy, cwd, env, g, ["TW-FIX-Q15.", "TW-FIX-A15."])
        print(
            "PASS: Google ACP replays newly appended suffix before any new Google turn.",
            flush=True,
        )
        turn("agy", g, 16, [10, 11, 12, 13, 14, 15])
        assert hand(reverse, "codex", "agy")["native_id"] == imported
        turn("codex", imported, 17, [10, 11, 12, 13, 14, 15, 16])
        assert hand(reverse, "agy", "codex")["native_id"] == g
        turn("agy", g, 18, [10, 11, 12, 13, 14, 15, 16, 17])
        print(
            "PASS: Codex → Antigravity → Claude → Antigravity → Codex → Antigravity, same IDs and exact prior roles in Google's native model requests.",
            flush=True,
        )
        initial = json.loads(turn("agy", None, 30, []))["conversation_id"]
        assert tw("scan")["new"] >= 1
        ids = [
            t["id"]
            for t in tw("list")
            if t["agents"] == "agy"
            and tw("status", t["id"])["sessions"]["agy"] == initial
        ]
        assert len(ids) == 1
        b = hand(ids[0], "claude", "agy")["native_id"]
        turn("claude", b, 31, [30])
        g2 = hand(ids[0], "agy", "claude")["native_id"]
        assert g2 != initial
        turn("agy", g2, 32, [30, 31])
        assert hand(ids[0], "claude", "agy")["native_id"] == b
        turn("claude", b, 33, [30, 31, 32])
        turn("agy", g2, 34, [30, 31, 32])
        assert (
            tw("switch", ids[0], "--from", "agy", "--to", "claude", "--dry-run")[
                "action"
            ]
            == "diverged"
        )
        print(
            "PASS: Google-originated discovery, protected original, bidirectional continuation and divergent Google/Claude histories.",
            flush=True,
        )
    # A fresh native fixture carries one synthetic completed call into Claude's next request.
    turn("codex", None, 60, [])
    tw("scan")
    rollouts = Path(env["CODEX_HOME"]).rglob("rollout-*.jsonl")
    (tool_source,) = (p for p in rollouts if b"TW-FIX-Q60." in p.read_bytes())
    source_id = json.loads(tool_source.read_bytes().splitlines()[0])["payload"]["id"]
    (tool_task,) = (
        t["id"]
        for t in tw("list")
        if t["agents"] == "codex"
        and tw("status", t["id"])["sessions"]["codex"] == source_id
    )
    marker_path = cwd / "threadway-unexpected-replay"
    command = "touch threadway-unexpected-replay"
    call_id = "tw-fixture-call"
    with tool_source.open("a") as stream:
        for item in [
            dict(
                type="function_call",
                call_id=call_id,
                name="exec_command",
                arguments=json.dumps({"cmd": command}),
            ),
            dict(
                type="function_call_output", call_id=call_id, output="historical result"
            ),
        ]:
            print(
                json.dumps(
                    dict(
                        type="response_item",
                        timestamp="2026-11-22T12:00:00Z",
                        payload=item,
                    )
                ),
                file=stream,
            )
    handoff = tw("switch", tool_task, "--to", "claude", "--tools", "--no-launch")
    turn("claude", handoff["native_id"], 61, [60], claude_tools="Bash")
    # The CLI may also send a title-generation request quoting the current prompt.
    request = next(
        msgs
        for name, msgs in server.calls
        if name == "claude"
        and "TW-FIX-Q61." in json.dumps(msgs)
        and any(
            msg["role"] == "assistant"
            and isinstance(msg["content"], list)
            and any(block.get("id") == call_id for block in msg["content"])
            for msg in msgs
        )
    )
    history = [
        (msg["role"], block)
        for msg in request
        if isinstance(msg.get("content"), list)
        for block in msg["content"]
        if block.get("type") in ("tool_use", "tool_result")
    ]
    assert len(history) == 2, history
    (actor, use), (recipient, result) = history
    assert (actor, use["id"], use["name"], use["input"]) == (
        "assistant",
        call_id,
        "Bash",
        {"command": command},
    )
    assert (recipient, result["tool_use_id"], result["content"]) == (
        "user",
        call_id,
        "historical result",
    )
    assert not marker_path.exists()
    print(
        "PASS: official Claude resume sends the qualified historical Bash call and result without replay.",
        flush=True,
    )
    server.shutdown()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    for n in ("codex", "claude", "codec"):
        ap.add_argument("--" + n, required=True, type=Path)
    ap.add_argument("--agy", type=Path)
    ns = ap.parse_args()
    with tempfile.TemporaryDirectory(prefix="threadway-native-") as tmp:
        test(
            Path(tmp).resolve(),
            {
                a: p.resolve()
                for a in ("codex", "claude", "agy")
                if (p := getattr(ns, a))
            },
            ns.codec.resolve(),
        )
