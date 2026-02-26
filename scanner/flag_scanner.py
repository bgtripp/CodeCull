"""
Scanner — finds feature flag usage in a codebase and cross-references a
mock LaunchDarkly service to identify stale flags.

A flag is considered *stale* when:
  - It has been serving a single variation (always-on or always-off) in
    production for >= 90 days, AND
  - It is NOT in an active percentage rollout.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

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
# LaunchDarkly mock reader
# ---------------------------------------------------------------------------

def load_ld_flags(ld_data_path: str) -> dict:
    """Load the mock LaunchDarkly JSON and return a dict keyed by flag key."""
    with open(ld_data_path, encoding="utf-8") as fh:
        data = json.load(fh)
    return {f["key"]: f for f in data["flags"]}


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


def clone_target_repo() -> str:
    """Clone the target GitHub repo into a temp directory and return its path.

    The clone is cached for the lifetime of the process so repeated scans
    do ``git pull`` instead of a full clone.
    """
    global _cloned_repo_dir

    repo_slug = os.getenv("TARGET_REPO", "bgtripp/LogiOps")
    repo_url = f"https://github.com/{repo_slug}.git"

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
            logger.warning("git pull failed (rc=%d): %s", result.returncode, result.stderr.strip())
        return _cloned_repo_dir

    tmp = tempfile.mkdtemp(prefix="codecull-target-")
    logger.info("Cloning %s into %s", repo_url, tmp)
    result = subprocess.run(
        ["git", "clone", repo_url, tmp],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError(f"Failed to clone {repo_url}: {result.stderr.strip()}")

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
    """Run a full scan and return stale flag candidates."""
    repo_path = repo_path or get_target_repo_path()
    ld_data_path = ld_data_path or os.getenv("MOCK_LD_DATA_PATH", "./mock_launchdarkly.json")

    code_flags = scan_codebase(repo_path)
    ld_flags = load_ld_flags(ld_data_path)
    return analyse_flags(code_flags, ld_flags)
