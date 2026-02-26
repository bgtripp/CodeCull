"""Thin wrapper that re-exports the FastAPI ``app`` for the deployment server.

The deploy tool expects ``app/main.py`` with a FastAPI instance named ``app``.
All real logic lives in ``dashboard.app``.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure the project root is on sys.path so ``scanner.*`` / ``dashboard.*``
# imports work when the deploy server starts uvicorn from this file.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from dashboard.app import app  # noqa: E402, F401 — re-export for uvicorn
