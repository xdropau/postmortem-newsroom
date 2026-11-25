# Costs, limits and verification status

Checked 24 Nov 2026 against vendor documentation (searches in this build session). Re-check before relying on any figure.

## Chosen infrastructure: GitHub Actions (public runner repo) + private data repo + Telegram. Expected cost: USD 0.00 / month

| Item | Free-tier fact (source) | How we stay inside it |
|---|---|---|
| GitHub Actions, public repo | Standard runners are free in public repositories (docs.github.com, "How use of GitHub Actions is measured") | Code lives in a PUBLIC repo; it contains no secrets or story data |
| GitHub Actions, private repo | 2,000 free minutes/month on GitHub Free | NOT used for the schedule: a 15-minute cron needs ~2,880 one-minute runs/month, which would exceed 2,000 |
| Scheduled workflows in public repos | Disabled automatically after 60 days with no repository activity (GitHub docs) | `keepalive.yml` pushes a tiny commit weekly; the dashboard push also counts |
| Cron timing | Minimum 5-minute interval; runs can start late under load (GitHub behaviour) | 15-minute cadence; the briefing tolerates a late start; health check alerts if radar goes quiet for 45 min |
| GitHub Pages | Free for public repos | Hosts ONLY the AES-256-GCM-encrypted dashboard |
| Private data repo | Free private repos on GitHub Free | Holds SQLite DB + story packages, squashed to one commit per run so it stays tiny |
| Telegram Bot API | Free (not re-verified this session) | ~tens of messages per day |
| Cloudflare Workers (rejected) | Free plan: 10 ms CPU per cron invocation, 50 subrequests per invocation, 5 cron triggers per account (developers.cloudflare.com/workers/platform/limits) | Cannot poll 40 feeds and score them in 10 ms CPU / 50 subrequests |
| Supabase (not selected) | Not evaluated in this session; limits not verified | SQLite in the private repo is simpler and has no extra account or pause-on-inactivity behaviour to manage |

## Things that are NOT free or NOT available
- **AI drafting is OFF.** Writing the 170-190-word narration, captions and article needs a model. Free fallback: every package contains `04_drafting_brief.md` to paste into Claude yourself. Optional automation (`ENABLE_LLM_DRAFTS=1` + `ANTHROPIC_API_KEY`, capped at 3 calls/day) is a PAID API and must not be switched on without your approval. A Claude subscription does not include API credit.
- **X, Facebook, Instagram, TikTok trends:** no free, compliant API for reading trends; skipped. YouTube trending works only if you add a free YouTube Data API key (10,000 units/day quota; we use ~1 unit per cycle).
- **Reuters / AP:** no official free RSS. They are discovered via Google News RSS search (headline + link only). Bloomberg and FT feeds give headlines only. No paywalls are bypassed.
- **Primary-source feed URLs flagged `"verify": true` in `config/sources.json`** could not be test-fetched from the build environment (it has no access to them). Run `ops > audit` once; broken feeds are listed in Telegram and must be fixed or disabled.

## Actual operating costs (fill in after the first 7 days)
- GitHub Actions minutes used: ____ (public repo: not billed)
- Telegram messages/day (dashboard > API usage): ____
- Any invoice or charge: ____ (expected none)
