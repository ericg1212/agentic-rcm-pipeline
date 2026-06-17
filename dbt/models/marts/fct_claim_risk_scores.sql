-- Copyright (c) 2026 Eric Grynspan. All rights reserved.
{{
  config(
    materialized = 'table',
    cluster_by = ['payer_id', 'service_date'],
    tags = ['mart', 'core']
  )
}}

/*
  Keystone mart table: joins every claim to its gate decision and LLM score.

  Claims that never reached the LLM (PASS, holdout) have NULL for all llm_* columns.
  final_risk_score and final_action represent the pipeline's actual decision:
    - LLM decision when available
    - Deterministic gate fallback otherwise

  Used by:
    - Streamlit hero row (clean claim rate: intervention vs holdout)
    - LLM trust charts (P/R/F1 vs rules, lift on dirty claims)
    - Operational charts (action breakdown, denial driver table)
*/

with claims as (
    select * from {{ ref('stg_claim_events') }}
),

gate as (
    select * from {{ ref('stg_gate_decisions') }}
),

scores as (
    select * from {{ ref('stg_llm_scoring_results') }}
),

joined as (
    select
        c.claim_id,
        c.event_time,
        c.service_date,
        c.provider_npi,
        c.payer_id,
        c.claim_type,
        c.place_of_service,
        c.submitted_charge_usd,
        c.ncci_edit_version,
        c.is_holdout,

        -- Gate signals
        g.gate_route,
        g.risk_score                            as gate_risk_score,
        g.deterministic_carc,
        g.is_pass                               as gate_passed,
        g.requires_intervention                 as gate_flagged,

        -- LLM signals (null for claims that never reached the LLM)
        s.score_id,
        s.model_id,
        s.prompt_version,
        s.risk_score                            as llm_risk_score,
        s.confidence                            as llm_confidence,
        s.predicted_denial_code,
        s.driving_fields_str,
        s.recommended_action,
        s.rationale,
        s.latency_ms                            as llm_latency_ms,
        s.input_tokens,
        s.output_tokens,
        s.is_auto_correct,
        s.is_escalate,

        -- Derived: was this claim LLM-scored?
        s.score_id is not null                  as was_llm_scored,

        -- Final risk: LLM when available, gate score otherwise
        coalesce(s.risk_score, g.risk_score)    as final_risk_score,

        -- Final action: LLM recommendation when available, deterministic otherwise
        coalesce(
            s.recommended_action,
            case g.gate_route
                when 'pass'      then 'pass'
                when 'hard_fail' then 'flag'
                else                  'flag'
            end
        )                                       as final_action,

        -- Final denial code: LLM prediction when available, gate CARC otherwise
        coalesce(s.predicted_denial_code, g.deterministic_carc)
                                                as final_denial_code,

        -- Holdout / intervention flag for lift calculation
        -- is_holdout=true → control arm (no intervention)
        -- is_holdout=false + was_llm_scored=true → intervention arm
        case
            when c.is_holdout                       then 'holdout'
            when s.score_id is not null             then 'intervention'
            else                                         'deterministic'
        end                                     as cohort

    from claims c
    left join gate g
        on c.claim_id = g.claim_id
    left join scores s
        on c.claim_id = s.claim_id
)

select * from joined
