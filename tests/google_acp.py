"""Verify the imported session is discoverable and replayed by Google's official ACP surface."""

import json
import queue
import subprocess
import threading
import time


def verify(executable, cwd, env, sid, expected):
    p = subprocess.Popen(
        [str(executable), "acp", "--stdio"],
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    q = queue.Queue()
    updates = []

    def read():
        try:
            for line in p.stdout:
                q.put(json.loads(line))
        finally:
            q.put(None)

    threading.Thread(target=read, daemon=True).start()

    def call(i, method, params):
        p.stdin.write(
            json.dumps(dict(jsonrpc="2.0", id=i, method=method, params=params)) + "\n"
        )
        p.stdin.flush()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            m = q.get(timeout=max(0.01, deadline - time.monotonic()))
            if m is None:
                raise AssertionError("ACP closed early")
            if m.get("id") == i and "method" not in m:
                assert "result" in m, (method, m.get("error"))
                return m["result"]
            if m.get("method") == "session/update":
                updates.append(m["params"]["update"])
        raise AssertionError("ACP timeout")

    try:
        info = call(
            1,
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {},
                "clientInfo": {"name": "threadway-test", "version": "0.2.1"},
            },
        )
        assert info["agentInfo"]["version"] == "1.5.15"
        registered = call(2, "session/list", {"cwd": str(cwd)})["sessions"]
        assert any(x["sessionId"] == sid and x["cwd"] == str(cwd) for x in registered)
        call(3, "session/load", {"cwd": str(cwd), "sessionId": sid, "mcpServers": []})
        history = json.dumps(updates)
        assert all(marker in history for marker in expected), (
            "ACP did not replay all expected messages"
        )
    finally:
        p.stdin.close()
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(timeout=3)
        p.stdout.close()
