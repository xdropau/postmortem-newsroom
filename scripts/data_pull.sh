#!/usr/bin/env bash
# Clone the PRIVATE data repo into ./data using a repo-scoped deploy key. Nothing sensitive is printed.
set -euo pipefail
: "${DATA_REPO:?DATA_REPO variable not set}"
: "${DATA_DEPLOY_KEY:?DATA_DEPLOY_KEY secret not set}"
mkdir -p ~/.ssh && chmod 700 ~/.ssh
printf '%s\n' "$DATA_DEPLOY_KEY" > ~/.ssh/data_key && chmod 600 ~/.ssh/data_key
ssh-keyscan -t ed25519,rsa github.com >> ~/.ssh/known_hosts 2>/dev/null
export GIT_SSH_COMMAND="ssh -i $HOME/.ssh/data_key -o IdentitiesOnly=yes"
rm -rf data
git clone --depth 1 "git@github.com:${DATA_REPO}.git" data
mkdir -p data
