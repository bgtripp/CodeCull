# CodeCull

Automated stale feature flag cleanup. Scans your codebase for feature flags, cross-references LaunchDarkly (or a mock), surfaces cleanup candidates in a dashboard, and dispatches [Devin](https://devin.ai) to remove them — one draft PR per flag.

## How it works

```
Scanner  ──▶  Dashboard  ──▶  Devin API  ──▶  Draft PR  ──▶  Slack DM
 (code +       (ranked        (cleanup       (one per       (to flag
  LD data)      candidates)    session)        flag)         author)
```

1. **Scanner** walks the repo looking for `is_enabled("flag-key")` calls and cross-references a LaunchDarkly mock to find flags that have been always-on or always-off for 90+ days.
2. **Dashboard** (FastAPI + Jinja2) shows the ranked candidates with context — flag name, age, files affected, code references — and Approve / Skip buttons.
3. **Devin integration** — on approval, a Devin session is created via the API. Devin removes the flag, cleans up dead code paths, updates tests, and opens a draft PR.
4. **Slack notification** — `git blame` identifies who introduced the flag. Their Slack user is resolved via `users.lookupByEmail` and they receive a DM with the PR link.

## Quick start

```bash
# Install dependencies
poetry install

# Copy and fill in env vars
cp .env.example .env

# Run the scanner only (CLI)
python main.py scan

# Run the dashboard
python main.py
# Open http://localhost:8000
```

## Environment variables

| Variable | Description |
|---|---|
| `DEVIN_API_KEY` | Devin API key (required for cleanup sessions) |
| `SLACK_BOT_TOKEN` | Slack bot token with `users:read.email` + `chat:write` |
| `TARGET_REPO` | GitHub repo in `owner/repo` format |
| `TARGET_REPO_PATH` | Path to local clone of the target repo |
| `MOCK_LD_DATA_PATH` | Path to mock LaunchDarkly JSON file |

## Target repo

CodeCull scans an external repo — [`bgtripp/LogiOps`](https://github.com/bgtripp/LogiOps) — a demo Python service seeded with 5 feature flags:

| Flag | Status | Candidate? |
|---|---|---|
| `enable-new-checkout-flow` | Always ON for 120+ days | Yes |
| `show-redesigned-dashboard` | Always ON for 95+ days | Yes |
| `use-v2-pricing-engine` | Always OFF for 100+ days | Yes |
| `rollout-search-suggestions` | 50% rollout (active) | No |
| `enable-dark-mode` | ON for 10 days (too recent) | No |

On startup the scanner clones `TARGET_REPO` automatically. Set `TARGET_REPO_PATH` to point at a local checkout instead.

## Project structure

```
CodeCull/
├── main.py                  # Entry point (dashboard or CLI scan)
├── mock_launchdarkly.json   # Mock LD flag data
├── scanner/
│   ├── flag_scanner.py      # Code scanner + staleness analysis + repo cloning
│   ├── devin_integration.py # Devin API session management
│   └── slack_notify.py      # Slack DM notifications
└── dashboard/
    ├── app.py               # FastAPI application
    ├── templates/            # Jinja2 templates
    └── static/              # CSS
```
