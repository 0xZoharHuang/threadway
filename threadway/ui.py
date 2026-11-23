"""Ephemeral CLI GUI. Commit only a project-scoped plan reviewed in this browser."""

import hashlib
import hmac
import http.server
import json
import secrets
import shlex
import sqlite3
import sys
import threading
import webbrowser
from pathlib import Path

from . import native as n

PAGE = Path(__file__).with_name("ui.html").read_text(encoding="utf-8")


def serve(store, paths, cwd, open_browser):
    token = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(18)
    terminal = sys.stdin.isatty() and sys.stdout.isatty()
    launched = []

    def ticket(task, agent, source, branch, plan):
        return hmac.new(
            token.encode(),
            n.packed([task, agent, source, branch, plan]),
            hashlib.sha256,
        ).hexdigest()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def route(self):
            if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
                self.send_error(403)
                return None
            prefix = "/" + token + "/"
            if not self.path.startswith(prefix):
                self.send_error(404)
                return None
            return self.path[len(prefix) :]

        def reply(self, data, code=200):
            html = isinstance(data, str)
            body = (
                data.encode() if html else json.dumps(data, ensure_ascii=False).encode()
            )
            self.send_response(code)
            self.send_header(
                "Content-Type",
                "text/html; charset=utf-8" if html else "application/json",
            )
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                f"default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-{nonce}'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def local_task(self, tid):
            if not any(t["id"] == tid for t in store.tasks(paths)):
                raise KeyError("Unknown task in this project")
            return store.task(tid)

        def do_GET(self):
            route = self.route()
            if route is None:
                return
            if route == "":
                self.reply(PAGE.replace("NONCE", nonce))
                return
            if route == "tasks":
                self.reply(
                    dict(
                        project=str(cwd),
                        agents=n.AGENTS,
                        terminal=terminal,
                        tasks=[
                            {
                                k: r[k]
                                for k in (
                                    "id",
                                    "title",
                                    "active",
                                    "parent",
                                    "cwd",
                                    "agents",
                                )
                            }
                            for r in store.tasks(paths)
                        ],
                    )
                )
                return
            fields = route.split("/")
            if (
                len(fields) not in (3, 4)
                or fields[0] != "detail"
                or fields[2] not in n.AGENTS
            ):
                self.send_error(404)
                return
            try:
                t = self.local_task(fields[1])
                to = fields[2]
                src = fields[3] if len(fields) == 4 else None
                if src and src not in store.refs(t):
                    raise KeyError("Unavailable source")
                d = store.switch(t["id"], to, src, dry=True)
                branch = d["action"] == "diverged" and bool(src) and src != to
                if d["action"] == "pending":
                    self.reply(
                        dict(
                            error="A handoff is pending. Inspect status in the terminal."
                        )
                    )
                    return
                cmd = [
                    "threadway",
                    "switch",
                    t["id"],
                    "--to",
                    to,
                    "--project",
                    t["cwd"],
                ]
                if src:
                    cmd += ["--from", src]
                if branch:
                    cmd += ["--branch"]
                d.update(command=shlex.join(cmd), source=src, branch=branch)
                if d["action"] != "diverged" or branch:
                    d["ticket"] = ticket(t["id"], to, src, branch, d["plan_id"])
                self.reply(d)
            except KeyError:
                self.send_error(404)
            except (ValueError, RuntimeError, OSError, sqlite3.Error) as e:
                self.reply({"error": str(e)}, 409)

        def do_POST(self):
            route = self.route()
            if route is None:
                return
            if route != "handoff":
                self.send_error(404)
                return
            if (
                self.headers.get("Origin")
                != f"http://127.0.0.1:{self.server.server_port}"
                or self.headers.get("Content-Type") != "application/json"
            ):
                self.send_error(403)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4096:
                    raise ValueError("Invalid request size")
                obj = json.loads(self.rfile.read(length))
                t = self.local_task(obj["task"])
                agent = obj["to"]
                src = obj["source"]
                branch = obj["branch"]
                launch = obj["launch"]
                if (
                    agent not in n.AGENTS
                    or (src is not None and src not in store.refs(t))
                    or type(branch) is not bool
                    or type(launch) is not bool
                    or (launch and not terminal)
                ):
                    raise ValueError("Unavailable handoff")
                if not hmac.compare_digest(
                    obj["ticket"], ticket(t["id"], agent, src, branch, obj["plan_id"])
                ):
                    self.send_error(403)
                    return
                result = store.switch(
                    t["id"], agent, src, branch=branch, expected_preview=obj["plan_id"]
                )
                result["command"] = shlex.join(
                    [
                        "threadway",
                        "switch",
                        result["task"],
                        "--project",
                        t["cwd"],
                        "--from",
                        agent,
                        "--to",
                        agent,
                    ]
                )
                self.reply(result)
                if launch:
                    launched.append(result)
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
            except (
                KeyError,
                TypeError,
                ValueError,
                RuntimeError,
                OSError,
                sqlite3.Error,
            ) as e:
                self.reply({"error": str(e)}, 409)

    with http.server.HTTPServer(("127.0.0.1", 0), Handler) as server:
        url = f"http://127.0.0.1:{server.server_port}/{token}/"
        print(url, flush=True)
        if open_browser:
            webbrowser.open(url)
        server.serve_forever()
    return store.launch(launched[0]) if launched else 0
