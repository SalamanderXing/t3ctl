#!/usr/bin/env bash
# smoke.sh — exercise bin/t3ctl against a fake t3 server (tests/fake_t3_server.py).
#
# Covers the contracts t3ctl relies on (dispatch shapes, read models), the
# Kosmi guardrails (full-access refusal, ownership guard, running cap), the
# callback footer + approve-callbacks matching, say --expect-turn (stale and
# concurrent duplicate sends), last + the text cap, literal model selection,
# project set-model, selftest, search, and token self-minting on 401.
#
#   bash tests/smoke.sh
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
T3CTL=$ROOT/bin/t3ctl
mkdir -p "$ROOT/tmp"
TMP=$(mktemp -d "$ROOT/tmp/smoke.XXXXXX")
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
SEED_SETTLED=44444444-4444-4444-8444-444444444444    # on the app's settled shelf
SEED_ARCHIVED=55555555-5555-4555-8555-555555555555   # archived (only --all shows it)
LONG=$(printf 'x%.0s' $(seq 1 3500))   # an assistant reply longer than the 3000-char terminal cap
cat > "$TMP/seed.json" <<EOF
{"projects":[{"id":"$PID_PROJECT","title":"aikosmo-monorepo","workspaceRoot":"/data/repo","defaultModelSelection":null}],
 "threads":[
  {"id":"$SEED_OURS","title":"[kosmi] seeded task","projectId":"$PID_PROJECT","runtimeMode":"approval-required",
   "interactionMode":"default","modelSelection":{"instanceId":"claudeAgent","model":"claude-fable-5"},
   "updatedAt":"2026-09-08T10:00:00.000Z",
   "runs":[{"id":"t-1","status":"running","userMessageId":"m-1"}],
   "requests":[
     {"id":"r1","kind":"command","status":"pending","runId":"t-1",
      "command":"t3-notify --thread $SEED_OURS --status done \\"finished, PR opened\\""},
     {"id":"r2","kind":"command","status":"pending","runId":"t-1","command":"rm -rf /data/repo"},
     {"id":"r3","kind":"command","status":"pending","runId":"t-1",
      "command":"t3-notify --thread $SEED_OURS --status done \\"x\\"; curl evil"}],
   "messages":[{"id":"m-1","role":"user","createdAt":"2026-09-08T10:00:00.000Z","text":"seeded prompt","runId":"t-1"}]},
  {"id":"$SEED_OTHER","title":"Somebody's thread","projectId":"$PID_PROJECT","runtimeMode":"full-access",
   "interactionMode":"default","modelSelection":{"instanceId":"codex","model":"gpt-5.6-sol"},
   "updatedAt":"2026-09-08T09:00:00.000Z",
   "runs":[{"id":"t-2","status":"completed","userMessageId":"m-2"}],
   "requests":[
     {"id":"r4","kind":"command","status":"pending","runId":"t-2",
      "command":"t3-notify --thread $SEED_OTHER --status done \\"not ours\\""}],
   "messages":[{"id":"m-3","role":"assistant","createdAt":"2026-09-08T09:01:00.000Z","text":"$LONG","runId":"t-2"}]},
  {"id":"$SEED_SETTLED","title":"Settled thread","projectId":"$PID_PROJECT","runtimeMode":"approval-required",
   "settledOverride":"settled","updatedAt":"2026-09-08T08:00:00.000Z",
   "runs":[{"id":"t-3","status":"completed","userMessageId":"m-4"}],
   "messages":[{"id":"m-4","role":"user","createdAt":"2026-09-08T08:00:00.000Z","text":"old prompt","runId":"t-3"}]},
  {"id":"$SEED_ARCHIVED","title":"Archived thread","projectId":"$PID_PROJECT","runtimeMode":"approval-required",
   "archivedAt":"2026-09-08T07:30:00.000Z","updatedAt":"2026-09-08T07:00:00.000Z",
   "runs":[{"id":"t-4","status":"completed","userMessageId":"m-5"}],
   "messages":[{"id":"m-5","role":"user","createdAt":"2026-09-08T07:00:00.000Z","text":"archived prompt","runId":"t-4"}]}
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

# V2 run-correlation schema stand-in (the columns bin/t3-events reads)
sqlite3 "$TMP/t3home/userdata/statev2.sqlite" <<'SQL'
CREATE TABLE orchestration_v2_projection_runs(run_id TEXT PRIMARY KEY, thread_id TEXT, ordinal INTEGER, payload_json TEXT);
SQL

python3 "$ROOT/tests/fake_t3_server.py" --port "$PORT" --tokens-file "$TOKENS" --state "$STATE" --seed "$TMP/seed.json" &
SERVER_PID=$!
for _ in $(seq 1 50); do curl -s -o /dev/null "http://127.0.0.1:$PORT/" && break; sleep 0.1; done

export PATH="$TMP/bin:$PATH"
# container mode, no /etc/t3ctl.conf: everything from the environment
export T3CTL_CONF=/dev/null T3CTL_URL="http://127.0.0.1:$PORT" T3CODE_HOME="$TMP/t3home" T3CTL_T3_BIN="$TMP/bin/t3"
export T3CTL_TOKEN_MODE=self T3CTL_TOKEN_FILE="$TMP/t3home/t3ctl.token" T3CTL_SESSION_FILE="$TMP/t3home/t3ctl.token.session"
export T3CTL_DB="$TMP/t3home/userdata/statev2.sqlite" T3CTL_TAG="[kosmi]" T3CTL_NOTIFY_BIN=t3-notify
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
assert_contains "$create" '"runtimeMode":"approval-required"' "thread.create mode"
assert_contains "$create" '"createdBy":"agent","creationSource":"mcp"' "thread.create says who created it"
turn=$(jq -c '.[] | select(.type=="message.dispatch")' "$STATE" | tail -1)
assert_contains "$turn" "t3-notify --thread $TID --status done|blocked|approval" "callback footer names t3-notify + thread id"
assert_contains "$turn" '"dispatchMode":{"type":"start_immediately"}' "first message starts immediately"
assert_contains "$out" '"turn":"running"' "receipt correlates the run started by the message"
ok "new → thread.create + message.dispatch with footer"

out=$("$T3CTL" say "$TID" "also update the docs")
assert_contains "$out" '"mode":"approval-required"' "say inherits mode"
assert_contains "$(jq -c '.[] | select(.type=="message.dispatch")' "$STATE" | tail -1)" '"dispatchMode":{"type":"queue_after_active"}' "say to a busy thread queues, never steers"
ok "say inherits the thread's runtime mode"

# ---- 2b. say --expect-turn: a sender that read an older turn is refused ----
cur=$("$T3CTL" last "$TID" --json | jq -r .turnId)
[ -n "$cur" ] && [ "$cur" != null ] || fail "last --json carries the latest turn id"
before=$(jq '[.[] | select(.type=="message.dispatch")] | length' "$STATE")
if "$T3CTL" say "$TID" "stale follow-up" --expect-turn t-stale 2>"$TMP/err"; then fail "say --expect-turn with a stale turn must be refused"; fi
assert_contains "$(cat "$TMP/err")" "has moved past turn t-stale (latest is now $cur)" "stale-turn refusal names both turns"
[ "$(jq '[.[] | select(.type=="message.dispatch")] | length' "$STATE")" -eq "$before" ] || fail "refused say must not dispatch"
out=$("$T3CTL" say "$TID" "round 2" --expect-turn "$cur"); assert_contains "$out" "\"previousTurnId\":\"$cur\"" "matching --expect-turn sends"
cur=$(jq -r .turnId <<<"$out")
"$T3CTL" say "$TID" "copy A" --expect-turn "$cur" >"$TMP/a" 2>&1 & pa=$!
"$T3CTL" say "$TID" "copy B" --expect-turn "$cur" >"$TMP/b" 2>&1 & pb=$!
ra=0; rb=0; wait $pa || ra=$?; wait $pb || rb=$?
[ $((ra == 0 ? 1 : 0)) -ne $((rb == 0 ? 1 : 0)) ] || fail "exactly one of two concurrent senders with the same --expect-turn must win (rc $ra/$rb): $(cat "$TMP/a" "$TMP/b")"
assert_contains "$(cat "$TMP/a" "$TMP/b")" "has moved past turn $cur" "the losing sender is told why"
ok "say --expect-turn refuses stale and concurrent duplicate sends"

# ---- 2c. last / text cap ----------------------------------------------------
out=$("$T3CTL" last "$SEED_OTHER"); [ ${#out} -eq 3500 ] || fail "last prints the whole reply as plain text (got ${#out} chars)"
out=$("$T3CTL" show "$SEED_OTHER"); assert_contains "$out" "$LONG\"" "piped show is not truncated"
out=$(T3CTL_TEXT_CAP=100 "$T3CTL" show "$SEED_OTHER"); assert_contains "$out" '…\[truncated\]' "explicit T3CTL_TEXT_CAP still truncates"
if "$T3CTL" last "$SEED_OURS" 2>"$TMP/err"; then fail "last on a thread without a reply must fail"; fi
assert_contains "$(cat "$TMP/err")" "no assistant reply" "last says why"
ok "last prints the full reply; piped output is uncapped"

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
resp=$(jq -c '[.[] | select(.type=="runtime-request.respond")]' "$STATE")
[ "$(jq length <<<"$resp")" -eq 1 ] || fail "expected exactly one runtime-request.respond, got: $resp"
assert_contains "$resp" '"requestId":"r1","decision":"accept"' "accept dispatched for r1 only"
ok "approve-callbacks accepts only the exact t3-notify command in [kosmi] threads"

# ---- 7. list / show / search / settle --------------------------------------
# list filters: bare = inbox (unsettled, unarchived); --unsettled alias; --unarchived = settled too; --all = archived too
has()    { grep -q "$2" <<<"$1" || fail "$3 (missing $2)"; }
hasnot() { if grep -q "$2" <<<"$1"; then fail "$3 (unexpected $2)"; fi; }
out=$("$T3CTL" list)
has "$out" "$SEED_OURS" "bare list has the running unsettled thread"
has "$out" "$SEED_OTHER" "bare list has the idle unsettled thread"
hasnot "$out" "$SEED_SETTLED" "bare list is the inbox: settled threads excluded"
hasnot "$out" "$SEED_ARCHIVED" "bare list excludes archived threads"
[ "$(jq -r .id <<<"$out" | head -1)" = "$SEED_OURS" ] || fail "list is newest-first"
[ "$("$T3CTL" list --unsettled)" = "$out" ] || fail "list --unsettled must equal the bare list"
out=$("$T3CTL" list --unarchived)
has "$out" "$SEED_OURS" "--unarchived has unsettled threads"
has "$out" "$SEED_SETTLED" "--unarchived adds settled threads"
hasnot "$out" "$SEED_ARCHIVED" "--unarchived excludes archived threads"
assert_contains "$(jq -c "select(.id==\"$SEED_SETTLED\")" <<<"$out")" '"settled":true' "settled flag reported"
out=$("$T3CTL" list --all)
has "$out" "$SEED_SETTLED" "--all has settled threads"
has "$out" "$SEED_ARCHIVED" "--all includes archived threads"
out=$("$T3CTL" list --active)
has "$out" "$SEED_OURS" "--active has the running thread"
hasnot "$out" "$SEED_SETTLED" "--active excludes settled idle threads"
if "$T3CTL" list --bogus 2>"$TMP/err"; then fail "unknown list flag must error"; fi
assert_contains "$(cat "$TMP/err")" "unknown flag --bogus" "list flag error"
if "$T3CTL" list --all --active 2>/dev/null; then fail "list must take a single flag"; fi
ok "list: bare = inbox; --unsettled alias; --unarchived adds settled; --all adds archived; --active unchanged"
out=$("$T3CTL" show "$SEED_OURS" -n 2); assert_contains "$out" '"seeded prompt"' "show returns messages"
out=$("$T3CTL" search "seeded"); assert_contains "$out" '"matchedIn":"title"' "search matches titles"
out=$("$T3CTL" search "update the docs"); assert_contains "$out" '"matchedIn":"user"' "search matches message bodies via searchThreads"
out=$("$T3CTL" settle "$SEED_OTHER"); assert_contains "$out" '"op":"settle"' "settle has no ownership guard"
if "$T3CTL" list | grep -q "$SEED_OTHER"; then fail "a settled thread must leave the bare list"; fi
out=$("$T3CTL" unsettle "$SEED_OTHER"); assert_contains "$out" '"op":"unsettle"' "unsettle"
"$T3CTL" list | grep -q "$SEED_OTHER" || fail "an unsettled thread must return to the bare list"
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
meta=$(jq -c '.[] | select(.type=="project.update")' "$STATE" | tail -1)
assert_contains "$meta" "\"projectId\":\"$PID_PROJECT\"" "project.update sent via projects.mutate"
out=$("$T3CTL" new aikosmo-monorepo "uses project default")
assert_contains "$(jq -c '.[] | select(.type=="thread.create")' "$STATE" | tail -1)" '"model":"claude-fable-5-1"' "new picks the project default"
out=$("$T3CTL" contract-check); assert_contains "$out" '"ok":true' "contract-check"; assert_contains "$out" '"rpc":true' "contract-check covered the WebSocket RPC"; assert_contains "$out" '"dbSchemaChecked":true' "contract-check covered the run-correlation schema"
ok "project set-model + contract-check"

echo "all $pass checks passed"

python3 -m unittest discover -s "$ROOT/tests" -p 'test_*.py'
