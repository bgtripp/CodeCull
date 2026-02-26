"""
Scanner — finds feature flag usage in a codebase and cross-references a
feature flag service (Unleash or mock JSON) to identify stale flags.

A flag is considered *stale* when:
  - It has been serving a single variation (always-on or always-off) in
    production for >= 90 days, AND
  - It is NOT in an active percentage rollout.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class FlagOccurrence:
    """A single reference to a feature flag in a source file."""

    file_path: str
    line_number: int
    line_text: str


@dataclass
class FlagCandidate:
    """A cleanup candidate surfaced by the scanner."""

    flag_key: str
    flag_name: str
    description: str
    variation_served: str  # "always-on" | "always-off"
    days_stale: int
    files_affected: list[str] = field(default_factory=list)
    total_lines: int = 0
    occurrences: list[FlagOccurrence] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    maintainer_email: str = ""
    maintainer_name: str = ""
    status: str = "pending"  # pending | approved | skipped | in_progress | done


# ---------------------------------------------------------------------------
# Code scanner
# ---------------------------------------------------------------------------

# Patterns that indicate feature flag usage
FLAG_PATTERNS = [
    # is_enabled("flag-key")  or  is_enabled('flag-key')
    re.compile(r"""is_enabled\(\s*["']([a-z0-9\-]+)["']\s*\)"""),
    # Generic variation / flag check patterns
    re.compile(r"""feature_flags?\.is_enabled\(\s*["']([a-z0-9\-]+)["']\s*\)"""),
    # String references to flag keys in config / constants
    re.compile(r"""["']([a-z]+-[a-z0-9\-]+-[a-z0-9\-]+)["']"""),
]


def scan_codebase(repo_path: str, extensions: tuple[str, ...] = (".py",)) -> dict[str, list[FlagOccurrence]]:
    """Walk *repo_path* and return a mapping of flag_key -> [FlagOccurrence].

    Only files with one of the given *extensions* are inspected.
    """
    flags: dict[str, list[FlagOccurrence]] = {}
    repo = Path(repo_path)

    for file_path in repo.rglob("*"):
        if not file_path.is_file():
            continue
        if file_path.suffix not in extensions:
            continue
        # Skip hidden dirs, __pycache__, etc.
        parts = file_path.relative_to(repo).parts
        if any(p.startswith(".") or p == "__pycache__" for p in parts):
            continue

        try:
            lines = file_path.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, PermissionError):
            continue

        for lineno, line in enumerate(lines, start=1):
            for pattern in FLAG_PATTERNS:
                for match in pattern.finditer(line):
                    key = match.group(1)
                    occurrence = FlagOccurrence(
                        file_path=str(file_path.relative_to(repo)),
                        line_number=lineno,
                        line_text=line.strip(),
                    )
                    flags.setdefault(key, []).append(occurrence)

    return flags


# ---------------------------------------------------------------------------
# Flag service readers (Unleash or mock JSON fallback)
# ---------------------------------------------------------------------------

def load_ld_flags(ld_data_path: str) -> dict:
    """Load the mock LaunchDarkly JSON and return a dict keyed by flag key."""
    with open(ld_data_path, encoding="utf-8") as fh:
        data = json.load(fh)
    return {f["key"]: f for f in data["flags"]}


def _unleash_login(base_url: str) -> str:
    """Authenticate to Unleash and return a session cookie string."""
    user = os.getenv("UNLEASH_ADMIN_USER", "admin")
    password = os.getenv("UNLEASH_ADMIN_PASSWORD", "unleash4all")
    with httpx.Client(timeout=30) as client:
        resp = client.post(
            f"{base_url}/auth/simple/login",
            json={"username": user, "password": password},
        )
        resp.raise_for_status()
        # Session cookie is returned in Set-Cookie header
        return resp.headers.get("set-cookie", "")


