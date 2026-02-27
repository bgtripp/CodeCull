"""
Devin API integration — creates a Devin session to remove a stale feature
flag and open a draft PR.

Supports both v1 (personal API keys) and v3 (service user keys starting with
``cog_``) endpoints.
"""

from __future__ import annotations

import logging
import os
import re
import time

import httpx

logger = logging.getLogger(__name__)

DEVIN_API_V1 = "https://api.devin.ai/v1"
DEVIN_API_V3_BASE = "https://api.devin.ai/v3/organizations"


def _api_base() -> str:
    """Return the correct API base URL based on the key type.

    For ``cog_`` service-user keys the v3 org-scoped endpoint is used.
    The org ID must be supplied via ``DEVIN_ORG_ID``; a ``RuntimeError`` is
    raised if it is missing for ``cog_`` keys.
    """
    token = os.getenv("DEVIN_API_KEY", "")
    if token.startswith("cog_"):
        org_id = os.getenv("DEVIN_ORG_ID", "")
        if org_id:
            return f"{DEVIN_API_V3_BASE}/{org_id}"
        raise RuntimeError(
            "DEVIN_ORG_ID environment variable is required for cog_ service-user keys"
        )
    return DEVIN_API_V1


POLL_INTERVAL_SECONDS = 15
MAX_POLL_MINUTES = 30


def _headers() -> dict[str, str]:
    token = os.getenv("DEVIN_API_KEY", "")
    if not token:
        raise RuntimeError("DEVIN_API_KEY environment variable is not set")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _build_prompt(flag_key: str, repo: str, variation: str, files: list[str]) -> str:
    """Build the prompt that tells Devin what to do."""
    file_list = "\n".join(f"  - {f}" for f in files)

    if variation == "always-on":
        action = (
            "The flag is always ON. Remove the feature flag check and keep only "
            "the 'enabled' / truthy code path. Delete the dead 'else' branch entirely."
        )
    else:
        action = (
            "The flag is always OFF. Remove the feature flag check and keep only "
            "the 'disabled' / falsy code path. Delete the dead 'if' branch entirely."
        )

    return f"""You are cleaning up the stale feature flag `{flag_key}` in the repo `{repo}`.

{action}

Affected files:
{file_list}

Instructions:
1. Remove every call to `is_enabled("{flag_key}")` and the surrounding if/else.
2. Keep the live code path inline (no conditional).
3. Remove any imports or config references that are now unused.
4. Update or remove tests that reference the flag.
5. Do NOT change any behaviour — the live code path must stay identical.
6. Open a **draft** pull request with a clear title: "Remove stale flag: {flag_key}"
"""


_MAX_RETRIES = 2
_RETRY_BASE_DELAY = 5  # seconds


def _post_with_retry(url: str, headers: dict, json: dict, *, timeout: int = 30) -> httpx.Response:
    """POST with a short retry on 429 Too Many Requests.

    Only retries a couple of times with short delays to stay within
    Fly.io's 60 s request timeout when called synchronously.
    """
    delay = _RETRY_BASE_DELAY
    for attempt in range(1, _MAX_RETRIES + 1):
        resp = httpx.post(url, headers=headers, json=json, timeout=timeout)
        if resp.status_code != 429:
            resp.raise_for_status()
            return resp
        try:
            retry_after = min(int(resp.headers.get("Retry-After", str(delay))), 15)
        except (ValueError, TypeError):
            retry_after = delay
        logger.warning(
            "429 rate-limited (attempt %d/%d) — retrying in %ds",
            attempt, _MAX_RETRIES, retry_after,
        )
        time.sleep(retry_after)
        delay = min(delay * 2, 15)
    # Final attempt — let the error propagate
    resp = httpx.post(url, headers=headers, json=json, timeout=timeout)
    resp.raise_for_status()
    return resp


def create_cleanup_session(
    flag_key: str,
    repo: str,
    variation: str,
    files: list[str],
) -> dict:
    """Create a Devin session that removes the given flag.

    Returns a dict with 'session_id' and 'url'.
    Retries automatically on 429 (rate limit) with exponential back-off.
    """
    prompt = _build_prompt(flag_key, repo, variation, files)

    payload = {
        "prompt": prompt,
        "idempotent": False,
        "tags": ["CodeCull", f"flag:{flag_key}"],
    }

    resp = _post_with_retry(
        f"{_api_base()}/sessions",
        headers=_headers(),
        json=payload,
    )
    data = resp.json()

    session_id = data.get("session_id", "")
    url = data.get("url", f"https://app.devin.ai/sessions/{session_id}")

    logger.info("Created Devin session %s for flag %s", session_id, flag_key)
    return {"session_id": session_id, "url": url}


