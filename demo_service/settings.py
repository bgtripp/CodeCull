"""Settings / dark-mode module — uses the 'enable-dark-mode' feature flag.

This flag was turned on only 10 days ago — it should NOT be flagged for cleanup.
"""

from demo_service.feature_flags import is_enabled


def get_user_settings(user_id: str) -> dict:
    """Return user-facing settings, including theme preference."""
    base = {
        "user_id": user_id,
        "language": "en",
        "notifications": True,
    }

    if is_enabled("enable-dark-mode"):
        base["theme"] = "dark"
        base["accent_color"] = "#6366f1"
    else:
        base["theme"] = "light"
        base["accent_color"] = "#3b82f6"

    return base


def get_css_variables(user_id: str) -> dict:
    """Return CSS variables for the given user's theme."""
    if is_enabled("enable-dark-mode"):
        return {
            "--bg-primary": "#1e1e2e",
            "--text-primary": "#cdd6f4",
            "--accent": "#6366f1",
        }
    else:
        return {
            "--bg-primary": "#ffffff",
            "--text-primary": "#1e293b",
            "--accent": "#3b82f6",
        }
