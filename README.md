# CodeCull

Automated stale feature flag cleanup. Scans your codebase for feature flags, cross-references LaunchDarkly (or a mock), identifies flags that have been always-on or always-off for 90+ days, and dispatches [Devin](https://devin.ai) to remove them — one draft PR per flag.

## How it works

```
Sync job  ──▶  Scanner  ──▶  Devin API  ──▶  Draft PRs  ──▶  Slack DM  ──▶  Dashboard
 (cron /        (code +       (cleanup       (one per        ("3 PRs        (review hub,
  manual)        LD data)      sessions)      flag)           ready")        click → PR)
```

1. **Sync job** (`poetry run python main.py sync`) scans the target repo for stale flags, checks GitHub for existing cleanup PRs, dispatches Devin for any flags that don't have PRs yet, and sends a Slack DM when PRs are ready.
2. **Devin** creates a draft PR per flag — removes the flag check, cleans up dead code paths, updates tests. Sessions are tagged `CodeCull` for easy search in the Devin UI.
3. **Slack notification** — DMs the engineer: *"CodeCull found N PRs ready for review"* with a link to the dashboard.
4. **Dashboard** (FastAPI + Jinja2) is the review hub. Cards show flag name, staleness, impact (lines removed, files changed), and a **Review PR** button linking directly to the GitHub PR. Merged or closed PRs are automatically removed on page refresh.

## Demo flow (local)

### 1. Setup

```bash
# Clone and install
git clone https://github.com/bgtripp/CodeCull.git
cd CodeCull
poetry install

# Configure environment
cp .env.example .env
```

Edit `.env` and fill in the required values:

```env
DEVIN_API_KEY=your_devin_api_key       # Devin API key (cog_ for service user keys)
DEVIN_ORG_ID=org-...                   # Your Devin org ID (Settings > Organization)
GITHUB_TOKEN=github_pat_...            # Fine-grained PAT scoped to LogiOps
SLACK_BOT_TOKEN=xoxb-...               # Slack bot token
SLACK_NOTIFY_EMAIL=you@example.com     # Your Slack-associated email
```

**GitHub PAT permissions** (fine-grained, scoped to `bgtripp/LogiOps`):
- Contents: Read-only (clone the repo)
- Pull requests: Read-only (fetch PR stats)

### 2. Run the sync job

This scans LogiOps for stale flags, discovers or creates cleanup PRs, and sends a Slack DM:

```bash
poetry run python main.py sync
```

Expected output:
```
Synced 3 PR(s) to dashboard state file.
```

What happens behind the scenes:
- Scanner clones `bgtripp/LogiOps` and finds 3 stale flags (out of 5 total)
- Checks GitHub for existing Devin-created PRs matching each flag
- If a flag has no PR yet, dispatches a Devin session to create one
- Fetches PR stats (files changed, lines added/removed) from the GitHub API
- Writes everything to `.codecull_state.json`
- Sends a Slack DM: *"CodeCull found 3 PRs ready for review"*

### 3. Open the dashboard

```bash
poetry run python main.py
# Open http://localhost:8000
```

You should see 3 cards, sorted by lines removed:
1. **use-v2-pricing-engine** — -35 lines across 3 files
2. **enable-new-checkout-flow** — -31 lines across 3 files
3. **show-redesigned-dashboard** — -30 lines across 3 files

Each card has a **Review PR** button that opens the real GitHub PR on LogiOps.

### 4. Demo walkthrough

The demo CUJ (critical user journey) is:

1. **Receive Slack DM** — *"CodeCull found 3 PRs ready for review. View dashboard →"*
2. **Click the dashboard link** — opens the review hub at `http://localhost:8000`
3. **Review a card** — see flag name, staleness (e.g. "128 days stale"), impact ("-31 lines across 3 files"), affected files
4. **Click "Review PR"** — opens the actual Devin-created draft PR on GitHub
5. **Merge the PR** — refresh the dashboard, the card disappears automatically

### 5. Re-running the demo

To reset and run again from scratch:

```bash
# Delete the state file
rm .codecull_state.json

# Re-run sync (discovers existing PRs, or creates new Devin sessions)
poetry run python main.py sync

# Restart the dashboard
poetry run python main.py
```

## CLI commands

| Command | Description |
|---|---|
| `poetry run python main.py` | Start the dashboard (http://localhost:8000) |
| `poetry run python main.py scan` | Run the scanner only (prints stale flags to stdout) |
| `poetry run python main.py sync` | Full sync: scan + discover/create PRs + Slack DM |

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `DEVIN_API_KEY` | For sync | Devin API key (required for creating cleanup sessions) |
| `DEVIN_ORG_ID` | For sync | Devin org ID (required for `cog_` service-user keys) |
| `GITHUB_TOKEN` | Yes | GitHub PAT — Contents:Read-only + Pull requests:Read-only on target repo |
| `SLACK_BOT_TOKEN` | For sync | Slack bot token with `users:read.email` + `chat:write` scopes |
| `SLACK_NOTIFY_EMAIL` | For sync | Fallback email for Slack DM (used when git blame returns a bot email) |
| `DASHBOARD_URL` | No | URL included in Slack DM (default: `http://localhost:8000`) |
| `TARGET_REPO` | No | GitHub repo in `owner/repo` format (default: `bgtripp/LogiOps`) |
| `TARGET_REPO_PATH` | No | Local path to target repo (skips git clone if set) |
| `MOCK_LD_DATA_PATH` | No | Path to mock LaunchDarkly JSON (default: `./mock_launchdarkly.json`) |

## Target repo

CodeCull scans an external repo — [`bgtripp/LogiOps`](https://github.com/bgtripp/LogiOps) — a demo Python service seeded with 5 feature flags:

| Flag | Status | Candidate? |
|---|---|---|
| `enable-new-checkout-flow` | Always ON for 120+ days | Yes |
| `show-redesigned-dashboard` | Always ON for 95+ days | Yes |
| `use-v2-pricing-engine` | Always OFF for 100+ days | Yes |
| `rollout-search-suggestions` | 50% rollout (active) | No |
| `enable-dark-mode` | ON for 10 days (too recent) | No |

## Project structure

```
CodeCull/
├── main.py                    # Entry point (dashboard, scan, or sync)
├── mock_launchdarkly.json     # Mock LD flag data
├── .codecull_state.json       # Generated — PR state for dashboard (git-ignored)
├── scanner/
│   ├── flag_scanner.py        # Code scanner + staleness analysis + repo cloning
│   ├── pr_sync.py             # Sync job: scan → discover PRs → dispatch Devin → Slack
│   ├── github_stats.py        # GitHub API: fetch PR stats, discover cleanup PRs
│   ├── state_store.py         # Read/write .codecull_state.json
│   ├── devin_integration.py   # Devin API session management (tagged "CodeCull")
│   └── slack_notify.py        # Slack DM notifications
└── dashboard/
    ├── app.py                 # FastAPI app (review hub, auto-removes merged PRs)
    ├── templates/             # Jinja2 templates
    └── static/                # CSS
```
