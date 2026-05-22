"""Tests for future planned cashflow events."""

import sqlite3
import subprocess
import sys

import pytest

from app.scripts.planned_events import (
    create_planned_event,
    forecast_events_by_month,
    parse_yuan_to_cents,
    set_planned_event_enabled,
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


def test_parse_yuan_to_cents_requires_positive_amount():
    assert parse_yuan_to_cents("1,234.56") == 123456
    with pytest.raises(ValueError):
        parse_yuan_to_cents("0")


def test_create_planned_event_and_forecast_by_month(db_path):
    event_id = create_planned_event(
        db_path,
        "年终奖",
        "2026-06-20",
        50_000_00,
        "inflow",
        "one_time_income",
        category_l1="收入",
        category_l2="奖金",
    )

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    forecast = forecast_events_by_month(conn, "2026-06", 2)
    row = conn.execute("SELECT event_name FROM planned_cashflow_events WHERE id = ?", (event_id,)).fetchone()
    conn.close()

    assert row["event_name"] == "年终奖"
    assert forecast == {"2026-06": 50_000_00}


def test_disabled_or_matched_events_do_not_enter_forecast(db_path):
    event_id = create_planned_event(db_path, "买车首付", "2026-06-01", 80_000_00, "outflow", "asset_purchase")
    set_planned_event_enabled(db_path, event_id, False)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    forecast = forecast_events_by_month(conn, "2026-06", 1)
    conn.close()

    assert forecast == {}


def test_decision_simulation_includes_unmatched_planned_events(db_path):
    baseline = simulate_decision(db_path, "large_purchase", 1_200_000, "2026-06")
    create_planned_event(db_path, "奖金到账", "2026-06-20", 50_000_00, "inflow", "one_time_income")

    with_plan = simulate_decision(db_path, "large_purchase", 1_200_000, "2026-06")

    assert with_plan.min_cash_cents > baseline.min_cash_cents
    assert "未来计划现金流" in with_plan.explanation


def test_cli_can_create_planned_event(db_path):
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.scripts.planned_events",
            "--db",
            str(db_path),
            "--name",
            "奖金到账",
            "--date",
            "2026-06-20",
            "--amount",
            "50000",
            "--direction",
            "inflow",
            "--financial-type",
            "one_time_income",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "saved=1" in completed.stdout
    assert "event_id=1" in completed.stdout
