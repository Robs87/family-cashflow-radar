"""Tests for current cash balance calibration."""

import sqlite3
import subprocess
import sys

import pytest

from app.scripts.cash_balance import (
    latest_cash_balance,
    parse_yuan_to_cents,
    safety_months,
    save_cash_balance_calibration,
)
from app.scripts.simulate_decision import simulate_decision
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


def test_parse_yuan_to_cents_accepts_commas_and_rounds():
    assert parse_yuan_to_cents("12,345.678") == 1_234_568


def test_save_and_read_latest_cash_balance(db_path):
    save_cash_balance_calibration(db_path, 120_000_00, "2026-05-20", scope="活期+货基")
    latest_id = save_cash_balance_calibration(db_path, 150_000_00, "2026-05-22", note="月底前校准")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    latest = latest_cash_balance(conn)
    conn.close()

    assert latest is not None
    assert latest.id == latest_id
    assert latest.available_cash_cents == 150_000_00
    assert latest.calibration_date == "2026-05-22"
    assert latest.note == "月底前校准"


def test_safety_months_uses_fixed_expense_and_debt_payment():
    assert safety_months(150_000_00, 50_000_00) == 3.0


def test_decision_simulation_uses_calibrated_cash_as_opening_buffer(db_path):
    without_balance = simulate_decision(db_path, "large_purchase", 1_200_000, "2026-06")
    save_cash_balance_calibration(db_path, 100_000_00, "2026-05-22")
    with_balance = simulate_decision(db_path, "large_purchase", 1_200_000, "2026-06")

    assert with_balance.min_safety_months > without_balance.min_safety_months
    assert with_balance.min_cash_cents > without_balance.min_cash_cents


def test_cli_prints_stable_summary(db_path):
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.scripts.cash_balance",
            "--db",
            str(db_path),
            "--amount",
            "12345.67",
            "--date",
            "2026-05-22",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "saved=1" in completed.stdout
    assert "calibration_id=1" in completed.stdout
