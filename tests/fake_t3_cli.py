#!/usr/bin/env python3
"""Fake `t3` CLI: just the `auth` subcommands t3ctl and t3-token-renew call.

Shares state with tests/fake_t3_server.py through files, so a token it mints
is accepted by the fake server and a revoked session's token stops working:
  FAKE_T3_TOKENS      ordinary sessions' tokens (legacy routes), one per line
  FAKE_T3_MCP_TOKENS  "<token> <access>" lines accepted on /mcp
  FAKE_T3_AUTH_DIR    sessions.jsonl, pairings.jsonl, revoked (an audit log)
"""
import json
import os
from pathlib import Path
import secrets
import sys
import uuid

auth = Path(os.environ["FAKE_T3_AUTH_DIR"])
auth.mkdir(parents=True, exist_ok=True)


def flags(argv):
    out, rest, i = {"scope": []}, [], 0
    while i < len(argv):
        a = argv[i]
        if a in ("--scope", "--subject", "--ttl", "--label"):
            key = a[2:]
            if key == "scope":
                out["scope"].append(argv[i + 1])
            else:
                out[key] = argv[i + 1]
            i += 2
        elif a == "--json":
            i += 1
        else:
            rest.append(a)
            i += 1
    return out, rest


def records(name):
    path = auth / name
    return [json.loads(l) for l in path.read_text().splitlines() if l] if path.exists() else []


def save(name, rows):
    (auth / name).write_text("".join(json.dumps(r) + "\n" for r in rows))


def drop_token(token):
    for env in ("FAKE_T3_TOKENS", "FAKE_T3_MCP_TOKENS"):
        path = Path(os.environ[env])
        if path.exists():
            path.write_text("".join(l + "\n" for l in path.read_text().splitlines() if l.split() and l.split()[0] != token))


def main(argv):
    if os.environ.get("FAKE_T3_FAIL") and " ".join(argv[:3]) in os.environ["FAKE_T3_FAIL"].split(","):
        print("fake t3: forced failure", file=sys.stderr)
        return 1
    f, rest = flags(argv)
    cmd = " ".join(rest[:3])
    with open(auth / "calls", "a") as log:
        log.write(" ".join(argv) + "\n")
    if cmd == "auth session issue":
        scopes = f["scope"] or ["orchestration:read", "orchestration:operate", "access:write", "terminal:operate"]
        sid, tok = str(uuid.uuid4()), "tok-" + secrets.token_hex(8)
        subject = f.get("subject", "cli-issued-session")
        if subject == "mcp-client":
            access = "approval-required" if "orchestration:operate" in scopes else "read-only"
            with open(os.environ["FAKE_T3_MCP_TOKENS"], "a") as t:
                t.write(f"{tok} {access}\n")
        else:
            with open(os.environ["FAKE_T3_TOKENS"], "a") as t:
                t.write(tok + "\n")
        rec = {"sessionId": sid, "subject": subject, "scopes": scopes, "token": tok,
               "client": {"label": f.get("label"), "deviceType": "bot"}}
        save("sessions.jsonl", records("sessions.jsonl") + [rec])
        print(json.dumps({**rec, "method": "bearer-access-token", "expiresAt": "2026-12-01T00:00:00.000Z"}))
        return 0
    if cmd == "auth session list":
        print(json.dumps([{k: v for k, v in r.items() if k != "token"} for r in records("sessions.jsonl")]))
        return 0
    if cmd == "auth session revoke":
        sid = rest[3]
        rows = records("sessions.jsonl")
        for r in rows:
            if r["sessionId"] == sid:
                drop_token(r["token"])
        save("sessions.jsonl", [r for r in rows if r["sessionId"] != sid])
        with open(auth / "revoked", "a") as log:
            log.write(f"revoked {sid}\n")
        return 0
    if cmd == "auth pairing create":
        rec = {"id": str(uuid.uuid4()), "credential": "PAIR-" + secrets.token_hex(8), "scopes": f["scope"],
               **({"unknownToServer": True} if os.environ.get("FAKE_T3_BAD_PAIRING") else {}),
               "label": f.get("label"), "expiresAt": "2026-12-01T00:00:00.000Z"}
        if not rec.pop("unknownToServer", False):
            save("pairings.jsonl", records("pairings.jsonl") + [rec])
        print(json.dumps(rec))
        return 0
    if cmd == "auth pairing revoke":
        rows = records("pairings.jsonl")
        for r in rows:
            if r["id"] == rest[3]:
                r["revoked"] = True
        save("pairings.jsonl", rows)
        with open(auth / "revoked", "a") as log:
            log.write(f"pairing-revoked {rest[3]}\n")
        return 0
    print(f"fake t3: unsupported: {' '.join(argv)}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
