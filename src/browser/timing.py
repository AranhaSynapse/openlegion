"""Human-like timing helpers for browser interactions.

Uses clamped Gaussian distributions to produce natural variance in
action delays, keystroke timing, and scroll behavior. No third-party
dependencies — built on stdlib ``random``.
"""

from __future__ import annotations

import random


def _clamped_gauss(mean: float, stddev: float, low: float, high: float) -> float:
    """Sample from a Gaussian distribution, clamped to [low, high]."""
    return max(low, min(high, random.gauss(mean, stddev)))


# ── Action timing ──────────────────────────────────────────────


def action_delay() -> float:
    """Post-click pause (seconds). μ=0.30, σ=0.08, range 0.15–0.50."""
    return _clamped_gauss(0.30, 0.08, 0.15, 0.50)


def navigation_jitter() -> float:
    """Extra jitter added on top of wait_ms after navigation (seconds).

    μ=0.20, σ=0.10, range 0.0–0.50.
    """
    return _clamped_gauss(0.20, 0.10, 0.0, 0.50)


def keystroke_delay(char: str) -> float:
    """Per-key delay (seconds). Symbols/digits are slower than letters.

    Letters: μ=0.08, σ=0.025, range 0.04–0.20.
    Symbols/digits: μ=0.11, σ=0.03, range 0.04–0.20.
    """
    if char.isalpha():
        return _clamped_gauss(0.08, 0.025, 0.04, 0.20)
    return _clamped_gauss(0.11, 0.03, 0.04, 0.20)


# ── Scroll timing ─────────────────────────────────────────────


def scroll_pause() -> float:
    """Pause between scroll increments (seconds). μ=0.08, σ=0.03, range 0.03–0.15."""
    return _clamped_gauss(0.08, 0.03, 0.03, 0.15)


def scroll_increment() -> int:
    """Per-step scroll distance (pixels). μ=140, σ=30, range 80–200."""
    return int(_clamped_gauss(140, 30, 80, 200))
