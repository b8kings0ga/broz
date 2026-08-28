#!/usr/bin/env bash
set -euo pipefail

API_URL="${BROZ_API_URL:-https://mimir.broz.uk}"
PUBLIC_SCHEME="${BROZ_PUBLIC_SCHEME:-https}"
OPEN_BROWSER=1
RUNTIME=auto
ARCH=amd64
NAME=""
DOMAIN=""
BINARY_FILE=""

die() { printf 'broz: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"; }
now_ms() { perl -MTime::HiRes=clock_gettime,CLOCK_MONOTONIC -e 'printf "%.0f",clock_gettime(CLOCK_MONOTONIC)*1000'; }
elapsed() { printf '%s' "$(( $(now_ms) - $1 ))"; }
progress() { printf 'broz: %s\n' "$*" >&2; }

usage() {
  cat >&2 <<'EOF'
usage:
  broz-deploy.sh deploy PATH [--runtime auto|bun|python|binary] [--name NAME] [--domain NAME] [--binary FILE --arch amd64] [--open|--no-open]
  broz-deploy.sh status PATH
  broz-deploy.sh stop PATH
  broz-deploy.sh delete PATH --yes
EOF
  exit 2
}

sha256_text() {
  if command -v shasum >/dev/null 2>&1; then printf '%s' "$1" | shasum -a 256 | awk '{print $1}';
  else printf '%s' "$1" | sha256sum | awk '{print $1}'; fi
}
random_hex() { od -An -N32 -tx1 /dev/urandom | tr -d ' \n'; }
new_uuid() {
  if command -v uuidgen >/dev/null 2>&1; then uuidgen | tr '[:upper:]' '[:lower:]';
  else
    local h; h="$(random_hex)"; printf '%s-%s-4%s-%s%s-%s\n' "${h:0:8}" "${h:8:4}" "${h:13:3}" "$(( (0x${h:16:1} & 3) | 8 ))" "${h:17:3}" "${h:20:12}";
  fi
}
api_call() {
  local method="$1" path="$2" token="$3" data="${4:-}" output="$5" code
  local args=(--silent --show-error --connect-timeout 5 --max-time 185 --request "$method" --output "$output" --write-out '%{http_code}' -H 'accept: application/json')
  [ -z "$token" ] || args+=(-H "authorization: Bearer $token")
  if [ -n "$data" ]; then args+=(-H 'content-type: application/json' --data "$data"); fi
  code="$(curl "${args[@]}" "$API_URL$path")" || die "Membership request failed: $method $path"
  case "$code" in 2??) ;; *) die "Membership $method $path returned HTTP $code: $(jq -c '{error:(.error // "unknown")}' "$output" 2>/dev/null || printf '%s' unknown)";; esac
  printf '%s' "$code"
}

load_project() {
  PROJECT_DIR="$(cd "$1" 2>/dev/null && pwd)" || die "project directory not found: $1"
  STATE_FILE="$PROJECT_DIR/.broz.json"
	if [ -f "$STATE_FILE" ]; then
		PROJECT_ID="$(jq -er '.project_id' "$STATE_FILE")" || die "invalid .broz.json"
	else
		PROJECT_ID="$(new_uuid)"
		jq -nc --arg p "$PROJECT_ID" '{project_id:$p}' >"$STATE_FILE.tmp"
		mv "$STATE_FILE.tmp" "$STATE_FILE"
	fi
  CRED_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/broz/credentials"
  CRED_FILE="$CRED_DIR/$PROJECT_ID.json"
}
ensure_guest() {
  mkdir -p "$CRED_DIR"; chmod 700 "$CRED_DIR"
  if [ -f "$CRED_FILE" ]; then
    [ "$(stat_mode "$CRED_FILE")" = 600 ] || die "credential file must have mode 0600: $CRED_FILE"
    TOKEN="$(jq -er '.token' "$CRED_FILE")" || die "invalid credential file"
    return
  fi
  TOKEN="$(random_hex)"
  local token_hash body response
  token_hash="$(sha256_text "$TOKEN")"
	body="$(jq -nc --arg p "$PROJECT_ID" --arg h "$token_hash" '{project_id:$p,token_hash:$h}')"
	response="$TMP_DIR/guest.json"
	local attempt code
	for attempt in 1 2 3 4 5; do
		code="$(curl --silent --show-error --connect-timeout 5 --max-time 15 --request POST --output "$response" --write-out '%{http_code}' -H 'accept: application/json' -H 'content-type: application/json' --data "$body" "$API_URL/v1/guest-sessions" 2>/dev/null || true)"
		[ -n "$code" ] || code=000
		case "$code" in
			2??) break;;
			502|503|504|000) [ "$attempt" -eq 5 ] || { sleep "0.$((attempt * 2))"; continue; };;
		esac
		die "Membership POST /v1/guest-sessions returned HTTP $code: $(jq -c '{error:(.error // "unknown")}' "$response" 2>/dev/null || printf '%s' unknown)"
	done
	umask 077
  jq -nc --arg p "$PROJECT_ID" --arg t "$TOKEN" '{project_id:$p,token:$t}' >"$CRED_FILE.tmp"
  chmod 600 "$CRED_FILE.tmp"; mv "$CRED_FILE.tmp" "$CRED_FILE"
}
stat_mode() { if stat -f '%Lp' "$1" >/dev/null 2>&1; then stat -f '%Lp' "$1"; else stat -c '%a' "$1"; fi; }

