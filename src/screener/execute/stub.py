"""Execution adapter — Phase 2/3 placeholder.

Phase 1 is screen-only. Phase 2 wires this to tastytrade's paper-trade
endpoints; Phase 3 to live order placement. The screener calls
`submit(candidate)` regardless of phase — only this file changes.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def submit(candidate: dict, dry_run: bool = True) -> dict:
    log.info(
        "[stub] would submit short put: %s %s %s @ %s",
        candidate.get("symbol"),
        candidate.get("expiry"),
        candidate.get("strike"),
        candidate.get("put_price"),
    )
    return {"status": "skipped", "reason": "phase 1 — execution disabled", "dry_run": dry_run}
