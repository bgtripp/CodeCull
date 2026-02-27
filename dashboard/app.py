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
import secrets
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer

from scanner.demo_reset import run_demo_reset
from scanner.devin_integration import (
    create_cleanup_session,
    create_rebase_session,
    extract_pr_url,
    get_session_status,
    poll_session_until_done,
)
from scanner.flag_scanner import FlagCandidate, run_scan
from scanner.github_stats import (
    discover_cleanup_prs,
    fetch_pr_stats,
    list_pull_requests,
    merge_main_into_branch,
)
from scanner.pr_sync import sync_state
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
        elif status in ("error", "finished", "stopped", "blocked"):
            c.status = "error"
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
# Email OTP Auth
# ---------------------------------------------------------------------------
_SESSION_SECRET = os.getenv("SESSION_SECRET", secrets.token_hex(32))
_RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
_SESSION_MAX_AGE = 60 * 60 * 24 * 7  # 7 days
_OTP_TTL = 600  # 10 minutes

_serializer = URLSafeTimedSerializer(_SESSION_SECRET)

# Allowed email addresses / domains
_ALLOWED_EMAILS: set[str] = set()
_ALLOWED_DOMAINS: set[str] = set()

for entry in os.getenv("ALLOWED_AUTH_EMAILS", "bentrippx@gmail.com,@cognition.ai").split(","):
    entry = entry.strip().lower()
    if not entry:
        continue
    if entry.startswith("@"):
        _ALLOWED_DOMAINS.add(entry)
    else:
        _ALLOWED_EMAILS.add(entry)

# In-memory OTP store: email -> {code, expires_at}
_otp_store: dict[str, dict] = {}


def _is_email_allowed(email: str) -> bool:
    """Check if an email is in the allow-list."""
    email = email.lower().strip()
    if email in _ALLOWED_EMAILS:
        return True
    domain = "@" + email.split("@")[-1]
    return domain in _ALLOWED_DOMAINS


def _generate_otp(email: str) -> str:
    """Generate and store a 6-digit OTP for the given email."""
    code = "".join(secrets.choice("0123456789") for _ in range(6))
    _otp_store[email.lower()] = {
        "code": code,
        "expires_at": time.time() + _OTP_TTL,
        "attempts": 0,
    }
    return code


def _verify_otp(email: str, code: str) -> bool:
    """Verify an OTP code for the given email."""
    entry = _otp_store.get(email.lower())
    if not entry:
        return False
    if time.time() > entry["expires_at"]:
        _otp_store.pop(email.lower(), None)
        return False
    if not secrets.compare_digest(entry["code"], code.strip()):
        entry["attempts"] = entry.get("attempts", 0) + 1
        if entry["attempts"] >= 5:
            _otp_store.pop(email.lower(), None)
        return False
    # OTP is single-use
    _otp_store.pop(email.lower(), None)
    return True


def _send_otp_email(email: str, code: str) -> bool:
    """Send the OTP code via Resend API. Returns True on success."""
    if not _RESEND_API_KEY:
        logger.warning("RESEND_API_KEY not set — logging OTP to console instead")
        logger.info("OTP for %s: %s", email, code)
        return True

    try:
        resp = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {_RESEND_API_KEY}"},
            json={
                "from": os.getenv("OTP_FROM_EMAIL", "CodeCull <onboarding@resend.dev>"),
                "to": [email],
                "subject": f"Your CodeCull login code: {code}",
                "html": (
                    f"<h2>Your CodeCull verification code</h2>"
                    f"<p style='font-size:32px;font-family:monospace;letter-spacing:8px;"  # noqa: E501
                    f"font-weight:bold;'>{code}</p>"
                    f"<p>This code expires in 10 minutes.</p>"
                    f"<p style='color:#666;font-size:12px;'>If you didn't request this, ignore this email.</p>"
                ),
            },
            timeout=15,
        )
        if resp.status_code >= 400:
            logger.error("Resend API error %d: %s", resp.status_code, resp.text)
            return False
        return True
    except Exception:
        logger.exception("Failed to send OTP email")
        return False