detect_runtime() {
  if [ "$RUNTIME" != auto ]; then return; fi
  if [ -n "$BINARY_FILE" ]; then RUNTIME=binary
  elif [ -f "$PROJECT_DIR/package.json" ] && { [ -f "$PROJECT_DIR/bun.lock" ] || [ -f "$PROJECT_DIR/bun.lockb" ]; }; then RUNTIME=bun
  elif [ -f "$PROJECT_DIR/pyproject.toml" ] && [ -f "$PROJECT_DIR/uv.lock" ]; then RUNTIME=python
  else die "cannot detect runtime; use --runtime"; fi
}
safe_source_tree() {
  local source="$1" stage="$2" rel base
  while IFS= read -r -d '' rel; do
    rel="${rel#./}"; base="${rel##*/}"
    case "/$rel/" in */.git/*|*/node_modules/*|*/.venv/*|*/venv/*|*/__pycache__/*|*/.pytest_cache/*|*/.mypy_cache/*|*/.ruff_cache/*|*/.npm/*|*/.aws/*|*/.ssh/*|*/.config/gcloud/*) continue;; esac
    case "$base" in .broz.json|.env|.env.*|*.pyc|*.pyo|*.sock|id_rsa|id_dsa|id_ecdsa|id_ed25519|credentials|credentials.json|.npmrc|.pypirc) continue;; esac
    case "$base" in *.pem|*.key|*.p12|*.pfx) die "refusing sensitive key file: $rel";; esac
    if [ -f "$source/.brozignore" ] && grep -Fqx "$rel" "$source/.brozignore"; then continue; fi
    [ -f "$source/$rel" ] || continue
    mkdir -p "$stage/$(dirname "$rel")"; cp "$source/$rel" "$stage/$rel"
  done < <(cd "$source" && find . -type f -print0)
}
package_artifact() {
  local stage="$TMP_DIR/stage"; mkdir -p "$stage"
  case "$RUNTIME" in
    bun)
      [ -f "$PROJECT_DIR/package.json" ] || die "Bun requires package.json"
      { [ -f "$PROJECT_DIR/bun.lock" ] || [ -f "$PROJECT_DIR/bun.lockb" ]; } || die "Bun requires bun.lock or bun.lockb"
      jq -e '.scripts.start | type == "string" and length > 0' "$PROJECT_DIR/package.json" >/dev/null || die "Bun requires scripts.start"
      safe_source_tree "$PROJECT_DIR" "$stage"
      ;;
    python)
      need uv
      [ -f "$PROJECT_DIR/pyproject.toml" ] && [ -f "$PROJECT_DIR/uv.lock" ] || die "Python requires pyproject.toml and uv.lock"
      grep -q 'mimir-service' "$PROJECT_DIR/pyproject.toml" || die "Python requires a mimir-service entry point"
      mkdir -p "$TMP_DIR/wheels"
      (cd "$PROJECT_DIR" && uv build --wheel --out-dir "$TMP_DIR/wheels" >/dev/null)
      set -- "$TMP_DIR"/wheels/*.whl; [ "$#" -eq 1 ] && [ -f "$1" ] || die "Python build must produce exactly one wheel"
      cp "$PROJECT_DIR/pyproject.toml" "$PROJECT_DIR/uv.lock" "$1" "$stage/"
      ;;
    binary)
      [ "$ARCH" = amd64 ] || die "v1 supports only --arch amd64"
      [ -n "$BINARY_FILE" ] || die "binary runtime requires --binary FILE"
      BINARY_FILE="$(cd "$(dirname "$BINARY_FILE")" && pwd)/$(basename "$BINARY_FILE")"
      [ -f "$BINARY_FILE" ] || die "binary file not found"
      file "$BINARY_FILE" | grep -Eq 'ELF 64-bit.*(x86-64|x86_64)' || die "binary must be an amd64 Linux ELF"
      cp "$BINARY_FILE" "$stage/service"; chmod 755 "$stage/service"
      ;;
    *) die "unsupported runtime: $RUNTIME";;
  esac
	ARTIFACT_FILE="$TMP_DIR/artifact.tar.gz"
	(cd "$stage" && find . -mindepth 1 -maxdepth 1 -print | COPYFILE_DISABLE=1 tar -czf "$ARTIFACT_FILE" -T -)
  [ "$(wc -c <"$ARTIFACT_FILE" | tr -d ' ')" -le 8388608 ] || die "guest artifact exceeds 8 MiB"
}
write_state() {
  local service_id="$1" hostname="$2"
  jq -nc --arg p "$PROJECT_ID" --arg s "$service_id" --arg h "$hostname" --arg r "$RUNTIME" --arg a "$ARCH" '{project_id:$p,service_id:$s,hostname:$h,runtime:$r,arch:$a}' >"$STATE_FILE.tmp"
  mv "$STATE_FILE.tmp" "$STATE_FILE"
}
open_url() {
  [ "$OPEN_BROWSER" -eq 1 ] || return 0
  if command -v open >/dev/null 2>&1; then open "$1" >/dev/null 2>&1 &
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$1" >/dev/null 2>&1 & fi
}
wait_page() {
  local url="$1" deployment="$2" deadline=$(( $(now_ms) + 120000 )) code header
  while [ "$(now_ms)" -lt "$deadline" ]; do
    code="$(curl --silent --show-error --location --connect-timeout 2 --max-time 5 --dump-header "$TMP_DIR/page.headers" --output "$TMP_DIR/page.body" --write-out '%{http_code}' "$url/" 2>/dev/null || true)"
    header="$(awk 'tolower($0) ~ /^x-mim-deployment:/{gsub("\r",""); sub(/^[^:]*:[[:space:]]*/,""); v=$0} END{print v}' "$TMP_DIR/page.headers" 2>/dev/null || true)"
    if [ "$code" = 200 ] && [ "$header" = "$deployment" ] && ! grep -qi 'service warming up' "$TMP_DIR/page.body"; then return 0; fi
    sleep 0.1
  done
  die "public page did not become ready (HTTP ${code:-none}, deployment header ${header:-missing})"
}