def stop_session(session_id: str) -> bool:
    """Stop a Devin session. Returns True if stopped, False on error."""
    try:
        resp = httpx.post(
            f"{_api_base()}/sessions/{session_id}/stop",
            headers=_headers(),
            timeout=15,
        )
        if resp.status_code < 300:
            logger.info("Stopped session %s", session_id)
            return True
        logger.warning("Failed to stop session %s: %d", session_id, resp.status_code)
        return False
    except Exception:
        logger.exception("Error stopping session %s", session_id)
        return False


def stop_codecull_sessions() -> int:
    """Stop all running/suspended CodeCull sessions to free up org slots.

    Returns the number of sessions stopped.
    """
    stopped = 0
    for status_filter in ("running", "suspended"):
        try:
            resp = httpx.get(
                f"{_api_base()}/sessions",
                headers=_headers(),
                params={"limit": 50, "status": status_filter},
                timeout=30,
            )
            resp.raise_for_status()
            items = resp.json().get("items", [])
        except Exception:
            logger.exception("Failed to list %s sessions", status_filter)
            continue

        for session in items:
            tags = session.get("tags") or []
            if "CodeCull" not in tags:
                continue
            sid = session.get("session_id", "")
            if stop_session(sid):
                stopped += 1

    logger.info("Stopped %d old CodeCull sessions", stopped)
    return stopped


def get_session_status(session_id: str) -> dict:
    """Retrieve the current status of a Devin session."""
    resp = httpx.get(
        f"{_api_base()}/sessions/{session_id}",
        headers=_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def poll_session_until_done(session_id: str) -> dict:
    """Poll a session until it reaches a terminal state.

    Returns the final session payload.
    """
    deadline = time.time() + MAX_POLL_MINUTES * 60

    while time.time() < deadline:
        status = get_session_status(session_id)
        state = status.get("status_enum", "unknown")

        if state in ("finished", "stopped", "blocked"):
            logger.info("Session %s reached state: %s", session_id, state)
            return status

        logger.debug("Session %s state: %s — polling again in %ds", session_id, state, POLL_INTERVAL_SECONDS)
        time.sleep(POLL_INTERVAL_SECONDS)

    logger.warning("Session %s timed out after %d minutes", session_id, MAX_POLL_MINUTES)
    return get_session_status(session_id)


def create_rebase_session(repo: str, branch: str, pr_number: int) -> dict:
    """Create a Devin session to rebase/resolve conflicts on a PR branch.

    This is a lightweight task — Devin merges ``main`` into the branch,
    resolves any conflicts, and pushes the result.

    Returns a dict with 'session_id' and 'url'.
    """
    prompt = f"""You need to resolve merge conflicts on branch `{branch}` in the repo `{repo}`.

Instructions:
1. Check out the branch `{branch}`.
2. Run `git merge origin/main` to merge the latest main branch.
3. If there are merge conflicts, resolve them:
   - This branch is removing a stale feature flag. The conflicts likely come from other flag removals that were merged first.
   - Keep the changes from BOTH sides: the flag removal from this branch AND the other flag removals from main.
   - Remove any references to flags that were already removed in main.
4. After resolving conflicts, commit and push.
5. Do NOT create a new PR — just push to the existing branch `{branch}` (PR #{pr_number} already exists).
"""

    payload = {
        "prompt": prompt,
        "idempotent": False,
        "tags": ["CodeCull", "rebase", f"pr:{pr_number}"],
    }

    resp = _post_with_retry(
        f"{_api_base()}/sessions",
        headers=_headers(),
        json=payload,
    )
    data = resp.json()

    session_id = data.get("session_id", "")
    url = data.get("url", f"https://app.devin.ai/sessions/{session_id}")

    logger.info("Created Devin rebase session %s for branch %s (PR #%d)", session_id, branch, pr_number)
    return {"session_id": session_id, "url": url}


def extract_pr_url(session_status: dict) -> str | None:
    """Try to extract a pull request URL from the session result."""
    # The structured_output or result may contain a PR URL
    structured = session_status.get("structured_outputs") or []
    for output in structured:
        if "pull_request" in output:
            return output["pull_request"].get("url")

    # Fallback: scan the last message / result text for a GitHub PR URL
    result_text = session_status.get("result", "") or ""
    pr_match = re.search(r"https://github\.com/[^\s]+/pull/\d+", result_text)
    if pr_match:
        return pr_match.group(0)

    return None
