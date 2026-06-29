-- fct_denial_clusters: aggregated DBSCAN cluster summaries from RAW.DENIAL_CLUSTERS.
-- Written nightly by src/feedback/cluster_analyzer.py DenialClusterAnalyzer.
-- is_new_pattern = cluster first appeared in the last 7 days (emerging denial pattern).
-- CLUSTER_ID=-1 = noise (outlier claims that don't form a pattern).
-- Interview story: new patterns detected here re-enter the rule graph via rules.control Kafka topic.
{{
    config(
        materialized='table',
        schema='MART',
        cluster_by=['payer_id', 'dominant_carc']
    )
}}

SELECT
    cluster_id,
    dominant_carc,
    procedure_group,
    payer_id,
    COUNT(*)                                                        AS cluster_size,
    MIN(analyzed_at)                                                AS first_seen,
    MAX(analyzed_at)                                                AS last_seen,
    -- New pattern: cluster appeared within the last 7 days
    CASE
        WHEN MIN(analyzed_at) >= DATEADD('day', -7, CURRENT_TIMESTAMP())
         AND cluster_id != -1
        THEN TRUE ELSE FALSE
    END                                                             AS is_new_pattern,
    -- Noise flag: cluster_id=-1 means DBSCAN found no dense neighborhood
    CASE WHEN cluster_id = -1 THEN TRUE ELSE FALSE END              AS is_noise,
    ROUND(AVG(risk_score), 1)                                       AS avg_risk_score
FROM {{ source('raw', 'denial_clusters') }}
GROUP BY cluster_id, dominant_carc, procedure_group, payer_id
ORDER BY cluster_size DESC
