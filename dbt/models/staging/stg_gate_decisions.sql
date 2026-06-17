-- Copyright (c) 2026 Eric Grynspan. All rights reserved.
{{
  config(
    materialized = 'view',
    tags = ['staging', 'gate']
  )
}}

with source as (
    select * from {{ source('raw', 'GATE_DECISIONS') }}
),

normalized as (
    select
        decision_id,
        claim_id,
        evaluated_at,
        gate_route,
        round(risk_score * 100)::int   as risk_score,
        risk_score                     as risk_score_norm,
        violations,
        deterministic_carc,
        target_topic,
        ncci_edit_version,
        loaded_at,

        -- Convenience flags
        gate_route = 'pass'            as is_pass,
        gate_route = 'hard_fail'       as is_hard_fail,
        gate_route = 'ambiguous'       as is_ambiguous,
        gate_route != 'pass'           as requires_intervention

    from source
)

select * from normalized
