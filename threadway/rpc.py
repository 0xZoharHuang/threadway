"""Narrow synchronous client for Codex's native stdio app-server."""

import json
import queue
import subprocess
import threading
import time


class RPC:
    def __init__(self, executable, cwd, env=None):
        self.p = subprocess.Popen(
            [str(executable), "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            cwd=cwd,
            env=env,
        )
        self.incoming = queue.Queue()
        self.events = []
        self.serial = 0
        threading.Thread(target=self._reader, daemon=True).start()
        try:
            self.call(
                "initialize",
                {
                    "clientInfo": {"name": "threadway", "version": "0.2.0"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            self.send({"method": "initialized"})
        except BaseException:
            self.close()
            raise

    def _reader(self):
        try:
            for line in self.p.stdout:
                self.incoming.put(json.loads(line))
        finally:
            self.incoming.put(None)

    def send(self, obj):
        self.p.stdin.write(json.dumps(obj) + "\n")
        self.p.stdin.flush()

    def next(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("Codex app-server timed out")
        try:
            msg = self.incoming.get(timeout=remaining)
        except queue.Empty:
            raise RuntimeError("Codex app-server timed out") from None
        if msg is None:
            raise RuntimeError("Codex app-server closed unexpectedly")
        if "method" in msg and "id" in msg:
            self.send(
                {
                    "id": msg["id"],
                    "error": {
                        "code": -32601,
                        "message": "No interactive tool approval in Threadway",
                    },
                }
            )
        return msg

    def call(self, method, params):
        self.serial += 1
        self.send({"id": self.serial, "method": method, "params": params})
        deadline = time.monotonic() + 30
        while True:
            msg = self.next(deadline)
            if msg.get("id") == self.serial and "method" not in msg:
                if "error" in msg:
                    raise RuntimeError(
                        f"{method}: {msg['error'].get('message', 'failed')}"
                    )
                return msg["result"]
            if "method" in msg:
                self.events.append(msg)

    def wait(self, method, predicate=lambda x: True, seconds=40):
        deadline = time.monotonic() + seconds
        while True:
            for i, msg in enumerate(self.events):
                if msg.get("method") == method and predicate(msg.get("params", {})):
                    return self.events.pop(i)["params"]
            self.events.append(self.next(deadline))

    def close(self):
        self.p.stdin.close()
        try:
            self.p.wait(timeout=4)
        except subprocess.TimeoutExpired:
            self.p.terminate()
            try:
                self.p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.p.kill()
                self.p.wait(timeout=5)
        self.p.stdout.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
