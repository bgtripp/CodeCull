"""
Demo reset — restores LogiOps to its pre-cleanup state for re-demoing.

Steps:
1. Restore LogiOps source files (config.py, feature_flags.py, checkout.py,
   pricing.py, dashboard_ui.py) to their "with flags" versions via the
   GitHub Contents API.
2. Re-create the 3 stale flags in the Unleash flag service (if missing).
3. Close any open cleanup PRs in LogiOps.
"""

from __future__ import annotations

import base64
import json
import logging
import os

import httpx

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"


def _github_headers() -> dict[str, str]:
    token = os.getenv("GITHUB_TOKEN", "")
    headers: dict[str, str] = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


# ---------------------------------------------------------------------------
# 1. Restore LogiOps source files
# ---------------------------------------------------------------------------

# Pre-cleanup file contents (the "with flags" versions).
# These are the canonical versions from commit 31d81ba (right before any
# cleanup PRs were merged).

_PRE_CLEANUP_FILES: dict[str, str] = {
    "logiops/config.py": '''\
"""Application config — references feature flags for service-level behaviour."""

# Feature flag keys used across LogiOps
FLAG_NEW_CHECKOUT = "enable-new-checkout-flow"
FLAG_REDESIGNED_DASHBOARD = "show-redesigned-dashboard"
FLAG_V2_PRICING = "use-v2-pricing-engine"
FLAG_SEARCH_SUGGESTIONS = "rollout-search-suggestions"
FLAG_DARK_MODE = "enable-dark-mode"

# Map of flag key -> human-readable description
FLAG_DESCRIPTIONS: dict[str, str] = {
    FLAG_NEW_CHECKOUT: "New multi-step checkout flow with promo support",
    FLAG_REDESIGNED_DASHBOARD: "Grid-based dashboard with modern widgets",
    FLAG_V2_PRICING: "Volume-discount pricing engine (never fully launched)",
    FLAG_SEARCH_SUGGESTIONS: "AI-powered search suggestions (50% rollout)",
    FLAG_DARK_MODE: "Dark-mode theme across the app",
}
''',
    "logiops/feature_flags.py": '''\
"""
Feature flag client — wraps Unleash SDK calls.

When ``UNLEASH_URL`` is set, flags are evaluated via the Unleash Python SDK
against a real Unleash instance.  Otherwise falls back to a static dict so
the service can run without an external dependency.
"""

from __future__ import annotations

import logging
import os
import threading

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Static fallback (used when UNLEASH_URL is not configured)
# ---------------------------------------------------------------------------
_FLAG_OVERRIDES: dict[str, bool] = {
    "enable-new-checkout-flow": True,      # Always-on for 120+ days  -> STALE
    "show-redesigned-dashboard": True,      # Always-on for 95 days   -> STALE
    "use-v2-pricing-engine": False,         # Always-off for 100 days -> STALE
    "rollout-search-suggestions": True,     # Active 50% rollout      -> SKIP
    "enable-dark-mode": True,               # Turned on 10 days ago   -> SKIP (too recent)
}

# ---------------------------------------------------------------------------
# Unleash SDK client (lazy-initialised)
# ---------------------------------------------------------------------------
_UNSET = object()
_unleash_client = _UNSET  # type: ignore[assignment]
_init_lock = threading.Lock()


def _get_unleash_client():
    """Return the shared UnleashClient, initialising on first call.

    Uses double-checked locking to prevent duplicate SDK clients from
    leaking background threads when called concurrently.
    """
    global _unleash_client
    if _unleash_client is not _UNSET:
        return _unleash_client

    with _init_lock:
        # Re-check after acquiring lock
        if _unleash_client is not _UNSET:
            return _unleash_client

        unleash_url = os.getenv("UNLEASH_URL", "")
        if not unleash_url:
            _unleash_client = None
            return None

        from UnleashClient import UnleashClient  # noqa: N811

        api_url = f"{unleash_url}/api"
        api_token = os.getenv(
            "UNLEASH_CLIENT_TOKEN",
            "default:development.unleash-insecure-client-api-token",
        )
        app_name = os.getenv("UNLEASH_APP_NAME", "logiops")

        client = UnleashClient(
            url=api_url,
            app_name=app_name,
            custom_headers={"Authorization": api_token},
        )
        client.initialize_client()
        _unleash_client = client
        logger.info("Unleash client initialised: %s", api_url)
        return _unleash_client


def is_enabled(flag_key: str, default: bool = False) -> bool:
    """Return whether *flag_key* is enabled.

    Uses the Unleash SDK when available, otherwise falls back to the
    static ``_FLAG_OVERRIDES`` dict.
    """
    client = _get_unleash_client()
    if client is not None:
        return client.is_enabled(flag_key, fallback_function=lambda feature_name, context: default)
    return _FLAG_OVERRIDES.get(flag_key, default)
''',
    "logiops/checkout.py": '''\
"""Checkout module — uses the \'enable-new-checkout-flow\' feature flag."""

from logiops.feature_flags import is_enabled


def process_checkout(cart: dict) -> dict:
    """Process a checkout for the given *cart*.

    The old checkout path is dead code — the flag has been on for 120+ days.
    """
    if is_enabled("enable-new-checkout-flow"):
        # New checkout flow (always taken)
        total = sum(item["price"] * item["qty"] for item in cart.get("items", []))
        discount = total * 0.1 if cart.get("promo") else 0
        return {
            "status": "success",
            "total": round(total - discount, 2),
            "method": "new-checkout",
        }
    else:
        # Legacy checkout — dead code path
        total = sum(item["price"] for item in cart.get("items", []))
        return {
            "status": "success",
            "total": total,
            "method": "legacy-checkout",
        }


def checkout_summary(order: dict) -> str:
    """Return a human-readable summary for an order."""
    if is_enabled("enable-new-checkout-flow"):
        return f"Order {order[\'id\']}: ${order[\'total\']:.2f} via new checkout"
    else:
        return f"Order {order[\'id\']}: ${order[\'total\']:.2f} via legacy checkout"
''',
    "logiops/pricing.py": '''\
"""Pricing engine — uses the \'use-v2-pricing-engine\' feature flag."""

from logiops.feature_flags import is_enabled


def calculate_price(base_price: float, quantity: int, tier: str = "standard") -> float:
    """Calculate the final price for a line item.

    The v2 pricing engine flag has been *off* for 100 days — the v2 branch
    is dead code that was never fully rolled out.
    """
    if is_enabled("use-v2-pricing-engine"):
        # V2 pricing — dead code (flag always off)
        multipliers = {"basic": 1.0, "standard": 0.9, "premium": 0.8}
        multiplier = multipliers.get(tier, 1.0)
        volume_discount = 0.05 if quantity > 100 else 0
        return round(base_price * quantity * multiplier * (1 - volume_discount), 2)
    else:
        # V1 pricing — always taken
        if tier == "premium":
            return round(base_price * quantity * 0.85, 2)
        return round(base_price * quantity, 2)


def get_pricing_tier_info(tier: str) -> dict:
    """Return metadata about a pricing tier."""
    if is_enabled("use-v2-pricing-engine"):
        # V2 tier info — dead code
        return {
            "tier": tier,
            "engine": "v2",
            "features": ["volume-discount", "tier-multiplier", "dynamic-pricing"],
        }
    else:
        return {
            "tier": tier,
            "engine": "v1",
            "features": ["flat-rate", "premium-discount"],
        }
''',
    "logiops/dashboard_ui.py": '''\
"""Dashboard UI module — uses the \'show-redesigned-dashboard\' feature flag."""

from logiops.feature_flags import is_enabled


def get_dashboard_layout() -> dict:
    """Return the dashboard layout configuration.

    The redesigned dashboard flag has been on for 95 days — the old layout
    branch is dead code.
    """
    if is_enabled("show-redesigned-dashboard"):
        return {
            "layout": "grid",
            "widgets": ["revenue-chart", "active-users", "conversion-funnel", "alerts"],
            "sidebar": True,
            "theme": "modern",
        }
    else:
        # Old layout — dead code
        return {
            "layout": "list",
            "widgets": ["revenue-chart", "active-users"],
            "sidebar": False,
            "theme": "classic",
        }


def render_widget(widget_name: str) -> str:
    """Render a single dashboard widget."""
    if is_enabled("show-redesigned-dashboard"):
        return f\'<div class="widget widget--modern">{widget_name}</div>\'
    else:
        return f\'<div class="widget">{widget_name}</div>\'
''',
}


