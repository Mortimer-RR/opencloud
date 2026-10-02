#!/usr/bin/env bash
# Builds the opencloud-integrity:dev image from this checkout (vendored reva fork) and the
# forked web client. Everything runs in containers; no Go or Node toolchain needed locally.
#
#   WEB_DIST=/path/to/web/dist ./build.sh   # use an already built web client (pnpm build)
#   WEB_DIR=/path/to/web ./build.sh         # build the web client from a checkout first
#   SKIP_IDP=1 ...                          # reuse services/idp/assets from a previous run
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# docker.exe on Windows (Git Bash) needs native paths
if command -v cygpath >/dev/null; then HERE="$(cygpath -m "$HERE")"; ROOT="$(cygpath -m "$ROOT")"; fi
IMAGE="${IMAGE:-opencloud-integrity:dev}"
export MSYS_NO_PATHCONV=1

if [[ -z "${WEB_DIST:-}" ]]; then
  : "${WEB_DIR:?set WEB_DIST (a built web dist/) or WEB_DIR (a web checkout)}"
  echo "### building web client in $WEB_DIR"
  docker run --rm -v "$WEB_DIR:/web" -w /web node:24 \
    sh -c 'corepack enable && pnpm install --frozen-lockfile && pnpm build'
  WEB_DIST="$WEB_DIR/dist"
fi

echo "### web assets from $WEB_DIST"
# keep the tracked .keep file, replace everything else
find "$ROOT/services/web/assets/core" -mindepth 1 ! -name .keep -exec rm -rf {} + 2>/dev/null || true
mkdir -p "$ROOT/services/web/assets/core"
cp -r "$WEB_DIST/." "$ROOT/services/web/assets/core/"

if [[ -z "${SKIP_IDP:-}" ]]; then
  echo "### idp assets"
  docker run --rm -v "$ROOT:/oc" -v integrity-pnpm-store:/pnpm-store -e npm_config_store_dir=/pnpm-store -w /oc/services/idp node:24 \
    sh -c 'corepack enable && make assets'
fi

# cgo is needed (go-crypt), so build against musl for the alpine runtime image
echo "### opencloud binary"
docker run --rm -v "$ROOT:/oc" -v gomodcache:/go/pkg/mod -v integrity-gobuildcache:/root/.cache/go-build -w /oc golang:1.26-alpine \
  sh -c 'apk add --no-cache git gcc musl-dev >/dev/null && git config --global --add safe.directory "*" &&
         CGO_ENABLED=1 go build -o opencloud/bin/opencloud ./opencloud/cmd/opencloud'

echo "### image $IMAGE"
docker build -t "$IMAGE" -f "$HERE/Dockerfile" "$ROOT/opencloud/bin"
