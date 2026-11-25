#!/usr/bin/env bash
# Commit the data repo as a single squashed snapshot (keeps the repo tiny; the database is a binary file).
set -euo pipefail
[ -d data/.git ] || { echo "no data clone; nothing to save"; exit 0; }
export GIT_SSH_COMMAND="ssh -i $HOME/.ssh/data_key -o IdentitiesOnly=yes"
cd data
if [ -f .inbox_done ]; then
  while read -r br; do [ -n "$br" ] && git push -q origin --delete "$br" || true; done < .inbox_done
  rm -f .inbox_done
fi
git config user.name "newsroom-bot"
git config user.email "newsroom-bot@users.noreply.github.com"
git checkout -q --orphan snapshot
git add -A
git commit -q -m "snapshot $(date -u +%Y-%m-%dT%H:%M:%SZ)"
git push -q -f origin snapshot:main
echo "data saved"
