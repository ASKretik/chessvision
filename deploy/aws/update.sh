#!/usr/bin/env bash
# Keep the server in step with GitHub. Run every 5 minutes by
# chessvision-update.timer, and once by bootstrap.sh with --force.
#
#   1. Re-point the DuckDNS name at this instance: the public IP changes
#      whenever the instance is stopped and started.
#   2. Pull main, and rebuild only if something the API image uses changed.
#      Frontend, docs and workflow edits are served by GitHub Pages instead.
set -euo pipefail

DIR=${CHESSVISION_DIR:-/opt/chessvision}
cd "$DIR"
set -a; . deploy/aws/.env; set +a

# An empty ip= makes DuckDNS use the address the request came from.
curl -fsS -m 20 "https://www.duckdns.org/update?domains=${DUCKDNS_DOMAIN}&token=${DUCKDNS_TOKEN}&ip=" \
  >/dev/null || echo "update: DuckDNS refresh failed, will retry next run"

git fetch --quiet origin main
before=$(git rev-parse HEAD)
after=$(git rev-parse origin/main)

if [ "$before" = "$after" ] && [ "${1:-}" != "--force" ]; then
  exit 0
fi

changed=$(git diff --name-only "$before" "$after")
# .env is gitignored, so a hard reset never touches the secrets.
git reset --hard --quiet origin/main

if [ "${1:-}" = "--force" ] || \
   echo "$changed" | grep -qvE '^(frontend/|docs/|\.github/|[^/]*\.md$)'; then
  echo "update: deploying ${after:0:7}"
  docker compose -f deploy/aws/compose.yml up -d --build --remove-orphans
  docker image prune -f >/dev/null
else
  echo "update: ${after:0:7} changes nothing the API uses, no rebuild"
fi
