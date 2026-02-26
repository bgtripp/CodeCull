"""
Feature flag client — wraps LaunchDarkly SDK calls.

In a real service this would call ld_client.variation(); here we use a simple
dict so the demo repo compiles without an SDK dependency.
"""

from __future__ import annotations

# Simulated flag values (would come from LaunchDarkly SDK at runtime)
_FLAG_OVERRIDES: dict[str, bool] = {
    "show-redesigned-dashboard": True,      # Always-on for 95 days   -> STALE
    "use-v2-pricing-engine": False,         # Always-off for 100 days -> STALE
    "rollout-search-suggestions": True,     # Active 50% rollout      -> SKIP
    "enable-dark-mode": True,               # Turned on 10 days ago   -> SKIP (too recent)
}


def is_enabled(flag_key: str, default: bool = False) -> bool:
    """Return whether *flag_key* is enabled."""
    return _FLAG_OVERRIDES.get(flag_key, default)
