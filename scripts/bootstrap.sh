#!/usr/bin/env bash
# One-shot setup, run on YOUR computer. Needs: git, gh (run `gh auth login` first), python3, ssh-keygen, curl.
# Secrets are typed into THIS terminal only (hidden). They are never put in chat, files or git.
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$PATH:/c/Program Files/GitHub CLI"   # Windows Git Bash: where winget installs gh
# Pick a Python that really runs (on Windows "python3" is often a Microsoft Store stub that does nothing).
PY=""; for c in python3 python py; do if "$c" -c 'import sys' >/dev/null 2>&1; then PY=$c; break; fi; done
[ -n "$PY" ] || { echo "Missing tool: python (3.10+)"; exit 1; }
for t in gh git ssh-keygen curl; do command -v "$t" >/dev/null || { echo "Missing tool: $t"; exit 1; }; done
gh auth status >/dev/null || { echo "Run: gh auth login"; exit 1; }
gh auth setup-git >/dev/null 2>&1 || true
OWNER=$(gh api user -q .login)
CODE_REPO="$OWNER/postmortem-newsroom"
DATA_REPO="$OWNER/postmortem-newsroom-data"
echo "GitHub account: $OWNER"

echo "== 1/7 Repositories (code repo PUBLIC = free unlimited Actions; data repo PRIVATE) =="
gh repo view "$CODE_REPO" >/dev/null 2>&1 || gh repo create "$CODE_REPO" --public --description "THE POST-MORTEM newsroom runner (code only, no secrets)"
gh repo view "$DATA_REPO" >/dev/null 2>&1 || gh repo create "$DATA_REPO" --private --description "THE POST-MORTEM newsroom data (private)"

echo "== 2/7 Push code =="
[ -d .git ] || git init -q -b main
git checkout -q -B main 2>/dev/null || git branch -M main
# Windows machines often have no git identity; use a repo-local GitHub no-reply one (only if none is set).
git config user.name  >/dev/null || git config user.name  "$OWNER"
git config user.email >/dev/null || git config user.email "$OWNER@users.noreply.github.com"
git config core.autocrlf false
git add -A
if ! git rev-parse --verify -q HEAD >/dev/null || ! git diff --cached --quiet; then
  git commit -q -m "THE POST-MORTEM newsroom system" || { echo "git commit failed (see message above)"; exit 1; }
fi
git rev-parse --verify -q HEAD >/dev/null || { echo "No commit exists; nothing to push"; exit 1; }
git remote get-url origin >/dev/null 2>&1 || git remote add origin "https://github.com/$CODE_REPO.git"
git push -u origin main

echo "== 3/7 Private-data access (deploy key limited to the data repo only) =="
if ! gh secret list -R "$CODE_REPO" | grep -q '^DATA_DEPLOY_KEY'; then
  KD=$(mktemp -d)
  ssh-keygen -q -t ed25519 -N "" -C newsroom-runner -f "$KD/k"
  gh repo deploy-key add "$KD/k.pub" -R "$DATA_REPO" --allow-write --title newsroom-runner
  gh secret set DATA_DEPLOY_KEY -R "$CODE_REPO" < "$KD/k"
  rm -rf "$KD"
fi
gh variable set DATA_REPO -R "$CODE_REPO" --body "$DATA_REPO"
TOKEN_FILE="${TOKEN_FILE:-$(cd ../.. && pwd)/token.txt}"   # C:\Users\hello\PostMortem\token.txt
if [ -f "$TOKEN_FILE" ]; then CE=""; else read -rp "Public contact email for the feed user-agent [depostmortemchannel@gmail.com]: " CE; fi
gh variable set CONTACT_EMAIL -R "$CODE_REPO" --body "${CE:-depostmortemchannel@gmail.com}"

echo "== 4/7 Telegram =="
tg_secrets_set() { gh secret list -R "$CODE_REPO" | grep -q '^TELEGRAM_BOT_TOKEN' && gh secret list -R "$CODE_REPO" | grep -q '^TELEGRAM_CHAT_ID'; }
if [ ! -f "$TOKEN_FILE" ] && tg_secrets_set; then
  echo "Telegram secrets already stored; skipping."
