"""Fake of T3's orchestration MCP endpoint and its OAuth sign-in, over fake_t3_server.State.

Mirrors what bin/t3-mcp and t3ctl rely on, as observed on t3
0.1.6-nightly.20261030.3917 (docs/COMPAT.md):

  POST /mcp      streamable HTTP JSON-RPC: initialize (mcp-session-id header),
                 notifications/initialized (202), tools/list, tools/call.
                 After initialize, a request without mcp-session-id or
                 mcp-protocol-version is a 400. Only tokens listed in the MCP
                 tokens file ("<token> <access>") are accepted; anything else
                 (an ordinary session) is a 401 invalid_mcp_credential.
                 Tool refusals are results with isError and an
                 OrchestratorMcpFailure {code, message} text, as T3 sends them.
  DELETE /mcp    ends the session (204)
  OAuth          /.well-known/oauth-authorization-server, /oauth/mcp/register,
                 /oauth/mcp/decision (pairing-code approval), /oauth/mcp/token (PKCE)

Each MCP token carries its access level, which is the runtime-mode ceiling T3
enforces: launching, messaging or organizing a thread in a broader mode is
runtime_mode_escalation_denied. clientRequestId derives stable ids within one
credential (the namespace), so retries return the same thread/message.
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import secrets
import threading
import time
import uuid
from urllib.parse import parse_qs, urlencode

ACTIVE = ("preparing", "queued", "starting", "running", "waiting")
TERMINAL = ("completed", "failed", "cancelled", "interrupted", "rolled_back")
RANK = {"read-only": -1, "approval-required": 0, "auto-accept-edits": 1, "auto": 2, "full-access": 3}
TOOLS = ("t3_project_list", "t3_thread_list", "t3_thread_read", "t3_thread_search", "t3_thread_launch",
         "t3_thread_send", "t3_thread_wait", "t3_thread_configure", "t3_thread_organize",
         "t3_thread_interrupt", "t3_pending_request", "orchestrator_capabilities")
WRITES = {"t3_thread_launch", "t3_thread_send", "t3_thread_organize", "t3_thread_interrupt", "t3_thread_configure"}
PROTOCOL = "2025-06-18"


class Fail(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def scopes_for(access):
    if access == "read-only":
        return ["orchestration:read"]
    if access == "full-access":
        return ["orchestration:read", "orchestration:operate", "settings:write", "source-control:write"]
    return ["orchestration:read", "orchestration:operate"]


def mcp_tokens(server):
    try:
        lines = Path(server.mcp_tokens_file).read_text().splitlines()
    except (FileNotFoundError, AttributeError, TypeError):
        return {}
    out = {}
    for line in lines:
        parts = line.split()
        if parts:
            out[parts[0]] = parts[1] if len(parts) > 1 else "approval-required"
    return out


# ---- views over State -------------------------------------------------------

def _status(t):
    last = t["runs"][-1] if t["runs"] else None
    active = next((r for r in reversed(t["runs"]) if r["status"] in ACTIVE), None)
    return (active or last or {}).get("status", "idle"), last, active


def _items(t):
    items = []
    for m in t["messages"]:
        items.append({"type": "user_message" if m["role"] == "user" else "assistant_message",
                      "messageId": m.get("id"), "runId": m.get("runId"), "text": m["text"],
                      "updatedAt": m.get("createdAt"), "itemId": f"item:{m.get('id')}"})
    for r in t["requests"]:
        if r["kind"] == "user_input":
            items.append({"type": "user_input_request", "runId": r.get("runId"), "messageId": None,
                          "text": json.dumps(r.get("questions", [])), "itemId": f"item:{r['id']}", "updatedAt": None})
        else:
            items.append({"type": "command_execution", "runId": r.get("runId"), "messageId": None,
                          "text": f"$ {r.get('command', '')}", "itemId": f"item:cmd:{r['id']}", "updatedAt": None})
            items.append({"type": "approval_request", "runId": r.get("runId"), "messageId": None,
                          "text": "Run a command", "itemId": f"item:{r['id']}", "updatedAt": None})
    _, last, _ = _status(t)
    if t.get("lastError") and last is not None:
        items.append({"type": "error", "runId": last["id"], "messageId": None, "text": t["lastError"],
                      "itemId": f"item:error:{last['id']}", "updatedAt": None, "title": "Provider error"})
    for i, item in enumerate(items):
        item.update(position=i, status="completed", visibility="local", textTruncated=False, nextTextOffset=None)
    return items


def list_item(t):
    status, last, _ = _status(t)
    sel = t.get("modelSelection") or {}
    return {"threadId": t["id"], "projectId": t["projectId"], "title": t["title"], "link": f"http://fake/{t['id']}",
            "createdBy": "user", "creationSource": "web", "status": status,
            "latestRunId": last["id"] if last else None,
            "providerInstanceId": sel.get("instanceId"), "model": sel.get("model"),
            "runtimeMode": t["runtimeMode"], "interactionMode": t["interactionMode"],
            "settled": t["settledOverride"] == "settled", "settledAt": None,
            "snoozed": bool(t.get("snoozedUntil")), "snoozedUntil": t.get("snoozedUntil"),
            "parentThreadId": None, "relationshipToParent": None, "itemCount": len(_items(t)),
            "createdAt": t["updatedAt"], "updatedAt": t["updatedAt"]}


def detail(t):
    status, last, active = _status(t)
    sel = t.get("modelSelection") or {}
    return {"threadId": t["id"], "projectId": t["projectId"], "title": t["title"], "link": f"http://fake/{t['id']}",
            "status": status, "latestRunId": last["id"] if last else None, "activeRunId": active["id"] if active else None,
            "providerInstanceId": sel.get("instanceId"), "model": sel.get("model"),
            "runtimeMode": t["runtimeMode"], "interactionMode": t["interactionMode"],
            "runCount": len(t["runs"]), "itemCount": len(_items(t)),
            "pendingRequestCount": sum(1 for r in t["requests"] if r["status"] == "pending"),
            "archived": t["archivedAt"] is not None, "settled": t["settledOverride"] == "settled",
            "snoozed": bool(t.get("snoozedUntil")), "snoozedUntil": t.get("snoozedUntil"),
            "createdAt": t["updatedAt"], "updatedAt": t["updatedAt"]}


def _thread(state, tid):
    t = state.threads.get(tid)
    if t is None:
        raise Fail("thread_not_found", f"Thread {tid} is no longer available.")
    return t


def _within(access, t):
    if RANK[t["runtimeMode"]] > RANK.get(access, 0):
        raise Fail("runtime_mode_escalation_denied",
                   f"Child runtime mode {t['runtimeMode']} is broader than parent mode {access}.")


def call(state, name, args, access, namespace):
    """One tool call; returns the structured result or raises Fail."""
    if name in WRITES and access == "read-only":
        raise Fail("capability_denied", "This credential can only read.")
    with state.lock:
        return _call(state, name, args, access, namespace)


def _call(state, name, args, access, namespace):
    if name == "t3_project_list":
        projects = [{"deletedAt": None, **p} for p in state.projects]
        cursor, limit = args.get("cursor", 0), args.get("limit", 50)
        page = projects[cursor:cursor + limit]
        return {"projects": page, "nextCursor": cursor + len(page) if cursor + len(page) < len(projects) else None}

    if name == "t3_thread_list":
        rows = [t for t in state.threads.values() if t["archivedAt"] is None]
        if not args.get("allProjects"):
            if "projectId" not in args:
                raise Fail("target_required", "Pass projectId: this MCP client is not running inside a T3 thread.")
            rows = [t for t in rows if t["projectId"] == args["projectId"]]
        items = [list_item(t) for t in rows]
        if args.get("statuses") is not None:
            items = [i for i in items if i["status"] in args["statuses"]]
        if args.get("settled") is not None:
            items = [i for i in items if i["settled"] == args["settled"]]
        if args.get("titleContains"):
            items = [i for i in items if args["titleContains"].lower() in i["title"].lower()]
        items.sort(key=lambda i: i["updatedAt"], reverse=True)
        cursor, limit = args.get("cursor", 0), args.get("limit", 20)
        page = items[cursor:cursor + limit]
        return {"projectId": None, "currentThreadId": None, "threads": page, "total": len(items),
                "nextCursor": cursor + len(page) if cursor + len(page) < len(items) else None}

    if name == "t3_thread_read":
        t = _thread(state, args["threadId"])
        cap = args.get("maxCharsPerItem", 4000)
        items = _items(t)
        if args.get("itemId"):
            item = next((dict(i) for i in items if i["itemId"] == args["itemId"]), None)
            if item is None:
                raise Fail("invalid_request", "The item was not found.")
            pages = [item]
            offset = args.get("textOffset", 0)
            item["text"] = item["text"][offset:]
            base = offset
        else:
            view = args.get("view", "messages")
            if view == "messages":
                items = [i for i in items if i["type"] in ("user_message", "assistant_message")]
            after = args.get("afterPosition", -1)
            items = [dict(i) for i in items if i["position"] > after]
            limit = args.get("limit", 20)
            pages, base = items[:limit], 0
        for i in pages:
            text = i.get("text") or ""
            if len(text) > cap:
                i["text"], i["textTruncated"], i["nextTextOffset"] = text[:cap], True, base + cap
        runs = [{"runId": r["id"], "ordinal": n + 1, "status": r["status"],
                 "startedAt": None if r.get("neverStarted") else "2026-09-08T10:00:00.000Z"} for n, r in enumerate(t["runs"])]
        more = not args.get("itemId") and len(items) > len(pages)
        return {"thread": detail(t), "recentRuns": runs[-args.get("runLimit", 5):],
                "items": pages, "nextPosition": pages[-1]["position"] if pages else None, "hasMore": more}

    if name == "t3_thread_search":
        q = args["query"].lower()
        live = [t for t in state.threads.values() if t["archivedAt"] is None]
        out = [{"threadId": t["id"], "projectId": t["projectId"], "title": t["title"], "source": "title",
                "snippet": t["title"], "messageCreatedAt": None} for t in live if q in t["title"].lower()]
        seen = {m["threadId"] for m in out}
        for t in live:
            for m in t["messages"]:
                if q in m["text"].lower() and t["id"] not in seen:
                    seen.add(t["id"])
                    out.append({"threadId": t["id"], "projectId": t["projectId"], "title": t["title"],
                                "source": m["role"], "snippet": m["text"][:240], "messageCreatedAt": m.get("createdAt")})
        return {"matches": out[:args.get("limit", 20)]}

    if name == "t3_thread_launch":
        mode = args.get("runtimeMode", "approval-required")
        if RANK[mode] > RANK.get(access, 0):
            raise Fail("runtime_mode_escalation_denied", f"Child runtime mode {mode} is broader than parent mode {access}.")
        key = args.get("clientRequestId") or str(uuid.uuid4())
        tid = "mcp:launch:" + hashlib.sha256(f"{namespace}:{key}".encode()).hexdigest()
        if tid not in state.threads:
            state.log({"type": "mcp.t3_thread_launch", **args})
            state.sequence += 1
            state.threads[tid] = state.new_thread({
                "id": tid, "title": args["title"], "projectId": args["projectId"], "runtimeMode": mode,
                "interactionMode": args.get("interactionMode", "default"),
                "modelSelection": args.get("modelSelection")})
        t = state.threads[tid]
        return {"threadId": tid, "link": f"http://fake/{tid}", "projectId": t["projectId"],
                "modelSelection": t["modelSelection"], "runId": None, "status": "idle"}

    if name == "t3_thread_send":
        t = _thread(state, args["threadId"])
        _within(access, t)
        if t["archivedAt"] is not None:
            raise Fail("thread_not_sendable", "The thread is archived.")
        key = args.get("clientRequestId") or str(uuid.uuid4())
        mid = f"message:mcp:client%3A{namespace}:thread-send:{key}"
        existing = next((m for m in t["messages"] if m.get("id") == mid), None)
        if existing is None:
            state.log({"type": "mcp.t3_thread_send", **args})
            state.sequence += 1
            busy = any(r["status"] in ACTIVE for r in t["runs"])
            run = {"id": f"run-{uuid.uuid4()}", "status": "queued" if busy else "running", "userMessageId": mid}
            t["runs"].append(run)
            t["messages"].append({"id": mid, "role": "user", "text": args["message"], "createdAt": state.now(), "runId": run["id"]})
            t["updatedAt"] = state.now()
            state.on_run(t, run, mid)
        else:
            run = next(r for r in t["runs"] if r["id"] == existing["runId"])
        return {"threadId": t["id"], "link": f"http://fake/{t['id']}", "messageId": mid, "runId": run["id"],
                "status": run["status"], "delivery": "queued" if run["status"] == "queued" else "started"}

    if name == "t3_thread_wait":
        deadline = time.time() + args.get("timeoutMs", 0) / 1000
        tid = args["threadId"]
        while True:
            t = _thread(state, tid)
            run = next((r for r in t["runs"] if r["id"] == args.get("runId")), None) if args.get("runId") else (t["runs"] or [None])[-1]
            if run is None:
                if args.get("runId"):
                    raise Fail("run_not_found", "The run was not found.")
                return {"threadId": tid, "runId": None, "status": "idle", "timedOut": False}
            if run["status"] in TERMINAL or time.time() >= deadline:
                return {"threadId": tid, "runId": run["id"], "status": run["status"], "timedOut": run["status"] not in TERMINAL}
            state.lock.release()
            try:
                time.sleep(0.05)
            finally:
                state.lock.acquire()

    if name == "t3_thread_configure":
        t = _thread(state, args["threadId"])
        if args.get("modelSelection"):
            _within(access, t)
            state.log({"type": "mcp.t3_thread_configure", **args})
            t["modelSelection"] = args["modelSelection"]
        return {"threadId": t["id"], "modelSelection": t["modelSelection"],
                "runtimeMode": t["runtimeMode"], "interactionMode": t["interactionMode"]}

    if name == "t3_thread_organize":
        t = _thread(state, args["threadId"])
        _within(access, t)
        action = args["action"]
        if action == "settle" and any(r["status"] in ACTIVE for r in t["runs"]):
            raise Fail("orchestration_error", "A running thread cannot be settled.")
        state.log({"type": "mcp.t3_thread_organize", **args})
        state.sequence += 1
        if action == "settle":
            t["settledOverride"] = "settled"
        elif action == "unsettle":
            t["settledOverride"] = None
        return {"sequence": state.sequence}

    if name == "t3_thread_interrupt":
        t = _thread(state, args["threadId"])
        _within(access, t)
        run = next((r for r in reversed(t["runs"]) if r["status"] in ACTIVE and r["id"] == args.get("runId", r["id"])), None)
        if run is None:
            return {"threadId": t["id"], "runId": None, "status": "no_active_run"}
        state.log({"type": "mcp.t3_thread_interrupt", **args})
        run["status"] = "interrupted"
        return {"threadId": t["id"], "runId": run["id"], "status": "interrupt_requested"}

    if name == "t3_pending_request":
        t = _thread(state, args["threadId"])
        questions = [r for r in t["requests"] if r["kind"] == "user_input"]
        if args["action"] == "list":
            return {"requestIds": [r["id"] for r in questions if r["status"] == "pending"]}
        req = next((r for r in questions if r["id"] == args.get("requestId")), None)
        if req is None:
            raise Fail("invalid_request", "The pending user-input request was not found.")
        if req["status"] != "pending":
            raise Fail("invalid_request", "The user-input request was already answered.")
        if args["action"] == "read":
            return {"requestId": req["id"], "questions": req.get("questions", [])}
        if access == "read-only":
            raise Fail("capability_denied", "This credential can only read.")
        state.log({"type": "mcp.t3_pending_request", **args})
        state.sequence += 1
        req["status"], req["answers"] = "resolved", args["answers"]
        return {"sequence": state.sequence}

    if name == "orchestrator_capabilities":
        return {"runtimeMode": access, "interactionMode": "default", "providers": []}
    raise Fail("invalid_request", f"unknown tool {name}")


# ---- HTTP -------------------------------------------------------------------

def handle_mcp(handler):
    """POST /mcp for fake_t3_server.Handler."""
    server = handler.server
    header = handler.headers.get("Authorization", "")
    token = header[len("Bearer "):].strip() if header.startswith("Bearer ") else ""
    access = mcp_tokens(server).get(token)
    body = handler.rfile.read(int(handler.headers.get("Content-Length") or 0))
    if access is None:
        return handler._json(401, {"error": "invalid_mcp_credential", "message": "A valid T3 Code MCP credential is required."})
    if handler.command == "DELETE":
        server.mcp_sessions.discard(handler.headers.get("mcp-session-id"))
        handler.send_response(204)
        handler.send_header("Content-Length", "0")
        handler.end_headers()
        return
    try:
        msg = json.loads(body)
    except ValueError:
        return handler._json(400, {"error": "parse error"})
    method = msg.get("method")
    if method != "initialize":
        if handler.headers.get("mcp-session-id") not in server.mcp_sessions:
            return handler._json(400, {"error": "missing or unknown mcp-session-id"})
        if handler.headers.get("mcp-protocol-version") != PROTOCOL:
            return handler._json(400, {"error": "mcp-protocol-version header required"})
    server.mcp_calls.append(method if method != "tools/call" else msg["params"]["name"])
    if "id" not in msg:   # notification
        handler.send_response(202)
        handler.send_header("Content-Length", "0")
        handler.end_headers()
        return
    session = None
    if method == "initialize":
        session = str(uuid.uuid4())
        server.mcp_sessions.add(session)
        result = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {"listChanged": True}},
                  "serverInfo": {"name": "T3 Code", "version": "fake"}}
    elif method == "tools/list":
        missing = set(getattr(server, "mcp_missing", ()) or ())
        result = {"tools": [{"name": n, "inputSchema": {"type": "object"}} for n in TOOLS if n not in missing]}
    elif method == "tools/call":
        name, args = msg["params"]["name"], msg["params"].get("arguments", {})
        try:
            structured = call(server.state, name, args, access, hashlib.sha256(token.encode()).hexdigest()[:12])
            result = {"content": [{"type": "text", "text": json.dumps(structured)}], "structuredContent": structured}
            if name == getattr(server, "mcp_revoke_after", None):
                # test knob: the call was accepted, then the credential is revoked and the reply lost
                server.mcp_revoke_after = None
                path = Path(server.mcp_tokens_file)
                path.write_text("".join(l + "\n" for l in path.read_text().splitlines() if l.split()[:1] != [token]))
                return handler._json(401, {"error": "invalid_mcp_credential", "message": "revoked"})
        except Fail as e:
            result = {"content": [{"type": "text", "text": json.dumps(
                {"_tag": "OrchestratorMcpFailure", "code": e.code, "message": e.message})}], "isError": True}
    else:
        return _reply(handler, {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "Method not found"}}, session)
    _reply(handler, {"jsonrpc": "2.0", "id": msg["id"], "result": result}, session)


def _reply(handler, payload, session):
    sse = getattr(handler.server, "mcp_sse", False)
    body = (f"event: message\ndata: {json.dumps(payload)}\n\n" if sse else json.dumps(payload)).encode()
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream" if sse else "application/json")
    handler.send_header("Content-Length", str(len(body)))
    if session:
        handler.send_header("mcp-session-id", session)
    handler.end_headers()
    handler.wfile.write(body)


def handle_oauth(handler, path):
    """The pairing-code sign-in t3ctl uses to mint an MCP credential with a ceiling."""
    server = handler.server
    base = f"http://127.0.0.1:{server.server_port}"
    if handler.command == "GET":
        return handler._json(200, {"issuer": base, "authorization_endpoint": f"{base}/oauth/mcp/authorize",
                                   "token_endpoint": f"{base}/oauth/mcp/token",
                                   "registration_endpoint": f"{base}/oauth/mcp/register",
                                   "response_types_supported": ["code"], "grant_types_supported": ["authorization_code"],
                                   "code_challenge_methods_supported": ["S256"],
                                   "token_endpoint_auth_methods_supported": ["none"],
                                   "scopes_supported": ["orchestration:read", "orchestration:operate"]})
    raw = handler.rfile.read(int(handler.headers.get("Content-Length") or 0)).decode()
    auth_dir = Path(server.auth_dir)
    if path == "/oauth/mcp/register":
        p = json.loads(raw)
        cid = "client-" + secrets.token_hex(8)
        server.oauth_clients[cid] = p
        return handler._json(201, {"client_id": cid, "client_name": p.get("client_name", ""), "redirect_uris": p["redirect_uris"],
                                   "grant_types": ["authorization_code"], "response_types": ["code"],
                                   "token_endpoint_auth_method": "none"})
    if path == "/oauth/mcp/decision":
        p = json.loads(raw)
        a, d = p["authorization"], p["decision"]
        pairings = [json.loads(l) for l in (auth_dir / "pairings.jsonl").read_text().splitlines() if l] if (auth_dir / "pairings.jsonl").exists() else []
        match = next((x for x in pairings if x["credential"] == d.get("code") and not x.get("used") and not x.get("revoked")), None)
        if match is None or not set(scopes_for(d["access"])) <= set(match["scopes"]):
            return handler._json(400, {"_tag": "AuthMcpApprovalError", "message": "That pairing code cannot grant this access."})
        match["used"] = True
        (auth_dir / "pairings.jsonl").write_text("".join(json.dumps(x) + "\n" for x in pairings))
        code = secrets.token_urlsafe(16)
        server.oauth_codes[code] = {"client_id": a["client_id"], "challenge": a["code_challenge"],
                                    "redirect_uri": a["redirect_uri"], "access": d["access"]}
        return handler._json(200, {"redirectTo": a["redirect_uri"] + "?" + urlencode({"code": code, "state": a.get("state", ""), "iss": base})})
    if path == "/oauth/mcp/token":
        p = {k: v[0] for k, v in parse_qs(raw).items()}
        pending = server.oauth_codes.pop(p.get("code", ""), None)
        if pending is None or pending["client_id"] != p.get("client_id"):
            return handler._json(400, {"error": "invalid_grant", "error_description": "The authorization code is invalid or expired."})
        verifier_hash = base64.urlsafe_b64encode(hashlib.sha256(p["code_verifier"].encode()).digest()).decode().rstrip("=")
        if verifier_hash != pending["challenge"]:
            return handler._json(400, {"error": "invalid_grant", "error_description": "PKCE verification failed."})
        tok = "mcp-" + secrets.token_hex(12)
        with open(server.mcp_tokens_file, "a") as f:
            f.write(f"{tok} {pending['access']}\n")
        client = server.oauth_clients.get(p["client_id"], {})
        with open(auth_dir / "sessions.jsonl", "a") as f:
            f.write(json.dumps({"sessionId": str(uuid.uuid4()), "subject": "mcp-client", "token": tok,
                                "scopes": scopes_for(pending["access"]),
                                "client": {"label": client.get("client_name", "MCP client"), "deviceType": "bot"}}) + "\n")
        return handler._json(200, {"access_token": tok, "token_type": "Bearer", "expires_in": 2592000,
                                   "scope": " ".join(scopes_for(pending["access"]))})
    return handler._json(404, {"error": "not found"})


_ENSURE = threading.Lock()


def ensure(server):
    """Per-server fake state, created on first use (tests build Server() then set attributes)."""
    if getattr(server, "_mcp_ready", False):
        return
    with _ENSURE:
        if getattr(server, "_mcp_ready", False):
            return
        for attr, default in (("mcp_tokens_file", None), ("auth_dir", None), ("mcp_sse", False), ("mcp_missing", [])):
            if not hasattr(server, attr):
                setattr(server, attr, default)
        server.mcp_sessions = set()
        server.mcp_calls = []
        server.oauth_clients = {}
        server.oauth_codes = {}
        server._mcp_ready = True
