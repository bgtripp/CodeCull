"""Application config — references feature flags for service-level behaviour."""

# Feature flag keys used across the demo service
FLAG_REDESIGNED_DASHBOARD = "show-redesigned-dashboard"
FLAG_V2_PRICING = "use-v2-pricing-engine"
FLAG_SEARCH_SUGGESTIONS = "rollout-search-suggestions"
FLAG_DARK_MODE = "enable-dark-mode"

# Map of flag key -> human-readable description
FLAG_DESCRIPTIONS: dict[str, str] = {
    FLAG_REDESIGNED_DASHBOARD: "Grid-based dashboard with modern widgets",
    FLAG_V2_PRICING: "Volume-discount pricing engine (never fully launched)",
    FLAG_SEARCH_SUGGESTIONS: "AI-powered search suggestions (50% rollout)",
    FLAG_DARK_MODE: "Dark-mode theme across the app",
}