deploy() {
  local package_start upload_start create_start deploy_start ready_start total_start response upload code artifact_id artifact_digest service_id hostname deployment_id public_url
  total_start="$(now_ms)"; package_start="$total_start"
  detect_runtime; progress "packaging $RUNTIME artifact"; package_artifact; PACKAGE_MS="$(elapsed "$package_start")"
  ensure_guest
  upload_start="$(now_ms)"; progress "uploading artifact"
  response="$TMP_DIR/artifact.json"
	local attempt
	for attempt in 1 2 3 4 5; do
		code="$(curl --silent --show-error --connect-timeout 5 --max-time 60 --request POST --output "$response" --write-out '%{http_code}' -H "authorization: Bearer $TOKEN" -H 'content-type: application/gzip' --data-binary "@$ARTIFACT_FILE" "$API_URL/v1/artifacts?kind=$RUNTIME&arch=$ARCH" 2>/dev/null || true)"
		[ -n "$code" ] || code=000
		case "$code" in 2??) break;; 502|503|504|000) [ "$attempt" -eq 5 ] || { sleep "0.$((attempt * 2))"; continue; };; esac
		die "artifact upload returned HTTP $code: $(jq -c '{error:(.error // "unknown")}' "$response" 2>/dev/null || printf '%s' unknown)"
	done
  artifact_id="$(jq -er '.id' "$response")"; artifact_digest="$(jq -er '.digest' "$response")"; UPLOAD_MS="$(elapsed "$upload_start")"

  create_start="$(now_ms)"
  if [ -f "$STATE_FILE" ] && jq -e '.service_id' "$STATE_FILE" >/dev/null 2>&1; then
    service_id="$(jq -er '.service_id' "$STATE_FILE")"; hostname="$(jq -er '.hostname' "$STATE_FILE")"
  else
    [ -n "$NAME" ] || NAME="$(basename "$PROJECT_DIR" | tr '[:upper:]_' '[:lower:]-' | tr -cd 'a-z0-9-' | cut -c1-40)"
    [ -n "$DOMAIN" ] || DOMAIN="$NAME"
    response="$TMP_DIR/service.json"
    api_call POST /v1/services "$TOKEN" "$(jq -nc --arg n "$NAME" --arg a "$artifact_id" --arg d "$DOMAIN" '{name:$n,artifact_id:$a,subdomain:$d}')" "$response" >/dev/null
		service_id="$(jq -er '.id' "$response")"; hostname="$(jq -er '.hostname' "$response")"
	fi
	write_state "$service_id" "$hostname"
	CREATE_MS="$(elapsed "$create_start")"

  deploy_start="$(now_ms)"; progress "deploying $service_id"
  response="$TMP_DIR/deploy.json"
	# Keep retries within this command idempotent without making a later rollback
	# to identical bytes reuse an old, no-longer-active deployment.
	local request_id="broz_$(printf '%s' "$PROJECT_ID:$artifact_digest" | tr -cd 'A-Za-z0-9_' | cut -c1-88)_$(random_hex | cut -c1-16)"
	for attempt in 1 2 3 4 5; do
		code="$(curl --silent --show-error --connect-timeout 5 --max-time 185 --request POST --output "$response" --write-out '%{http_code}' -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' -H "Idempotency-Key: $request_id" --data "$(jq -nc --arg a "$artifact_id" '{artifact_id:$a}')" "$API_URL/v1/services/$service_id/deploy" 2>/dev/null || true)"
		[ -n "$code" ] || code=000
		case "$code" in 200) break;; 502|503|504|000) [ "$attempt" -eq 5 ] || { sleep "0.$((attempt * 2))"; continue; };; esac
		die "deploy returned HTTP $code: $(jq -c '{error:(.error // "unknown")}' "$response" 2>/dev/null || printf '%s' unknown)"
	done
  deployment_id="$(jq -er '.id' "$response")"; DEPLOY_MS="$(elapsed "$deploy_start")"
	public_url="$PUBLIC_SCHEME://$hostname"; ready_start="$(now_ms)"; progress "waiting for exact public homepage"
  wait_page "$public_url" "$deployment_id"; PAGE_READY_MS="$(elapsed "$ready_start")"; TOTAL_MS="$(elapsed "$total_start")"
  open_url "$public_url"
  jq -nc --arg artifact_id "$artifact_id" --arg service_id "$service_id" --arg deployment_id "$deployment_id" --arg public_url "$public_url" --argjson package_ms "$PACKAGE_MS" --argjson upload_ms "$UPLOAD_MS" --argjson create_ms "$CREATE_MS" --argjson deploy_ms "$DEPLOY_MS" --argjson page_ready_ms "$PAGE_READY_MS" --argjson total_ms "$TOTAL_MS" '{ok:true,artifact_id:$artifact_id,service_id:$service_id,deployment_id:$deployment_id,public_url:$public_url,timings:{package_ms:$package_ms,upload_ms:$upload_ms,create_ms:$create_ms,deploy_ms:$deploy_ms,page_ready_ms:$page_ready_ms,total_ms:$total_ms,within_10s:($total_ms<10000)}}'
}

