# Copyright (c) 2026 Eric Grynspan. All rights reserved.
"""
Kill-switch state distribution tests (ADR-010).

The FCA single-lever guarantee across replicas: activating the switch on
one KillSwitch instance must flag claims on every instance sharing the
same store — and deactivation must propagate the same way.
"""
from __future__ import annotations

import json

from src.action.kill_switch import KillSwitch
from src.action.kill_switch_store import (
    STATE_KEY,
    InMemoryKillSwitchStore,
    KafkaKillSwitchStore,
    build_kill_switch,
)


# ------------------------------------------------------------------
# Fakes
# ------------------------------------------------------------------

class FakeMessage:
    def __init__(self, value: bytes | None, error=None):
        self._value = value
        self._error = error

    def value(self):
        return self._value

    def error(self):
        return self._error


class FakeProducer:
    def __init__(self):
        self.produced: list[dict] = []
        self.flushes = 0

    def produce(self, topic, key, value):
        self.produced.append({"topic": topic, "key": key, "value": value})

    def flush(self, timeout=None):
        self.flushes += 1
        return 0


class FakeConsumer:
    """Serves queued messages, then None."""

    def __init__(self, messages=None):
        self.messages = list(messages or [])

    def poll(self, timeout):
        return self.messages.pop(0) if self.messages else None


# ------------------------------------------------------------------
# Cross-replica propagation (the guarantee itself)
# ------------------------------------------------------------------

class TestCrossReplicaPropagation:
    def test_activation_propagates_to_other_replica(self):
        store = InMemoryKillSwitchStore()
        replica_a = KillSwitch(store=store)
        replica_b = KillSwitch(store=store)

        replica_a.activate("drift_breach")

        assert replica_b.is_active is True
        assert replica_b.reason == "drift_breach"
        assert replica_b.activation_count == 1

    def test_deactivation_propagates(self):
        store = InMemoryKillSwitchStore()
        replica_a = KillSwitch(store=store)
        replica_b = KillSwitch(store=store)

        replica_a.activate("drift_breach")
        assert replica_b.is_active is True
        replica_b.deactivate()

        assert replica_a.is_active is False
        assert replica_a.reason is None

    def test_late_joining_replica_bootstraps_current_state(self):
        store = InMemoryKillSwitchStore()
        KillSwitch(store=store).activate("manual_ops")

        late_replica = KillSwitch(store=store)
        assert late_replica.is_active is True
        assert late_replica.reason == "manual_ops"

    def test_status_reports_distributed(self):
        assert KillSwitch(store=InMemoryKillSwitchStore()).status()["distributed"] is True
        assert KillSwitch().status()["distributed"] is False

    def test_no_store_behaves_process_local(self):
        a = KillSwitch()
        b = KillSwitch()
        a.activate("drift_breach")
        assert a.is_active is True
        assert b.is_active is False


# ------------------------------------------------------------------
# Kafka store mechanics
# ------------------------------------------------------------------

class TestKafkaKillSwitchStore:
    def test_publish_produces_compacted_key_and_flushes(self):
        producer = FakeProducer()
        store = KafkaKillSwitchStore(producer=producer, consumer=FakeConsumer())

        store.publish(True, "auto_correct_spike")

        assert len(producer.produced) == 1
        record = producer.produced[0]
        assert record["topic"] == "control.kill-switch"
        assert record["key"] == STATE_KEY
        state = json.loads(record["value"])
        assert state["active"] is True
        assert state["reason"] == "auto_correct_spike"
        assert "changed_at" in state
        assert producer.flushes == 1

    def test_poll_latest_returns_newest_state(self):
        msgs = [
            FakeMessage(json.dumps({"active": True, "reason": "old"}).encode()),
            FakeMessage(json.dumps({"active": False, "reason": None}).encode()),
        ]
        store = KafkaKillSwitchStore(producer=FakeProducer(), consumer=FakeConsumer(msgs))
        assert store.poll_latest()["active"] is False

    def test_poll_latest_empty_topic_returns_none(self):
        store = KafkaKillSwitchStore(producer=FakeProducer(), consumer=FakeConsumer())
        assert store.poll_latest() is None

    def test_poll_latest_skips_malformed_messages(self):
        msgs = [
            FakeMessage(json.dumps({"active": True, "reason": "real"}).encode()),
            FakeMessage(b"\x00not json"),
            FakeMessage(None),
        ]
        store = KafkaKillSwitchStore(producer=FakeProducer(), consumer=FakeConsumer(msgs))
        assert store.poll_latest()["active"] is True


# ------------------------------------------------------------------
# Factory
# ------------------------------------------------------------------

class TestFactory:
    def test_default_is_process_local(self, monkeypatch):
        monkeypatch.delenv("KILL_SWITCH_DISTRIBUTED", raising=False)
        assert build_kill_switch().status()["distributed"] is False
