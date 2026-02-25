"""Tests for the search module."""

from demo_service.search import search


def test_search_with_suggestions():
    result = search("python")
    assert len(result["results"]) > 0
    assert len(result["suggestions"]) > 0


def test_search_results_structure():
    result = search("fastapi")
    assert "title" in result["results"][0]
    assert "score" in result["results"][0]
