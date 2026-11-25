# THE POST-MORTEM: autonomous news intelligence system
Documents. Money. Evidence. Runs in the cloud (GitHub Actions) with no computer, browser or home internet needed.

## What runs
| Bot | What it does | Where |
|---|---|---|
| 01 Radar | 17 outlet feeds every 15 min; records publication time, detection time, URL, elapsed time | `cycle.yml` |
| 02 Primary sources | SEC, DOJ, FBI, White House, Treasury/OFAC, UK, EU, AFP, ASIC, ACCC, APH, ICC, ICJ, UN, ICIJ, OCCRP feeds | `cycle.yml` |
| 03 Trends | Google Trends AU/US, Reddit, optional YouTube. Leads only, capped at score 60, never alert alone | `cycle.yml` |
| 04 Selection | Deterministic 100-point score (freshness 25, interest 20, evidence 25, audience 15, angle 10, visual 5). Alert needs score >= 80 AND evidence gate AND no unsupported sensationalism | `newsroom/score.py` |
| 05 Verification | Fact ledger. Opens sources for real, matches figures/quotes literally, never auto-verifies names/dates/legal/denials. Approval blocked while any claim is UNVERIFIED | `newsroom/verify.py` |
| 06 Package | Fact register, sources, hooks, drafting brief (with locked closing line), Part E report per alert-grade story. Never publishes | `newsroom/package.py` |
| 07 Telegram + health | Alerts, 07:00 Sydney briefing, ops alerts (stale radar, dead source, repeated failure, quota, no new info), duplicate-suppressed | `health.yml`, `briefing.yml` |

Dedup: canonical URL, content hash, headline similarity (48 h window). Same story from different outlets becomes ONE story with corroboration, one alert.

## Telegram commands (owner chat only; answered at the next 15-minute cycle)
`/status` `/top` `/story ID` `/verify ID CLAIM STATUS note` `/approve ID` `/reject ID` `/published ID` `/confirm_offline`
(use underscores in multi-word statuses, e.g. `INDEPENDENTLY_CORROBORATED`).

## Set up (about 10 minutes, on your computer)
1. Telegram: message **@BotFather**, send `/newbot`, keep the token it gives you.
2. Install GitHub CLI and run `gh auth login`.
3. In this folder run `./scripts/bootstrap.sh`. It creates the two repos, deploy key, secrets and variables, runs the first cloud jobs, and prints your dashboard password once. Tokens are typed into the terminal only.

## Operate
- Dashboard: `https://<your-github-name>.github.io/postmortem-newsroom/` (encrypted; refreshed every 30 min).
- Add or fix a feed: edit `config/sources.json`, push. Actions > ops > `audit` tests every feed.
- Acceptance: Actions > ops > `acceptance`. Prints PASS/FAIL per criterion; the system must not be called operational until every line passes.
- Dead-man's switch: if the 07:00 Sydney briefing does not arrive, the system (or GitHub) is down.

## Known limits (honest list)
- Public-repo logs are public: logs contain counts and source IDs only, never story text, tokens or chat IDs.
- GitHub cron can start minutes late; commands via Telegram can take up to 15 minutes.
- Headline-similarity clustering is heuristic; check merged/split stories on the dashboard.
- "INDEPENDENTLY CORROBORATED" from headline matching alone can still be one wire story reworded; the ledger labels it as headline-level.
- Dashboard encryption is only as strong as its password; the generated one is 32 characters.
- Run history is squashed in the data repo (one snapshot); there is no rollback. Export if you need an archive.

## Editorial layer (same database, same dashboard)
| Need | Where | How you use it |
|---|---|---|
| Version-specific approval | `editorial.py`, tables `content_versions`, `approvals` | `/approve ID [kind] [destination]` approves the LATEST version only. Any edit makes a new version and VOIDS the approval. `/published ID` is refused unless every latest version is approved and no claim is UNVERIFIED. |
| Corrections log, approval/rejection history | `corrections`, `approvals` | `/correct ID text`, `/reject ID reason`, `/research ID note`. Dashboard tabs: Corrections, Approval history. |
| Investigative leads | `leads` | `/lead title \| why it matters`, or `python -m newsroom lead '{"title":..,"documents":..,"alternatives":..,"missing_evidence":..,"next_steps":..,"parties":..}'`. Originality stays NOT CHECKED until you set it. |
| Breaking flag, auto-expiry | `stories.breaking_until` | `/breaking ID [hours]` (max 24h), `/unbreak ID`. Expires itself each cycle. |
| Live coverage | `live_coverage`, `live_updates` | `/live ID`, `/livestate ID LIVE\|PAUSED\|CONCLUDED`, `/liveadd ID text \| url`, `/liveapprove UPDATE_ID`. New items on a LIVE story become DRAFT updates; none is public until approved; editing voids approval. |
| One story ID everywhere | `PM-000123` (`editorial.uid`), `story_links` | One article and one video per story (a second is refused). Shown in /story, dashboard, evidence export. |
| Latency metrics | dashboard "Latency", `python -m newsroom metrics` | Real timestamps only: source to detection per outlet (median, p90), plus draft/approval/publication stages. |
| Evidence Library | dashboard "Evidence Library", `python -m newsroom export-evidence` | CSV + JSON: title, issuer, date, URL, relevance, classification, opened-by-system, public-eligible. |
| Drafts from your computer | `python -m newsroom submit ops.json` | Pushes a git branch `inbox-*` to the private data repo. The next cloud cycle applies it. Your machine never writes to the live database. |
| Narration check | `python -m newsroom check-narration file.txt` | 170-190 words, closing line verbatim, no labels, no AI/XDrop mentions. |

Publication is never automatic: nothing in this system posts to Shopify, YouTube, Facebook, Instagram or TikTok.
Audience and commercial dashboards are NOT built (no data source is connected); the tab says so instead of showing invented numbers.

## Review desk, audit and backups
- Dashboard: Top 5 today cards, three columns (New and worth a look / Needs your review / Ready to approve), filters, everything else under "More". Still AES-256-GCM encrypted.
- 07:00 Sydney briefing: top 5 only + one pipeline line. Priority alerts still need score 80+ and the evidence gate.
- `/secondcheck ID`: re-opens the stored sources and reports PASS/FAIL/UNCLEAR per check with a verbatim extract. MATERIAL checks (source opened, headline, figures, names, quotes, recycled, satire/rumour, allegation vs fact) block approval; ADVISORY ones (dates, denials, contradiction, corroboration) are shown only. A human can settle a check with `/scresolve` (note required, original result kept).
- Evidence archive (private data repo only): URL, issuer, publication and retrieval time, HTTP status, SHA-256, <=300-char extract, archive.org capture link; FULL copies only for official public records. `/evidence ID` writes a per-story bundle. Published stories get a permanent bundle under `evidence/permanent/PM-xxxxxx/` tied to the article URL.
- Change log: append-only, hash-chained, written by database triggers. Honest limit: someone with raw file access could rebuild the whole chain; the private repo and the head hash are the protection.
- Backups: weekly `backup-YYYY-MM-DD` release asset in the private data repo, restore-tested; keeps newest 12 plus the first of every month. Laptop copy: `python -m newsroom export-backup`.
