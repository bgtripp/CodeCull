"""
CodeCull Dashboard — FastAPI + Jinja2 web UI.

Displays a review queue of stale feature-flag cleanup PRs.  Devin creates
draft PRs in advance (dispatched on startup / cron).  Engineers open the
dashboard, see how many lines / files each PR touches, and click through to
GitHub — or skip for now.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from scanner.flag_scanner import FlagCandidate, run_scan
from scanner.github_stats import fetch_pr_stats
from scanner.state_store import load_state, save_state

# Use explicit path so .env is found regardless of CWD / uvicorn reloader.
# override=True ensures stale shell env vars don't shadow the .env values.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env", override=True)

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# ---------------------------------------------------------------------------
# In-memory state (sufficient for a demo)
# ---------------------------------------------------------------------------
_candidates: list[FlagCandidate] = []
_sessions: dict[str, dict] = {}  # flag_key -> {session_id, url, status, pr_url}
_pr_stats: dict[str, dict] = {}  # flag_key -> {files_changed, additions, deletions, ...}
_last_scan_time: datetime | None = None


_STATE_PATH = Path(os.getenv("CODECULL_STATE_PATH", str(_PROJECT_ROOT / ".codecull_state.json")))


def _apply_state_to_candidates(candidates: list[FlagCandidate]) -> None:
    """Overlay persisted session/PR state on top of freshly scanned candidates."""
    for c in candidates:
        s = _sessions.get(c.flag_key)
        if not s:
            c.status = "pending"
            continue

        pr_url = s.get("pr_url")
        status = s.get("status")

        if pr_url:
            c.status = "done"
        elif status in ("running", "in_progress"):
            c.status = "in_progress"
        else:
            c.status = "pending"


# ---------------------------------------------------------------------------
# Lifespan — scan + load persisted state
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Run the scanner at startup, then overlay persisted PR/session state."""
    global _candidates, _sessions, _pr_stats, _last_scan_time

    logger.info("Running scan...")
    _candidates = run_scan()
    _last_scan_time = datetime.now(timezone.utc)

    state = load_state(_STATE_PATH)
    _sessions = state.get("sessions", {}) or {}
    _pr_stats = state.get("pr_stats", {}) or {}

    _apply_state_to_candidates(_candidates)

    # Sort: most lines removed first, then most stale
    _candidates.sort(
        key=lambda c: (
            _pr_stats.get(c.flag_key, {}).get("deletions", 0),
            c.days_stale,
        ),
        reverse=True,
    )

    logger.info(
        "Loaded %d candidates, %d sessions, %d PR stats",
        len(_candidates),
        len(_sessions),
        len(_pr_stats),
    )

    yield


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="CodeCull Dashboard", lifespan=lifespan)

BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Main dashboard page.

    On every load we re-check each PR's status on GitHub.  Merged or
    closed PRs are automatically removed from the queue so the engineer
    never has to manually dismiss them.
    """
    _refresh_pr_statuses()

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "candidates": _candidates,
            "sessions": _sessions,
            "pr_stats": _pr_stats,
            "last_scan_time": _last_scan_time,
        },
    )


@app.post("/skip/{flag_key}")
async def skip_flag(flag_key: str):
    """Mark a flag as skipped."""
    candidate = _find_candidate(flag_key)
    if candidate is not None:
        candidate.status = "skipped"
    return RedirectResponse(url="/", status_code=303)


@app.get("/status/{flag_key}")
async def flag_status(flag_key: str):
    """Return JSON status for a flag (used by the dashboard for polling)."""
    session_info = _sessions.get(flag_key, {})
    candidate = _find_candidate(flag_key)
    return {
        "flag_key": flag_key,
        "candidate_status": candidate.status if candidate else "unknown",
        "session": session_info,
        "pr_stats": _pr_stats.get(flag_key),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _refresh_pr_statuses() -> None:
    """Check GitHub for merged/closed PRs and drop them from the queue."""
    global _candidates, _sessions, _pr_stats

    keys_to_remove: list[str] = []

    for flag_key, session in list(_sessions.items()):
        pr_url = session.get("pr_url")
        if not pr_url:
            continue

        stats = fetch_pr_stats(pr_url)
        if stats is None:
            continue

        # Update cached stats while we're at it
        _pr_stats[flag_key] = stats

        if stats.get("merged") or stats.get("state") == "closed":
            logger.info("PR for %s is %s — removing from queue", flag_key,
                        "merged" if stats.get("merged") else "closed")
            keys_to_remove.append(flag_key)

    if keys_to_remove:
        for key in keys_to_remove:
            _sessions.pop(key, None)
            _pr_stats.pop(key, None)

        _candidates = [c for c in _candidates if c.flag_key not in keys_to_remove]

        # Persist the updated state so restarts also reflect the removal
        save_state(_STATE_PATH, _sessions, _pr_stats)
        logger.info("Removed %d merged/closed PR(s) from queue", len(keys_to_remove))


def _find_candidate(flag_key: str) -> FlagCandidate | None:
    for c in _candidates:
        if c.flag_key == flag_key:
            return c
    return None
