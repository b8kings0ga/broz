#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$ROOT/skills/broz-deploy/scripts/broz-deploy.sh"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/broz-test.XXXXXX")"
trap 'kill "${WATCH_PID:-}" "${SERVER_PID:-}" 2>/dev/null || true; rm -rf "$TMP"' EXIT INT TERM
mkdir -p "$TMP/home" "$TMP/project"
printf '{"scripts":{"start":"bun run server.js"}}\n' >"$TMP/project/package.json"
printf 'lockfileVersion = 1\n' >"$TMP/project/bun.lock"
printf 'Bun.serve({port:Number(process.env.PORT)})\n' >"$TMP/project/server.js"
printf 'secret\n' >"$TMP/project/.env.local"
python3 "$ROOT/tests/mock_membership.py" --port-file "$TMP/port" & SERVER_PID=$!
for _ in $(seq 1 100); do [ -s "$TMP/port" ] && break; sleep .02; done
PORT="$(cat "$TMP/port")"
export HOME="$TMP/home" BROZ_API_URL="http://127.0.0.1:$PORT" BROZ_PUBLIC_SCHEME=http
result="$($SCRIPT deploy "$TMP/project" --domain mock-demo --no-open)"
jq -e '.ok and .service_id=="svc_test" and (.timings.total_ms >= 0) and (.timings.upload_to_ready_ms >= .timings.upload_ms) and (.timings.deploy_to_ready_ms >= .timings.deploy_ms) and (.timings.within_10s == (.timings.upload_to_ready_ms < 10000))' <<<"$result" >/dev/null
[ "$(stat -f '%Lp' "$TMP/home/.config/broz/credentials/"*.json 2>/dev/null || stat -c '%a' "$TMP/home/.config/broz/credentials/"*.json)" = 600 ]
! tar -tzf "$TMP"/nonexistent 2>/dev/null || exit 1
$SCRIPT status "$TMP/project" | jq -e '.service.id=="svc_test"' >/dev/null
$SCRIPT stop "$TMP/project" | jq -e '.status=="stopped"' >/dev/null
second="$($SCRIPT deploy "$TMP/project" --no-open)"
jq -e '.ok and .service_id=="svc_test"' <<<"$second" >/dev/null
if $SCRIPT delete "$TMP/project" >/dev/null 2>&1; then echo "delete succeeded without --yes" >&2; exit 1; fi
$SCRIPT delete "$TMP/project" --yes | jq -e '.status=="deleted"' >/dev/null
[ ! -e "$TMP/project/.broz.json" ]
[ -z "$(find "$TMP/home/.config/broz/credentials" -type f -print -quit)" ]

mkdir -p "$TMP/home/.config/broz/profiles" "$TMP/hot-project"
chmod 700 "$TMP/home/.config/broz/profiles"
jq -nc --arg api "http://127.0.0.1:$PORT" --arg token mock-token '{api:$api,token:$token}' >"$TMP/home/.config/broz/profiles/mock.json"
chmod 600 "$TMP/home/.config/broz/profiles/mock.json"
printf '{"scripts":{"start":"bun run server.js"}}\n' >"$TMP/hot-project/package.json"
printf 'lockfileVersion = 1\n' >"$TMP/hot-project/bun.lock"
printf 'Bun.serve({port:Number(process.env.PORT)})\n' >"$TMP/hot-project/server.js"
prepared="$($SCRIPT prepare "$TMP/hot-project" --profile mock --domain mock-hot --once --no-open)"
jq -e '.ok and .state=="prepared" and .service_id=="svc_test" and .revision_id' <<<"$prepared" >/dev/null
printf '// revision two\nBun.serve({port:Number(process.env.PORT)})\n' >"$TMP/hot-project/server.js"
hot="$($SCRIPT deploy "$TMP/hot-project" --profile mock --no-open)"
jq -e '.ok and .mode=="hot" and .service_id=="svc_test" and .revision_id and .deployment_id and (.timings.total_ms >= 0) and (.timings.within_1s == (.timings.total_ms < 1000))' <<<"$hot" >/dev/null
[ "$(stat -f '%Lp' "$TMP/home/.cache/broz/projects/"*.json 2>/dev/null || stat -c '%a' "$TMP/home/.cache/broz/projects/"*.json)" = 600 ]
watching="$($SCRIPT prepare "$TMP/hot-project" --profile mock --watch --no-open)"
WATCH_PID="$(jq -er '.pid' <<<"$watching")"
socket_path="$(jq -er '.socket_path' "$TMP/home/.cache/broz/workers/"*.json)"
[ -S "$socket_path" ]
[ "$(stat -f '%Lp' "$socket_path" 2>/dev/null || stat -c '%a' "$socket_path")" = 600 ]
printf '// worker revision\nBun.serve({port:Number(process.env.PORT)})\n' >"$TMP/hot-project/server.js"
for _ in $(seq 1 100); do
  grep -q '"event": "prepared"' "$TMP/home/.cache/broz/logs/"*.log 2>/dev/null && break
  sleep .02
done
worker_hot="$($SCRIPT deploy "$TMP/hot-project" --profile mock --no-open)"
jq -e '.ok and (.worker | startswith("persistent")) and (.timings.command_total_ms >= .timings.total_ms)' <<<"$worker_hot" >/dev/null
$SCRIPT status "$TMP/hot-project" --profile mock | jq -e '.service.id=="svc_test"' >/dev/null
if $SCRIPT delete "$TMP/hot-project" --profile mock >/dev/null 2>&1; then echo "profile delete succeeded without --yes" >&2; exit 1; fi
$SCRIPT delete "$TMP/hot-project" --profile mock --yes | jq -e '.status=="deleted"' >/dev/null
for _ in $(seq 1 100); do kill -0 "$WATCH_PID" 2>/dev/null || break; sleep .02; done
if kill -0 "$WATCH_PID" 2>/dev/null; then echo "delete left prepare worker running" >&2; exit 1; fi
[ -f "$TMP/home/.config/broz/profiles/mock.json" ]
WATCH_PID=""

mkdir -p "$TMP/key-project"
printf '{"scripts":{"start":"bun run index.js"}}\n' >"$TMP/key-project/package.json"
printf 'lockfileVersion = 1\n' >"$TMP/key-project/bun.lock"
printf test >"$TMP/key-project/private.pem"
if $SCRIPT deploy "$TMP/key-project" --no-open >/dev/null 2>&1; then echo "private key was accepted" >&2; exit 1; fi
echo "broz mock tests passed"
