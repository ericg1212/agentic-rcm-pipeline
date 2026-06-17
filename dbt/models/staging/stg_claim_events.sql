{{
  config(
    materialized = 'view',
    tags = ['staging', 'claims']
  )
}}

with source as (
    select * from {{ source('raw', 'CLAIM_EVENTS') }}
),

renamed as (
    select
        claim_id,
        event_time,
        service_date,
        provider_npi,
        payer_id,
        lower(claim_type)        as claim_type,
        place_of_service,
        procedure_codes,
        diagnosis_codes,
        modifiers,
        units,
        submitted_charge::float  as submitted_charge_usd,
        ncci_edit_version,
        is_holdout,
        kafka_topic,
        kafka_partition,
        kafka_offset,
        loaded_at

    from source
)

select * from renamed
