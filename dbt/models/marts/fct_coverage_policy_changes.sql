-- fct_coverage_policy_changes: tracks coverage policy version history per HCPCS code.
-- Interview story: "We version every rule update — first seen, last changed, change count."
-- Answers: which LCDs changed in the last 30 days? Which procedures have unstable coverage?
{{
    config(
        materialized='table',
        schema='MART',
        cluster_by=['hcpcs_code', 'source_type']
    )
}}

WITH rule_history AS (
    SELECT
        hcpcs_code,
        source_type,
        contractor_id,
        coverage_status,
        requires_prior_auth,
        typical_denial_code,
        effective_date,
        expiration_date,
        ingested_at,
        raw_document_id,
        -- Track order of ingestion per (hcpcs, contractor) pair
        ROW_NUMBER() OVER (
            PARTITION BY hcpcs_code, COALESCE(contractor_id, 'NCD')
            ORDER BY ingested_at
        )                                                           AS version_seq,
        -- Detect changes vs previous version
        LAG(coverage_status) OVER (
            PARTITION BY hcpcs_code, COALESCE(contractor_id, 'NCD')
            ORDER BY ingested_at
        )                                                           AS prev_coverage_status,
        LAG(requires_prior_auth) OVER (
            PARTITION BY hcpcs_code, COALESCE(contractor_id, 'NCD')
            ORDER BY ingested_at
        )                                                           AS prev_requires_pa
    FROM {{ ref('stg_payer_rules') }}
)

SELECT
    hcpcs_code,
    source_type,
    contractor_id,
    -- First and latest ingestion timestamps
    MIN(ingested_at)                                                AS first_seen,
    MAX(ingested_at)                                                AS last_ingested,
    -- Unique versions seen (proxy for change count)
    COUNT(DISTINCT coverage_status || '|' || CAST(requires_prior_auth AS VARCHAR)) AS distinct_rule_states,
    -- Current active state
    MAX_BY(coverage_status, ingested_at)                            AS current_coverage_status,
    MAX_BY(requires_prior_auth, ingested_at)                        AS current_requires_prior_auth,
    MAX_BY(typical_denial_code, ingested_at)                        AS current_denial_code,
    -- Flag: did this rule change in the last 30 days?
    CASE
        WHEN MAX(ingested_at) >= DATEADD('day', -30, CURRENT_TIMESTAMP())
         AND COUNT(DISTINCT coverage_status) > 1
        THEN TRUE ELSE FALSE
    END                                                             AS changed_last_30d,
    COUNT(*)                                                        AS total_records
FROM rule_history
GROUP BY hcpcs_code, source_type, contractor_id
