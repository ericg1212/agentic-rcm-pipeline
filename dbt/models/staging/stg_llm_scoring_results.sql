{{
  config(
    materialized = 'view',
    tags = ['staging', 'llm']
  )
}}

with source as (
    select * from {{ source('raw', 'LLM_SCORING_RESULTS') }}
),

normalized as (
    select
        score_id,
        claim_id,
        scored_at,
        model_id,
        prompt_version,
        ncci_edit_version,
        input_hash,

        -- Normalize stored probability (0.0–1.0) back to 0–100 int for readability
        round(risk_score * 100)::int           as risk_score,
        risk_score                             as risk_score_norm,
        confidence,

        predicted_denial_code,
        -- Flatten VARIANT driving_fields array to comma-separated string
        array_to_string(driving_fields, ', ')  as driving_fields_str,
        driving_fields                         as driving_fields_raw,

        recommended_action,
        rationale,
        full_response,
        latency_ms,
        input_tokens,
        output_tokens,
        loaded_at,

        -- Convenience flags
        predicted_denial_code is not null      as has_denial_prediction,
        recommended_action = 'auto_correct'    as is_auto_correct,
        recommended_action = 'escalate'        as is_escalate

    from source
)

select * from normalized
