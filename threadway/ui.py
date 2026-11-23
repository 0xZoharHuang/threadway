"""Ephemeral loopback view; commands are copied and run in the user's own terminal."""

import http.server
import json
import secrets
import shlex
import webbrowser
from pathlib import Path


PAGE = Path(__file__).with_name("ui.html").read_text(encoding="utf-8")


def serve(store, paths, cwd, open_browser):
    token = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(18)

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
                self.send_error(403)
                return
            prefix = "/" + token + "/"
            if not self.path.startswith(prefix):
                self.send_error(404)
                return
            route = self.path[len(prefix) :]
            typ = "application/json"
            tasks = store.tasks(paths)
            if not route:
                body = PAGE.replace("NONCE", nonce).encode()
                typ = "text/html; charset=utf-8"
            elif route == "tasks":
                body = json.dumps(
                    {
                        "project": str(cwd),
                        "tasks": [
                            {
                                k: r[k]
                                for k in ("id", "title", "active", "parent", "cwd")
                            }
                            for r in tasks
                        ],
                    }
                ).encode()
            elif route.startswith("detail/"):
                fields = route.split("/")
                if (
                    len(fields) != 3
                    or not any(t["id"] == fields[1] for t in tasks)
                    or fields[2] not in ("claude", "codex")
                ):
                    self.send_error(404)
                    return
                t = store.task(fields[1])
                target = fields[2]
                try:
                    d = store.switch(t["id"], target, dry=True)
                    cmd = [
                        "threadway",
                        "switch",
                        t["id"],
                        "--to",
                        target,
                        "--project",
                        t["cwd"],
                    ]
                    if d["action"] == "diverged":
                        d["lines"] = []
                        for agent, ref in store.refs(t).items():
                            messages = store.inspect(ref, t["tool_mode"])[0]
                            details = d["branches"][agent]
                            added = details.get("delta_messages")
                            start = 0 if added is None else details["common_messages"]
                            preview = messages[start:][:3]
                            d["lines"].append(
                                agent
                                + " · "
                                + (
                                    "history rewritten"
                                    if added is None
                                    else str(added) + " new messages"
                                )
                                + "\n"
                                + "\n".join(
                                    m["role"] + ": " + str(m["content"])[:180]
                                    for m in preview
                                )
                            )
                        d["lines"] += [
                            shlex.join(
                                [
                                    "threadway",
                                    "switch",
                                    t["id"],
                                    "--project",
                                    t["cwd"],
                                    "--from",
                                    a,
                                    "--to",
                                    ("claude" if a == "codex" else "codex"),
                                    "--branch",
                                ]
                            )
                            for a in store.refs(t)
                        ]
                    else:
                        d["command"] = shlex.join(cmd)
                except (RuntimeError, ValueError, OSError) as e:
                    d = {"error": str(e)}
                body = json.dumps(d).encode()
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", typ)
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

    with http.server.HTTPServer(("127.0.0.1", 0), Handler) as server:
        url = f"http://127.0.0.1:{server.server_port}/{token}/"
        print(url, flush=True)
        if open_browser:
            webbrowser.open(url)
        server.serve_forever()
