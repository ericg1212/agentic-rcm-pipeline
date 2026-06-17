# Copyright (c) 2026 Eric Grynspan. All rights reserved.
"""
Layer 4 — Operational Streamlit dashboard.

Run: streamlit run app/dashboard.py

Shows live pipeline health across all 4 layers:
  - Kill-switch status (sidebar)
  - Claims processed / denial rate
  - Holdout lift (intervention vs. control arm)
  - Auto-correction rate + recent actions
  - Drift monitor status

Demo mode: loads synthetic outcome data when DEMO_MODE=true (default).
Production: wire outcome_store + audit_log from the shared process state
or query RAW/MART tables in Snowflake.
"""
from __future__ import annotations

import os
import random
from datetime import datetime, timedelta, timezone

import streamlit as st

from src.action.audit import ImmutableAuditLog, AuditRecord
from src.action.kill_switch import KillSwitch
from src.feedback.drift_monitor import DriftMonitor
from src.feedback.lift_calculator import LiftCalculator
from src.feedback.outcome_store import AdjudicationOutcomeStore, OutcomeRecord

DEMO_MODE = os.getenv("DEMO_MODE", "true").lower() == "true"


# ---------------------------------------------------------------------------
# Demo data factory
# ---------------------------------------------------------------------------

def _build_demo_store(
    n_intervention: int = 180,
    n_holdout: int = 20,
    intervention_denial_rate: float = 0.08,
    holdout_denial_rate: float = 0.22,
    seed: int = 42,
) -> AdjudicationOutcomeStore:
    rng = random.Random(seed)
    store = AdjudicationOutcomeStore()
    base_ts = datetime.now(timezone.utc) - timedelta(hours=6)

    for i in range(n_intervention + n_holdout):
        arm = "holdout" if i < n_holdout else "intervention"
        rate = holdout_denial_rate if arm == "holdout" else intervention_denial_rate
        denied = rng.random() < rate
        store.record_outcome(OutcomeRecord(
            claim_id=f"CLAIM-{i:05d}",
            payer_id="MEDICARE_FFS",
            adjudication_timestamp=(base_ts + timedelta(minutes=i * 2)).isoformat(),
            outcome="DENIED" if denied else "PAID",
            denial_code="CO-97" if denied else None,
            paid_amount=None if denied else rng.uniform(50, 800),
            adjustment_amount=None,
            arm=arm,
        ))

    return store


def _build_demo_audit_log(n: int = 200, seed: int = 42) -> ImmutableAuditLog:
    rng = random.Random(seed)
    log = ImmutableAuditLog()
    actions = ["auto_correct", "flag", "flag", "flag", "pass", "escalate"]
    base_ts = datetime.now(timezone.utc) - timedelta(hours=6)
    for i in range(n):
        log.append(AuditRecord(
            action_id=f"ACT-{i:05d}",
            claim_id=f"CLAIM-{i:05d}",
            score_id=f"SCORE-{i:05d}",
            action_taken=rng.choice(actions),
            action_timestamp=(base_ts + timedelta(minutes=i)).isoformat(),
            confidence=rng.uniform(0.75, 0.99),
            risk_score=rng.randint(20, 95),
            governing_rule_cited="NCCI PTP 93000-93005 | Modifier 59" if rng.random() > 0.6 else None,
            correction_applied=None,
            escalation_draft=None,
            reversible=True,
            kill_switch_active=False,
        ))
    return log


# ---------------------------------------------------------------------------
# Page layout
# ---------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(
        page_title="RCM Prevention Pipeline",
        page_icon="🏥",
        layout="wide",
    )

    store = _build_demo_store() if DEMO_MODE else AdjudicationOutcomeStore()
    audit_log = _build_demo_audit_log() if DEMO_MODE else ImmutableAuditLog()
    kill_switch = KillSwitch()
    lift_calc = LiftCalculator(store)
    drift_monitor = DriftMonitor(store, kill_switch)

    # Sidebar — kill-switch + pipeline status
    with st.sidebar:
        st.title("Pipeline Control")
        if kill_switch.is_active:
            st.error(f"KILL-SWITCH ACTIVE\n{kill_switch.reason}")
            if st.button("Deactivate Kill-Switch"):
                kill_switch.deactivate()
                st.rerun()
        else:
            st.success("Kill-switch: OFF")
            if st.button("Activate Kill-Switch (manual)"):
                kill_switch.activate("manual_dashboard")
                st.rerun()

        st.divider()
        st.metric("Claims Processed", len(audit_log))
        st.metric("Outcomes Received", len(store))
        if DEMO_MODE:
            st.caption("DEMO MODE — synthetic data")

    # Header
    st.title("Agentic RCM Prevention Pipeline")
    st.caption("Real-time pre-submission claim risk scoring | Layers 1–4")
    st.divider()

    # Row 1 — action distribution
    col1, col2, col3, col4 = st.columns(4)
    total = len(audit_log) or 1
    action_counts = {a: 0 for a in ["pass", "auto_correct", "flag", "escalate"]}
    for record in audit_log:
        if record.action_taken in action_counts:
            action_counts[record.action_taken] += 1

    col1.metric("Pass", action_counts["pass"], help="Routed to clearinghouse")
    col2.metric("Auto-Corrected", action_counts["auto_correct"],
                f"{action_counts['auto_correct']/total:.1%}")
    col3.metric("Flagged", action_counts["flag"],
                f"{action_counts['flag']/total:.1%}")
    col4.metric("Escalated", action_counts["escalate"],
                f"{action_counts['escalate']/total:.1%}")

    st.divider()

    # Row 2 — lift analysis
    st.subheader("Holdout Lift Analysis")
    lift = lift_calc.calculate()

    lc1, lc2, lc3, lc4 = st.columns(4)
    lc1.metric(
        "Intervention Denial Rate",
        f"{lift.intervention_denial_rate:.1%}",
        help="Claims that received a pre-submission action",
    )
    lc2.metric(
        "Holdout Denial Rate",
        f"{lift.holdout_denial_rate:.1%}",
        help="Control arm — no intervention",
    )
    lc3.metric(
        "Absolute Lift",
        f"{lift.absolute_lift:+.1%}",
        help="Reduction in denial rate from intervention",
    )
    lc4.metric(
        "Relative Lift",
        f"{lift.relative_lift:+.1%}",
        help="Relative reduction vs. holdout baseline",
    )

    if not lift.sufficient_power:
        st.warning(
            f"Insufficient power — need {30} records per arm. "
            f"Currently: intervention={lift.n_intervention}, holdout={lift.n_holdout}"
        )
    else:
        st.success(lift.summary())

    st.divider()

    # Row 3 — drift monitor
    st.subheader("Denial Rate Drift Monitor")
    drift_alert = drift_monitor.check_drift()
    if drift_alert is None:
        st.info("Insufficient data for drift detection (need 150+ outcomes)")
    elif drift_alert.triggered:
        st.error(drift_alert.message)
    else:
        st.success(drift_alert.message)

    st.divider()

    # Row 4 — recent audit log
    st.subheader("Recent Actions (last 20)")
    rows = []
    for record in list(audit_log)[-20:][::-1]:
        rows.append({
            "Claim ID": record.claim_id,
            "Action": record.action_taken.upper(),
            "Risk Score": record.risk_score,
            "Confidence": f"{record.confidence:.0%}" if record.confidence else "—",
            "Rule Cited": record.governing_rule_cited or "—",
            "Timestamp": record.action_timestamp[:19],
        })
    if rows:
        import pandas as pd
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


if __name__ == "__main__":
    main()
