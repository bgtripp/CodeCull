"""
Slack notification — DMs the engineer who introduced a stale flag.

Flow:
  1. `git blame` on the file where the flag was introduced to find the author email.
  2. Slack `users.lookupByEmail` to resolve the Slack user ID.
  3. Slack `chat.postMessage` to DM them with the PR link.
"""

from __future__ import annotations

import logging
import os
import subprocess

import httpx

logger = logging.getLogger(__name__)

SLACK_API = "https://slack.com/api"


def _slack_headers() -> dict[str, str]:
    token = os.getenv("SLACK_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("SLACK_BOT_TOKEN environment variable is not set")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# Git blame
# ---------------------------------------------------------------------------

def find_flag_author_email(repo_path: str, file_path: str, flag_key: str) -> str | None:
    """Use ``git blame`` to find the email of who introduced *flag_key* in *file_path*.

    Returns the first author email found on a line containing the flag key,
    or ``None`` if it cannot be determined.
    """
    abs_file = os.path.join(repo_path, file_path)
    if not os.path.isfile(abs_file):
        logger.warning("File not found for blame: %s", abs_file)
        return None

    try:
        result = subprocess.run(
            ["git", "blame", "--porcelain", file_path],
            capture_output=True,
            text=True,
            cwd=repo_path,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        logger.exception("git blame failed for %s", file_path)
        return None

    if result.returncode != 0:
        logger.warning("git blame exited %d: %s", result.returncode, result.stderr.strip())
        return None

    # Parse porcelain output — look for author-mail on lines with the flag key
    lines = result.stdout.splitlines()
    current_email: str | None = None
    for line in lines:
        if line.startswith("author-mail "):
            # Extract email, stripping angle brackets
            current_email = line.split(" ", 1)[1].strip("<>")
        if flag_key in line and current_email:
            return current_email

    return current_email  # fallback: last seen email


# ---------------------------------------------------------------------------
# Slack helpers
# ---------------------------------------------------------------------------

def lookup_slack_user(email: str) -> str | None:
    """Resolve *email* to a Slack user ID via ``users.lookupByEmail``."""
    try:
        resp = httpx.get(
            f"{SLACK_API}/users.lookupByEmail",
            params={"email": email},
            headers=_slack_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("ok"):
            return data["user"]["id"]
        logger.warning("Slack lookup failed for %s: %s", email, data.get("error"))
    except Exception:
        logger.exception("Slack lookupByEmail request failed")
    return None


def send_dm(user_id: str, text: str, blocks: list | None = None) -> bool:
    """Send a DM to *user_id* using ``chat.postMessage``.

    Slack automatically opens a DM channel when you post to a user ID.
    """
    payload: dict = {
        "channel": user_id,
        "text": text,
    }
    if blocks:
        payload["blocks"] = blocks

    try:
        resp = httpx.post(
            f"{SLACK_API}/chat.postMessage",
            headers=_slack_headers(),
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("ok"):
            logger.info("DM sent to %s", user_id)
            return True
        logger.warning("Slack postMessage failed: %s", data.get("error"))
    except Exception:
        logger.exception("Slack postMessage request failed")
    return False


# ---------------------------------------------------------------------------
# High-level notification
# ---------------------------------------------------------------------------

def notify_flag_author(
    repo_path: str,
    file_path: str,
    flag_key: str,
    pr_url: str,
    session_url: str,
) -> bool:
    """End-to-end: blame -> lookup -> DM.

    Returns True if the DM was sent successfully.
    """
    email = find_flag_author_email(repo_path, file_path, flag_key)
    if not email:
        logger.warning("Could not determine author for flag %s", flag_key)
        return False

    logger.info("Flag %s was introduced by %s", flag_key, email)

    slack_user_id = lookup_slack_user(email)
    if not slack_user_id:
        logger.warning("Could not find Slack user for %s", email)
        return False

    text = (
        f":recycle: *CodeCull cleanup PR ready*\n\n"
        f"The stale feature flag `{flag_key}` has been cleaned up and a "
        f"draft PR is ready for your review.\n\n"
        f":link: *PR:* {pr_url}\n"
        f":robot_face: *Devin session:* {session_url}\n\n"
        f"Please review and merge when ready."
    )

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": text,
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "View PR"},
                    "url": pr_url,
                    "style": "primary",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "View Devin Session"},
                    "url": session_url,
                },
            ],
        },
    ]

    return send_dm(slack_user_id, text, blocks)
