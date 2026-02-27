"""
GitHub PR stats — fetches metadata from pull requests via the GitHub API.

Used by the dashboard to display how many files and lines each cleanup PR
touches, without embedding a static code analyzer.
"""

from __future__ import annotations

import logging
import os
import re

import httpx

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"

# Matches URLs like https://github.com/owner/repo/pull/42
_PR_URL_RE = re.compile(r"https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)")


def _github_headers() -> dict[str, str]:
    """Return headers for GitHub API requests.

    Uses ``GITHUB_TOKEN`` for authentication (required for private repos).
    """
    token = os.getenv("GITHUB_TOKEN", "")
    headers: dict[str, str] = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def parse_pr_url(pr_url: str) -> tuple[str, str, int] | None:
    """Extract (owner, repo, pr_number) from a GitHub PR URL.

    Returns ``None`` if the URL doesn't match the expected pattern.
    """
    m = _PR_URL_RE.search(pr_url)
    if not m:
        return None
    return m.group("owner"), m.group("repo"), int(m.group("number"))


def fetch_pr_stats(pr_url: str) -> dict | None:
    """Fetch stats for a GitHub pull request.

    Returns a dict with keys:
      - ``files_changed`` (int)
      - ``additions`` (int)
      - ``deletions`` (int)
      - ``title`` (str)
      - ``state`` (str)  — "open", "closed", "merged"
      - ``draft`` (bool)

    Returns ``None`` if the PR URL cannot be parsed or the API call fails.
    """
    parsed = parse_pr_url(pr_url)
    if not parsed:
        logger.warning("Cannot parse PR URL: %s", pr_url)
        return None

    owner, repo, number = parsed

    try:
        resp = httpx.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{number}",
            headers=_github_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        return {
            "files_changed": data.get("changed_files", 0),
            "additions": data.get("additions", 0),
            "deletions": data.get("deletions", 0),
            "title": data.get("title", ""),
            "state": data.get("state", "unknown"),
            "draft": data.get("draft", False),
            "merged": data.get("merged", False),
        }
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "GitHub API returned %d for %s: %s",
            exc.response.status_code,
            pr_url,
            exc.response.text[:200],
        )
    except Exception:
        logger.exception("Failed to fetch PR stats for %s", pr_url)

    return None


def _parse_repo_slug(repo_slug: str) -> tuple[str, str]:
    """Parse ``owner/repo`` into (owner, repo)."""
    if "/" not in repo_slug:
        raise ValueError(f"Invalid repo slug: {repo_slug!r}")
    owner, repo = repo_slug.split("/", 1)
    return owner, repo


def list_pull_requests(repo_slug: str, state: str = "open") -> list[dict]:
    """List pull requests for *repo_slug*.

    Returns a list of GitHub PR JSON objects.
    """
    owner, repo = _parse_repo_slug(repo_slug)

    resp = httpx.get(
        f"{GITHUB_API}/repos/{owner}/{repo}/pulls",
        params={"state": state, "per_page": 100},
        headers=_github_headers(),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def merge_main_into_branch(repo_slug: str, branch: str) -> bool:
    """Merge ``main`` into *branch* using the GitHub API.

    Returns ``True`` if the merge succeeded (or was already up-to-date).
    Returns ``False`` if there are merge conflicts that need manual resolution.
    """
    owner, repo = _parse_repo_slug(repo_slug)

    try:
        resp = httpx.post(
            f"{GITHUB_API}/repos/{owner}/{repo}/merges",
            headers=_github_headers(),
            json={"base": branch, "head": "main", "commit_message": f"Merge main into {branch}"},
            timeout=30,
        )
        if resp.status_code in (201, 204):
            logger.info("Successfully merged main into %s", branch)
            return True
        if resp.status_code == 204:
            logger.info("Branch %s is already up-to-date with main", branch)
            return True
        if resp.status_code == 409:
            logger.warning("Merge conflict: cannot auto-merge main into %s", branch)
            return False
        resp.raise_for_status()
        return True
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "GitHub merge API returned %d for %s: %s",
            exc.response.status_code,
            branch,
            exc.response.text[:200],
        )
        return False
    except Exception:
        logger.exception("Failed to merge main into %s", branch)
        return False


def get_pr_branch(repo_slug: str, pr_number: int) -> str | None:
    """Return the head branch name for a pull request."""
    owner, repo = _parse_repo_slug(repo_slug)
    try:
        resp = httpx.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}",
            headers=_github_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("head", {}).get("ref")
    except Exception:
        logger.exception("Failed to get branch for PR #%d", pr_number)
        return None


def is_pr_mergeable(repo_slug: str, pr_number: int) -> bool | None:
    """Check if a PR is mergeable (no conflicts).

    Returns ``True`` if mergeable, ``False`` if conflicts, ``None`` if unknown.
    """
    owner, repo = _parse_repo_slug(repo_slug)
    try:
        resp = httpx.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}",
            headers=_github_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("mergeable")
    except Exception:
        logger.exception("Failed to check mergeability for PR #%d", pr_number)
        return None


def discover_cleanup_prs(repo_slug: str, flag_keys: list[str]) -> dict[str, dict]:
    """Discover Devin-created cleanup PRs for the provided *flag_keys*.

    This uses the GitHub API to list open PRs and matches by title containing
    the flag key.

    Returns a mapping: flag_key -> {"pr_url": str, "stats": dict}
    """
    wanted = {k.lower(): k for k in flag_keys}
    results: dict[str, dict] = {}

    try:
        pulls = list_pull_requests(repo_slug, state="open")
    except Exception:
        logger.exception("Failed to list PRs for %s", repo_slug)
        return results

    for pr in pulls:
        title = (pr.get("title") or "").lower()
        pr_url = pr.get("html_url") or ""
        if not pr_url:
            continue

        for key_lower, original_key in wanted.items():
            if original_key in results:
                continue
            if key_lower in title:
                stats = fetch_pr_stats(pr_url)
                results[original_key] = {"pr_url": pr_url, "stats": stats or {}}

    return results
