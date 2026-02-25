"""Search module — uses the 'rollout-search-suggestions' feature flag.

This flag is an *active rollout* at 50 %% — it should NOT be flagged for cleanup.
"""

from demo_service.feature_flags import is_enabled


def search(query: str, user_id: str | None = None) -> dict:
    """Run a search and optionally include suggestions."""
    results = _execute_query(query)

    if is_enabled("rollout-search-suggestions"):
        suggestions = _generate_suggestions(query)
        return {"results": results, "suggestions": suggestions}
    else:
        return {"results": results, "suggestions": []}


def _execute_query(query: str) -> list[dict]:
    """Stub: execute the search query."""
    return [{"title": f"Result for '{query}'", "score": 0.95}]


def _generate_suggestions(query: str) -> list[str]:
    """Generate search suggestions based on the query."""
    return [f"{query} tutorial", f"{query} examples", f"{query} best practices"]
