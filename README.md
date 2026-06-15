# Agentic RCM Prevention Pipeline

[![CI](https://github.com/ericg1212/agentic-rcm-pipeline/actions/workflows/ci.yml/badge.svg)](https://github.com/ericg1212/agentic-rcm-pipeline/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/ericg1212/agentic-rcm-pipeline/branch/master/graph/badge.svg)](https://codecov.io/gh/ericg1212/agentic-rcm-pipeline)
[![Python 3.13](https://img.shields.io/badge/python-3.13-blue.svg)](https://www.python.org/downloads/)
[![Apache Kafka](https://img.shields.io/badge/kafka-3.8.0-231F20?logo=apache-kafka)](https://kafka.apache.org/)
[![Snowflake](https://img.shields.io/badge/snowflake-gl20220-29B5E8?logo=snowflake)](https://snowflake.com)
[![Claude API](https://img.shields.io/badge/claude-sonnet--4--6-blueviolet)](https://anthropic.com)

A real-time agentic pipeline that intercepts healthcare claims at the **pre-submission stage**, scores each against real Medicare adjudication rules using an LLM, and autonomously routes or corrects claims to prevent denials before they occur — closing the loop via post-adjudication feedback.

> "P2 and P3 built the architecture on synthetic data — intentionally. P4 runs on real CMS claim distributions: real NCCI edits, real denial codes, real payer denial rates. That sequencing was deliberate. I didn't build four projects. I built one platform in layers."

---

## Architecture

```
Live Claim Generator (real CMS 2024 distributions)
        │
        ▼
  Kafka claims.raw (6 partitions, keyed by payer_id)
        │
        ▼
  Layer 1 — NCCI Gate (deterministic PTP + MUE)
  ┌─────┴──────────────────────────────────┐
  │ PASS → clearinghouse                   │
  │ HARD_FAIL (low-$) → claims.actions     │
  │ AMBIGUOUS / HARD_FAIL (high-$)         │
  │         → claims.scored                │
  └────────────────────────────────────────┘
        │
        ▼
  Layer 2 — Claude API (claude-sonnet-4-6, temp=0)
  Tool-use: lookup_ncci_edit() · get_lcd_policy()
            check_modifier() · get_payer_history()
  Output: risk score · CARC code · action · rationale
        │
        ▼
  Layer 3 — Action (tiered confidence-gated autonomy)
  auto_correct (conf ≥ 0.92, charge ≤ $500)
  flag + enrich → clearinghouse
  escalate → human queue + drafted correction
  holdout (10%) → control arm (no intervention)
        │
        ▼
  Layer 4 — Feedback Loop
  adjudications.outcomes (delayed, out-of-order)
  prediction vs adjudication → Snowflake MART
  Great Expectations drift monitoring
        │
        ▼
  Snowflake RAW → dbt STAGING → dbt MART
  Streamlit dashboard: clean claim rate lift (intervention vs holdout)
```

---

## Stack

| Layer | Technology |
|---|---|
| Streaming | Apache Kafka 3.8.0 (KRaft, no ZooKeeper) |
| LLM | Claude API (`claude-sonnet-4-6`, tool-use) |
| Orchestration | Dagster *(Phase 1 complete)* |
| Warehouse | Snowflake (RAW → STAGING → MART) |
| Transform | dbt |
| Quality | Great Expectations |
| Dashboard | Streamlit |
| Language | Python 3.13 |

---

## Data Strategy

No real PHI or beneficiary-level claims are used. Realness lives in the **policy and distributions**:

| Element | Source |
|---|---|
| Claim substrate | CMS Medicare Physician & Other Practitioners 2024 (real HCPCS distributions + NPIs) |
| Denial logic | NCCI PTP + MUE edits, 2026 Q3 (real quarterly CMS CSVs) |
| Denial codes | X12/WPC CARC/RARC canonical enum |
| Denial rate baseline | CMS Transparency in Coverage PUF |

The live generator emits novel claim events by sampling these real distributions — immune to "you're just replaying a CSV."

---

## Quickstart

```bash
# 1. Start Kafka stack
make up

# 2. Copy and fill env vars
cp .env.example .env

# 3. Install dependencies
make install

# 4. Run the claim generator
make producer

# 5. Run the NCCI gate consumer
make consumer

# 6. Run tests
make test
```

Kafka UI: http://localhost:8080 | Schema Registry: http://localhost:8081

**NCCI data:** Download real quarterly PTP/MUE CSVs from CMS and place in `data/ncci/`.
Seed files (`data/ncci/seed_ptp.csv`, `seed_mue.csv`) are included for dev/testing.

---

## Portfolio Arc

| Project | Focus | Data |
|---|---|---|
| P2 — Healthcare Claims Intelligence | RCM analytics + RWE cohort | Synthea (FHIR R4) |
| P3 — Clinical AI Governance Engine | LLM enrichment + LLM-as-Judge | Synthetic clinical notes |
| **P4 — Agentic RCM Prevention** | **Real-time pre-submission prevention** | **Real CMS distributions** |

---

## ADRs

- [ADR-001: Kafka vs alternatives](docs/adrs/ADR-001-kafka-vs-alternatives.md)
- [ADR-002: Data strategy — real distributions vs row-level claims](docs/adrs/ADR-002-data-ground-truth.md)
- [ADR-003: Latency model and LLM trigger gate](docs/adrs/ADR-003-latency-llm-gate.md)
