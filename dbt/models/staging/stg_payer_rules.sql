-- stg_payer_rules: normalizes RAW.PAYER_RULES for mart joins.
-- Adds convenience flags and resolves contractor_id=NULL (NCD) vs non-null (LCD).
{{
    config(
        materialized='view',
        schema='STAGING'
    )
}}

SELECT
    RULE_ID                                                         AS rule_id,
    SOURCE_TYPE                                                     AS source_type,
    CONTRACTOR_ID                                                   AS contractor_id,
    -- NCD = national (no MAC), LCD = MAC-specific
    CASE WHEN CONTRACTOR_ID IS NULL THEN TRUE ELSE FALSE END        AS is_ncd,
    CASE WHEN CONTRACTOR_ID IS NOT NULL THEN TRUE ELSE FALSE END    AS is_lcd,
    HCPCS_CODE                                                      AS hcpcs_code,
    NULLIF(ICD10_PREFIX, '')                                        AS icd10_prefix,  -- NULL when broadly covered
    COVERAGE_STATUS                                                 AS coverage_status,
    CASE WHEN COVERAGE_STATUS = 'covered' THEN TRUE ELSE FALSE END  AS is_broadly_covered,
    REQUIRES_PRIOR_AUTH                                             AS requires_prior_auth,
    PA_CRITERIA                                                     AS pa_criteria,
    TYPICAL_DENIAL_CODE                                             AS typical_denial_code,
    EFFECTIVE_DATE                                                  AS effective_date,
    EXPIRATION_DATE                                                  AS expiration_date,
    -- Active rule: effective today and not yet expired
    CASE
        WHEN EFFECTIVE_DATE <= CURRENT_DATE()
         AND (EXPIRATION_DATE IS NULL OR EXPIRATION_DATE > CURRENT_DATE())
        THEN TRUE ELSE FALSE
    END                                                             AS is_active,
    INGESTED_AT                                                     AS ingested_at,
    RAW_DOCUMENT_ID                                                 AS raw_document_id
FROM {{ source('raw', 'payer_rules') }}
