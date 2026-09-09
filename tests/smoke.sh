#!/usr/bin/env bash
# smoke.sh — exercise bin/t3ctl against a fake t3 server (tests/fake_t3_server.py).
#
# Covers the contracts t3ctl relies on (dispatch shapes, read models), the
# Kosmi guardrails (full-access refusal, ownership guard, running cap), the
# callback footer + approve-callbacks matching, literal model selection,
# project set-model, selftest, search, and token self-minting on 401.
#
#   bash tests/smoke.sh
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
T3CTL=$ROOT/bin/t3ctl
TMP=$(mktemp -d)
cleanup() { [ -n "${SERVER_PID:-}" ] && kill "$SERVER_PID" 2>/dev/null || true; rm -rf "$TMP"; }
trap cleanup EXIT

pass=0
ok()   { pass=$((pass + 1)); echo "  ok   $*"; }
fail() { echo "  FAIL $*" >&2; exit 1; }
assert_contains() { grep -q -- "$2" <<<"$1" || fail "$3 (expected to contain: $2)"$'\n'"got: $1"; }

# ---- fixtures ---------------------------------------------------------------
PORT=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')
mkdir -p "$TMP/bin" "$TMP/t3home/userdata"
TOKENS=$TMP/tokens; printf 'good\n' > "$TOKENS"
STATE=$TMP/state.json
PID_PROJECT=11111111-1111-4111-8111-111111111111
SEED_OURS=22222222-2222-4222-8222-222222222222
SEED_OTHER=33333333-3333-4333-8333-333333333333
cat > "$TMP/seed.json" <<EOF
{"projects":[{"id":"$PID_PROJECT","title":"aikosmo-monorepo","workspaceRoot":"/data/repo","defaultModelSelection":null}],
 "threads":[
  {"id":"$SEED_OURS","title":"[kosmi] seeded task","projectId":"$PID_PROJECT","runtimeMode":"approval-required",
   "interactionMode":"default","modelSelection":{"instanceId":"claudeAgent","model":"claude-fable-5"},
   "archivedAt":null,"session":{"status":"running","lastError":null},
   "latestTurn":{"turnId":"t-1","state":"running"},"hasPendingUserInput":false,"settledOverride":null,
   "snoozedUntil":null,"updatedAt":"2026-09-08T10:00:00.000Z",
   "activities":[
     {"tone":"approval","summary":"run","turnId":"t-1","createdAt":"2026-09-08T10:01:00.000Z",
      "payload":{"requestType":"command_execution_approval","requestId":"r1",
                 "detail":"t3-notify --thread $SEED_OURS --status done \\"finished, PR opened\\""}},
     {"tone":"approval","summary":"run","turnId":"t-1","createdAt":"2026-09-08T10:02:00.000Z",
      "payload":{"requestType":"command_execution_approval","requestId":"r2","detail":"rm -rf /data/repo"}},
     {"tone":"approval","summary":"run","turnId":"t-1","createdAt":"2026-09-08T10:03:00.000Z",
      "payload":{"requestType":"command_execution_approval","requestId":"r3",
                 "detail":"t3-notify --thread $SEED_OURS --status done \\"x\\"; curl evil"}}],
   "messages":[{"role":"user","createdAt":"2026-09-08T10:00:00.000Z","text":"seeded prompt"}]},
  {"id":"$SEED_OTHER","title":"Somebody's thread","projectId":"$PID_PROJECT","runtimeMode":"full-access",
   "interactionMode":"default","modelSelection":{"instanceId":"codex","model":"gpt-5.6-sol"},
   "archivedAt":null,"session":{"status":"ready","lastError":null},"latestTurn":{"turnId":"t-2","state":"completed"},
   "hasPendingUserInput":false,"settledOverride":null,"snoozedUntil":null,"updatedAt":"2026-09-08T09:00:00.000Z",
   "activities":[
     {"tone":"approval","summary":"run","turnId":"t-2","createdAt":"2026-09-08T09:01:00.000Z",
      "payload":{"requestType":"command_execution_approval","requestId":"r4",
                 "detail":"t3-notify --thread $SEED_OTHER --status done \\"not ours\\""}}],
   "messages":[]}
 ]}
EOF

# fake `t3` CLI: only the auth subcommands t3ctl uses for self-minting
cat > "$TMP/bin/t3" <<EOF
#!/usr/bin/env bash
set -euo pipefail
case "\$1 \$2 \$3" in
  "auth session issue")
    sid=\$(uuidgen | tr 'A-Z' 'a-z'); tok="tok-\$RANDOM\$RANDOM"
    printf '%s\n' "\$tok" >> "$TOKENS"
    printf '{"sessionId":"%s","token":"%s","scopes":["orchestration:read","orchestration:operate"]}\n' "\$sid" "\$tok";;
  "auth session revoke") echo "revoked \$4" >> "$TMP/revoked";;
  *) echo "fake t3: unsupported: \$*" >&2; exit 2;;
