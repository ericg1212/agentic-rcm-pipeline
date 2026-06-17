# Copyright (c) 2026 Eric Grynspan. All rights reserved.
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).parent.parent.parent


class KafkaConfig:
    BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    SCHEMA_REGISTRY_URL = os.getenv("SCHEMA_REGISTRY_URL", "http://localhost:8081")
    CONSUMER_GROUP_ID = os.getenv("CONSUMER_GROUP_ID", "rcm-prevention-v1")
    MAX_POLL_RECORDS = int(os.getenv("MAX_POLL_RECORDS", "50"))

    # Topic names — single source of truth
    TOPIC_CLAIMS_RAW = "claims.raw"
    TOPIC_CLAIMS_SCORED = "claims.scored"
    TOPIC_CLAIMS_ACTIONS = "claims.actions"
    TOPIC_ADJUDICATIONS = "adjudications.outcomes"
    TOPIC_RULES_CONTROL = "rules.control"
    TOPIC_DLQ = "claims.dlq"

    # Partition key: payer_id ensures per-payer ordering for rule application consistency
    PARTITION_KEY_FIELD = "payer_id"


class LLMConfig:
    MODEL = "claude-sonnet-4-6"
    MAX_TOKENS = 512
    TEMPERATURE = 0
    API_KEY = os.getenv("ANTHROPIC_API_KEY")
    # Pinned version tag for reproducibility audit log
    MODEL_VERSION_TAG = "claude-sonnet-4-6-20250722"


class SnowflakeConfig:
    ACCOUNT = os.getenv("SNOWFLAKE_ACCOUNT", "gl20220")
    USER = os.getenv("SNOWFLAKE_USER")
    PASSWORD = os.getenv("SNOWFLAKE_PASSWORD")
    DATABASE = os.getenv("SNOWFLAKE_DATABASE", "RCM_PREVENTION")
    WAREHOUSE = os.getenv("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH")
    SCHEMA_RAW = "RAW"
    SCHEMA_STAGING = "STAGING"
    SCHEMA_MART = "MART"


class GateConfig:
    # Minimum deterministic risk score to trigger LLM call (0–1 scale)
    LLM_RISK_THRESHOLD = float(os.getenv("LLM_RISK_THRESHOLD", "0.30"))
    # Fraction of all claims routed to control arm (no intervention)
    HOLDOUT_FRACTION = float(os.getenv("HOLDOUT_FRACTION", "0.10"))


class ActionConfig:
    # LLM confidence floor for autonomous auto-correct
    AUTO_CORRECT_CONFIDENCE_MIN = float(os.getenv("AUTO_CORRECT_CONFIDENCE_MIN", "0.92"))
    # Dollar-value ceiling for autonomous auto-correct (claims above go to human queue)
    AUTO_CORRECT_MAX_CHARGE = float(os.getenv("AUTO_CORRECT_MAX_CHARGE", "500.00"))
    # Risk score floor that triggers escalation regardless of confidence
    ESCALATE_RISK_MIN = float(os.getenv("ESCALATE_RISK_MIN", "0.85"))


class GeneratorConfig:
    EVENTS_PER_SECOND = float(os.getenv("CLAIM_EVENTS_PER_SECOND", "10"))
    POISSON_LAMBDA = float(os.getenv("POISSON_LAMBDA", "10"))
    NCCI_EDIT_VERSION = os.getenv("NCCI_EDIT_VERSION", "2026Q3")
    # Fraction of generated claims that contain deliberate NCCI violations (for eval)
    DIRTY_CLAIM_FRACTION = float(os.getenv("DIRTY_CLAIM_FRACTION", "0.35"))


class DataConfig:
    NCCI_DIR = ROOT / "data" / "ncci"
    CARC_FILE = ROOT / "data" / "carc" / "carc_rarc_enum.json"
    PROVIDER_UTIL_DIR = ROOT / "data" / "provider_utilization"
    SEED_PTP_FILE = NCCI_DIR / "seed_ptp.csv"
    SEED_MUE_FILE = NCCI_DIR / "seed_mue.csv"


class FeedbackConfig:
    # Baseline window size for drift detection (first N outcomes establish reference rate)
    DRIFT_BASELINE_WINDOW = int(os.getenv("DRIFT_BASELINE_WINDOW", "100"))
    # Rolling window size for drift comparison (most recent N outcomes)
    DRIFT_ROLLING_WINDOW = int(os.getenv("DRIFT_ROLLING_WINDOW", "50"))
    # Relative change threshold that triggers the kill-switch (0.20 = 20% relative change)
    DRIFT_THRESHOLD = float(os.getenv("DRIFT_THRESHOLD", "0.20"))
