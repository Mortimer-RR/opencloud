#!/usr/bin/env bash
# Starts an image with opencloud-compose's docker-compose.yml and checks that it works and
# carries the upload-integrity fixes. Uses its own compose project and volumes, and removes
# them afterwards. Does not change the opencloud-compose checkout.
#
#   COMPOSE_DIR=/path/to/opencloud-compose ./smoke.sh opencloud-integrity:<tag>
set -euo pipefail

IMAGE_REF="${1:?usage: smoke.sh <image:tag>}"
HERE="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_DIR="${COMPOSE_DIR:-$HERE/../../../opencloud-compose}"
COMPOSE_DIR="$(cd "$COMPOSE_DIR" && pwd)"
if command -v cygpath >/dev/null; then COMPOSE_DIR="$(cygpath -m "$COMPOSE_DIR")"; fi
export MSYS_NO_PATHCONV=1

PROJECT=integrity-smoke
ENV_FILE="$(mktemp)"
PASSWORD="$(head -c 18 /dev/urandom | base64 | tr -dc 'A-Za-z0-9')Aa1!"
cat > "$ENV_FILE" <<EOF
OC_DOCKER_IMAGE=${IMAGE_REF%:*}
OC_DOCKER_TAG=${IMAGE_REF##*:}
OC_DOMAIN=cloud.opencloud.test
INSECURE=true
INITIAL_ADMIN_PASSWORD=$PASSWORD
PROXY_ENABLE_BASIC_AUTH=true
START_ADDITIONAL_SERVICES=
EOF

ENV_FILE_ARG="$ENV_FILE"
if command -v cygpath >/dev/null; then ENV_FILE_ARG="$(cygpath -m "$ENV_FILE")"; fi

compose() {
  docker compose -p "$PROJECT" --project-directory "$COMPOSE_DIR" -f "$COMPOSE_DIR/docker-compose.yml" --env-file "$ENV_FILE_ARG" "$@"
}
cleanup() {
  compose down -v >/dev/null 2>&1 || true
  rm -f "$ENV_FILE"
}
trap cleanup EXIT

# curl inside the compose network, against the server's plain HTTP port (PROXY_TLS=false)
curl_in() {
  docker run --rm --network "${PROJECT}_opencloud-net" curlimages/curl -s "$@"
}
BASE=http://opencloud:9200
DAV="$BASE/remote.php/dav/files/admin"

echo "### starting $IMAGE_REF with opencloud-compose"
compose up -d opencloud
for _ in $(seq 1 60); do
  if curl_in -o /dev/null -w '%{http_code}' "$BASE/status.php" | grep -q 200; then break; fi
  sleep 3
done

fail=0
check() { if eval "$2"; then echo "ok   $1"; else echo "FAIL $1"; fail=1; fi; }

status="$(curl_in "$BASE/status.php")"
check "status.php answers" '[[ "$status" == *productversion* ]]'
echo "     $status"
check "runs as uid 1000 (compose user)" '[[ "$(compose exec -T opencloud id -u)" == 1000 ]]'

check "admin can PROPFIND" '[[ "$(curl_in -o /dev/null -w "%{http_code}" -u "admin:$PASSWORD" -X PROPFIND "$DAV/")" == 207 ]]'

# tus upload with a wrong checksum must be rejected with 460 (fix 4)
loc="$(curl_in -o /dev/null -w '%{redirect_url}' -D - -u "admin:$PASSWORD" -X POST "$DAV/" \
  -H 'Tus-Resumable: 1.0.0' -H 'Upload-Length: 5' \
  -H "Upload-Metadata: filename $(printf smoke.txt | base64),checksum $(printf 'sha1 %040d' 0 | base64)" \
  | tr -d '\r' | sed -n 's/^[Ll]ocation: //p')"
code="$(curl_in -o /dev/null -w '%{http_code}' -u "admin:$PASSWORD" -X PATCH "$loc" \
  -H 'Tus-Resumable: 1.0.0' -H 'Upload-Offset: 0' -H 'Content-Type: application/offset+octet-stream' --data-binary hello)"
check "tus upload with a wrong checksum is rejected with 460 (got $code)" '[[ "$code" == 460 ]]'

# the same upload with the right checksum is stored
loc="$(curl_in -o /dev/null -D - -u "admin:$PASSWORD" -X POST "$DAV/" \
  -H 'Tus-Resumable: 1.0.0' -H 'Upload-Length: 5' \
  -H "Upload-Metadata: filename $(printf smoke.txt | base64),checksum $(printf 'sha1 aaf4c61ddcc5e8a2dabede0f3b482cd9aea9434d' | base64)" \
  | tr -d '\r' | sed -n 's/^[Ll]ocation: //p')"
code="$(curl_in -o /dev/null -w '%{http_code}' -u "admin:$PASSWORD" -X PATCH "$loc" \
  -H 'Tus-Resumable: 1.0.0' -H 'Upload-Offset: 0' -H 'Content-Type: application/offset+octet-stream' --data-binary hello)"
check "tus upload with the right checksum succeeds (got $code)" '[[ "$code" == 204 ]]'
for _ in $(seq 1 20); do
  body="$(curl_in -u "admin:$PASSWORD" "$DAV/smoke.txt")"
  [[ "$body" == hello ]] && break
  sleep 1
done
check "uploaded file reads back" '[[ "$body" == hello ]]'

# with opencloud-compose's csp.yaml, the web client may compile WebAssembly (fast checksums);
# without 'wasm-unsafe-eval' it falls back to a slower JavaScript SHA1
csp="$(curl_in -D - -o /dev/null "$BASE/" | tr -d '' | sed -n 's/^[Cc]ontent-[Ss]ecurity-[Pp]olicy: //p')"
if [[ "$csp" == *wasm-unsafe-eval* ]]; then
  echo "ok   CSP allows WebAssembly ('wasm-unsafe-eval'): fast upload checksums"
else
  echo "note CSP has no 'wasm-unsafe-eval': the web client uses the slower JavaScript SHA1"
fi

# the embedded web client contains the upload checksum plugin (fix 5); embed.FS stores the
# assets uncompressed in the binary
check "embedded web client includes the upload checksum plugin"   '[[ "$(compose exec -T opencloud grep -c UploadChecksum /usr/bin/opencloud)" -gt 0 ]]'

exit $fail