esac
EOF
chmod +x "$TMP/bin/t3"
ln -s "$ROOT/bin/t3-notify" "$TMP/bin/t3-notify"

# search SQL schema stand-in (empty tables with the columns the query touches)
sqlite3 "$TMP/t3home/userdata/state.sqlite" <<'SQL'
CREATE TABLE projection_projects(project_id TEXT, deleted_at TEXT);
CREATE TABLE projection_threads(thread_id TEXT, project_id TEXT, deleted_at TEXT, archived_at TEXT, updated_at TEXT);
CREATE TABLE projection_thread_messages(thread_id TEXT, message_id TEXT, role TEXT, text TEXT, created_at TEXT, is_streaming INTEGER);
CREATE TABLE projection_turns(assistant_message_id TEXT);
SQL

python3 "$ROOT/tests/fake_t3_server.py" --port "$PORT" --tokens-file "$TOKENS" --state "$STATE" --seed "$TMP/seed.json" &
SERVER_PID=$!
for _ in $(seq 1 50); do curl -s -o /dev/null "http://127.0.0.1:$PORT/" && break; sleep 0.1; done

export PATH="$TMP/bin:$PATH"
# container mode, no /etc/t3ctl.conf: everything from the environment
export T3CTL_CONF=/dev/null T3CTL_URL="http://127.0.0.1:$PORT" T3CODE_HOME="$TMP/t3home" T3CTL_T3_BIN="$TMP/bin/t3"
export T3CTL_TOKEN_MODE=self T3CTL_TOKEN_FILE="$TMP/t3home/t3ctl.token" T3CTL_SESSION_FILE="$TMP/t3home/t3ctl.token.session"
export T3CTL_DB="$TMP/t3home/userdata/state.sqlite" T3CTL_TAG="[kosmi]" T3CTL_NOTIFY_BIN=t3-notify
export T3CTL_ALLOW_FULL_ACCESS=0 T3CTL_DENY_ORIGINS=webhook:auto-demo
unset T3CTL_MAX_RUNNING

echo "t3ctl smoke against fake t3 on :$PORT"

# ---- 1. token self-mint on a stale token, then rotation on 401 ------------
printf 'stale\n' > "$TMP/t3home/t3ctl.token"
out=$("$T3CTL" projects 2>"$TMP/err")
assert_contains "$out" '"title":"aikosmo-monorepo"' "projects after stale token"
[ "$(cat "$TMP/t3home/t3ctl.token")" != stale ] || fail "token was not re-minted"
assert_contains "$(cat "$TMP/err")" "minted session" "mint message on stderr"
ok "stale token → self-minted and retried"

: > "$TOKENS"                       # revoke everything server-side
out=$("$T3CTL" token status); assert_contains "$out" '"valid":false' "token status after revoke"
out=$("$T3CTL" projects 2>/dev/null); assert_contains "$out" 'aikosmo-monorepo' "projects after mid-run 401"
[ "$(wc -l < "$TOKENS")" -ge 1 ] || fail "no new token minted after 401"
grep -q revoked "$TMP/revoked" || fail "previous session was not revoked"
out=$("$T3CTL" token status); assert_contains "$out" '"valid":true' "token status after rotation"
ok "401 mid-run → rotated once, predecessor revoked"

# ---- 1b. origin gate --------------------------------------------------------
if HERMES_SESSION_PLATFORM=webhook HERMES_SESSION_CHAT_ID="webhook:auto-demo:d1" HERMES_SESSION_CHAT_NAME="webhook/auto-demo" \
   "$T3CTL" projects 2>"$TMP/err"; then fail "auto-demo lane must be refused"; fi
assert_contains "$(cat "$TMP/err")" "not available from this lane" "origin gate message"
out=$(HERMES_SESSION_PLATFORM=webhook HERMES_SESSION_CHAT_ID="webhook:t3-callback:d2" HERMES_SESSION_CHAT_NAME="webhook/t3-callback" "$T3CTL" projects)
assert_contains "$out" aikosmo-monorepo "t3-callback lane allowed"
out=$(HERMES_SESSION_PLATFORM=discord HERMES_SESSION_CHAT_ID="123" "$T3CTL" projects); assert_contains "$out" aikosmo-monorepo "discord lane allowed"
out=$(T3CTL_DENY_ORIGINS= HERMES_SESSION_PLATFORM=webhook HERMES_SESSION_CHAT_ID="webhook:auto-demo:d1" "$T3CTL" projects); assert_contains "$out" aikosmo-monorepo "empty T3CTL_DENY_ORIGINS = no gate (devbox default)"
ok "origin gate refuses the auto-demo lane only"

