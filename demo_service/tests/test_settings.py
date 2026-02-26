"""Tests for the settings module."""

from demo_service.settings import get_css_variables, get_user_settings


def test_get_user_settings():
    settings = get_user_settings("user-42")
    assert settings["user_id"] == "user-42"
    assert settings["theme"] in ("dark", "light")
    assert "accent_color" in settings


def test_get_css_variables():
    css = get_css_variables("user-42")
    assert "--bg-primary" in css
    assert "--text-primary" in css
