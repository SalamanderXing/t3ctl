#!/usr/bin/env python3
"""Minimal stand-in for t3's orchestration V2 API (t3 >= 0.0.46), for smoke.sh
and test_transport.py.

Implements exactly the surface bin/t3ctl uses:
  GET /api/orchestration/shell                 (needs x-t3-orchestration-protocol: 2)
  GET /api/orchestration/threads/<id>/bounded  (same)
  GET /ws?orchestrationProtocol=2              WebSocket, Effect-RPC JSON frames:
      orchestration.dispatchCommand, orchestration.searchThreads, projects.mutate
Bearer auth: any token listed (one per line) in --tokens-file is accepted;
the file is re-read on every request so the test can revoke/mint tokens.
Every dispatched command (and project mutation) is appended to --state (JSON
list) and applied to the in-memory model just enough for t3ctl's follow-up
reads. Like the real server, a bare GET without the protocol header is a 400,
a /ws upgrade without orchestrationProtocol=2 is a 426, thread.create and
message.dispatch without createdBy/creationSource die, and an unknown command
type (any V1 leftover) is rejected.

Seed / test model, per thread (compact; shell + projection are derived):
  {id, title, projectId, runtimeMode, interactionMode, modelSelection,
   archivedAt, settledOverride, snoozedUntil, updatedAt, lastError,
   runs: [{id, status, userMessageId}],
   messages: [{id, role, text, createdAt, runId}],
   requests: [{id, kind: "command"|"user_input", status, runId,
               command | questions, decision?, answers?}]}
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import struct
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ACTIVE = ("preparing", "queued", "starting", "running", "waiting")
COMMANDS = {
    "thread.create", "message.dispatch", "runtime-request.respond", "thread.delete",
    "run.interrupt", "provider-session.detach", "thread.settle", "thread.unsettle",
    "thread.runtime-mode.set", "thread.interaction-mode.set",
}


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())


class Reject(Exception):
    def __init__(self, cause):
        super().__init__(json.dumps(cause))
        self.cause = cause


def normalise(t: dict) -> dict:
    t.setdefault("interactionMode", "default")
    for key in ("archivedAt", "settledOverride", "snoozedUntil", "lastError", "modelSelection"):
        t.setdefault(key, None)
    t.setdefault("updatedAt", now())
    t.setdefault("runs", [])
    t.setdefault("messages", [])
    t.setdefault("requests", [])
    return t


class State:
    def __init__(self, seed: dict | None, state_path: Path):
        self.state_path = state_path
        self.projects = [{"defaultModelSelection": None, **p} for p in (seed or {}).get("projects", [])]
        self.threads = {t["id"]: normalise(t) for t in (seed or {}).get("threads", [])}
        self.sequence = 0
        self.dispatched: list[dict] = []

    def _log(self, cmd: dict) -> None:
        self.dispatched.append(cmd)
        self.state_path.write_text(json.dumps(self.dispatched, indent=1))

    # ---- reads ---------------------------------------------------------------
    def shell_thread(self, t: dict) -> dict:
        normalise(t)
        last = t["runs"][-1] if t["runs"] else None
        active = last if last and last["status"] in ACTIVE else None
        pending = next((r for r in t["requests"] if r["status"] == "pending"), None)
        return {
            "id": t["id"], "projectId": t["projectId"], "title": t["title"],
            "modelSelection": t["modelSelection"], "runtimeMode": t["runtimeMode"],
            "interactionMode": t["interactionMode"],
            "latestRunId": last["id"] if last else None,
            "activeRunId": active["id"] if active else None,
            "activityRunStatus": active["status"] if active else None,
            "status": last["status"] if last else "idle",
            "lastError": t["lastError"],
            "pendingRuntimeRequest": {"id": pending["id"], "kind": pending["kind"], "createdAt": now()} if pending else None,
            "archivedAt": t["archivedAt"], "settledOverride": t["settledOverride"],
            "snoozedUntil": t["snoozedUntil"], "updatedAt": t["updatedAt"], "deletedAt": None,
        }

    def shell(self) -> dict:
        threads = [self.shell_thread(t) for t in self.threads.values()]
        return {"schemaVersion": 1, "snapshotSequence": self.sequence, "projects": self.projects,
                "threads": [t for t in threads if t["archivedAt"] is None],
                "archivedThreads": [t for t in threads if t["archivedAt"] is not None]}

    def bounded(self, t: dict) -> dict:
        normalise(t)
        items, runtime_requests = [], []
        for r in t["requests"]:
            tool = f"tool-{r['id']}"
            runtime_requests.append({"id": r["id"], "kind": r["kind"], "status": r["status"],
                                     **({"decision": r["decision"]} if r.get("decision") else {})})
            if r["kind"] == "user_input":
                items.append({"type": "user_input_request", "requestId": r["id"], "runId": r.get("runId"),
                              "questions": r.get("questions", []), "nativeItemRef": None})
                continue
            items.append({"type": "command_execution", "runId": r.get("runId"), "input": r["command"],
                          "nativeItemRef": {"driver": "fake", "nativeId": tool, "strength": "strong"}})
            items.append({"type": "approval_request", "requestId": r["id"], "requestKind": "command",
                          "runId": r.get("runId"), "prompt": "Run a command", "title": None,
                          "startedAt": now(), "nativeItemRef": {"driver": "fake", "nativeId": tool, "strength": "strong"}})
        runs = [{"id": r["id"], "ordinal": i + 1, "status": r["status"], "userMessageId": r.get("userMessageId")}
                for i, r in enumerate(t["runs"])]
        messages = [{"id": m.get("id", f"m{i}"), "role": m["role"], "text": m["text"],
                     "createdAt": m.get("createdAt", now()), "runId": m.get("runId"), "streaming": False}
                    for i, m in enumerate(t["messages"])]
        thread = {k: t[k] for k in ("id", "title", "projectId", "runtimeMode", "interactionMode", "modelSelection")}
        return {"snapshotSequence": self.sequence, "historyCursor": None, "hasMoreHistory": False,
                "latestLocalTurnOrdinal": None,
                "projection": {"thread": thread, "runs": runs, "runtimeRequests": runtime_requests,
                               "messages": messages, "turnItems": items, "providerSessions": []}}

    # ---- RPC -----------------------------------------------------------------
    def rpc(self, method: str, payload: dict):
        if method == "orchestration.dispatchCommand":
            return self.dispatch(payload)
        if method == "orchestration.searchThreads":
            q = payload["query"].lower()
            matches = [{"threadId": t["id"], "projectId": t["projectId"], "source": m["role"],
                        "snippet": m["text"][:240], "messageCreatedAt": m.get("createdAt")}
                       for t in self.threads.values() for m in t["messages"] if q in m["text"].lower()]
            return {"matches": matches[: payload.get("limit", 20)]}
        if method == "projects.mutate":
            self._log(payload)
            if payload["type"] == "project.create":
                p = {"id": payload["projectId"], "title": payload["title"],
                     "workspaceRoot": payload["workspaceRoot"], "defaultModelSelection": None}
                self.projects.append(p)
                return p
            for p in self.projects:
                if p["id"] == payload["projectId"]:
                    if "defaultModelSelection" in payload:
                        p["defaultModelSelection"] = payload["defaultModelSelection"]
                    return p
            raise Reject([{"_tag": "Fail", "error": {"_tag": "ProjectMutationError", "message": "no such project"}}])
        raise Reject([{"_tag": "Die", "defect": f"unknown RPC {method}"}])

    def dispatch(self, cmd: dict) -> dict:
        kind = cmd.get("type")
        if kind not in COMMANDS:
            raise Reject([{"_tag": "Die", "defect": f"Expected a V2 command, got type {kind!r}"}])
        if kind in ("thread.create", "message.dispatch"):
            for key in ("createdBy", "creationSource"):
                if key not in cmd:
                    raise Reject([{"_tag": "Die", "defect": f'Missing key\n  at ["{key}"]'}])
        self.sequence += 1
        self._log(cmd)
        if kind == "thread.create":
            self.threads[cmd["threadId"]] = normalise({
                "id": cmd["threadId"], "title": cmd["title"], "projectId": cmd["projectId"],
                "runtimeMode": cmd["runtimeMode"], "interactionMode": cmd["interactionMode"],
                "modelSelection": cmd["modelSelection"],
            })
            return {"sequence": self.sequence}
        t = self.threads.get(cmd["threadId"])
        if t is None:
            raise Reject([{"_tag": "Fail", "error": {"_tag": "OrchestrationV2DispatchCommandError",
                                                     "message": f"Thread {cmd['threadId']} does not exist"}}])
        if kind == "message.dispatch":
            run = {"id": f"run-{uuid.uuid4()}", "status": "running", "userMessageId": cmd["messageId"]}
            if cmd["dispatchMode"]["type"] == "queue_after_active" and any(r["status"] in ACTIVE for r in t["runs"]):
                run["status"] = "queued"
            t["runs"].append(run)
            t["messages"].append({"id": cmd["messageId"], "role": "user", "text": cmd["text"],
                                  "createdAt": now(), "runId": run["id"]})
            if cmd.get("modelSelection"):
                t["modelSelection"] = cmd["modelSelection"]
        elif kind == "runtime-request.respond":
            for r in t["requests"]:
                if r["id"] == cmd["requestId"]:
                    r["status"] = "resolved"
                    for key in ("decision", "answers"):
                        if key in cmd:
                            r[key] = cmd[key]
        elif kind == "thread.delete":
            self.threads.pop(cmd["threadId"], None)
        elif kind == "run.interrupt":
            for r in t["runs"]:
                if r["id"] == cmd["runId"]:
                    r["status"] = "interrupted"
        elif kind == "thread.settle":
            t["settledOverride"] = "settled"
        elif kind == "thread.unsettle":
            t["settledOverride"] = None
        elif kind == "thread.runtime-mode.set":
            t["runtimeMode"] = cmd["runtimeMode"]
        elif kind == "thread.interaction-mode.set":
            t["interactionMode"] = cmd["interactionMode"]
        t["updatedAt"] = now()
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
        url = urlparse(self.path)
        if url.path == "/ws":
            return self._websocket(url)
        if not self._auth():
            self._json(401, {"_tag": "EnvironmentAuthInvalidError", "code": "auth_invalid"})
            return
        if url.path.startswith("/api/orchestration/") and self.headers.get("x-t3-orchestration-protocol") != "2":
            self.send_response(400)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        state = self.server.state
        if url.path == "/api/orchestration/shell":
            self._json(200, state.shell())
            return
        prefix, suffix = "/api/orchestration/threads/", "/bounded"
        if url.path.startswith(prefix) and url.path.endswith(suffix):
            t = state.threads.get(url.path[len(prefix):-len(suffix)])
            if t is None:
                self._json(404, {"error": "no such thread"})
                return
            self._json(200, state.bounded(t))
            return
        self._json(404, {"error": "not found"})

    # ---- WebSocket RPC -------------------------------------------------------
    def _websocket(self, url) -> None:
        if parse_qs(url.query).get("orchestrationProtocol") != ["2"]:
            self._json(426, {"code": "orchestration_protocol_incompatible"})
            return
        if not self._auth():
            self._json(401, {"_tag": "EnvironmentAuthInvalidError", "code": "auth_invalid"})
            return
        accept = base64.b64encode(hashlib.sha1(
            (self.headers["Sec-WebSocket-Key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.close_connection = True
        while True:
            frame = self._ws_read()
            if frame is None:
                return
            msg = json.loads(frame)
            if msg.get("_tag") != "Request":
                continue
            try:
                exit_ = {"_tag": "Success", "value": self.server.state.rpc(msg["tag"], msg["payload"])}
            except Reject as e:
                exit_ = {"_tag": "Failure", "cause": e.cause}
            except (KeyError, TypeError) as e:
                exit_ = {"_tag": "Failure", "cause": [{"_tag": "Die", "defect": f"bad payload: {e}"}]}
            self._ws_send(json.dumps({"_tag": "Exit", "requestId": msg["id"], "exit": exit_}))

    def _ws_read(self):
        head = self.rfile.read(2)
        if len(head) < 2:
            return None
        opcode, n = head[0] & 0x0F, head[1] & 0x7F
        if n == 126:
            n = struct.unpack(">H", self.rfile.read(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self.rfile.read(8))[0]
        mask = self.rfile.read(4)
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(self.rfile.read(n)))
        return None if opcode == 8 else data.decode()

    def _ws_send(self, text: str) -> None:
        data = text.encode()
        n = len(data)
        hdr = bytes([0x81]) + (bytes([n]) if n < 126 else
                               bytes([126]) + struct.pack(">H", n) if n < 65536 else
                               bytes([127]) + struct.pack(">Q", n))
        self.wfile.write(hdr + data)
        self.wfile.flush()

    def log_message(self, *args) -> None:  # quiet
        return


class Server(ThreadingHTTPServer):
    daemon_threads = True
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
