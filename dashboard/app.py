"""
CodeCull Dashboard — FastAPI + Jinja2 web UI.

Displays a review queue of stale feature-flag cleanup PRs.  The scanner runs
automatically (on startup / cron) and Devin creates draft PRs in advance.
Engineers open the dashboard, review the dead-code preview, and click through
to the PR — or skip for now.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from scanner.devin_integration import (
    create_cleanup_session,
    extract_pr_url,
    poll_session_until_done,
)
from scanner.dead_code_analyzer import CleanupPreview, generate_cleanup_preview
from scanner.flag_scanner import FlagCandidate, get_target_repo_path, run_scan
from scanner.slack_notify import notify_flag_author

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
_previews: dict[str, CleanupPreview] = {}  # flag_key -> dead-code preview
_last_scan_time: datetime | None = None


# ---------------------------------------------------------------------------
# Lifespan — run scanner on startup
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Run the scanner once at startup to populate candidates."""
    global _candidates, _previews, _last_scan_time
    logger.info("Running initial scan...")
    _candidates = run_scan()
    _last_scan_time = datetime.now(timezone.utc)
    _previews = _generate_previews(_candidates)
    logger.info("Found %d stale flag candidates", len(_candidates))
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
    """Main dashboard page."""
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "candidates": _candidates,
            "sessions": _sessions,
            "previews": _previews,
            "last_scan_time": _last_scan_time,
        },
    )


@app.post("/approve/{flag_key}")
async def approve_flag(flag_key: str, background_tasks: BackgroundTasks):
    """Approve a flag for cleanup — dispatches a Devin session."""
    candidate = _find_candidate(flag_key)
    if candidate is None:
        return RedirectResponse(url="/", status_code=303)

    candidate.status = "in_progress"

    target_repo = os.getenv("TARGET_REPO", "bgtripp/LogiOps")

    try:
        result = create_cleanup_session(
            flag_key=flag_key,
            repo=target_repo,
            variation=candidate.variation_served,
            files=candidate.files_affected,
        )
        _sessions[flag_key] = {
            "session_id": result["session_id"],
            "url": result["url"],
            "status": "running",
            "pr_url": None,
        }
        # Poll in background
        background_tasks.add_task(_poll_and_notify, flag_key, candidate)
    except Exception:
        logger.exception("Failed to create Devin session for %s", flag_key)
        candidate.status = "error"

    return RedirectResponse(url="/", status_code=303)


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
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _generate_previews(candidates: list[FlagCandidate]) -> dict[str, CleanupPreview]:
    """Generate dead-code previews for all candidates."""
    previews: dict[str, CleanupPreview] = {}
    try:
        repo_path = get_target_repo_path()
    except Exception:
        logger.exception("Cannot generate previews — target repo unavailable")
        return previews

    for c in candidates:
        try:
            preview = generate_cleanup_preview(
                repo_path=repo_path,
                flag_key=c.flag_key,
                variation=c.variation_served,
                affected_files=c.files_affected,
            )
            if preview.total_dead_lines > 0:
                previews[c.flag_key] = preview
        except Exception:
            logger.exception("Failed to generate preview for %s", c.flag_key)
    return previews


def _find_candidate(flag_key: str) -> FlagCandidate | None:
    for c in _candidates:
        if c.flag_key == flag_key:
            return c
    return None


def _poll_and_notify(flag_key: str, candidate: FlagCandidate) -> None:
    """Background task: poll Devin session, then send Slack DM."""
    session_info = _sessions.get(flag_key)
    if not session_info:
        return

    session_id = session_info["session_id"]
    logger.info("Polling Devin session %s for flag %s", session_id, flag_key)

    final = poll_session_until_done(session_id)
    status_enum = final.get("status_enum", "unknown")

    _sessions[flag_key]["status"] = status_enum

    pr_url = extract_pr_url(final)
    _sessions[flag_key]["pr_url"] = pr_url

    if status_enum == "finished" and pr_url:
        candidate.status = "done"

        # Send Slack DM to the flag author
        repo_path = get_target_repo_path()
        first_file = candidate.files_affected[0] if candidate.files_affected else ""

        try:
            notify_flag_author(
                repo_path=repo_path,
                file_path=first_file,
                flag_key=flag_key,
                pr_url=pr_url,
                session_url=session_info.get("url", ""),
            )
        except Exception:
            logger.exception("Slack notification failed for %s", flag_key)
    else:
        candidate.status = "error"
        logger.warning("Session %s ended with status %s", session_id, status_enum)
