"""Tests for generate_monthly_cashflow.py: cents aggregation, formulas, idempotency."""

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from app.scripts.classify import classify
from app.scripts.add_transaction import add_manual_transaction
from app.scripts.generate_monthly_cashflow import generate_monthly_cashflow
from app.scripts.import_csv import import_csv
from app.scripts.normalize import normalize
from tests.conftest import FIXTURES_DIR, PROJECT_ROOT, SCHEMA_SQL, SEED_RULES_SQL

MONTHLY_SCRIPT = PROJECT_ROOT / "app" / "scripts" / "generate_monthly_cashflow.py"


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    conn.executescript(SEED_RULES_SQL.read_text(encoding="utf-8"))
    conn.close()
    return path


@pytest.fixture
def db_with_edge_classified(db_path):
    import_csv(db_path, FIXTURES_DIR / "sample_pixiu_edge_cases.csv")
    normalize(db_path)
    classify(db_path)
    return db_path


@pytest.fixture
def db_with_all_classified(db_path):
    import_csv(db_path, FIXTURES_DIR)
    normalize(db_path)
    classify(db_path)
    return db_path


def _fetch_monthly(db_path: Path) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM monthly_cashflow ORDER BY year, month").fetchall()
    conn.close()
    return [dict(row) for row in rows]


def _fetch_one_month(db_path: Path, year: int, month: int) -> dict:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM monthly_cashflow WHERE year = ? AND month = ?",
        (year, month),
    ).fetchone()
    conn.close()
    assert row is not None, f"missing monthly_cashflow row for {year}-{month:02d}"
    return dict(row)


