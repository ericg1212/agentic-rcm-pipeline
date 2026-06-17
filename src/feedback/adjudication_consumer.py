# Copyright (c) 2026 Eric Grynspan. All rights reserved.
"""
Layer 4 — Adjudication outcome consumer.

Consumes adjudications.outcomes Kafka topic. This is the feedback sink
that closes the pre-submission → clearinghouse → payer → outcome loop.

Each message is a payer's final adjudication decision on a claim that
was previously processed by the RCM prevention pipeline. The consumer:
  1. Parses the outcome message
  2. Records it to the AdjudicationOutcomeStore (keyed by arm)
  3. Runs a drift check every DRIFT_CHECK_INTERVAL outcomes
  4. Logs a lift snapshot every 500 outcomes

When drift is detected, the DriftMonitor activates the kill-switch
shared with the ActionRouter — all subsequent claims route to FLAG
until a human reviews and deactivates it.
"""
from __future__ import annotations

import json
import signal
import sys
from typing import Optional

import structlog
from confluent_kafka import Consumer, KafkaError, KafkaException

from src.action.kill_switch import KillSwitch
from src.config.settings import FeedbackConfig, KafkaConfig
from src.feedback.drift_monitor import DriftMonitor
from src.feedback.lift_calculator import LiftCalculator
from src.feedback.outcome_store import AdjudicationOutcomeStore, OutcomeRecord

log = structlog.get_logger(__name__)

DRIFT_CHECK_INTERVAL = 50


class AdjudicationConsumer:
    """
    Kafka consumer for adjudication outcomes.

    Share the same KillSwitch instance as ActionRouter so drift detection
    halts autonomous corrections pipeline-wide.
    """

    def __init__(
        self,
        outcome_store: Optional[AdjudicationOutcomeStore] = None,
        kill_switch: Optional[KillSwitch] = None,
    ) -> None:
        self._store = outcome_store or AdjudicationOutcomeStore()
        self._kill_switch = kill_switch or KillSwitch()
        self._drift_monitor = DriftMonitor(
            outcome_store=self._store,
            kill_switch=self._kill_switch,
            baseline_window=FeedbackConfig.DRIFT_BASELINE_WINDOW,
            drift_window=FeedbackConfig.DRIFT_ROLLING_WINDOW,
            drift_threshold=FeedbackConfig.DRIFT_THRESHOLD,
        )
        self._lift_calculator = LiftCalculator(self._store)
        self._consumer = self._build_consumer()
        self._running = False

    def start(self) -> None:
        self._consumer.subscribe([KafkaConfig.TOPIC_ADJUDICATIONS])
        self._running = True

        signal.signal(signal.SIGINT, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)

        log.info("adjudication_consumer_started", topic=KafkaConfig.TOPIC_ADJUDICATIONS)
        processed = 0

        while self._running:
            try:
                msg = self._consumer.poll(timeout=1.0)
                if msg is None:
                    continue
                if msg.error():
                    self._handle_error(msg.error())
                    continue

                raw = msg.value()
                if raw is None:
                    continue

                outcome = self._parse(raw)
                if outcome is not None:
                    self._store.record_outcome(outcome)
                    processed += 1

                    if processed % DRIFT_CHECK_INTERVAL == 0:
                        self._drift_monitor.check_drift()

                    if processed % 500 == 0:
                        result = self._lift_calculator.calculate()
                        log.info("lift_snapshot", summary=result.summary())

            except Exception as e:
                log.error("adjudication_consumer_error", error=str(e), exc_info=True)

    @property
    def outcome_store(self) -> AdjudicationOutcomeStore:
        return self._store

    @property
    def lift_calculator(self) -> LiftCalculator:
        return self._lift_calculator

    @property
    def drift_monitor(self) -> DriftMonitor:
        return self._drift_monitor

    def _parse(self, raw: bytes) -> Optional[OutcomeRecord]:
        try:
            data = json.loads(raw.decode("utf-8"))
            return OutcomeRecord(
                claim_id=data["claim_id"],
                payer_id=data.get("payer_id", ""),
                adjudication_timestamp=data.get("adjudication_timestamp", ""),
                outcome=data["outcome"],
                denial_code=data.get("denial_code"),
                paid_amount=data.get("paid_amount"),
                adjustment_amount=data.get("adjustment_amount"),
                arm=data.get("arm", "intervention"),
            )
        except (KeyError, json.JSONDecodeError, ValueError) as e:
            log.warning("outcome_parse_failed", error=str(e))
            return None

    def _handle_error(self, error: KafkaError) -> None:
        if error.code() == KafkaError._PARTITION_EOF:
            return
        log.error("kafka_error", code=error.code(), reason=error.str())
        if error.fatal():
            raise KafkaException(error)

    def _shutdown(self, sig, frame) -> None:
        log.info("adjudication_consumer_shutdown")
        self._running = False
        self._consumer.close()
        sys.exit(0)

    @staticmethod
    def _build_consumer() -> Consumer:
        return Consumer({
            "bootstrap.servers": KafkaConfig.BOOTSTRAP_SERVERS,
            "group.id": f"{KafkaConfig.CONSUMER_GROUP_ID}-adjudication",
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
        })


if __name__ == "__main__":
    consumer = AdjudicationConsumer()
    consumer.start()
