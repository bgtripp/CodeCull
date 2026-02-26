"""
CodeCull — entry point.

Usage:
    # Run the dashboard (default)
    python main.py

    # Run the scanner only (prints results to stdout)
    python main.py scan
"""

from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root before anything else.
# override=True ensures stale shell env vars don't shadow the .env values.
load_dotenv(Path(__file__).resolve().parent / ".env", override=True)


def _run_dashboard() -> None:
    import uvicorn
    uvicorn.run("dashboard.app:app", host="0.0.0.0", port=8000, reload=True)


def _run_scan() -> None:
    from scanner.flag_scanner import run_scan

    candidates = run_scan()
    if not candidates:
        print("No stale flags found.")
        return

    print(f"\nFound {len(candidates)} stale flag candidate(s):\n")
    for i, c in enumerate(candidates, 1):
        print(f"  {i}. {c.flag_key}")
        print(f"     Name:       {c.flag_name}")
        print(f"     Status:     {c.variation_served} for {c.days_stale} days")
        print(f"     Files:      {len(c.files_affected)}")
        print(f"     References: {c.total_lines}")
        print(f"     Tags:       {', '.join(c.tags)}")
        print()


def _run_sync() -> None:
    from scanner.pr_sync import sync_state

    result = sync_state()
    sessions = result.get("sessions", {})
    ready = [k for k, v in sessions.items() if v.get("pr_url")]
    print(f"Synced {len(ready)} PR(s) to dashboard state file.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "dashboard"

    if cmd == "scan":
        _run_scan()
    elif cmd == "sync":
        _run_sync()
    else:
        _run_dashboard()