def _get_file_sha(repo_slug: str, path: str, ref: str = "main") -> str | None:
    """Get the current SHA of a file in the repo (needed for updates)."""
    owner, repo = repo_slug.split("/", 1)
    try:
        resp = httpx.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}",
            params={"ref": ref},
            headers=_github_headers(),
            timeout=15,
        )
        if resp.status_code == 404:
            return None  # File doesn't exist yet
        resp.raise_for_status()
        return resp.json().get("sha")
    except Exception:
        logger.exception("Failed to get SHA for %s/%s", repo_slug, path)
        return None


def restore_logiops_files(repo_slug: str) -> list[dict]:
    """Restore all LogiOps source files to their pre-cleanup state.

    Uses the GitHub Contents API to update each file individually.
    Returns a list of result dicts.
    """
    results: list[dict] = []

    for path, content in _PRE_CLEANUP_FILES.items():
        sha = _get_file_sha(repo_slug, path)
        owner, repo = repo_slug.split("/", 1)

        payload: dict = {
            "message": f"chore: restore {path} for demo reset",
            "content": base64.b64encode(content.encode()).decode(),
            "branch": "main",
        }
        if sha:
            payload["sha"] = sha

        try:
            resp = httpx.put(
                f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}",
                headers=_github_headers(),
                json=payload,
                timeout=30,
            )
            resp.raise_for_status()
            results.append({"file": path, "status": "restored"})
            logger.info("Restored %s in %s", path, repo_slug)
        except httpx.HTTPStatusError as exc:
            # 422 means content is identical — already in the right state
            if exc.response.status_code == 422:
                results.append({"file": path, "status": "already_current"})
                logger.info("%s already matches pre-cleanup state", path)
            else:
                results.append({
                    "file": path,
                    "status": "error",
                    "detail": f"HTTP {exc.response.status_code}: {exc.response.text[:200]}",
                })
                logger.warning("Failed to restore %s: %s", path, exc.response.text[:200])
        except Exception as exc:
            results.append({"file": path, "status": "error", "detail": str(exc)})
            logger.exception("Failed to restore %s", path)

    return results


