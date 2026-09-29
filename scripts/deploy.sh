#!/usr/bin/env bash
# Rebuild and restart the stack with the current git commit baked into every image.
# Usage: scripts/deploy.sh [extra `docker compose up` args]
set -euo pipefail

cd "$(dirname "$0")/.."

commit="$(git rev-parse --short HEAD)"
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    commit="${commit}-dirty"
fi
echo "Deploying commit ${commit}"

# Resolve the commit as the invoking user (git refuses repos owned by others
# under sudo), then elevate only for docker when the user cannot reach it.
elevate=()
if ! docker info >/dev/null 2>&1; then
    elevate=(sudo)
fi

# `env` passes GIT_COMMIT through sudo's environment reset.
"${elevate[@]}" env GIT_COMMIT="$commit" docker compose up -d --build "$@"