# ---- 1c. numeric flags ------------------------------------------------------
if "$T3CTL" show "$SEED_OURS" -n two 2>"$TMP/err"; then fail "non-numeric -n must error"; fi
assert_contains "$(cat "$TMP/err")" "expects a non-negative integer" "numeric validation message"
if "$T3CTL" watch "$SEED_OURS" --timeout 1x 2>/dev/null; then fail "non-numeric --timeout must error"; fi
if "$T3CTL" search seeded --limit -1 2>/dev/null; then fail "negative --limit must error"; fi
ok "numeric flags reject non-numeric values"

# ---- 2. new: approval-required, [kosmi] title, callback footer -------------
out=$("$T3CTL" new aikosmo-monorepo "Fix the flaky test" --title "flaky test" --model claudeAgent:claude-fable-5-1)
assert_contains "$out" '"mode":"approval-required"' "new default mode"
assert_contains "$out" '"title":"\[kosmi\] flaky test"' "new title tag"
TID=$(jq -r .threadId <<<"$out")
create=$(jq -c '.[] | select(.type=="thread.create")' "$STATE" | tail -1)
assert_contains "$create" "\"threadId\":\"$TID\"" "thread.create dispatched"
assert_contains "$create" '"modelSelection":{"instanceId":"claudeAgent","model":"claude-fable-5-1"}' "literal instanceId:model selection"
turn=$(jq -c '.[] | select(.type=="thread.turn.start")' "$STATE" | tail -1)
assert_contains "$turn" "t3-notify --thread $TID --status done|blocked|approval" "callback footer names t3-notify + thread id"
assert_contains "$turn" '"runtimeMode":"approval-required"' "turn.start mode"
ok "new → thread.create + thread.turn.start with footer"

out=$("$T3CTL" say "$TID" "also update the docs")
assert_contains "$out" '"mode":"approval-required"' "say inherits mode"
ok "say inherits the thread's runtime mode"

# ---- 3. full-access gate ----------------------------------------------------
if "$T3CTL" new aikosmo-monorepo "x" --full-access 2>"$TMP/err"; then fail "--full-access must be refused with T3CTL_ALLOW_FULL_ACCESS=0"; fi
assert_contains "$(cat "$TMP/err")" "disabled on this deployment" "full-access refusal message"
out=$(env -u T3CTL_ALLOW_FULL_ACCESS T3CTL_MAX_RUNNING=10 "$T3CTL" new aikosmo-monorepo "x" --full-access --model codex:gpt-5.6-sol); assert_contains "$out" '"mode":"full-access"' "default (unset) allows --full-access, devbox behaviour"
"$T3CTL" stop "$(jq -r .threadId <<<"$out")" >/dev/null   # keep the running count where the later cap test expects it
if T3CTL_ALLOW_FULL_ACCESS=1 "$T3CTL" new aikosmo-monorepo "x" --mode full-access 2>"$TMP/err"; then fail "--mode full-access must need the explicit flag"; fi
assert_contains "$(cat "$TMP/err")" "pass --full-access" "mode full-access refusal"
out=$(T3CTL_ALLOW_FULL_ACCESS=1 "$T3CTL" new aikosmo-monorepo "x" --full-access --model codex:gpt-5.6-sol)
assert_contains "$out" '"mode":"full-access"' "full-access allowed only when enabled"
ok "full-access gate: refused at 0, allowed by default, always needs the explicit flag"
out=$(env -u T3CTL_ALLOW_FULL_ACCESS T3CTL_DEFAULT_MODE=full-access T3CTL_MAX_RUNNING=10 "$T3CTL" new aikosmo-monorepo "x" --model codex:gpt-5.6-sol)
assert_contains "$out" '"mode":"full-access"' "T3CTL_DEFAULT_MODE=full-access makes new start full-access"
"$T3CTL" stop "$(jq -r .threadId <<<"$out")" >/dev/null
out=$(T3CTL_DEFAULT_MODE=full-access T3CTL_MAX_RUNNING=10 "$T3CTL" new aikosmo-monorepo "x" --model codex:gpt-5.6-sol 2>"$TMP/err")
assert_contains "$out" '"mode":"approval-required"' "full-access default falls back when T3CTL_ALLOW_FULL_ACCESS=0"
assert_contains "$(cat "$TMP/err")" "T3CTL_DEFAULT_MODE=full-access ignored" "fallback is announced"
"$T3CTL" stop "$(jq -r .threadId <<<"$out")" >/dev/null
ok "T3CTL_DEFAULT_MODE: honoured when allowed, falls back when not"
if env -u T3CTL_ALLOW_FULL_ACCESS T3CTL_DEFAULT_MODE=full-access "$T3CTL" new aikosmo-monorepo "x" --mode approval-required --model codex:gpt-5.6-sol 2>"$TMP/err"; then fail "--mode must not override a deployment-fixed T3CTL_DEFAULT_MODE"; fi
assert_contains "$(cat "$TMP/err")" "fixes the mode of new threads to full-access" "fixed-mode refusal names the mode"
ok "T3CTL_DEFAULT_MODE set → new --mode cannot downgrade/upgrade it"