def _count_monthly(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    count = conn.execute("SELECT count(*) FROM monthly_cashflow").fetchone()[0]
    conn.close()
    return count


class TestMonthlyAggregation:
    def test_edge_case_march_amounts(self, db_with_edge_classified):
        generate_monthly_cashflow(db_with_edge_classified)
        row = _fetch_one_month(db_with_edge_classified, 2025, 3)

        assert row["stable_income_cents"] == 2_000_000
        assert row["one_time_income_cents"] == 5_008_800
        assert row["total_real_income_cents"] == 7_008_800
        assert row["fixed_expense_cents"] == 200_000
        assert row["living_expense_cents"] == 105_000
        assert row["debt_payment_cents"] == 1_150_000
        assert row["investment_outflow_cents"] == 500_000
        assert row["investment_inflow_cents"] == 300_000
        assert row["asset_purchase_cents"] == 25_000_000
        assert row["asset_sale_cents"] == 800_000
        assert row["refund_cents"] == 29_900
        assert row["debt_inflow_cents"] == 3_000_000

    def test_neutral_amounts_are_separate(self, db_with_edge_classified):
        generate_monthly_cashflow(db_with_edge_classified)
        row = _fetch_one_month(db_with_edge_classified, 2025, 3)

        assert row["internal_transfer_cents"] == 1_000_000
        assert row["credit_card_payment_cents"] == 500_000
        assert row["total_real_income_cents"] == (
            row["stable_income_cents"] + row["one_time_income_cents"]
        )
        assert row["fixed_expense_cents"] + row["living_expense_cents"] + row["debt_payment_cents"] == 1_455_000

    def test_formula_fields(self, db_with_edge_classified):
        generate_monthly_cashflow(db_with_edge_classified)
        row = _fetch_one_month(db_with_edge_classified, 2025, 3)

        assert row["net_operating_cashflow_cents"] == 545_000
        assert row["net_total_cashflow_cents"] == -15_816_300
        assert row["cashflow_health_score"] == 27.25

    def test_one_time_income_not_in_operating_cashflow(self, db_with_edge_classified):
        generate_monthly_cashflow(db_with_edge_classified)
        row = _fetch_one_month(db_with_edge_classified, 2025, 3)
        expected = (
            row["stable_income_cents"]
            - row["fixed_expense_cents"]
            - row["living_expense_cents"]
            - row["debt_payment_cents"]
        )
        assert row["net_operating_cashflow_cents"] == expected

    def test_refund_not_counted_as_real_income(self, db_with_edge_classified):
        generate_monthly_cashflow(db_with_edge_classified)
        row = _fetch_one_month(db_with_edge_classified, 2025, 3)
        assert row["refund_cents"] > 0
        assert row["total_real_income_cents"] == row["stable_income_cents"] + row["one_time_income_cents"]

    def test_reimbursements_affect_total_but_not_operating_cashflow(self, db_path):
        add_manual_transaction(db_path, "2026-05-16", 20_00000, "inflow", "stable_income", "工资")
        add_manual_transaction(db_path, "2026-05-16", 320000, "outflow", "reimbursable_expense", "帮公司垫付机票")
        add_manual_transaction(db_path, "2026-05-20", 320000, "inflow", "reimbursement_income", "公司报销到账")

        generate_monthly_cashflow(db_path)
        row = _fetch_one_month(db_path, 2026, 5)

        assert row["stable_income_cents"] == 20_00000
        assert row["living_expense_cents"] == 0
        assert row["one_time_income_cents"] == 0
        assert row["reimbursable_expense_cents"] == 320000
        assert row["reimbursement_income_cents"] == 320000
        assert row["net_operating_cashflow_cents"] == 20_00000
        assert row["net_total_cashflow_cents"] == 20_00000


class TestHistoricalFiltering:
    def test_2021_2022_excluded_when_only_history_exists(self, db_path):
        import_csv(db_path, FIXTURES_DIR / "sample_pixiu_2021_2022.csv")
        normalize(db_path)
        classify(db_path)
        generate_monthly_cashflow(db_path)
        assert _count_monthly(db_path) == 0

    def test_all_fixtures_only_generate_2025_months(self, db_with_all_classified):
        generate_monthly_cashflow(db_with_all_classified)
        rows = _fetch_monthly(db_with_all_classified)
        assert [(row["year"], row["month"]) for row in rows] == [(2025, 1), (2025, 2), (2025, 3)]


class TestManualOverrides:
    def test_manual_financial_type_used_in_monthly_aggregation(self, db_with_edge_classified):
        conn = sqlite3.connect(str(db_with_edge_classified))
        conn.execute(
            """UPDATE normalized_transactions
               SET manual_financial_type = 'fixed_expense',
                   manual_cashflow_direction = 'outflow'
               WHERE description = '早餐'"""
        )
        conn.commit()
        conn.close()

        generate_monthly_cashflow(db_with_edge_classified)
        row = _fetch_one_month(db_with_edge_classified, 2025, 3)

        assert row["fixed_expense_cents"] == 220_000
        assert row["living_expense_cents"] == 85_000
        assert row["net_operating_cashflow_cents"] == 545_000


class TestMinimalFixtureMonths:
    def test_january_and_february_baseline(self, db_path):
        import_csv(db_path, FIXTURES_DIR / "sample_pixiu_minimal.csv")
        normalize(db_path)
        classify(db_path)
        generate_monthly_cashflow(db_path)

        jan = _fetch_one_month(db_path, 2025, 1)
        feb = _fetch_one_month(db_path, 2025, 2)

        assert jan["stable_income_cents"] == 1_500_000
        assert jan["fixed_expense_cents"] == 350_000
        assert jan["living_expense_cents"] == 20_050
        assert jan["net_operating_cashflow_cents"] == 1_129_950

        assert feb["stable_income_cents"] == 1_500_000
        assert feb["fixed_expense_cents"] == 350_000
        assert feb["living_expense_cents"] == 0
        assert feb["net_operating_cashflow_cents"] == 1_150_000


class TestIdempotencyAndDryRun:
    def test_run_twice_updates_same_months(self, db_with_edge_classified):
        generate_monthly_cashflow(db_with_edge_classified)
        count1 = _count_monthly(db_with_edge_classified)
        first = _fetch_one_month(db_with_edge_classified, 2025, 3)

        generate_monthly_cashflow(db_with_edge_classified)
        count2 = _count_monthly(db_with_edge_classified)
        second = _fetch_one_month(db_with_edge_classified, 2025, 3)

        assert count1 == count2 == 1
        for field in (
            "stable_income_cents",
            "net_operating_cashflow_cents",
            "net_total_cashflow_cents",
        ):
            assert first[field] == second[field]

    def test_dry_run_no_write(self, db_with_edge_classified):
        generate_monthly_cashflow(db_with_edge_classified, dry_run=True)
        assert _count_monthly(db_with_edge_classified) == 0


class TestOutputSummaryAndCLI:
    def test_summary_format(self, db_with_edge_classified, capsys):
        generate_monthly_cashflow(db_with_edge_classified)
        captured = capsys.readouterr()
        assert "monthly_generated=1" in captured.out
        assert "failed=0" in captured.out

    def test_cli_full_run(self, db_with_edge_classified):
        result = subprocess.run(
            [sys.executable, str(MONTHLY_SCRIPT), "--db", str(db_with_edge_classified)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "monthly_generated=1" in result.stdout
        assert _count_monthly(db_with_edge_classified) == 1

    def test_cli_required_args(self):
        result = subprocess.run(
            [sys.executable, str(MONTHLY_SCRIPT)],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