else
  if [ -f "$TOKEN_FILE" ]; then
    TG=$(tr -d '\r\n \t' < "$TOKEN_FILE")          # read from file; never echoed
  else
    read -rsp "Telegram bot token from @BotFather (hidden): " TG; echo
  fi
  # -o /dev/null keeps the token (which is in the URL) out of any output; --fail-with-body is not used on purpose
  curl -sS -o /dev/null -w '' --fail "https://api.telegram.org/bot${TG}/getMe" 2>/dev/null \
    || { echo "Telegram rejected the token (getMe failed). Check the token in $TOKEN_FILE."; exit 1; }
  echo "Telegram token accepted (getMe ok)."
  CHAT=""
  for attempt in 1 2 3 4 5 6; do
    CHAT=$(curl -sS --fail "https://api.telegram.org/bot${TG}/getUpdates" 2>/dev/null | "$PY" -c 'import sys,json
try: r=json.load(sys.stdin).get("result",[])
except Exception: r=[]
ids=[]
for u in r:
    for k in ("message","edited_message","channel_post","my_chat_member"):
        c=(u.get(k) or {}).get("chat") or {}
        if c.get("type")=="private" and c.get("id"): ids.append(c["id"])
print(ids[-1] if ids else "")') || CHAT=""
    [ -n "$CHAT" ] && break
    [ -f "$TOKEN_FILE" ] || read -rp "Send /start to your bot in Telegram, then press Enter: " _
    sleep 5
  done
  [ -n "$CHAT" ] || { echo "Telegram has no recent private message to the bot. Send it any message (e.g. hello) and rerun this one command."; exit 1; }
  echo "Found your Telegram chat."
  printf '%s' "$TG" | gh secret set TELEGRAM_BOT_TOKEN -R "$CODE_REPO" >/dev/null || { echo "Could not store TELEGRAM_BOT_TOKEN secret"; exit 1; }
  printf '%s' "$CHAT" | gh secret set TELEGRAM_CHAT_ID -R "$CODE_REPO" >/dev/null || { echo "Could not store TELEGRAM_CHAT_ID secret"; exit 1; }
  gh secret list -R "$CODE_REPO" | grep -q '^TELEGRAM_BOT_TOKEN' || { echo "Secret not visible after set; keeping token file."; exit 1; }
  unset TG CHAT
  if [ -f "$TOKEN_FILE" ]; then
    shred -u -n 3 "$TOKEN_FILE" 2>/dev/null || rm -f "$TOKEN_FILE"
    [ ! -f "$TOKEN_FILE" ] && echo "Secrets stored; token file securely deleted."
  fi
fi

echo "== 5/7 Dashboard password =="
if ! gh secret list -R "$CODE_REPO" | grep -q '^DASHBOARD_PASSWORD'; then
  PW=$("$PY" -c 'import secrets;print(secrets.token_urlsafe(24))')
  printf '%s' "$PW" | gh secret set DASHBOARD_PASSWORD -R "$CODE_REPO" >/dev/null
  PWFILE="$(cd ../.. && pwd)/dashboard-password.txt"      # outside the repo, never committed
  printf '%s\n' "$PW" > "$PWFILE"; chmod 600 "$PWFILE" 2>/dev/null || true
  unset PW
  echo "Dashboard password saved to: $PWFILE  (move it into your password manager, then delete the file)"
fi

run_wf () {  # run_wf <workflow-file> [input=value]
  gh workflow run "$1" -R "$CODE_REPO" ${2:+-f "$2"} >/dev/null
  sleep 10
  id=$(gh run list -R "$CODE_REPO" -w "$1" -L1 --json databaseId -q '.[0].databaseId')
  gh run watch "$id" -R "$CODE_REPO" --exit-status >/dev/null && echo "  $1 ${2:-}: ok" || echo "  $1 ${2:-}: FAILED (see https://github.com/$CODE_REPO/actions/runs/$id)"
}
echo "== 6/7 First cloud runs (feed audit, ingest, health + dashboard, self-test) =="
run_wf ops.yml task=audit
run_wf cycle.yml
run_wf cycle.yml
run_wf health.yml
gh api -X POST "repos/$CODE_REPO/pages" -f "source[branch]=gh-pages" -f "source[path]=/" >/dev/null 2>&1 || true
run_wf ops.yml task=selftest

echo "== 7/7 Acceptance report (will say NO until the 15-minute schedule has run for 30+ minutes) =="
run_wf ops.yml task=acceptance
echo
echo "Dashboard (after Pages finishes building, ~1 min):  https://${OWNER}.github.io/postmortem-newsroom/"
echo "Check Telegram: you should have a feed audit, a test message, an ops test and a briefing preview."
echo "Later: switch your computer off, wait 30 min, send /confirm_offline to the bot, then run Actions > ops > acceptance."
