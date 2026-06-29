# Copyright (c) 2026 Eric Grynspan. All rights reserved.
"""
Phase 2 — Payer Rule Intelligence Layer: Ingestion Jobs.

Dagster ops that fetch NCD/LCD policy documents from the CMS Coverage API
and write structured rule records to RAW.PAYER_RULES in Snowflake.

Ingestion cadence (configured via IntelligenceConfig):
  - NCD: weekly  (CMS quarterly update cycle; daily is unnecessary overhead)
  - LCD: daily per MAC (12 MACs may update independently, sometimes monthly)

Production path: op → CMS Coverage API → parse → upsert RAW.PAYER_RULES → reload cache
Seed path (dev/test): PayerRuleGraph.load_from_seed() directly from JSON files

ADR-006: Snowflake chosen over Neo4j (no dbt support, new infra) and static
JSON-only (no ingestion cadence, no delta tracking).
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Optional

import structlog

from src.config.settings import IntelligenceConfig

log = structlog.get_logger(__name__)


def _build_rule_record(
    source_type: str,
    contractor_id: Optional[str],
    hcpcs_code: str,
    icd10_prefix: str,
    coverage_status: str,
    requires_prior_auth: bool,
    pa_criteria: Optional[str],
    typical_denial_code: Optional[str],
    effective_date: str,
    expiration_date: Optional[str],
    raw_document_id: str,
) -> dict:
    """Build a single row for RAW.PAYER_RULES."""
    return {
        "RULE_ID": str(uuid.uuid4()),
        "SOURCE_TYPE": source_type,
        "CONTRACTOR_ID": contractor_id,
        "HCPCS_CODE": hcpcs_code,
        "ICD10_PREFIX": icd10_prefix or "",
        "COVERAGE_STATUS": coverage_status,
        "REQUIRES_PRIOR_AUTH": requires_prior_auth,
        "PA_CRITERIA": pa_criteria,
        "TYPICAL_DENIAL_CODE": typical_denial_code,
        "EFFECTIVE_DATE": effective_date,
        "EXPIRATION_DATE": expiration_date,
        "INGESTED_AT": datetime.now(timezone.utc).isoformat(),
        "RAW_DOCUMENT_ID": raw_document_id,
    }


def ingest_from_seed() -> list[dict]:
    """
    Load rules from seed_lcd.json and convert to RAW.PAYER_RULES row format.
    Used in CI and integration tests — no network calls.
    Returns list of row dicts ready for Snowflake upsert.
    """
    with open(IntelligenceConfig.SEED_LCD_FILE, encoding="utf-8") as f:
        lcd_data = json.load(f)

    rows: list[dict] = []
    for hcpcs, policy in lcd_data.get("policies", {}).items():
        prefixes = policy.get("covered_icd10_prefixes", [])
        # One row per (hcpcs, icd10_prefix) pair — matches Snowflake schema grain
        if not prefixes:
            prefixes = [""]  # broadly covered — no diagnosis restriction
        for prefix in prefixes:
            rows.append(_build_rule_record(
                source_type=policy.get("source_type", "lcd"),
                contractor_id=policy.get("contractor_id"),
                hcpcs_code=hcpcs,
                icd10_prefix=prefix,
                coverage_status=policy.get("coverage_status", "covered"),
                requires_prior_auth=policy.get("requires_prior_auth", False),
                pa_criteria=policy.get("pa_criteria"),
                typical_denial_code=policy.get("typical_denial_code"),
                effective_date=policy.get("effective_date", "2024-01-01"),
                expiration_date=policy.get("expiration_date"),
                raw_document_id=f"seed-v2-{hcpcs}",
            ))

    log.info("ingestion.seed_loaded", rows=len(rows))
    return rows


def ingest_ncd_rules(contractor_id: Optional[str] = None) -> list[dict]:
    """
    Dagster op: fetch National Coverage Determinations from CMS Coverage API.
    NCDs are CMS-wide (contractor_id=None in rule graph) — run weekly.

    Production: calls CMS Coverage API → parses NCD document list → upserts.
    Stub: returns seed rows with source_type='ncd' for integration testing.
    """
    rows = [r for r in ingest_from_seed() if r["SOURCE_TYPE"] == "ncd"]
    log.info("ingestion.ncd_complete", rows_produced=len(rows))
    return rows


def ingest_lcd_rules(contractor_id: str) -> list[dict]:
    """
    Dagster op: fetch Local Coverage Determinations for a specific MAC.
    LCDs are jurisdiction-specific — run daily per MAC (12 MACs).

    Production: calls CMS Coverage API with contractor_id filter → parses LCD
    document list → upserts to RAW.PAYER_RULES → triggers PayerRuleGraph.reload().

    Stub: returns seed rows filtered to the given contractor (or all lcd rows
    when seed contractor_id is null — conservative fallback for testing).
    """
    all_rows = ingest_from_seed()
    # Seed data uses contractor_id=null (national scope) — treat all LCD rows
    # as applicable to any MAC for testing purposes
    lcd_rows = [r for r in all_rows if r["SOURCE_TYPE"] == "lcd"]
    for row in lcd_rows:
        if row["CONTRACTOR_ID"] is None:
            row["CONTRACTOR_ID"] = contractor_id
    log.info("ingestion.lcd_complete", contractor_id=contractor_id, rows_produced=len(lcd_rows))
    return lcd_rows
