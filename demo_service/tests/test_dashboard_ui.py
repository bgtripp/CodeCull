"""Tests for the dashboard UI module."""

from demo_service.dashboard_ui import get_dashboard_layout, render_widget


def test_get_dashboard_layout():
    layout = get_dashboard_layout()
    assert layout["layout"] == "grid"
    assert "revenue-chart" in layout["widgets"]
    assert layout["sidebar"] is True


def test_render_widget():
    html = render_widget("active-users")
    assert "active-users" in html
    assert "widget" in html
