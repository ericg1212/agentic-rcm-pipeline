-- Copyright (c) 2026 Eric Grynspan. All rights reserved.
-- P4 Agentic RCM Prevention Pipeline — Snowflake RAW Schema DDL
-- Account: gl20220 (AWS ca-central-1) | Warehouse: COMPUTE_WH (XS, auto-suspend 60s)
-- All RAW tables are append-only event stores — no updates, no deletes.
-- dbt staging layer handles normalization and deduplication downstream.

CREATE DATABASE IF NOT EXISTS RCM_PREVENTION;
USE DATABASE RCM_PREVENTION;

CREATE SCHEMA IF NOT EXISTS RAW;
USE SCHEMA RAW;

-- ============================================================
-- CLAIM_EVENTS
-- Every claim emitted by the live generator, regardless of outcome.
-- Ingested via Snowflake Kafka Connector / Snowpipe Streaming.
-- Partitioned by PAYER_ID and EVENT_DATE for denial-pattern queries.
-- ============================================================
CREATE TABLE IF NOT EXISTS RAW.CLAIM_EVENTS (
    CLAIM_ID               VARCHAR(36)     NOT NULL,
    EVENT_TIME             TIMESTAMP_NTZ   NOT NULL,
    SERVICE_DATE           DATE            NOT NULL,
    PROVIDER_NPI           VARCHAR(10)     NOT NULL,
    PAYER_ID               VARCHAR(50)     NOT NULL,
    CLAIM_TYPE             VARCHAR(20)     NOT NULL,  -- 'professional' | 'institutional'
    PLACE_OF_SERVICE       VARCHAR(2)      NOT NULL,
    PROCEDURE_CODES        VARIANT         NOT NULL,  -- JSON array of HCPCS strings
    DIAGNOSIS_CODES        VARIANT         NOT NULL,  -- JSON array of ICD-10-CM strings
    MODIFIERS              VARIANT         NOT NULL,  -- JSON array of modifier strings
    UNITS                  INTEGER         NOT NULL,
    SUBMITTED_CHARGE       NUMBER(10,2)    NOT NULL,
    NCCI_EDIT_VERSION      VARCHAR(10)     NOT NULL,
    IS_HOLDOUT             BOOLEAN         NOT NULL DEFAULT FALSE,
    KAFKA_TOPIC            VARCHAR(100),
    KAFKA_PARTITION        INTEGER,
    KAFKA_OFFSET           BIGINT,
    LOADED_AT              TIMESTAMP_NTZ   NOT NULL DEFAULT CURRENT_TIMESTAMP()
)
CLUSTER BY (PAYER_ID, SERVICE_DATE);

COMMENT ON TABLE RAW.CLAIM_EVENTS IS 'All pre-submission claim events from live generator. Source of truth for volume and distribution.';

-- ============================================================
-- LLM_SCORING_RESULTS
-- Every LLM call result for claims routed to the reasoning layer.
-- Immutable audit log: model_id, prompt_version, input_hash, output.
-- ============================================================
CREATE TABLE IF NOT EXISTS RAW.LLM_SCORING_RESULTS (
    SCORE_ID               VARCHAR(36)     NOT NULL,
    CLAIM_ID               VARCHAR(36)     NOT NULL,
    SCORED_AT              TIMESTAMP_NTZ   NOT NULL,
    MODEL_ID               VARCHAR(100)    NOT NULL,  -- e.g. 'claude-sonnet-5'
    PROMPT_VERSION         VARCHAR(20)     NOT NULL,  -- semver prompt tag
    NCCI_EDIT_VERSION      VARCHAR(10)     NOT NULL,
    INPUT_HASH             VARCHAR(64)     NOT NULL,  -- SHA-256 of prompt input JSON
    RISK_SCORE             NUMBER(5,4)     NOT NULL,  -- 0.0000–1.0000
    CONFIDENCE             NUMBER(5,4)     NOT NULL,
    PREDICTED_DENIAL_CODE  VARCHAR(10),               -- CARC code (e.g. 'CO-97'); NULL if PASS
    DRIVING_FIELDS         VARIANT,                   -- JSON array of field names driving risk
    RECOMMENDED_ACTION     VARCHAR(20)     NOT NULL,  -- auto_correct | flag | hold | escalate
    RATIONALE              VARCHAR(2000),             -- plain-English explanation for billing staff
    FULL_RESPONSE          VARIANT         NOT NULL,  -- complete LLM JSON response for audit
    LATENCY_MS             INTEGER,
    INPUT_TOKENS           INTEGER,
    OUTPUT_TOKENS          INTEGER,
    LOADED_AT              TIMESTAMP_NTZ   NOT NULL DEFAULT CURRENT_TIMESTAMP()
)
CLUSTER BY (PREDICTED_DENIAL_CODE, SCORED_AT::DATE);