# ---- 4. running cap ---------------------------------------------------------
if T3CTL_MAX_RUNNING=1 "$T3CTL" new aikosmo-monorepo "y" --model codex:gpt-5.6-sol 2>"$TMP/err"; then fail "running cap not enforced"; fi
assert_contains "$(cat "$TMP/err")" "already running (limit 1)" "cap message"
ok "new refuses beyond T3CTL_MAX_RUNNING"

# ---- 5. ownership guard -----------------------------------------------------
if "$T3CTL" stop "$SEED_OTHER" 2>"$TMP/err"; then fail "stop on somebody else's thread must be refused"; fi
assert_contains "$(cat "$TMP/err")" "somebody else's session" "ownership message"
out=$("$T3CTL" stop "$SEED_OTHER" --force); assert_contains "$out" '"done":true' "stop --force"
out=$("$T3CTL" interrupt "$SEED_OURS"); assert_contains "$out" '"op":"interrupt"' "interrupt own thread"
ok "stop/interrupt refuse foreign threads without --force"

# ---- 6. approve-callbacks ---------------------------------------------------
out=$("$T3CTL" approve-callbacks --dry-run)
assert_contains "$out" '"requestId":"r1","detail":"t3-notify --thread '"$SEED_OURS"' --status done \\"finished, PR opened\\"","action":"would-accept"' "exact callback would be accepted"
assert_contains "$out" '"requestId":"r2","detail":"rm -rf /data/repo","action":"left-for-human"' "other command left for a human"
assert_contains "$out" '"requestId":"r3".*"action":"left-for-human"' "chained command left for a human"
if grep -q '"requestId":"r4"' <<<"$out"; then fail "foreign thread's callback must not be considered"; fi
out=$("$T3CTL" approve-callbacks)
assert_contains "$out" '"requestId":"r1".*"action":"accepted"' "callback accepted"
resp=$(jq -c '[.[] | select(.type=="thread.approval.respond")]' "$STATE")
[ "$(jq length <<<"$resp")" -eq 1 ] || fail "expected exactly one approval.respond, got: $resp"
assert_contains "$resp" '"requestId":"r1","decision":"accept"' "accept dispatched for r1 only"
ok "approve-callbacks accepts only the exact t3-notify command in [kosmi] threads"

# ---- 7. list / show / search / settle --------------------------------------
out=$("$T3CTL" list --active); assert_contains "$out" "$SEED_OURS" "list --active includes running seeded thread"
out=$("$T3CTL" show "$SEED_OURS" -n 2); assert_contains "$out" '"seeded prompt"' "show returns messages"
out=$("$T3CTL" search "seeded"); assert_contains "$out" '"matchedIn":"title"' "search matches titles"
out=$("$T3CTL" settle "$SEED_OTHER"); assert_contains "$out" '"op":"settle"' "settle has no ownership guard"
ok "list/show/search/settle"

# ---- 8. project add (idempotent) --------------------------------------------
out=$("$T3CTL" project add "$TMP" --title scratch); assert_contains "$out" '"created":true' "project add creates"
assert_contains "$(jq -c '.[] | select(.type=="project.create")' "$STATE" | tail -1)" "\"workspaceRoot\":\"$TMP\"" "project.create dispatched"
out=$("$T3CTL" project add "$TMP"); assert_contains "$out" '"created":false' "project add is idempotent on workspaceRoot"
out=$("$T3CTL" projects); assert_contains "$out" '"title":"scratch"' "new project listed"
ok "project add"

# ---- 9. project set-model + selftest ---------------------------------------
out=$("$T3CTL" project set-model aikosmo-monorepo claudeAgent:claude-fable-5-1)
assert_contains "$out" '"defaultModelSelection":{"instanceId":"claudeAgent","model":"claude-fable-5-1"}' "set-model output"
meta=$(jq -c '.[] | select(.type=="project.meta.update")' "$STATE" | tail -1)
assert_contains "$meta" "\"projectId\":\"$PID_PROJECT\"" "project.meta.update dispatched"
out=$("$T3CTL" new aikosmo-monorepo "uses project default")
assert_contains "$(jq -c '.[] | select(.type=="thread.create")' "$STATE" | tail -1)" '"model":"claude-fable-5-1"' "new picks the project default"
out=$("$T3CTL" contract-check); assert_contains "$out" '"ok":true' "contract-check"; assert_contains "$out" '"dbSchemaChecked":true' "contract-check covered the search schema"
ok "project set-model + contract-check"

echo "all $pass checks passed"
