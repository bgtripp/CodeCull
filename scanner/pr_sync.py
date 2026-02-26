"""PR sync job — the main trigger for the CodeCull demo.

Usage::

    python main.py sync

Flow:
  1. Scan the target repo for stale feature flags.
  2. Dispatch a Devin session for each stale flag (creates cleanup PRs).
  3. Poll Devin sessions until they complete (up to 30 min).
  4. Extract PR URLs from completed sessions.
  5. Fetch PR stats from the GitHub API.
  6. Write ``.codecull_state.json`` for the dashboard to consume.
  7. Send a Slack DM: "N PRs ready for review" with a dashboard link.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from scanner.devin_integration import (
    create_cleanup_session,
    extract_pr_url,
    poll_session_until_done,
)
from scanner.flag_scanner import FlagCandidate, get_target_repo_path, run_scan
from scanner.github_stats import discover_cleanup_prs, fetch_pr_stats
from scanner.slack_notify import find_flag_author_email, lookup_slack_user, send_dm
from scanner.state_store import load_state, save_state

logger = logging.getLogger(__name__)


def sync_state(state_path: Path | None = None) -> dict:
    """Run the full sync: scan -> dispatch Devin -> poll -> persist -> Slack.

    Returns the persisted payload (sessions, pr_stats).
    """
    repo_slug = os.getenv("TARGET_REPO", "bgtripp/LogiOps")
    dashboard_url = os.getenv("DASHBOARD_URL", "http://localhost:8000")

    if state_path is None:
        project_root = Path(__file__).resolve().parent.parent
        state_path = Path(
            os.getenv("CODECULL_STATE_PATH", str(project_root / ".codecull_state.json"))
        )

    # 1. Scan for stale flags
    logger.info("Step 1/5: Scanning for stale flags...")
    candidates = run_scan()
    if not candidates:
        logger.info("No stale flags found — nothing to do.")
        save_state(state_path, sessions={}, pr_stats={})
        return {"sessions": {}, "pr_stats": {}}

    logger.info("Found %d stale flag candidate(s)", len(candidates))

    # 2. Load existing persisted state + check GitHub for existing PRs
    existing_state = load_state(state_path)
    existing_sessions = existing_state.get("sessions", {})

    sessions: dict[str, dict] = {}
    flag_keys = [c.flag_key for c in candidates]

    # Merge PRs we already know about from the state file
    for c in candidates:
        existing = existing_sessions.get(c.flag_key, {})
        if existing.get("pr_url"):
            sessions[c.flag_key] = existing

    # 3. Discover any existing PRs on GitHub (avoids dispatching Devin
    #    when cleanup PRs have already been created by a previous run)
    missing_keys = [k for k in flag_keys if not sessions.get(k, {}).get("pr_url")]
    if missing_keys:
        logger.info("Step 2/5: Checking GitHub for existing cleanup PRs...")
        try:
            discovered = discover_cleanup_prs(repo_slug, missing_keys)
            for flag_key, info in discovered.items():
                pr_url = info.get("pr_url")
                if pr_url:
                    sessions[flag_key] = {"status": "ready", "pr_url": pr_url}
                    logger.info("  -> %s: found existing PR: %s", flag_key, pr_url)
        except Exception:
            logger.exception("GitHub PR discovery failed")

    # 4. Dispatch Devin sessions for flags that still don't have a PR
    need_polling: list[str] = []
    still_missing = [c for c in candidates if not sessions.get(c.flag_key, {}).get("pr_url")]

    if still_missing:
        logger.info("Step 3/5: Dispatching Devin for %d flag(s) without PRs...", len(still_missing))
        for c in still_missing:
            try:
                result = create_cleanup_session(
                    flag_key=c.flag_key,
                    repo=repo_slug,
                    variation=c.variation_served,
                    files=c.files_affected,
                )
                sessions[c.flag_key] = {
                    "session_id": result["session_id"],
                    "url": result["url"],
                    "status": "running",
                    "pr_url": None,
                }
                need_polling.append(c.flag_key)
                logger.info("  -> Session %s created: %s", result["session_id"], result["url"])
            except Exception:
                logger.exception("Failed to dispatch Devin for %s", c.flag_key)
                sessions[c.flag_key] = {"status": "error", "pr_url": None}
    else:
        logger.info("Step 3/5: All flags already have PRs — skipping Devin dispatch")

    # 5. Poll newly-created Devin sessions until they complete
    if need_polling:
        logger.info("Step 4/5: Polling %d Devin session(s)...", len(need_polling))
        for flag_key in need_polling:
            sid = sessions[flag_key].get("session_id")
            if not sid:
                continue
            try:
                final = poll_session_until_done(sid)
                pr_url = extract_pr_url(final)
                sessions[flag_key]["status"] = final.get("status_enum", "finished")
                if pr_url:
                    sessions[flag_key]["pr_url"] = pr_url
                    logger.info("  -> %s: PR found: %s", flag_key, pr_url)
                else:
                    logger.warning("  -> %s: session finished but no PR URL found", flag_key)
            except Exception:
                logger.exception("Failed to poll session for %s", flag_key)
                sessions[flag_key]["status"] = "error"

    # 6. Fetch PR stats for all flags with PR URLs
    logger.info("Step 5/5: Fetching PR stats...")
    pr_stats: dict[str, dict] = {}
    for flag_key, sinfo in sessions.items():
        pr_url = sinfo.get("pr_url")
        if not pr_url:
            continue
        try:
            stats = fetch_pr_stats(pr_url)
            if stats:
                pr_stats[flag_key] = stats
                logger.info(
                    "  -> %s: -%d lines across %d files",
                    flag_key,
                    stats.get("deletions", 0),
                    stats.get("files_changed", 0),
                )
        except Exception:
            logger.exception("Failed to fetch PR stats for %s", flag_key)

    # 7. Persist state
    save_state(state_path, sessions=sessions, pr_stats=pr_stats)
    ready_count = sum(1 for s in sessions.values() if s.get("pr_url"))
    logger.info("Wrote state file %s (%d PRs ready)", state_path, ready_count)

    # 8. Send Slack notification
    _send_slack_ready_notification(candidates, sessions, dashboard_url)

    return {"sessions": sessions, "pr_stats": pr_stats}


def _send_slack_ready_notification(
    candidates: list[FlagCandidate],
    sessions: dict[str, dict],
    dashboard_url: str,
) -> None:
    """DM the flag author(s) a summary: 'N PRs ready for review'."""
    ready = [c for c in candidates if sessions.get(c.flag_key, {}).get("pr_url")]
    if not ready:
        logger.info("No ready PRs; skipping Slack notification")
        return

    # Use SLACK_NOTIFY_EMAIL if set (useful when git blame returns a bot email).
    # Otherwise fall back to git blame on the first ready flag's file.
    email = os.getenv("SLACK_NOTIFY_EMAIL", "")
    if not email:
        repo_path = get_target_repo_path()
        first = ready[0]
        first_file = first.files_affected[0] if first.files_affected else ""
        email = find_flag_author_email(repo_path, first_file, first.flag_key) or ""
    if not email:
        logger.warning("Could not find author email for Slack notification")
        return

    slack_user_id = lookup_slack_user(email)
    if not slack_user_id:
        logger.warning("Could not find Slack user for %s", email)
        return

    n = len(ready)
    flag_list = "\n".join(f"  - `{c.flag_key}`" for c in ready)

    text = (
        f":recycle: *CodeCull: {n} PR{'s' if n != 1 else ''} ready for review*\n\n"
        f"Dead code cleanup PRs are ready for:\n{flag_list}\n\n"
        f":link: *<{dashboard_url}|Open Dashboard>*"
    )

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Open Dashboard"},
                    "url": dashboard_url,
                    "style": "primary",
                }
            ],
        },
    ]

    send_dm(slack_user_id, text, blocks)
    logger.info("Sent Slack DM: %d PRs ready", n)