COMMENT ON TABLE RAW.LLM_SCORING_RESULTS IS 'Immutable LLM audit log. Full reproducibility via model_id + prompt_version + input_hash.';

-- ============================================================
-- GATE_DECISIONS
-- NCCI gate evaluation results for every claim — even those that
-- passed cleanly (PASS route), enabling gate accuracy reporting.
-- ============================================================
CREATE TABLE IF NOT EXISTS RAW.GATE_DECISIONS (
    DECISION_ID            VARCHAR(36)     NOT NULL,
    CLAIM_ID               VARCHAR(36)     NOT NULL,
    EVALUATED_AT           TIMESTAMP_NTZ   NOT NULL,
    GATE_ROUTE             VARCHAR(20)     NOT NULL,  -- pass | hard_fail | ambiguous
    RISK_SCORE             NUMBER(5,4)     NOT NULL,
    VIOLATIONS             VARIANT,                   -- JSON array of NCCIViolation dicts
    DETERMINISTIC_CARC     VARCHAR(10),
    TARGET_TOPIC           VARCHAR(100),
    NCCI_EDIT_VERSION      VARCHAR(10)     NOT NULL,
    LOADED_AT              TIMESTAMP_NTZ   NOT NULL DEFAULT CURRENT_TIMESTAMP()
)
CLUSTER BY (GATE_ROUTE, EVALUATED_AT::DATE);

COMMENT ON TABLE RAW.GATE_DECISIONS IS 'NCCI gate routing decisions. Used to compute LLM-touch rate (% routed to LLM vs cleared deterministically).';

-- ============================================================
-- ACTION_LOG
-- Every autonomous action taken by Layer 3. Immutable audit trail.
-- Required for FCA defense: every auto-correct cites the governing rule.
-- ============================================================
CREATE TABLE IF NOT EXISTS RAW.ACTION_LOG (
    ACTION_ID              VARCHAR(36)     NOT NULL,
    CLAIM_ID               VARCHAR(36)     NOT NULL,
    SCORE_ID               VARCHAR(36),               -- FK to LLM_SCORING_RESULTS (null for deterministic)
    ACTION_TAKEN           VARCHAR(20)     NOT NULL,  -- auto_correct | flag | hold | escalate | pass
    ACTION_TIMESTAMP       TIMESTAMP_NTZ   NOT NULL,
    CONFIDENCE             NUMBER(5,4),
    RISK_SCORE             NUMBER(5,4),
    GOVERNING_RULE_CITED   VARCHAR(200),              -- NCCI edit ref or LCD policy ID cited
    CORRECTION_APPLIED     VARIANT,                   -- JSON: what field was corrected and how
    ESCALATION_DRAFT       VARCHAR(4000),             -- drafted correction text for human review
    REVERSIBLE             BOOLEAN         NOT NULL DEFAULT TRUE,
    KILL_SWITCH_ACTIVE     BOOLEAN         NOT NULL DEFAULT FALSE,
    LOADED_AT              TIMESTAMP_NTZ   NOT NULL DEFAULT CURRENT_TIMESTAMP()
)
CLUSTER BY (ACTION_TAKEN, ACTION_TIMESTAMP::DATE);

COMMENT ON TABLE RAW.ACTION_LOG IS 'Immutable autonomy audit log. Every auto-correct must cite the governing rule (FCA defense).';

-- ============================================================
-- ADJUDICATION_OUTCOMES
-- Post-adjudication outcomes received on the adjudications.outcomes topic.
-- Delayed and potentially out-of-order (realistic 30–90 day delay).
-- Watermarked at ingestion time; feedback loop joins on claim_id.
-- ============================================================
CREATE TABLE IF NOT EXISTS RAW.ADJUDICATION_OUTCOMES (
    OUTCOME_ID             VARCHAR(36)     NOT NULL,
    CLAIM_ID               VARCHAR(36)     NOT NULL,
    ADJUDICATED_AT         TIMESTAMP_NTZ   NOT NULL,
    RECEIVED_AT            TIMESTAMP_NTZ   NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    PAYER_ADJUDICATION     VARCHAR(20)     NOT NULL,  -- approved | denied | partial
    ACTUAL_DENIAL_CODE     VARCHAR(10),               -- CARC if denied
    ACTUAL_DENIAL_REASON   VARCHAR(500),
    AMOUNT_PAID            NUMBER(10,2),
    AMOUNT_DENIED          NUMBER(10,2),
    LOADED_AT              TIMESTAMP_NTZ   NOT NULL DEFAULT CURRENT_TIMESTAMP()
)
CLUSTER BY (PAYER_ADJUDICATION, ADJUDICATED_AT::DATE);

COMMENT ON TABLE RAW.ADJUDICATION_OUTCOMES IS 'Post-adjudication feedback. May arrive 30-90 days after claim event. Joined to LLM_SCORING_RESULTS in mart layer for prediction accuracy.';
