"""State store for CodeCull.

The dashboard is a review hub and should not need to kick off Devin work.
Instead, a scheduled job runs scans + Devin sessions and writes the resulting
PR URLs and PR stats to a JSON file.

This module reads/writes that JSON state.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def load_state(path: Path) -> dict[str, Any]:
    """Load state from *path*.

    Returns an empty dict if the file doesn't exist or can't be parsed.
    """
    if not path.exists():
        return {}

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Failed to load state file %s", path)
        return {}


def save_state(
    path: Path,
    sessions: dict[str, Any],
    pr_stats: dict[str, Any],
    stacked_sessions: dict[str, Any] | None = None,
) -> None:
    """Persist *sessions*, *pr_stats*, and *stacked_sessions* to *path* as JSON."""
    payload: dict[str, Any] = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "sessions": sessions,
        "pr_stats": pr_stats,
    }
    if stacked_sessions is not None:
        payload["stacked_sessions"] = stacked_sessions

    try:
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    except Exception:
        logger.exception("Failed to write state file %s", path)