def load_unleash_flags(base_url: str, environment: str = "development") -> dict:
    """Fetch flags from Unleash Admin API and normalise to the LD-like schema.

    Returns a dict keyed by flag name (key) with the same shape the
    ``analyse_flags`` function expects, so the rest of the pipeline is
    unchanged.
    """
    api_token = os.getenv("UNLEASH_ADMIN_TOKEN", "")
    headers: dict[str, str] = {"Content-Type": "application/json"}
    cookies: dict[str, str] = {}

    if api_token:
        headers["Authorization"] = api_token
    else:
        # Fall back to session-based auth
        cookie_str = _unleash_login(base_url)
        if cookie_str:
            # Parse "unleash-session=abc123; Path=/; ..." → {"unleash-session": "abc123"}
            for part in cookie_str.split(","):
                part = part.strip()
                if "=" in part and not part.lower().startswith(("path", "expires", "max-age", "domain", "samesite", "httponly", "secure")):
                    k, v = part.split("=", 1)
                    # Strip trailing cookie attributes after ;
                    v = v.split(";")[0]
                    cookies[k.strip()] = v.strip()

    project = os.getenv("UNLEASH_PROJECT", "default")
    url = f"{base_url}/api/admin/projects/{project}/features"
    logger.info("Fetching flags from Unleash: %s", url)

    with httpx.Client(timeout=30) as client:
        resp = client.get(url, headers=headers, cookies=cookies)
        resp.raise_for_status()

    data = resp.json()
    features = data.get("features", [])

    # Owner email/name from env (Unleash OSS doesn't track per-flag owners)
    owner_email = os.getenv("FLAG_OWNER_EMAIL", "")
    owner_name = os.getenv("FLAG_OWNER_NAME", "")

    normalised: dict[str, dict] = {}
    for feat in features:
        name = feat["name"]
        created_at = feat.get("createdAt") or None
        stale = feat.get("stale", False)

        # Find the target environment
        env_data: dict = {}
        for env in feat.get("environments", []):
            if env.get("name") == environment:
                env_data = env
                break

        enabled = env_data.get("enabled", False)

        # Detect percentage rollout from strategies
        strategies = env_data.get("strategies", [])
        is_rollout = False
        rollout_pct: int | None = None
        for strat in strategies:
            if strat.get("name") == "flexibleRollout":
                pct = int(strat.get("parameters", {}).get("rollout", "100"))
                if pct < 100:
                    is_rollout = True
                    rollout_pct = pct

        # Build the LD-like structure
        if enabled:
            variation_served = 0  # True / Enabled
        else:
            variation_served = 1  # False / Disabled

        normalised[name] = {
            "key": name,
            "name": name.replace("-", " ").title(),
            "description": feat.get("description", ""),
            "kind": "boolean",
            "creation_date": created_at,
            "variations": [
                {"value": True, "name": "Enabled"},
                {"value": False, "name": "Disabled"},
            ],
            "on": enabled,
            "percentage_rollout": {"weight": rollout_pct} if is_rollout else None,
            "environments": {
                "production": {
                    "on": enabled,
                    "variation_served": variation_served,
                    "variation_served_since": created_at,
                },
            },
            "tags": [t.get("value", "") for t in feat.get("tags", [])],
            "stale": stale,
            "maintainer_email": owner_email,
            "maintainer_name": owner_name,
        }

    logger.info("Loaded %d flags from Unleash", len(normalised))
    return normalised


# ---------------------------------------------------------------------------
# Staleness analysis
# ---------------------------------------------------------------------------

STALE_THRESHOLD_DAYS = 90


def _days_since(iso_date: str | None) -> int | None:
    """Return the number of days between *iso_date* and now (UTC)."""
    if iso_date is None:
        return None
    dt = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - dt).days


def analyse_flags(
    code_flags: dict[str, list[FlagOccurrence]],
    ld_flags: dict,
) -> list[FlagCandidate]:
    """Cross-reference code usage with LD metadata and return stale candidates.

    A flag is stale when:
      1. It appears in the codebase.
      2. It is NOT in a percentage rollout.
      3. It has served a single variation for >= STALE_THRESHOLD_DAYS.

    Returns a list sorted by staleness (most stale first).
    """
    candidates: list[FlagCandidate] = []

    for flag_key, occurrences in code_flags.items():
        ld = ld_flags.get(flag_key)
        if ld is None:
            continue  # flag not known to LD — skip

        # Skip active percentage rollouts
        if ld.get("percentage_rollout") is not None:
            continue

        prod = ld.get("environments", {}).get("production", {})
        variation_served = prod.get("variation_served")
        served_since = prod.get("variation_served_since")

        if variation_served is None or served_since is None:
            continue  # not deterministic — skip

        days = _days_since(served_since)
        if days is None or days < STALE_THRESHOLD_DAYS:
            continue

        # Determine human-readable variation label
        variations = ld.get("variations", [])
        if variation_served < len(variations):
            val = variations[variation_served]["value"]
            variation_label = "always-on" if val is True else "always-off"
        else:
            variation_label = f"variation-{variation_served}"

        files = sorted({occ.file_path for occ in occurrences})
        total_lines = sum(1 for _ in occurrences)

        candidates.append(
            FlagCandidate(
                flag_key=flag_key,
                flag_name=ld.get("name", flag_key),
                description=ld.get("description", ""),
                variation_served=variation_label,
                days_stale=days,
                files_affected=files,
                total_lines=total_lines,
                occurrences=occurrences,
                tags=ld.get("tags", []),
                maintainer_email=ld.get("maintainer_email", ""),
                maintainer_name=ld.get("maintainer_name", ""),
            )
        )

    # Most stale first
    candidates.sort(key=lambda c: c.days_stale, reverse=True)
    return candidates


