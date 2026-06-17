# Copyright (c) 2026 Eric Grynspan. All rights reserved.
"""
Layer 3 — Autonomous kill-switch.

INTERVIEW-CRITICAL: own this cold.

When active, the kill-switch drops ALL claims to FLAG regardless of LLM score or
confidence. No auto-corrections fire. This is the FCA and "goes rogue" defense:
the system has a single lever that instantly reverts to full human review.

Activation triggers (any one is sufficient):
  - LLM output drift breach (Great Expectations structural check fails)
  - Fallback rate exceeds threshold (LLM systematically broken)
  - Auto-correct rate spike (runaway autonomy signal)
  - Manual trigger (ops team, on-call)

State is in-process for v1. Production upgrade: back with Redis or a Snowflake
control record so all consumer instances share the same switch state.
"""
from __future__ import annotations

import structlog

log = structlog.get_logger(__name__)


class KillSwitch:
    """
    Single-lever autonomy kill-switch for the action layer.

    Thread-safe for single-process use. In production, back with a shared
    control store (Redis SETNX or a Snowflake control table) so all replicas
    see the same state.
    """

    def __init__(self) -> None:
        self._active: bool = False
        self._reason: str | None = None
        self._activation_count: int = 0

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def reason(self) -> str | None:
        return self._reason

    @property
    def activation_count(self) -> int:
        return self._activation_count

    def activate(self, reason: str) -> None:
        """
        Activate the kill-switch. All subsequent route() calls will FLAG claims,
        no auto-corrections will fire. Idempotent — safe to call multiple times.
        """
        if not self._active:
            self._active = True
            self._reason = reason
            self._activation_count += 1
            log.warning(
                "kill_switch_activated",
                reason=reason,
                activation_count=self._activation_count,
            )

    def deactivate(self) -> None:
        """
        Deactivate the kill-switch. Restores normal tiered-autonomy routing.
        Should only be called after root cause is confirmed resolved.
        """
        if self._active:
            self._active = False
            prev_reason = self._reason
            self._reason = None
            log.info("kill_switch_deactivated", was_reason=prev_reason)

    def status(self) -> dict:
        return {
            "active": self._active,
            "reason": self._reason,
            "activation_count": self._activation_count,
        }