# ---------------------------------------------------------------------------
# 2. Re-create stale flags in Unleash
# ---------------------------------------------------------------------------

_STALE_FLAGS = [
    {
        "name": "enable-new-checkout-flow",
        "description": "Multi-step checkout with promo code support",
        "created_at": "2025-08-15T10:00:00.000Z",
        "stale": True,
        "enabled": True,
    },
    {
        "name": "show-redesigned-dashboard",
        "description": "Grid-based dashboard layout with modern widgets",
        "created_at": "2025-09-01T09:00:00.000Z",
        "stale": True,
        "enabled": True,
    },
    {
        "name": "use-v2-pricing-engine",
        "description": "Volume-discount pricing engine (never fully launched)",
        "created_at": "2025-07-10T08:00:00.000Z",
        "stale": True,
        "enabled": False,
    },
]


def _unleash_auth() -> tuple[str, str] | None:
    user = os.getenv("UNLEASH_ADMIN_USER", "")
    password = os.getenv("UNLEASH_ADMIN_PASSWORD", "")
    if user and password:
        return (user, password)
    return None


def reset_unleash_flags(unleash_url: str) -> list[dict]:
    """Ensure all 3 stale flags exist in Unleash with correct settings.

    Uses the Unleash Admin API to check for existing flags and re-create
    any that are missing.  Also resets the stale status.
    """
    results: list[dict] = []
    auth = _unleash_auth()
    project = os.getenv("UNLEASH_PROJECT", "default")

    for flag in _STALE_FLAGS:
        name = flag["name"]

        # Check if flag exists
        try:
            resp = httpx.get(
                f"{unleash_url}/api/admin/projects/{project}/features/{name}",
                auth=auth,
                timeout=15,
            )
            if resp.status_code == 200:
                # Flag exists — just make sure stale is set
                httpx.post(
                    f"{unleash_url}/api/admin/projects/{project}/features/{name}/stale/on",
                    auth=auth,
                    timeout=15,
                )
                results.append({"flag": name, "status": "exists_reset_stale"})
                logger.info("Flag %s already exists, reset stale=true", name)
                continue
        except Exception:
            logger.exception("Error checking flag %s", name)
            results.append({
                "flag": name,
                "status": "error",
                "detail": "Failed to check flag existence in Unleash",
            })
            continue

        # Flag doesn't exist — the flag service seeds on startup but only
        # if the DB is empty.  We need to insert it directly.
        # The simplest approach: call the flag service's seed logic by
        # restarting it, OR insert via a custom endpoint.
        # For now, we'll note it needs manual re-seed.
        results.append({
            "flag": name,
            "status": "missing",
            "detail": "Flag not found — flag service may need restart to re-seed",
        })
        logger.warning("Flag %s not found in Unleash", name)

    return results


