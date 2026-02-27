"""PR sync job — the main trigger for the CodeCull pipeline.

Usage::

    python main.py sync

Flow:
  1. Scan the target repo for stale feature flags.
  2. Check GitHub for any existing cleanup PRs (from prior Devin runs).
  3. Fetch PR stats for known PRs.
  4. Write ``.codecull_state.json`` for the dashboard to consume.
  5. Send a Slack DM (Phase 1): "N stale flags found — review in dashboard".

Devin dispatch is **not** triggered here — it's user-initiated from the
dashboard via the "Fix in Stacked PR" button.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from scanner.flag_scanner import FlagCandidate, get_target_repo_path, run_scan
from scanner.github_stats import discover_cleanup_prs, fetch_pr_stats
from scanner.slack_notify import find_flag_author_email, lookup_slack_user, send_dm
from scanner.state_store import load_state, save_state

logger = logging.getLogger(__name__)


def sync_state(state_path: Path | None = None) -> dict:
    """Run the sync: scan -> discover existing PRs -> persist -> Slack.

    Returns the persisted payload (sessions, pr_stats).
    """
    repo_slug = os.getenv("TARGET_REPO", "bgtripp/LogiOps")
    dashboard_url = os.getenv("DASHBOARD_URL", "http://localhost:8000")

    if state_path is None:
        project_root = Path(__file__).resolve().parent.parent
        state_path = Path(
            os.getenv("CODECULL_STATE_PATH", str(project_root / ".codecull_state.json"))
        )

    # Load existing stacked_sessions so we never lose them
    _existing = load_state(state_path)
    stacked_sessions: dict = _existing.get("stacked_sessions", {}) or {}

    # 1. Scan for stale flags
    logger.info("Step 1/4: Scanning for stale flags...")
    candidates = run_scan()
    if not candidates:
        logger.info("No stale flags found — nothing to do.")
        save_state(state_path, sessions={}, pr_stats={}, stacked_sessions=stacked_sessions)
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
        if existing.get("pr_url") or existing.get("session_id"):
            sessions[c.flag_key] = existing

    # 3. Discover any existing PRs on GitHub
    missing_keys = [k for k in flag_keys if not sessions.get(k, {}).get("pr_url")]
    if missing_keys:
        logger.info("Step 2/4: Checking GitHub for existing cleanup PRs...")
        try:
            discovered = discover_cleanup_prs(repo_slug, missing_keys)
            for flag_key, info in discovered.items():
                pr_url = info.get("pr_url")
                if pr_url:
                    sessions[flag_key] = {"status": "ready", "pr_url": pr_url}
                    logger.info("  -> %s: found existing PR: %s", flag_key, pr_url)
        except Exception:
            logger.exception("GitHub PR discovery failed")

    # 4. Fetch PR stats for all flags with PR URLs
    logger.info("Step 3/4: Fetching PR stats...")
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

    # 5. Persist state (preserve stacked_sessions)
    save_state(state_path, sessions=sessions, pr_stats=pr_stats, stacked_sessions=stacked_sessions)
    ready_count = sum(1 for s in sessions.values() if s.get("pr_url"))
    logger.info("Wrote state file %s (%d PRs ready)", state_path, ready_count)

    # 6. Send Slack Phase 1 notification
    _send_slack_scan_notification(candidates, sessions, dashboard_url)

    return {"sessions": sessions, "pr_stats": pr_stats}


def _send_slack_scan_notification(
    candidates: list[FlagCandidate],
    sessions: dict[str, dict],
    dashboard_url: str,
) -> None:
    """Phase 1 Slack DM: notify maintainer(s) about stale flags found.

    If some flags already have PRs, mentions how many are ready vs pending.

    Owner resolution order:
      1. ``maintainer_email`` (from flag metadata)
      2. ``git blame`` on the first affected file
      3. ``SLACK_NOTIFY_EMAIL`` env var (manual fallback)
    """
    if not candidates:
        return

    ready = [c for c in candidates if sessions.get(c.flag_key, {}).get("pr_url")]
    pending = [c for c in candidates if not sessions.get(c.flag_key, {}).get("pr_url")]

    # 1. Try maintainer_email from flag metadata
    email = ""
    first = candidates[0]
    if first.maintainer_email:
        email = first.maintainer_email
        logger.info("Using flag metadata maintainer_email: %s", email)

    # 2. Fall back to git blame
    if not email:
        repo_path = get_target_repo_path()
        first_file = first.files_affected[0] if first.files_affected else ""
        email = find_flag_author_email(repo_path, first_file, first.flag_key) or ""
        if email:
            logger.info("Using git blame author: %s", email)

    # 3. Fall back to SLACK_NOTIFY_EMAIL env var
    if not email:
        email = os.getenv("SLACK_NOTIFY_EMAIL", "")
        if email:
            logger.info("Using SLACK_NOTIFY_EMAIL fallback: %s", email)

    if not email:
        logger.warning("Could not find author email for Slack notification")
        return

    slack_user_id = lookup_slack_user(email)
    if not slack_user_id:
        logger.warning("Could not find Slack user for %s", email)
        return

    n_total = len(candidates)
    n_ready = len(ready)
    n_pending = len(pending)

    flag_list = "\n".join(
        f"  - `{c.flag_key}` ({c.days_stale} days stale, {c.variation_served})"
        for c in candidates
    )

    if n_ready > 0 and n_pending > 0:
        summary = (
            f":recycle: *CodeCull: {n_total} stale flag{'s' if n_total != 1 else ''} found*\n\n"
            f"{n_ready} already have cleanup PRs, {n_pending} need review:\n{flag_list}\n\n"
            f"Open the dashboard to select which flags to fix."
        )
    elif n_ready == n_total:
        summary = (
            f":white_check_mark: *CodeCull: all {n_total} stale flags have cleanup PRs*\n\n"
            f"{flag_list}\n\n"
            f"Open the dashboard to review them."
        )
    else:
        summary = (
            f":recycle: *CodeCull: {n_total} stale flag{'s' if n_total != 1 else ''} found*\n\n"
            f"{flag_list}\n\n"
            f"Open the dashboard to select which flags to fix."
        )

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": summary}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Open Dashboard"},
                    "action_id": "open_dashboard",
                    "url": dashboard_url,
                    "style": "primary",
                }
            ],
        },
    ]

    send_dm(slack_user_id, summary, blocks)
    logger.info("Sent Phase 1 Slack DM: %d flags found (%d ready)", n_total, n_ready)
