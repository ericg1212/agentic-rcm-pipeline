-- fct_calibration_curve: reliability diagram data — confidence decile vs actual denial rate.
-- Source: fct_claim_risk_scores (scored claims) JOIN RAW.ADJUDICATION_OUTCOMES (ground truth).
-- Used to render calibration reliability diagram in Power BI.
-- Calibration is "perfect" when mean_predicted_confidence ≈ actual_denial_rate per decile.
{{
    config(
        materialized='table',
        schema='MART'
    )
}}

WITH scored_with_outcomes AS (
    SELECT
        s.claim_id,
        s.confidence,
        CASE WHEN o.outcome = 'denied' THEN 1 ELSE 0 END   AS is_denied
    FROM {{ ref('fct_claim_risk_scores') }}                  AS s
    INNER JOIN {{ source('raw', 'adjudication_outcomes') }}  AS o
        ON s.claim_id = o.claim_id
    WHERE o.outcome IS NOT NULL
),

deciled AS (
    SELECT
        claim_id,
        confidence,
        is_denied,
        NTILE(10) OVER (ORDER BY confidence)                AS decile
    FROM scored_with_outcomes
)

SELECT
    decile,
    COUNT(*)                                                AS n_claims,
    ROUND(AVG(confidence), 4)                               AS mean_predicted_confidence,
    ROUND(AVG(is_denied), 4)                                AS actual_denial_rate,
    -- ECE contribution for this bin (calibration error component)
    ROUND(
        ABS(AVG(confidence) - AVG(is_denied)) * COUNT(*) / SUM(COUNT(*)) OVER (),
        6
    )                                                       AS ece_contribution,
    -- Gap: positive = overconfident, negative = underconfident
    ROUND(AVG(confidence) - AVG(is_denied), 4)              AS calibration_gap
FROM deciled
GROUP BY decile
ORDER BY decile
