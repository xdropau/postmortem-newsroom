#!/usr/bin/env bash
# Publish the approved-only website feed (pm-newsroom.json) to the public `site-data` branch.
# The draft website theme reads it from raw.githubusercontent.com. It contains ONLY material approved in the newsroom;
# the export code refuses everything else (see newsroom/siteexport.py). Skips the push when nothing changed.
set -euo pipefail
: "${GITHUB_TOKEN:?GITHUB_TOKEN not set}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY not set}"
rm -rf site_export
python -m newsroom site-export --out site_export
cd site_export
git init -q
git checkout -q -b site-data
git config user.name "newsroom-bot"
git config user.email "newsroom-bot@users.noreply.github.com"
REMOTE="https://x-access-token:${GITHUB_TOKEN}@github.com/${GITHUB_REPOSITORY}.git"
if git fetch -q --depth 1 "$REMOTE" site-data 2>/dev/null; then
  git show FETCH_HEAD:pm-newsroom.json > ../site_prev.json 2>/dev/null || echo '{}' > ../site_prev.json
  if python - <<'PY'
import json, sys
a = json.load(open("../site_prev.json")); b = json.load(open("pm-newsroom.json"))
for d in (a, b):
    d.pop("generated_at", None)
sys.exit(0 if a == b else 1)
PY
  then
    echo "site feed unchanged"
    exit 0
  fi
fi
git add -A
git commit -q -m "approved feed $(date -u +%FT%TZ)"
git push -q -f "$REMOTE" site-data
echo "site feed published"