def _get_session_email(request: Request) -> str | None:
    """Extract the authenticated email from the session cookie."""
    token = request.cookies.get("codecull_session")
    if not token:
        return None
    try:
        email = _serializer.loads(token, max_age=_SESSION_MAX_AGE)
        return email
    except BadSignature:
        return None


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.get("/auth/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Show the email login form."""
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/auth/login")
def login_submit(request: Request, email: str = Form(...)):
    """Validate email, generate OTP, send it, redirect to verify page."""
    email = email.strip().lower()

    if not _is_email_allowed(email):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "This email address is not authorized."},
            status_code=403,
        )

    code = _generate_otp(email)
    sent = _send_otp_email(email, code)

    if not sent:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Failed to send email. Please try again."},
            status_code=500,
        )

    return templates.TemplateResponse(
        "verify.html",
        {"request": request, "email": email, "error": None},
    )


@app.post("/auth/verify")
def verify_submit(request: Request, email: str = Form(...), code: str = Form(...)):
    """Verify OTP code and set session cookie."""
    email = email.strip().lower()

    if not _is_email_allowed(email) or not _verify_otp(email, code):
        return templates.TemplateResponse(
            "verify.html",
            {"request": request, "email": email, "error": "Invalid or expired code. Please try again."},
            status_code=401,
        )

    # Create signed session cookie
    token = _serializer.dumps(email)
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        key="codecull_session",
        value=token,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        max_age=_SESSION_MAX_AGE,
        path="/",
    )
    logger.info("User %s authenticated successfully", email)
    return response


@app.get("/auth/logout")
async def logout():
    """Clear session cookie and redirect to login."""
    response = RedirectResponse(url="/auth/login", status_code=303)
    response.delete_cookie("codecull_session", path="/")
    return response


