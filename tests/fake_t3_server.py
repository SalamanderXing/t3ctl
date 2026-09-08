#!/usr/bin/env python3
"""Minimal stand-in for the t3 orchestration HTTP API, for t3ctl-smoke.sh.

Implements exactly the surface hermes/t3ctl/bin/t3ctl uses:
  GET  /api/orchestration/shell
  GET  /api/orchestration/threads/<id>?turnLimit=N
  POST /api/orchestration/dispatch
Bearer auth: any token listed (one per line) in --tokens-file is accepted;
the file is re-read on every request so the test can revoke/mint tokens.
Every dispatched command is appended to --state (JSON list) and applied to
the in-memory read model just enough for t3ctl's follow-up reads.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())


class State:
    def __init__(self, seed: dict | None, state_path: Path):
        self.state_path = state_path
        self.projects = list((seed or {}).get("projects", []))
        self.threads = {t["id"]: t for t in (seed or {}).get("threads", [])}
        for t in self.threads.values():
            t.setdefault("activities", [])
            t.setdefault("messages", [])
        self.sequence = 0
        self.dispatched: list[dict] = []

    def shell(self) -> dict:
        shallow = []
        for t in self.threads.values():
            s = {k: v for k, v in t.items() if k not in ("activities", "messages")}
            s["hasPendingApprovals"] = any(
                a.get("tone") == "approval" and a.get("payload", {}).get("decision") is None
                for a in t["activities"]
            )
            shallow.append(s)
        return {"projects": self.projects, "threads": shallow}

    def dispatch(self, cmd: dict) -> dict:
        self.sequence += 1
        self.dispatched.append(cmd)
        self.state_path.write_text(json.dumps(self.dispatched, indent=1))
        kind = cmd.get("type")
        if kind == "thread.create":
            self.threads[cmd["threadId"]] = {
                "id": cmd["threadId"],
                "title": cmd["title"],
                "projectId": cmd["projectId"],
                "runtimeMode": cmd["runtimeMode"],
                "interactionMode": cmd.get("interactionMode", "default"),
                "modelSelection": cmd.get("modelSelection"),
                "archivedAt": None,
                "session": {"status": "ready", "lastError": None},
                "latestTurn": None,
                "hasPendingUserInput": False,
                "settledOverride": None,
                "snoozedUntil": None,
                "updatedAt": now(),
                "activities": [],
                "messages": [],
            }
        elif kind == "thread.turn.start":
            t = self.threads.get(cmd["threadId"])
            if t is None:
                raise KeyError(f"Thread '{cmd['threadId']}' does not exist for command 'thread.turn.start'.")
            t["latestTurn"] = {"turnId": str(uuid.uuid4()), "state": "running"}
            t["messages"].append({"role": "user", "createdAt": now(), "text": cmd["message"]["text"]})
            if cmd.get("modelSelection"):
                t["modelSelection"] = cmd["modelSelection"]
            t["updatedAt"] = now()
        elif kind == "thread.approval.respond":
            t = self.threads[cmd["threadId"]]
            for a in t["activities"]:
                if a.get("payload", {}).get("requestId") == cmd["requestId"]:
                    a["payload"]["decision"] = cmd["decision"]
        elif kind == "project.create":
            self.projects.append({
                "id": cmd["projectId"], "title": cmd["title"],
                "workspaceRoot": cmd["workspaceRoot"], "defaultModelSelection": None,
            })
        elif kind == "project.meta.update":
            for p in self.projects:
                if p["id"] == cmd["projectId"] and "defaultModelSelection" in cmd:
                    p["defaultModelSelection"] = cmd["defaultModelSelection"]
        elif kind in ("thread.delete",):
            self.threads.pop(cmd["threadId"], None)
        elif kind in ("thread.session.stop", "thread.turn.interrupt"):
            t = self.threads[cmd["threadId"]]
            if t.get("latestTurn"):
                t["latestTurn"]["state"] = "interrupted"
        return {"sequence": self.sequence}


class Handler(BaseHTTPRequestHandler):
    server: "Server"

    def _auth(self) -> bool:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False
        tok = header[len("Bearer "):].strip()
        try:
            valid = {l.strip() for l in self.server.tokens_file.read_text().splitlines() if l.strip()}
        except FileNotFoundError:
            valid = set()
        return tok in valid

    def _json(self, code: int, payload) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if not self._auth():
            self._json(401, {"_tag": "EnvironmentAuthInvalidError", "code": "auth_invalid"})
            return
        url = urlparse(self.path)
        if url.path == "/api/orchestration/shell":
            self._json(200, self.server.state.shell())
            return
        prefix = "/api/orchestration/threads/"
        if url.path.startswith(prefix):
            t = self.server.state.threads.get(url.path[len(prefix):])
            if t is None:
                self._json(404, {"error": "no such thread"})
                return
            self._json(200, {"thread": t})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._auth():
            self._json(401, {"_tag": "EnvironmentAuthInvalidError", "code": "auth_invalid"})
            return
        if urlparse(self.path).path != "/api/orchestration/dispatch":
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        cmd = json.loads(self.rfile.read(length) or b"{}")
        try:
            self._json(200, self.server.state.dispatch(cmd))
        except KeyError as e:
            self._json(400, {"_tag": "OrchestrationCommandInvariantError", "message": str(e)})

    def log_message(self, *args) -> None:  # quiet
        return


class Server(ThreadingHTTPServer):
    state: State
    tokens_file: Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--tokens-file", required=True)
    ap.add_argument("--state", required=True, help="JSON file receiving every dispatched command")
    ap.add_argument("--seed", help="JSON file with initial {projects, threads}")
    args = ap.parse_args()
    seed = json.loads(Path(args.seed).read_text()) if args.seed else None
    srv = Server(("127.0.0.1", args.port), Handler)
    srv.state = State(seed, Path(args.state))
    srv.tokens_file = Path(args.tokens_file)
    print(f"fake t3 listening on {args.port}", file=sys.stderr, flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