project_action() {
	local action="$1" response service_id
	response="$TMP_DIR/$action.json"
  [ -f "$STATE_FILE" ] || die "no .broz.json in $PROJECT_DIR"
  [ -f "$CRED_FILE" ] || die "guest credential not found"
  [ "$(stat_mode "$CRED_FILE")" = 600 ] || die "credential file must have mode 0600"
  TOKEN="$(jq -er '.token' "$CRED_FILE")"; service_id="$(jq -er '.service_id' "$STATE_FILE")"
  case "$action" in
    status) api_call GET /v1/services "$TOKEN" '' "$response" >/dev/null; jq -c --arg id "$service_id" '{ok:true,service:(.items[]|select(.id==$id))}' "$response";;
    stop) api_call POST "/v1/services/$service_id/stop" "$TOKEN" '{}' "$response" >/dev/null; jq -nc --arg id "$service_id" '{ok:true,service_id:$id,status:"stopped"}' ;;
    delete) api_call DELETE "/v1/services/$service_id" "$TOKEN" '' "$response" >/dev/null; rm -f "$STATE_FILE" "$CRED_FILE"; jq -nc --arg id "$service_id" '{ok:true,service_id:$id,status:"deleted",local_state_removed:true}' ;;
  esac
}

need curl; need jq; need tar; need perl; need od; need file
[ "$#" -ge 2 ] || usage
COMMAND="$1"; shift; PROJECT_PATH="$1"; shift
YES=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --runtime) [ "$#" -ge 2 ] || usage; RUNTIME="$2"; shift 2;;
    --name) [ "$#" -ge 2 ] || usage; NAME="$2"; shift 2;;
    --domain) [ "$#" -ge 2 ] || usage; DOMAIN="$2"; shift 2;;
    --binary) [ "$#" -ge 2 ] || usage; BINARY_FILE="$2"; shift 2;;
    --arch) [ "$#" -ge 2 ] || usage; ARCH="$2"; shift 2;;
    --open) OPEN_BROWSER=1; shift;; --no-open) OPEN_BROWSER=0; shift;; --yes) YES=1; shift;; *) usage;;
  esac
done
load_project "$PROJECT_PATH"
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/broz.XXXXXX")"; trap 'rm -rf "$TMP_DIR"' EXIT INT TERM
case "$COMMAND" in
  deploy) deploy;;
  status|stop) project_action "$COMMAND";;
  delete) [ "$YES" -eq 1 ] || die "delete requires explicit --yes"; project_action delete;;
  *) usage;;
esac
