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
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer

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
        elif status == "error":
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
