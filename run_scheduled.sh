#!/bin/sh

set -eu

script_directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$script_directory"

docker_command=${DOCKER_BIN:-docker}

if "$docker_command" compose version >/dev/null 2>&1; then
    "$docker_command" compose pull
    exec "$docker_command" compose run --rm --no-deps music-deduplicator \
        --apply --report /reports/latest.json
fi

docker-compose pull
exec docker-compose run --rm --no-deps music-deduplicator \
    --apply --report /reports/latest.json
