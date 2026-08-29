#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$ROOT/skills/broz-deploy/scripts/broz-deploy.sh"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/broz-test.XXXXXX")"
trap 'kill "${SERVER_PID:-}" 2>/dev/null || true; rm -rf "$TMP"' EXIT INT TERM
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

mkdir -p "$TMP/key-project"
printf '{"scripts":{"start":"bun run index.js"}}\n' >"$TMP/key-project/package.json"
printf 'lockfileVersion = 1\n' >"$TMP/key-project/bun.lock"
printf test >"$TMP/key-project/private.pem"
if $SCRIPT deploy "$TMP/key-project" --no-open >/dev/null 2>&1; then echo "private key was accepted" >&2; exit 1; fi
echo "broz mock tests passed"
