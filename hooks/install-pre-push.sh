#!/bin/sh
# Install the tracked pre-push guard into this clone's .git/hooks.
# Usage: sh hooks/install-pre-push.sh
# Run once after a fresh clone (and again whenever hooks/pre-push changes).
set -e
cd "$(dirname "$0")/.."
if [ ! -d .git ]; then
  echo "install-pre-push: no .git here; run inside a clone of slack-ata" >&2
  exit 1
fi
cp hooks/pre-push .git/hooks/pre-push
chmod +x .git/hooks/pre-push
echo "install-pre-push: installed .git/hooks/pre-push"