# ---------------------------------------------------------------------------
# Protected routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    """Main dashboard page.

    On every load we re-check each PR's status on GitHub.  Merged or
    closed PRs are automatically removed from the queue so the engineer
    never has to manually dismiss them.
    """
    email = _get_session_email(request)
    if not email:
        return RedirectResponse(url="/auth/login", status_code=303)

    _refresh_pr_statuses()

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "candidates": _candidates,
            "sessions": _sessions,
            "pr_stats": _pr_stats,
            "last_scan_time": _last_scan_time,
            "user_email": email,
        },
    )


@app.post("/skip/{flag_key}")
async def skip_flag(request: Request, flag_key: str):
    """Mark a flag as skipped."""
    if not _get_session_email(request):
        return RedirectResponse(url="/auth/login", status_code=303)
    candidate = _find_candidate(flag_key)
    if candidate is not None:
        candidate.status = "skipped"
    return RedirectResponse(url="/", status_code=303)


@app.get("/status/{flag_key}")
async def flag_status(request: Request, flag_key: str):
    """Return JSON status for a flag (used by the dashboard for polling)."""
    if not _get_session_email(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
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
    """Check GitHub for merged/closed PRs and drop them from the queue.

    For sessions still marked *running* (no PR yet), queries the Devin API
    to see if the session finished and produced a PR URL.  This lets the
    dashboard pick up completed Devin work even after a Fly.io machine
    restart wiped in-memory state.

    Also removes error-state sessions (no PR created) so they don't persist
    forever on the dashboard.
    """
    global _candidates, _sessions, _pr_stats

    keys_to_remove: list[str] = []
    state_changed = False

    for flag_key, session in list(_sessions.items()):
        pr_url = session.get("pr_url")

        # For sessions without a PR, check Devin for updates
        if not pr_url:
            status = session.get("status", "")
            sid = session.get("session_id")

            # If session is still running, poll Devin for completion
            if sid and status == "running":
                try:
                    devin_status = get_session_status(sid)
                    state = devin_status.get("status_enum", "unknown")

                    if state in ("finished", "stopped", "blocked"):
                        found_pr = extract_pr_url(devin_status)
                        if found_pr:
                            session["pr_url"] = found_pr
                            session["status"] = "ready"
                            state_changed = True
                            logger.info("%s: Devin finished, PR found: %s", flag_key, found_pr)
                        else:
                            session["status"] = "error"
                            state_changed = True
                            logger.info("%s: Devin finished but no PR (state=%s)", flag_key, state)
                except Exception:
                    logger.exception("Failed to check Devin status for %s", flag_key)
                continue

            # Remove sessions stuck in error/terminal state with no PR
            if status in ("error", "finished", "stopped", "blocked"):
                logger.info("Removing %s — session ended without a PR (status=%s)", flag_key, status)
                keys_to_remove.append(flag_key)
            continue

        stats = fetch_pr_stats(pr_url)
        if stats is None:
            continue

        # Update cached stats while we're at it
        _pr_stats[flag_key] = stats
        state_changed = True

        if stats.get("merged") or stats.get("state") == "closed":
            logger.info("PR for %s is %s — removing from queue", flag_key,
                        "merged" if stats.get("merged") else "closed")
            keys_to_remove.append(flag_key)

    if keys_to_remove:
        for key in keys_to_remove:
            _sessions.pop(key, None)
            _pr_stats.pop(key, None)

        _candidates = [c for c in _candidates if c.flag_key not in keys_to_remove]
        state_changed = True

    if state_changed:
        # Re-apply statuses and persist
        _apply_state_to_candidates(_candidates)
        save_state(_STATE_PATH, _sessions, _pr_stats)
        logger.info("State refreshed: removed %d, updated others", len(keys_to_remove))


def _find_candidate(flag_key: str) -> FlagCandidate | None:
    for c in _candidates:
        if c.flag_key == flag_key:
            return c
    return None


# ---------------------------------------------------------------------------
# Sync API
# ---------------------------------------------------------------------------

_sync_lock = threading.Lock()
_sync_status: dict = {"running": False, "last_run": None, "result": None, "error": None}

_SYNC_API_TOKEN = os.getenv("SYNC_API_TOKEN", "")


def _run_sync_background() -> None:
    """Execute sync_state in a background thread and reload dashboard state."""
    global _candidates, _sessions, _pr_stats, _last_scan_time
    try:
        result = sync_state(state_path=_STATE_PATH)
        # Reload dashboard in-memory state from the updated file
        _candidates = run_scan()
        _last_scan_time = datetime.now(timezone.utc)
        state = load_state(_STATE_PATH)
        _sessions = state.get("sessions", {}) or {}
        _pr_stats = state.get("pr_stats", {}) or {}
        _apply_state_to_candidates(_candidates)
        _candidates.sort(
            key=lambda c: (
                _pr_stats.get(c.flag_key, {}).get("deletions", 0),
                c.days_stale,
            ),
            reverse=True,
        )
        _sync_status["result"] = {
            "prs_ready": sum(1 for s in _sessions.values() if s.get("pr_url")),
            "candidates": len(_candidates),
        }
        _sync_status["error"] = None
        logger.info("Background sync completed successfully")
    except Exception as exc:
        logger.exception("Background sync failed")
        _sync_status["error"] = str(exc)
    finally:
        _sync_status["running"] = False
        _sync_status["last_run"] = datetime.now(timezone.utc).isoformat()


@app.post("/api/sync")
def api_sync(request: Request):
    """Trigger a full sync (scan + Devin dispatch + Slack notify).

    Gated by either:
      - A valid OTP session cookie (browser users), OR
      - A Bearer token matching SYNC_API_TOKEN (GitHub Actions / curl)
    """
    # Check session auth first
    email = _get_session_email(request)
    if not email:
        # Fall back to Bearer token auth
        auth_header = request.headers.get("authorization", "")
        if _SYNC_API_TOKEN and auth_header == f"Bearer {_SYNC_API_TOKEN}":
            pass  # authorized via token
        else:
            raise HTTPException(status_code=401, detail="Not authenticated")

    if _sync_status["running"]:
        return {"status": "already_running", "message": "A sync is already in progress."}

    if not _sync_lock.acquire(blocking=False):
        return {"status": "already_running", "message": "A sync is already in progress."}

    _sync_status["running"] = True
    _sync_status["error"] = None

    def _worker() -> None:
        try:
            _run_sync_background()
        finally:
            _sync_lock.release()

    threading.Thread(target=_worker, daemon=True).start()
    return {"status": "started", "message": "Sync started in background."}


@app.get("/api/sync/status")
def api_sync_status(request: Request):
    """Check the status of the last/current sync run."""
    email = _get_session_email(request)
    if not email:
        auth_header = request.headers.get("authorization", "")
        if _SYNC_API_TOKEN and auth_header == f"Bearer {_SYNC_API_TOKEN}":
            pass
        else:
            raise HTTPException(status_code=401, detail="Not authenticated")
    return _sync_status


# ---------------------------------------------------------------------------
# Auto-rebase API
# ---------------------------------------------------------------------------

_rebase_lock = threading.Lock()
_rebase_status: dict = {"running": False, "last_run": None, "results": []}


def _check_auth(request: Request) -> None:
    """Verify the request has a valid session cookie or Bearer token."""
    email = _get_session_email(request)
    if not email:
        auth_header = request.headers.get("authorization", "")
        if _SYNC_API_TOKEN and auth_header == f"Bearer {_SYNC_API_TOKEN}":
            return
        raise HTTPException(status_code=401, detail="Not authenticated")


@app.post("/api/rebase-next")
def api_rebase_next(request: Request):
    """Update remaining open cleanup PRs after one is merged.

    For each open PR in the target repo whose title starts with
    "Remove stale flag:", attempt to merge ``main`` into the PR branch
    via the GitHub API.  If that fails (merge conflict), dispatch a
    lightweight Devin session to resolve the conflict.

    Runs in a background thread; poll ``GET /api/rebase-next/status``
    for progress.
    """
    _check_auth(request)

    if _rebase_status["running"]:
        return {"status": "already_running", "message": "A rebase job is already in progress."}

    if not _rebase_lock.acquire(blocking=False):
        return {"status": "already_running", "message": "A rebase job is already in progress."}

    _rebase_status["running"] = True
    _rebase_status["results"] = []

    def _rebase_worker() -> None:
        repo_slug = os.getenv("TARGET_REPO", "bgtripp/LogiOps")
        results: list[dict] = []

        try:
            open_prs = list_pull_requests(repo_slug, state="open")
            cleanup_prs = [
                pr for pr in open_prs
                if (pr.get("title") or "").lower().startswith("remove stale flag")
            ]

            if not cleanup_prs:
                logger.info("No open cleanup PRs to rebase")
                results.append({"message": "No open cleanup PRs found"})
                return

            logger.info("Found %d open cleanup PR(s) to update", len(cleanup_prs))

            for pr in cleanup_prs:
                pr_number = pr["number"]
                pr_title = pr.get("title", "")
                branch = pr.get("head", {}).get("ref", "")

                if not branch:
                    results.append({
                        "pr": pr_number,
                        "title": pr_title,
                        "action": "skipped",
                        "reason": "Could not determine branch name",
                    })
                    continue

                # Try GitHub API merge first (fast, no Devin needed)
                logger.info("PR #%d (%s): attempting GitHub API merge of main into %s",
                            pr_number, pr_title, branch)
                merged = merge_main_into_branch(repo_slug, branch)

                if merged:
                    results.append({
                        "pr": pr_number,
                        "title": pr_title,
                        "action": "merged",
                        "method": "github_api",
                    })
                    logger.info("PR #%d: successfully merged main via GitHub API", pr_number)
                else:
                    # Conflict — dispatch Devin to resolve
                    logger.info("PR #%d: conflict detected, dispatching Devin to resolve", pr_number)
                    try:
                        session = create_rebase_session(repo_slug, branch, pr_number)
                        # Poll until Devin finishes (lightweight task, usually <5 min)
                        final = poll_session_until_done(session["session_id"])
                        final_state = final.get("status_enum", "unknown")
                        results.append({
                            "pr": pr_number,
                            "title": pr_title,
                            "action": "devin_resolved" if final_state == "finished" else "devin_failed",
                            "method": "devin",
                            "session_url": session["url"],
                            "session_state": final_state,
                        })
                        logger.info("PR #%d: Devin rebase session finished (state=%s)", pr_number, final_state)
                    except Exception:
                        logger.exception("PR #%d: Failed to dispatch Devin rebase session", pr_number)
                        results.append({
                            "pr": pr_number,
                            "title": pr_title,
                            "action": "failed",
                            "reason": "Could not dispatch Devin rebase session",
                        })
        except Exception:
            logger.exception("Rebase job failed")
            results.append({"error": "Rebase job failed unexpectedly"})
        finally:
            _rebase_status["results"] = results
            _rebase_status["running"] = False
            _rebase_status["last_run"] = datetime.now(timezone.utc).isoformat()
            logger.info("Rebase job completed: %d result(s)", len(results))

    def _locked_worker() -> None:
        try:
            _rebase_worker()
        finally:
            _rebase_lock.release()

    threading.Thread(target=_locked_worker, daemon=True).start()
    return {"status": "started", "message": "Rebase job started in background."}


@app.get("/api/rebase-next/status")
def api_rebase_status(request: Request):
    """Check the status of the last/current rebase job."""
    _check_auth(request)
    return _rebase_status


# ---------------------------------------------------------------------------
# Demo Reset API
# ---------------------------------------------------------------------------

_reset_lock = threading.Lock()


@app.post("/api/reset-demo")
def api_reset_demo(request: Request):
    """Reset the demo to its initial state (synchronous).

    Restores LogiOps source files, re-creates stale flags in Unleash,
    closes open cleanup PRs, and clears the dashboard state.

    Runs synchronously (~10-15 s) so the response contains the full
    result.  This avoids Fly.io machine-suspend issues that caused
    background-thread state to be lost between requests.
    """
    _check_auth(request)

    if not _reset_lock.acquire(blocking=False):
        return JSONResponse(
            {"status": "already_running", "message": "A reset is already in progress."},
            status_code=409,
        )

    global _candidates, _sessions, _pr_stats, _last_scan_time
    try:
        results = run_demo_reset()

        # Clear all dashboard state
        _sessions.clear()
        _pr_stats.clear()
        _candidates = []
        save_state(_STATE_PATH, _sessions, _pr_stats)

        # Re-scan to pick up restored flags
        _candidates = run_scan()
        _last_scan_time = datetime.now(timezone.utc)
        _apply_state_to_candidates(_candidates)

        logger.info("Demo reset completed successfully")

        # Dispatch Devin cleanup sessions synchronously (fast, ~2-3s each).
        # We do NOT poll — the dashboard auto-refreshes for in-progress cards.
        # This survives Fly.io machine suspension because state is persisted.
        repo_slug = os.getenv("TARGET_REPO", "bgtripp/LogiOps")
        dispatch_results: list[dict] = []

        # Check for existing cleanup PRs first (avoid duplicate Devin sessions)
        flag_keys = [c.flag_key for c in _candidates]
        existing_prs: dict[str, dict] = {}
        try:
            existing_prs = discover_cleanup_prs(repo_slug, flag_keys)
        except Exception:
            logger.exception("Failed to discover existing cleanup PRs")

        dispatched_count = 0
        for c in _candidates:
            existing = existing_prs.get(c.flag_key, {})
            if existing.get("pr_url"):
                _sessions[c.flag_key] = {
                    "status": "ready",
                    "pr_url": existing["pr_url"],
                }
                dispatch_results.append({"flag": c.flag_key, "action": "existing_pr"})
                logger.info("%s: existing PR found, skipping dispatch", c.flag_key)
                continue

            # Small delay between dispatches to avoid hammering the API
            if dispatched_count > 0:
                time.sleep(2)

            try:
                result = create_cleanup_session(
                    flag_key=c.flag_key,
                    repo=repo_slug,
                    variation=c.variation_served,
                    files=c.files_affected,
                )
                _sessions[c.flag_key] = {
                    "session_id": result["session_id"],
                    "url": result["url"],
                    "status": "running",
                    "pr_url": None,
                }
                dispatch_results.append({
                    "flag": c.flag_key,
                    "action": "dispatched",
                    "session_url": result["url"],
                })
                dispatched_count += 1
                logger.info("%s: Devin session dispatched: %s", c.flag_key, result["url"])
            except Exception as exc:
                logger.exception("Failed to dispatch Devin for %s", c.flag_key)
                dispatch_results.append({"flag": c.flag_key, "action": "dispatch_failed", "error": str(exc)})

        # Apply session state to candidates so the page shows correct statuses
        _apply_state_to_candidates(_candidates)

        # Persist sessions so state survives machine restarts
        save_state(_STATE_PATH, _sessions, _pr_stats)

        results["dispatch"] = dispatch_results
        return {"status": "done", "results": results}
    except Exception as exc:
        logger.exception("Demo reset failed")
        return JSONResponse(
            {"status": "error", "message": str(exc)},
            status_code=500,
        )
    finally:
        _reset_lock.release()
