"""
Kafka Avro producer — serializes ClaimEvents to the claims.raw topic.

Partition key = payer_id, ensuring per-payer ordering so the consumer
applies the correct rule version consistently for each payer's claim stream.
"""
from __future__ import annotations

import signal
import sys
from pathlib import Path

import structlog
from confluent_kafka import KafkaException
from confluent_kafka.avro import AvroProducer

from src.config.settings import GeneratorConfig, KafkaConfig
from src.generator.claim_generator import ClaimGenerator

log = structlog.get_logger(__name__)

SCHEMA_PATH = Path(__file__).parent.parent / "schemas" / "claim_event.avsc"


def _load_schema() -> str:
    return SCHEMA_PATH.read_text(encoding="utf-8")


def _build_producer() -> AvroProducer:
    return AvroProducer(
        {
            "bootstrap.servers": KafkaConfig.BOOTSTRAP_SERVERS,
            "schema.registry.url": KafkaConfig.SCHEMA_REGISTRY_URL,
            # Idempotent delivery: exactly-once at the producer level
            "enable.idempotence": True,
            "acks": "all",
            "retries": 5,
            "retry.backoff.ms": 200,
        },
        default_value_schema=_load_schema(),
    )


def delivery_report(err, msg):
    if err:
        log.error("delivery_failed", topic=msg.topic(), partition=msg.partition(), error=str(err))
    else:
        log.debug("delivered", topic=msg.topic(), partition=msg.partition(), offset=msg.offset())


def run_producer(
    events_per_second: float = GeneratorConfig.EVENTS_PER_SECOND,
    max_events: int | None = None,
) -> None:
    generator = ClaimGenerator()
    producer = _build_producer()
    topic = KafkaConfig.TOPIC_CLAIMS_RAW

    count = 0

    def _shutdown(sig, frame):
        log.info("shutdown_signal", signal=sig)
        producer.flush()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    log.info("producer_starting", topic=topic, rate=events_per_second)

    for event in generator.generate_stream(events_per_second):
        try:
            producer.produce(
                topic=topic,
                key=event.payer_id,  # partition key: per-payer ordering
                value=event.to_dict(),
                on_delivery=delivery_report,
            )
            producer.poll(0)
            count += 1

            if count % 100 == 0:
                log.info("producer_heartbeat", events_sent=count)

            if max_events and count >= max_events:
                log.info("max_events_reached", count=count)
                break

        except KafkaException as e:
            log.error("produce_error", error=str(e), claim_id=event.claim_id)
            # Dead-letter: non-fatal; continue producing
            continue

    producer.flush()
    log.info("producer_done", total_events=count)


if __name__ == "__main__":
    run_producer()
