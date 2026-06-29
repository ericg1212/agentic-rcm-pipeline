-- Phase 2 DDL — RAW schema additions
-- Execute after ddl.sql (Layers 1-4 tables already provisioned).
-- All tables are append-only (immutable audit trail) except PAYER_RULES (upsert on RULE_ID).

-- =============================================================================
-- 1. RAW.PAYER_RULES — Payer Rule Intelligence Graph storage
--    Grain: one row per (hcpcs_code, icd10_prefix, contractor_id, effective_date)
--    Source: CMS Coverage API (NCD weekly, LCD daily per MAC) + seed JSON
--    Ingested by: src/intelligence/ingestion.py Dagster ops
-- =============================================================================
CREATE TABLE IF NOT EXISTS RAW.PAYER_RULES (
    RULE_ID             VARCHAR(36)     NOT NULL,           -- UUID PK
    SOURCE_TYPE         VARCHAR(10)     NOT NULL,           -- 'ncd' | 'lcd'
    CONTRACTOR_ID       VARCHAR(20),                        -- NULL for NCDs (national scope)
    HCPCS_CODE          VARCHAR(10)     NOT NULL,
    ICD10_PREFIX        VARCHAR(20)     NOT NULL DEFAULT '', -- '' = broadly covered
    COVERAGE_STATUS     VARCHAR(40)     NOT NULL,           -- 'covered' | 'covered_with_restrictions' | 'not_covered'
    REQUIRES_PRIOR_AUTH BOOLEAN         NOT NULL DEFAULT FALSE,
    PA_CRITERIA         VARCHAR(1000),                      -- plain-English PA documentation requirements
    TYPICAL_DENIAL_CODE VARCHAR(20),                        -- e.g. 'CO-50', 'CO-197'
    EFFECTIVE_DATE      DATE            NOT NULL,
    EXPIRATION_DATE     DATE,                               -- NULL = currently active
    INGESTED_AT         TIMESTAMP_NTZ   NOT NULL,
    RAW_DOCUMENT_ID     VARCHAR(100),                       -- CMS document reference (NCD/LCD ID)
    PRIMARY KEY (RULE_ID)
)
COMMENT = 'Payer Rule Intelligence Graph — NCD/LCD coverage policies from CMS Coverage API. SOURCE_TYPE differentiates national (ncd) from MAC-specific (lcd) rules. CONTRACTOR_ID=NULL for NCDs.';

-- Clustering key: lookups are always by HCPCS_CODE + CONTRACTOR_ID
ALTER TABLE RAW.PAYER_RULES CLUSTER BY (HCPCS_CODE, CONTRACTOR_ID);

-- =============================================================================
-- 2. RAW.CALIBRATION_CHECKPOINTS — Platt scaling coefficients + ECE per run
--    Grain: one row per nightly calibration run
--    Source: src/feedback/calibration.py CalibrationMonitor.save_checkpoint()
--    Written by: Dagster calibrate_confidence job (nightly)
-- =============================================================================
CREATE TABLE IF NOT EXISTS RAW.CALIBRATION_CHECKPOINTS (
    CHECKPOINT_ID           VARCHAR(36)     NOT NULL,       -- UUID PK
    COMPUTED_AT             TIMESTAMP_NTZ   NOT NULL,
    PLATT_A                 FLOAT           NOT NULL,       -- logistic regression coefficient a
    PLATT_B                 FLOAT           NOT NULL,       -- logistic regression coefficient b
    ECE                     FLOAT           NOT NULL,       -- Expected Calibration Error (0.0 - 1.0)
    N_LABELED_OUTCOMES      INTEGER         NOT NULL,       -- labeled (confidence, is_denied) pairs used
    FCA_RISK_FLAG           BOOLEAN         NOT NULL DEFAULT FALSE,  -- ECE > 0.10 AND mean_conf > 0.85
    MEAN_CONFIDENCE         FLOAT,                          -- mean model confidence in calibration window
    PRIMARY KEY (CHECKPOINT_ID)
)
COMMENT = 'Platt scaling calibration checkpoints. FCA_RISK_FLAG=TRUE signals reckless disregard risk — ECE > 0.10 with high mean confidence means the model is systematically overconfident.';

-- =============================================================================
-- 3. RAW.DENIAL_CLUSTERS — DBSCAN denial pattern clustering output
--    Grain: one row per (claim_id, cluster analysis run)
--    Source: src/feedback/cluster_analyzer.py DenialClusterAnalyzer.fit()
--    Written by: Dagster analyze_denial_clusters job (nightly)
--    Consumed by: fct_denial_clusters dbt mart
-- =============================================================================
CREATE TABLE IF NOT EXISTS RAW.DENIAL_CLUSTERS (
    CLUSTER_RECORD_ID   VARCHAR(36)     NOT NULL,           -- UUID PK
    CLUSTER_ID          INTEGER         NOT NULL,           -- DBSCAN label; -1 = noise/outlier
    CLAIM_ID            VARCHAR(36)     NOT NULL,
    DOMINANT_CARC       VARCHAR(20),                        -- most common CARC code in this cluster
    PROCEDURE_GROUP     VARCHAR(5),                         -- first 2 HCPCS digits (procedure family)
    PAYER_ID            VARCHAR(50),
    RISK_SCORE          INTEGER,                            -- LLM risk score at time of scoring
    ANALYZED_AT         TIMESTAMP_NTZ   NOT NULL,
    PRIMARY KEY (CLUSTER_RECORD_ID)
)
COMMENT = 'DBSCAN denial pattern clustering output. CLUSTER_ID=-1 means noise (outlier claim that does not form a cluster). is_new_pattern computed in fct_denial_clusters dbt mart.';

-- Clustering key: pattern queries filter by PAYER_ID + ANALYZED_AT
ALTER TABLE RAW.DENIAL_CLUSTERS CLUSTER BY (PAYER_ID, ANALYZED_AT);
