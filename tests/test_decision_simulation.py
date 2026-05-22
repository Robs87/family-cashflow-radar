"""Tests for decision cashflow simulation."""

import sqlite3
import subprocess
import sys
from decimal import Decimal

import pytest

from app.scripts.recurring import create_mortgage_template
from app.scripts.simulate_decision import (
    parse_yuan_to_cents,
    save_decision_scenario,
    simulate_decision,
)
from tests.conftest import SCHEMA_SQL, SEED_RULES_SQL


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    conn.executescript(SEED_RULES_SQL.read_text(encoding="utf-8"))
    conn.execute(
        """INSERT INTO monthly_cashflow
           (year, month, stable_income_cents, living_expense_cents,
            fixed_expense_cents, debt_payment_cents, net_operating_cashflow_cents)
           VALUES (2026, 5, 2000000, 500000, 300000, 200000, 1000000)"""
    )
    conn.commit()
    conn.close()
    return path


def test_parse_yuan_to_cents_rounds_to_integer_cents():
    assert parse_yuan_to_cents("12.345") == 1235


def test_one_time_large_purchase_marks_cash_gap_as_danger(db_path):
    result = simulate_decision(
        db_path,
        "large_purchase",
        8_000_000,
        "2026-06",
    )

    assert result.risk_level == "danger"
    assert result.min_cash_cents == 0
    assert result.risk_month == "2026-06"
    assert "最低缺口" in result.explanation


def test_installment_purchase_can_be_safer_than_one_time(db_path):
    one_time = simulate_decision(
        db_path,
        "large_purchase",
        1_200_000,
        "2026-06",
    )
    installment = simulate_decision(
        db_path,
        "large_purchase",
        1_200_000,
        "2026-06",
        payment_type="installment",
        installment_months=6,
    )

    assert one_time.min_safety_months < installment.min_safety_months
    assert installment.risk_level in {"safe", "watch"}


def test_save_decision_scenario_persists_recommendation(db_path):
    scenario_id, simulation = save_decision_scenario(
        db_path,
        "提前还 5 万",
        "mortgage_prepayment",
        5_000_000,
        "2026-06",
    )

    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        """SELECT scenario_name, decision_type, result_risk_level, recommendation
           FROM decision_scenarios
           WHERE id = ?""",
        (scenario_id,),
    ).fetchone()
    conn.close()

    assert simulation.risk_level in {"safe", "watch", "tight", "danger"}
    assert row[0] == "提前还 5 万"
    assert row[1] == "mortgage_prepayment"
    assert row[2] == simulation.risk_level
    assert row[3]


def test_mortgage_prepayment_estimates_interest_savings(db_path):
    template_id = create_mortgage_template(
        db_path,
        "房贷",
        1_000_000,
        Decimal("3.6"),
        12,
        "2026-01-15",
        15,
    )

    result = simulate_decision(
        db_path,
        "mortgage_prepayment",
        300_000,
        "2026-04",
        mortgage_template_id=template_id,
        mortgage_effect_type="reduce_term",
    )

    assert result.interest_savings_cents > 0
    assert result.term_months_delta > 0
    assert "节省未来利息" in result.explanation
    assert "还款期数减少" in result.explanation


def test_large_purchase_includes_monthly_expense_impact(db_path):
    result = simulate_decision(
        db_path,
        "large_purchase",
        1_200_000,
        "2026-06",
        payment_type="installment",
        installment_months=6,
        expected_expense_impact_cents=200_000,
    )

    assert "每月新增固定支出 2,000.00 元" in result.explanation


def test_investment_reports_cash_reserve_and_upper_bound(db_path):
    result = simulate_decision(
        db_path,
        "investment",
        1_000_000,
        "2026-06",
    )

    assert "必须保留现金" in result.explanation
    assert "可投资现金上限" in result.explanation
    assert result.suggested_max_amount_cents >= 0


def test_cli_prints_stable_summary(db_path):
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.scripts.simulate_decision",
            "--db",
            str(db_path),
            "--name",
            "投资加仓",
            "--decision-type",
            "investment",
            "--amount",
            "10000",
            "--start-month",
            "2026-06",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "saved=1" in completed.stdout
    assert "scenario_id=1" in completed.stdout
    assert "risk_level=" in completed.stdout
