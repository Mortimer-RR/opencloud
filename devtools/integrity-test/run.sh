#!/usr/bin/env bash
# Runs the upload-integrity torture test against the image built by build.sh.
#
#   ./run.sh                 # all scenarios
#   ./run.sh baseline crash  # selected scenarios
#   ./run.sh --down          # remove containers and volumes
set -euo pipefail
cd "$(dirname "$0")"
export MSYS_NO_PATHCONV=1

if [[ "${1:-}" == "--down" ]]; then
  docker compose --profile client --profile proxy down -v
  exit 0
fi

# a throwaway admin password for the local test server
if [[ ! -f .env ]]; then
  echo "IDM_ADMIN_PASSWORD=$(head -c 18 /dev/urandom | base64 | tr -dc 'A-Za-z0-9')" > .env
fi

docker compose up -d opencloud
docker compose --profile client build client
docker compose --profile client run --rm client python3 torture.py "$@"
