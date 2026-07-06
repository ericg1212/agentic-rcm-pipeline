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

State distribution (ADR-007): pass a KillSwitchStore to share state across
replicas — production uses the compacted control.kill-switch Kafka topic
(same hot-swap pattern as rules.control). Every is_active check syncs the
latest published state, so activating the switch on any replica flags all
of them within seconds. Without a store, state is process-local (tests,
single-process dev).
"""
from __future__ import annotations

import structlog

from src.action.kill_switch_store import KillSwitchStore

log = structlog.get_logger(__name__)


class KillSwitch:
    """
    Single-lever autonomy kill-switch for the action layer.

    With a store, the single-lever guarantee holds across replicas: state
    changes publish to the store, and every is_active read applies the
    latest published state before answering.
    """

    def __init__(self, store: KillSwitchStore | None = None) -> None:
        self._store = store
        self._active: bool = False
        self._reason: str | None = None
        self._activation_count: int = 0

    @property
    def is_active(self) -> bool:
        self._sync()
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
        if self._store is not None:
            self._store.publish(True, reason)

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
        if self._store is not None:
            self._store.publish(False, None)

    def status(self) -> dict:
        self._sync()
        return {
            "active": self._active,
            "reason": self._reason,
            "activation_count": self._activation_count,
            "distributed": self._store is not None,
        }

    def _sync(self) -> None:
        """Apply the latest state published by any replica. No-op without a store."""
        if self._store is None:
            return
        state = self._store.poll_latest()
        if state is None or state.get("active") == self._active:
            return
        if state["active"]:
            self._active = True
            self._reason = state.get("reason")
            self._activation_count += 1
            log.warning(
                "kill_switch_remote_activation",
                reason=self._reason,
                changed_at=state.get("changed_at"),
            )
        else:
            prev_reason = self._reason
            self._active = False
            self._reason = None
            log.info(
                "kill_switch_remote_deactivation",
                was_reason=prev_reason,
                changed_at=state.get("changed_at"),
            )