# ---------------------------------------------------------------------------
# Convenience entry point
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Repo cloning helper
# ---------------------------------------------------------------------------

_cloned_repo_dir: str | None = None


def _build_repo_url() -> tuple[str, str]:
    """Build the clone URL, returning ``(url, token)``.

    If ``GITHUB_TOKEN`` is set the token is embedded in the URL for
    authentication.  The token is also returned so callers can sanitize
    log / error messages.
    """
    repo_slug = os.getenv("TARGET_REPO", "bgtripp/LogiOps")
    token = os.getenv("GITHUB_TOKEN", "")
    if token:
        return f"https://x-access-token:{token}@github.com/{repo_slug}.git", token
    return f"https://github.com/{repo_slug}.git", ""


def _sanitize(text: str, token: str) -> str:
    """Replace *token* in *text* with ``***`` to avoid leaking credentials."""
    if token:
        return text.replace(token, "***")
    return text


def _download_tarball() -> str:
    """Download the repo as a tarball via GitHub API (no git required)."""
    repo_slug = os.getenv("TARGET_REPO", "bgtripp/LogiOps")
    token = os.getenv("GITHUB_TOKEN", "")
    headers: dict[str, str] = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = f"https://api.github.com/repos/{repo_slug}/tarball"
    logger.info("Downloading tarball for %s via GitHub API", repo_slug)

    with httpx.Client(follow_redirects=True, timeout=120) as client:
        resp = client.get(url, headers=headers)
        resp.raise_for_status()

    tmp = tempfile.mkdtemp(prefix="codecull-target-")
    with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tar:
        tar.extractall(tmp)  # noqa: S202

    # GitHub tarballs extract into a single top-level directory
    entries = list(Path(tmp).iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        return str(entries[0])
    return tmp


def clone_target_repo() -> str:
    """Clone the target GitHub repo into a temp directory and return its path.

    The clone is cached for the lifetime of the process so repeated scans
    do ``git pull`` instead of a full clone.

    If ``GITHUB_TOKEN`` is set, it is used to authenticate the clone
    (required for private repos).

    Falls back to downloading a tarball via GitHub API when ``git`` is not
    available (e.g. in containerised deployments).
    """
    global _cloned_repo_dir

    # Check if git is available
    if not shutil.which("git"):
        if _cloned_repo_dir and Path(_cloned_repo_dir).exists():
            return _cloned_repo_dir
        logger.info("git not found — falling back to GitHub API tarball download")
        _cloned_repo_dir = _download_tarball()
        return _cloned_repo_dir

    repo_url, token = _build_repo_url()

    if _cloned_repo_dir and Path(_cloned_repo_dir).exists():
        logger.info("Pulling latest changes in cached clone %s", _cloned_repo_dir)
        result = subprocess.run(
            ["git", "pull", "--ff-only"],
            cwd=_cloned_repo_dir,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            logger.warning("git pull failed (rc=%d): %s", result.returncode, _sanitize(result.stderr.strip(), token))
        return _cloned_repo_dir

    tmp = tempfile.mkdtemp(prefix="codecull-target-")
    repo_slug = os.getenv("TARGET_REPO", "bgtripp/LogiOps")
    logger.info("Cloning %s into %s", repo_slug, tmp)
    result = subprocess.run(
        ["git", "clone", repo_url, tmp],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError(f"Failed to clone {repo_slug}: {_sanitize(result.stderr.strip(), token)}")

    _cloned_repo_dir = tmp
    return tmp


def get_target_repo_path() -> str:
    """Return the local path to the target repo.

    If ``TARGET_REPO_PATH`` is set, use it directly (local development).
    Otherwise clone ``TARGET_REPO`` from GitHub.
    """
    explicit = os.getenv("TARGET_REPO_PATH")
    if explicit:
        return explicit
    return clone_target_repo()


def run_scan(
    repo_path: str | None = None,
    ld_data_path: str | None = None,
) -> list[FlagCandidate]:
    """Run a full scan and return stale flag candidates.

    Uses Unleash Admin API when ``UNLEASH_URL`` is set, otherwise falls
    back to the mock LaunchDarkly JSON file.
    """
    repo_path = repo_path or get_target_repo_path()

    unleash_url = os.getenv("UNLEASH_URL", "")
    if unleash_url:
        flag_data = load_unleash_flags(unleash_url)
    else:
        ld_data_path = ld_data_path or os.getenv("MOCK_LD_DATA_PATH", "./mock_launchdarkly.json")
        flag_data = load_ld_flags(ld_data_path)

    code_flags = scan_codebase(repo_path)
    return analyse_flags(code_flags, flag_data)
