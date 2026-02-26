"""
CodeCull Dashboard — FastAPI + Jinja2 web UI.

Shows ranked cleanup candidates with approve / skip buttons.  On approval the
Devin API is called to spin up a cleanup session.
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
from scanner.flag_scanner import FlagCandidate, get_target_repo_path, run_scan
from scanner.slack_notify import notify_flag_author

load_dotenv()

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# ---------------------------------------------------------------------------
# In-memory state (sufficient for a demo)
# ---------------------------------------------------------------------------
_candidates: list[FlagCandidate] = []
_sessions: dict[str, dict] = {}  # flag_key -> {session_id, url, status, pr_url}
_last_scan_time: datetime | None = None


# ---------------------------------------------------------------------------
# Lifespan — run scanner on startup
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Run the scanner once at startup to populate candidates."""
    global _candidates, _last_scan_time
    logger.info("Running initial scan...")
    _candidates = run_scan()
    _last_scan_time = datetime.now(timezone.utc)
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
            "last_scan_time": _last_scan_time,
        },
    )


@app.post("/scan")
async def rescan():
    """Re-run the scanner and redirect back to the dashboard."""
    global _candidates, _last_scan_time
    _candidates = run_scan()
    _last_scan_time = datetime.now(timezone.utc)
    return RedirectResponse(url="/", status_code=303)


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
