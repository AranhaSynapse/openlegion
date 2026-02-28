"""Shared constants used across OpenLegion components.

Centralizes hardcoded timeouts and ports to avoid magic numbers scattered
throughout the codebase.
"""

from __future__ import annotations

# ── Network ──────────────────────────────────────────────────
MESH_PORT = 8420

# ── Timeouts (seconds) ───────────────────────────────────────
CHAT_TIMEOUT = 120
STATUS_TIMEOUT = 5
READINESS_TIMEOUT = 60
STARTUP_DEADLINE = 90
CONTAINER_STOP_TIMEOUT = 10
