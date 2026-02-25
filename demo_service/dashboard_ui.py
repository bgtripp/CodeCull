"""Dashboard UI module — uses the 'show-redesigned-dashboard' feature flag."""

from demo_service.feature_flags import is_enabled


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
        return f'<div class="widget widget--modern">{widget_name}</div>'
    else:
        return f'<div class="widget">{widget_name}</div>'
