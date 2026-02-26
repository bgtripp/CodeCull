"""PR sync job.

Discovers Devin-created cleanup PRs in GitHub and writes a JSON state file for
 the dashboard to consume.

This intentionally does *not* depend on the Devin API session polling, since
Devin sessions may not be readable via API in all org configurations.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from scanner.flag_scanner import get_target_repo_path, run_scan
from scanner.github_stats import discover_cleanup_prs
from scanner.slack_notify import find_flag_author_email, lookup_slack_user, send_dm
from scanner.state_store import save_state

logger = logging.getLogger(__name__)


def sync_state(state_path: Path | None = None) -> dict:
    """Run a scan, discover matching PRs, and persist state.

    Returns the persisted payload (sessions, pr_stats).
    """
    repo_slug = os.getenv("TARGET_REPO", "bgtripp/LogiOps")
    dashboard_url = os.getenv("DASHBOARD_URL", "http://localhost:8000")

    if state_path is None:
        project_root = Path(__file__).resolve().parent.parent
        state_path = Path(os.getenv("CODECULL_STATE_PATH", str(project_root / ".codecull_state.json")))

    candidates = run_scan()
    flag_keys = [c.flag_key for c in candidates]

    discovered = discover_cleanup_prs(repo_slug, flag_keys)

    sessions: dict[str, dict] = {}
    pr_stats: dict[str, dict] = {}

    for flag_key, info in discovered.items():
        pr_url = info.get("pr_url")
        stats = info.get("stats") or {}

        sessions[flag_key] = {
            "status": "ready" if pr_url else "unknown",
            "pr_url": pr_url,
        }
        if stats:
            pr_stats[flag_key] = stats

    save_state(state_path, sessions=sessions, pr_stats=pr_stats)
    logger.info("Wrote state file %s (%d PRs)", state_path, len(sessions))

    _send_slack_ready_notification(candidates, sessions, dashboard_url)

    return {"sessions": sessions, "pr_stats": pr_stats}


def _send_slack_ready_notification(candidates, sessions: dict[str, dict], dashboard_url: str) -> None:
    ready = [c for c in candidates if sessions.get(c.flag_key, {}).get("pr_url")]
    if not ready:
        logger.info("No ready PRs; skipping Slack notification")
        return

    # For the demo: DM the author of the first ready flag.
    repo_path = get_target_repo_path()
    first = ready[0]
    first_file = first.files_affected[0] if first.files_affected else ""
    email = find_flag_author_email(repo_path, first_file, first.flag_key)
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
