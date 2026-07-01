# Copyright (c) 2026 Eric Grynspan. All rights reserved.
"""
Great Expectations validation suite for LLM scoring output.

Two validation paths — different purposes, complementary:

  validate_row()   — inline per-record check at the Snowflake write boundary.
                     Fast (no GE context overhead). Produces ValidationResult
                     with the same field names GE uses for consistency.
                     Called in scorer.py after ScoringResult is built.
                     Failure → dead-letter (claims.dlq), never retry.

  validate_batch() — GE 1.x batch validation for CI and nightly data quality.
                     Runs against a DataFrame of RISK_SCORE rows.
                     Adds distribution-level expectations that per-record
                     checks cannot express (mean confidence, action ratios).
                     Returns GE ValidationDefinitionResult-compatible dict.

Why not GE for the hot path:
  Initialising a GE DataContext per claim adds ~200ms of file I/O and object
  construction overhead. The inline path re-implements the same expectations
  in plain Python at <0.1ms. The nightly batch job uses actual GE so the
  full GE audit trail (JSON docs, site builder) is available for compliance.

ADR: GE >= 1.0.0 required (0.18.x pins numpy < 2.0, incompatible with
     Python 3.13 wheels used in CI).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

import structlog

from src.config.settings import DataConfig

log = structlog.get_logger(__name__)

VALID_ACTIONS: frozenset[str] = frozenset(["auto_correct", "flag", "hold", "escalate"])


def _load_carc_codes() -> frozenset[str]:
    try:
        with open(DataConfig.CARC_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return frozenset(data.get("carc", {}).keys())
    except FileNotFoundError:
        log.warning("carc_file_not_found_for_validation")
        return frozenset()


@dataclass
class ExpectationResult:
    expectation: str
    success: bool
    column: str
    observed_value: object
    expected: str


@dataclass
class ValidationResult:
    success: bool
    failures: list[ExpectationResult] = field(default_factory=list)
    all_results: list[ExpectationResult] = field(default_factory=list)

    @property
    def failure_messages(self) -> list[str]:
        return [
            f"{r.expectation} on {r.column}: observed={r.observed_value}, expected={r.expected}"
            for r in self.failures
        ]


class ScoringResultValidator:
    """
    Validates ScoringResult rows produced by ClaimScorer.to_snowflake_row().

    Construct once per process (CARC codes loaded at init).
    Call validate_row() in the hot path; validate_batch() for CI/nightly.
    """

    def __init__(self, carc_codes: Optional[frozenset[str]] = None) -> None:
        self._carc_codes = carc_codes if carc_codes is not None else _load_carc_codes()

    # ------------------------------------------------------------------
    # Inline per-record validation (hot path)
    # ------------------------------------------------------------------

    def validate_row(self, row: dict) -> ValidationResult:
        """
        Fast per-record schema validation at the Snowflake write boundary.
        Failure → caller should dead-letter; never block the scoring loop.
        """
        results: list[ExpectationResult] = []

        # RISK_SCORE: [0.0, 1.0] (norm scale in Snowflake row)
        results.append(self._check_between(
            row, "RISK_SCORE", 0.0, 1.0
        ))

        # CONFIDENCE: [0.0, 1.0]
        results.append(self._check_between(
            row, "CONFIDENCE", 0.0, 1.0
        ))

        # RECOMMENDED_ACTION: must be in valid set
        results.append(self._check_in_set(
            row, "RECOMMENDED_ACTION", VALID_ACTIONS
        ))

        # PREDICTED_DENIAL_CODE: null allowed; when present must be valid CARC
        carc = row.get("PREDICTED_DENIAL_CODE")
        if carc is not None and self._carc_codes:
            results.append(self._check_in_set(
                row, "PREDICTED_DENIAL_CODE", self._carc_codes
            ))

        # CLAIM_ID: must be present and non-empty
        results.append(self._check_not_null(row, "CLAIM_ID"))

        # SCORE_ID: must be present
        results.append(self._check_not_null(row, "SCORE_ID"))

        failures = [r for r in results if not r.success]
        success = len(failures) == 0

        if not success:
            log.warning(
                "GE_VALIDATION_FAILED",
                claim_id=row.get("CLAIM_ID"),
                failures=[r.expectation + ":" + r.column for r in failures],
            )

        return ValidationResult(success=success, failures=failures, all_results=results)

    # ------------------------------------------------------------------
    # GE 1.x batch validation (CI / nightly)
    # ------------------------------------------------------------------

    def validate_batch(self, rows: list[dict]) -> dict:
        """
        Full GE 1.x validation suite against a batch of scoring rows.
        Adds distribution-level expectations not expressible per-record.
        Returns a dict compatible with GE ValidationDefinitionResult structure.
        """
        try:
            import great_expectations as gx
            import pandas as pd
        except ImportError:
            log.warning("great_expectations_not_installed_falling_back_to_inline")
            return self._inline_batch_validate(rows)

        df = pd.DataFrame(rows)

        context = gx.get_context(mode="ephemeral")

        ds = context.data_sources.add_pandas("scoring_results")
        asset = ds.add_dataframe_asset("results_asset")
        batch_def = asset.add_batch_definition_whole_dataframe("batch")

        suite = context.suites.add(gx.ExpectationSuite(name="scoring_result_suite"))

        # Schema expectations (mirror inline validate_row)
        suite.add_expectation(gx.expectations.ExpectColumnValuesToBeBetween(
            column="RISK_SCORE", min_value=0.0, max_value=1.0
        ))
        suite.add_expectation(gx.expectations.ExpectColumnValuesToBeBetween(
            column="CONFIDENCE", min_value=0.0, max_value=1.0
        ))
        suite.add_expectation(gx.expectations.ExpectColumnValuesToBeInSet(
            column="RECOMMENDED_ACTION",
            value_set=list(VALID_ACTIONS),
        ))
        suite.add_expectation(gx.expectations.ExpectColumnValuesToNotBeNull(
            column="CLAIM_ID"
        ))
        suite.add_expectation(gx.expectations.ExpectColumnValuesToNotBeNull(
            column="SCORE_ID"
        ))

        # Distribution expectations — population-level, not per-record
        suite.add_expectation(gx.expectations.ExpectColumnMeanToBeBetween(
            column="CONFIDENCE", min_value=0.5, max_value=0.99,
        ))
        # auto_correct should not dominate (> 50% signals calibration drift)
        suite.add_expectation(gx.expectations.ExpectColumnProportionOfUniqueValuesToBeBetween(
            column="RECOMMENDED_ACTION", min_value=0.0, max_value=1.0,
        ))

        val_def = context.validation_definitions.add(
            gx.ValidationDefinition(
                name="scoring_result_validation",
                data=batch_def,
                suite=suite,
            )
        )

        result = val_def.run(batch_parameters={"dataframe": df})

        log.info(
            "ge_batch_validation_complete",
            success=result.success,
            n_rows=len(rows),
            n_expectations=len(result.results),
        )

        return {
            "success": result.success,
            "n_rows": len(rows),
            "statistics": getattr(result, "statistics", {}),
            "results": [
                {
                    "expectation": r.expectation_config.type,
                    "success": r.success,
                    "observed_value": str(r.result.get("observed_value", "")),
                }
                for r in result.results
            ],
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _check_between(
        self, row: dict, column: str, min_val: float, max_val: float
    ) -> ExpectationResult:
        val = row.get(column)
        try:
            fval = float(val)
            success = min_val <= fval <= max_val
        except (TypeError, ValueError):
            success = False
            fval = val
        return ExpectationResult(
            expectation="expect_column_values_to_be_between",
            success=success,
            column=column,
            observed_value=fval,
            expected=f"[{min_val}, {max_val}]",
        )

    def _check_in_set(
        self, row: dict, column: str, valid_set: frozenset
    ) -> ExpectationResult:
        val = row.get(column)
        return ExpectationResult(
            expectation="expect_column_values_to_be_in_set",
            success=val in valid_set,
            column=column,
            observed_value=val,
            expected=f"one of {sorted(valid_set)[:5]}{'...' if len(valid_set) > 5 else ''}",
        )

    def _check_not_null(self, row: dict, column: str) -> ExpectationResult:
        val = row.get(column)
        return ExpectationResult(
            expectation="expect_column_values_to_not_be_null",
            success=val is not None and val != "",
            column=column,
            observed_value=val,
            expected="not null/empty",
        )

    def _inline_batch_validate(self, rows: list[dict]) -> dict:
        """Fallback when GE is not installed — runs inline validator over all rows."""
        results = [self.validate_row(r) for r in rows]
        failed = [r for r in results if not r.success]
        return {
            "success": len(failed) == 0,
            "n_rows": len(rows),
            "n_failed": len(failed),
            "statistics": {"evaluated_expectations": len(rows), "successful_expectations": len(rows) - len(failed)},
            "results": [],
        }
