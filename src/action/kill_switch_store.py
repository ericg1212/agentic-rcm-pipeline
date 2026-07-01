# Copyright (c) 2026 Eric Grynspan. All rights reserved.
"""
Kill-switch state distribution — compacted Kafka control topic.

INTERVIEW-CRITICAL: own this cold. See ADR-010.

The FCA "single lever" guarantee only holds if every consumer replica sees
the same switch state. v1 kept state in-process — true for one process,
false the moment the consumer scales horizontally. This module distributes
the state using the same pattern already proven for NCCI rule hot-swaps
(rules.control): a compacted Kafka topic holding exactly one current state
under a single key. Every replica polls the topic non-blocking on each
check and applies the latest state within seconds.

Why a compacted topic and not Redis or Snowflake:
  - Redis: correct primitive, but a new infra component for one boolean.
  - Snowflake: the analytical store is the wrong control plane — per-claim
    polling of a warehouse adds seconds of latency or a stale TTL cache.
  - Compacted topic: zero new infra, log-compacted to exactly the current
    state, replays to late-joining replicas automatically, and reuses the
    hot-swap pattern the pipeline already runs for rules.control.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Protocol

import structlog

log = structlog.get_logger(__name__)

# Single compacted key — the topic holds exactly one current state
STATE_KEY = "kill-switch"

# Bound on messages drained per poll_latest call (compaction keeps the
# backlog near 1; the bound guards against an uncompacted burst)
_MAX_DRAIN = 100


class KillSwitchStore(Protocol):
    """Shared state store contract for distributed kill-switch state."""

    def publish(self, active: bool, reason: str | None) -> None: ...

    def poll_latest(self) -> dict | None: ...


def make_state(active: bool, reason: str | None) -> dict:
    return {
        "active": active,
        "reason": reason,
        "changed_at": datetime.now(timezone.utc).isoformat(),
    }


class InMemoryKillSwitchStore:
    """
    Process-local store. Sharing one instance across KillSwitch objects
    bridges them within a process; used as the test double for the Kafka store.
    """

    def __init__(self) -> None:
        self._state: dict | None = None

    def publish(self, active: bool, reason: str | None) -> None:
        self._state = make_state(active, reason)

    def poll_latest(self) -> dict | None:
        return self._state


class KafkaKillSwitchStore:
    """
    Compacted-topic-backed store (control.kill-switch).

    Each replica consumes with a unique group.id from earliest so it always
    reads the full compacted log — one message per key after compaction —
    and converges on the current state without coordinating with peers.
    """

    def __init__(self, producer=None, consumer=None) -> None:
        self._producer = producer or self._build_producer()
        self._consumer = consumer or self._build_consumer()

    def publish(self, active: bool, reason: str | None) -> None:
        from src.config.settings import KafkaConfig

        state = make_state(active, reason)
        self._producer.produce(
            topic=KafkaConfig.TOPIC_KILL_SWITCH,
            key=STATE_KEY,
            value=json.dumps(state).encode("utf-8"),
        )
        self._producer.flush(2.0)
        log.info("kill_switch_state_published", **state)

    def poll_latest(self) -> dict | None:
        latest: dict | None = None
        for _ in range(_MAX_DRAIN):
            msg = self._consumer.poll(0)
            if msg is None:
                break
            if msg.error():
                log.warning("kill_switch_store_poll_error", error=msg.error().str())
                continue
            raw = msg.value()
            if raw is None:
                continue
            try:
                latest = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as e:
                log.warning("kill_switch_state_malformed", error=str(e))
        return latest

    @staticmethod
    def _build_producer():
        from confluent_kafka import Producer

        from src.config.settings import KafkaConfig

        return Producer({
            "bootstrap.servers": KafkaConfig.BOOTSTRAP_SERVERS,
            "enable.idempotence": True,
            "acks": "all",
        })

    @staticmethod
    def _build_consumer():
        from confluent_kafka import Consumer

        from src.config.settings import KafkaConfig

        consumer = Consumer({
            "bootstrap.servers": KafkaConfig.BOOTSTRAP_SERVERS,
            # Unique group per replica: every instance reads the full
            # compacted log independently — no offset coordination needed
            "group.id": f"kill-switch-{uuid.uuid4()}",
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
        })
        consumer.subscribe([KafkaConfig.TOPIC_KILL_SWITCH])
        return consumer


def build_kill_switch():
    """
    Factory for production construction sites. KILL_SWITCH_DISTRIBUTED=true
    backs the switch with the compacted control topic; default stays
    in-process (tests, offline dev).
    """
    import os

    from src.action.kill_switch import KillSwitch

    if os.getenv("KILL_SWITCH_DISTRIBUTED", "false").lower() == "true":
        return KillSwitch(store=KafkaKillSwitchStore())
    return KillSwitch()
