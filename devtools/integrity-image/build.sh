#!/usr/bin/env bash
# Builds the OpenCloud image of the upload-integrity fork for opencloud-compose.
#
#   ./build.sh                                 # opencloud-integrity:integrity-<commit>
#   TAG=8.0.1-integrity.1 ./build.sh           # explicit tag
#   WEB_REF=<branch|tag> ./build.sh            # another ref of the web fork
#   WEB_REPO=<git url> ./build.sh              # another web repository
#   PLATFORM=linux/arm64 ./build.sh            # another architecture (needs buildx/qemu)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

COMMIT="$(git rev-parse --short HEAD)"
IMAGE="${IMAGE:-opencloud-integrity}"
TAG="${TAG:-integrity-$COMMIT}"
WEB_REPO="${WEB_REPO:-https://github.com/Mortimer-RR/web.git}"
WEB_REF="${WEB_REF:-integrity}"

if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "warning: the checkout has uncommitted changes, they are built into the image" >&2
fi

docker build \
  ${PLATFORM:+--platform "$PLATFORM"} \
  -f devtools/integrity-image/Dockerfile \
  --build-arg WEB_REPO="$WEB_REPO" \
  --build-arg WEB_REF="$WEB_REF" \
  --build-arg VERSION="${VERSION:-}" \
  --build-arg STRING="$COMMIT" \
  --build-arg REVISION="$(git rev-parse HEAD)" \
  -t "$IMAGE:$TAG" \
  .

cat <<EOF

Built $IMAGE:$TAG. To use it with opencloud-compose, set in its .env:

  OC_DOCKER_IMAGE=$IMAGE
  OC_DOCKER_TAG=$TAG
EOF