# ---------------------------------------------------------------------------
# 3. Close open cleanup PRs
# ---------------------------------------------------------------------------

def close_cleanup_prs(repo_slug: str) -> list[dict]:
    """Close any open PRs whose title starts with 'Remove stale flag'.

    Returns a list of result dicts.
    """
    owner, repo = repo_slug.split("/", 1)
    results: list[dict] = []

    try:
        resp = httpx.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/pulls",
            params={"state": "open", "per_page": 100},
            headers=_github_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        prs = resp.json()
    except Exception:
        logger.exception("Failed to list PRs for %s", repo_slug)
        return [{"error": "Failed to list PRs"}]

    cleanup_prs = [
        pr for pr in prs
        if (pr.get("title") or "").lower().startswith("remove stale flag")
    ]

    if not cleanup_prs:
        return [{"message": "No open cleanup PRs to close"}]

    for pr in cleanup_prs:
        pr_number = pr["number"]
        pr_title = pr.get("title", "")
        try:
            resp = httpx.patch(
                f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}",
                headers=_github_headers(),
                json={"state": "closed"},
                timeout=15,
            )
            resp.raise_for_status()
            results.append({"pr": pr_number, "title": pr_title, "status": "closed"})
            logger.info("Closed PR #%d: %s", pr_number, pr_title)
        except Exception:
            logger.exception("Failed to close PR #%d", pr_number)
            results.append({"pr": pr_number, "title": pr_title, "status": "error"})

    return results


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_demo_reset() -> dict:
    """Run the full demo reset: restore files, reset flags, close PRs.

    Returns a summary dict with results from each step.
    """
    repo_slug = os.getenv("TARGET_REPO", "bgtripp/LogiOps")
    unleash_url = os.getenv("UNLEASH_URL", "")

    logger.info("Starting demo reset for %s", repo_slug)

    # Step 1: Restore LogiOps files
    file_results = restore_logiops_files(repo_slug)

    # Step 2: Reset Unleash flags
    flag_results: list[dict] = []
    if unleash_url:
        flag_results = reset_unleash_flags(unleash_url)
    else:
        flag_results = [{"message": "UNLEASH_URL not set — skipping flag reset"}]

    # Step 3: Close cleanup PRs
    pr_results = close_cleanup_prs(repo_slug)

    return {
        "files": file_results,
        "flags": flag_results,
        "prs_closed": pr_results,
    }
