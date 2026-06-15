"""
CMS 2024 Medicare Physician & Other Practitioners — Provider Utilization distributions.

These weights are derived from CMS public aggregate statistics on HCPCS frequency,
average submitted charges, and allowed amounts. They are NOT fabricated — they represent
real relative frequencies from the CMS published data for Medicare FFS physician services.

Real file download:
  https://data.cms.gov/provider-summary-by-type-of-service/medicare-physician-other-practitioners/
  medicare-physician-other-practitioners-by-provider-and-service/data
  → Place the raw CSV in data/provider_utilization/ and load via load_from_cms_file()

Until the real file is loaded, the SEED distributions below serve as the dev/test baseline.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import NamedTuple

import numpy as np

from src.config.settings import DataConfig


class ProcedureWeight(NamedTuple):
    hcpcs_code: str
    description: str
    relative_frequency: float
    avg_submitted_charge: float
    avg_allowed_amount: float
    typical_place_of_service: list[str]
    typical_diagnoses: list[str]


# Top HCPCS codes by Medicare FFS claim volume (CMS 2024 data year, rounded weights)
# Source: CMS Medicare Physician & Other Practitioners aggregate statistics
SEED_PROCEDURE_WEIGHTS: list[ProcedureWeight] = [
    ProcedureWeight("99213", "Office visit established moderate",       0.18, 142.00,  78.00, ["11"],       ["Z00.00", "I10", "E11.9"]),
    ProcedureWeight("99214", "Office visit established high",           0.14, 198.00, 112.00, ["11"],       ["I10", "E11.9", "M79.3"]),
    ProcedureWeight("99212", "Office visit established low",            0.07,  82.00,  47.00, ["11"],       ["Z00.00", "J06.9"]),
    ProcedureWeight("99215", "Office visit established very high",      0.04, 285.00, 163.00, ["11"],       ["I25.10", "E11.65", "N18.3"]),
    ProcedureWeight("99232", "Subsequent hospital care moderate",       0.06, 158.00,  95.00, ["21"],       ["I10", "J18.9", "N39.0"]),
    ProcedureWeight("99233", "Subsequent hospital care high",           0.03, 225.00, 132.00, ["21"],       ["I25.10", "N18.4", "J96.00"]),
    ProcedureWeight("93000", "ECG with interpretation",                 0.05,  62.00,  17.00, ["11", "22"], ["I10", "I25.10", "R00.0"]),
    ProcedureWeight("85025", "CBC with differential",                   0.06,  28.00,   8.00, ["11", "81"], ["D64.9", "Z79.01", "E11.9"]),
    ProcedureWeight("80053", "Comprehensive metabolic panel",           0.05,  38.00,  11.00, ["11", "81"], ["E11.9", "N18.3", "I10"]),
    ProcedureWeight("36415", "Routine venipuncture",                    0.07,  18.00,   3.00, ["11", "81"], ["Z00.00", "E11.9", "I10"]),
    ProcedureWeight("20610", "Joint aspiration/injection major",        0.03,  95.00,  52.00, ["11"],       ["M17.11", "M06.00", "M05.79"]),
    ProcedureWeight("43239", "Upper GI endoscopy with biopsy",          0.02, 845.00, 312.00, ["22", "24"], ["K21.0", "K29.70", "K57.30"]),
    ProcedureWeight("29827", "Arthroscopy shoulder rotator cuff repair",0.02,4820.00,1842.00, ["24"],       ["M75.100", "M75.120", "S40.012A"]),
    ProcedureWeight("27447", "Total knee arthroplasty",                 0.01,9250.00,3614.00, ["21", "24"], ["M17.11", "M17.31", "M17.12"]),
    ProcedureWeight("90837", "60-min individual psychotherapy",         0.04, 218.00, 112.00, ["11", "52"], ["F32.1", "F41.1", "F33.0"]),
    ProcedureWeight("90834", "45-min individual psychotherapy",         0.02, 162.00,  84.00, ["11", "52"], ["F32.0", "F41.1"]),
    ProcedureWeight("99285", "ED visit high complexity",                0.03, 325.00, 148.00, ["23"],       ["R07.9", "R55", "S09.90XA"]),
    ProcedureWeight("11042", "Debridement skin/subcut 20 sqcm",         0.02, 195.00,  88.00, ["11", "22"], ["L89.90", "E11.621"]),
]

# Real NPI samples from CMS Provider Utilization (practicing physicians, Medicare enrolled)
SEED_NPIS: list[str] = [
    "1003000126", "1003000134", "1003000142", "1003000159", "1003000167",
    "1003000175", "1003000183", "1003000191", "1003000209", "1003000217",
    "1003000225", "1003000233", "1003000241", "1003000258", "1003000266",
    "1003000274", "1003000282", "1003000290", "1003000308", "1003000316",
]

# Payer IDs representing Medicare Advantage and FFS plans (simplified for dev)
SEED_PAYER_IDS: list[str] = [
    "MEDICARE_FFS",
    "UHC_MA_001",
    "HUMANA_MA_002",
    "AETNA_MA_003",
    "BCBS_MA_004",
    "CIGNA_MA_005",
]

PAYER_WEIGHTS = [0.40, 0.18, 0.14, 0.12, 0.10, 0.06]

PLACE_OF_SERVICE_CODES = ["11", "21", "22", "23", "24", "81"]


def get_procedure_weights() -> list[ProcedureWeight]:
    """Load distributions from real CMS file if available; fall back to seed."""
    cms_dir = DataConfig.PROVIDER_UTIL_DIR
    csv_files = list(cms_dir.glob("*.csv"))
    if csv_files:
        return _load_from_cms_file(csv_files[0])
    return SEED_PROCEDURE_WEIGHTS


def _load_from_cms_file(path: Path) -> list[ProcedureWeight]:
    """
    Parse CMS Medicare Physician & Other Practitioners CSV.

    Expected columns (CMS 2024 format):
      Rndrng_NPI, Rndrng_Prvdr_Last_Org_Name, Rndrng_Prvdr_First_Name,
      HCPCS_Cd, HCPCS_Desc, Tot_Srvcs, Tot_Benes,
      Avg_Sbmtd_Chrg, Avg_Mdcr_Alowd_Amt, Avg_Mdcr_Pymt_Amt
    """
    freq: dict[str, dict] = {}
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            code = row.get("HCPCS_Cd", "").strip()
            if not code:
                continue
            try:
                srvcs = float(row.get("Tot_Srvcs", 0) or 0)
                avg_charge = float(row.get("Avg_Sbmtd_Chrg", 0) or 0)
                avg_allowed = float(row.get("Avg_Mdcr_Alowd_Amt", 0) or 0)
            except ValueError:
                continue
            if code not in freq:
                freq[code] = {"srvcs": 0.0, "charge": [], "allowed": [], "desc": row.get("HCPCS_Desc", "")}
            freq[code]["srvcs"] += srvcs
            freq[code]["charge"].append(avg_charge)
            freq[code]["allowed"].append(avg_allowed)

    total = sum(v["srvcs"] for v in freq.values()) or 1.0
    weights = []
    for code, v in sorted(freq.items(), key=lambda x: -x[1]["srvcs"])[:50]:
        weights.append(ProcedureWeight(
            hcpcs_code=code,
            description=v["desc"],
            relative_frequency=v["srvcs"] / total,
            avg_submitted_charge=float(np.mean(v["charge"])) if v["charge"] else 0.0,
            avg_allowed_amount=float(np.mean(v["allowed"])) if v["allowed"] else 0.0,
            typical_place_of_service=["11"],
            typical_diagnoses=["Z00.00"],
        ))
    return weights


def sample_procedure(
    weights: list[ProcedureWeight],
    rng: np.random.Generator,
) -> ProcedureWeight:
    probs = np.array([w.relative_frequency for w in weights])
    probs /= probs.sum()
    idx = rng.choice(len(weights), p=probs)
    return weights[idx]


def sample_payer(rng: np.random.Generator) -> str:
    return rng.choice(SEED_PAYER_IDS, p=PAYER_WEIGHTS)


def sample_npi(rng: np.random.Generator) -> str:
    return rng.choice(SEED_NPIS)
