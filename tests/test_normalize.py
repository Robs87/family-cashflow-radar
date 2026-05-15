"""Tests for normalize.py: normalization, idempotency, direction, flags, dry-run."""

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from app.scripts.import_csv import import_csv
from app.scripts.normalize import normalize
from tests.conftest import FIXTURES_DIR, PROJECT_ROOT, SCHEMA_SQL

NORMALIZE_SCRIPT = PROJECT_ROOT / "app" / "scripts" / "normalize.py"


# ============================================================
# fixtures
# ============================================================

@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    conn.close()
    return path


@pytest.fixture
def db_with_raw(db_path):
    """DB with edge_cases CSV imported into raw_transactions."""
    import_csv(db_path, FIXTURES_DIR / "sample_pixiu_edge_cases.csv")
    return db_path


@pytest.fixture
def db_with_all_raw(db_path):
    """DB with all fixture CSVs imported."""
    import_csv(db_path, FIXTURES_DIR)
    return db_path


def _fetch_normalized(db_path: Path) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM normalized_transactions ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _count_normalized(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    count = conn.execute("SELECT count(*) FROM normalized_transactions").fetchone()[0]
    conn.close()
    return count


# ============================================================
# basic: normalized count == raw count
# ============================================================

class TestNormalizedCount:
    def test_edge_cases(self, db_with_raw):
        normalize(db_with_raw)
        assert _count_normalized(db_with_raw) == 18

    def test_all_fixtures(self, db_with_all_raw):
        normalize(db_with_all_raw)
        assert _count_normalized(db_with_all_raw) == 35


# ============================================================
# idempotency: re-normalize doesn't increase row count
# ============================================================

class TestIdempotency:
    def test_run_twice_same_count(self, db_with_raw):
        normalize(db_with_raw)
        count1 = _count_normalized(db_with_raw)
        normalize(db_with_raw)
        count2 = _count_normalized(db_with_raw)
        assert count1 == count2 == 18

    def test_all_fixtures_twice(self, db_with_all_raw):
        normalize(db_with_all_raw)
        count1 = _count_normalized(db_with_all_raw)
        normalize(db_with_all_raw)
        count2 = _count_normalized(db_with_all_raw)
        assert count1 == count2 == 35


# ============================================================
# amount_cents: always positive
# ============================================================

class TestAmountCentsPositive:
    def test_all_amounts_positive(self, db_with_raw):
        normalize(db_with_raw)
        rows = _fetch_normalized(db_with_raw)
        for row in rows:
            assert row["amount_cents"] > 0, f"id={row['id']} amount_cents={row['amount_cents']}"

    def test_all_fixtures_positive(self, db_with_all_raw):
        normalize(db_with_all_raw)
        rows = _fetch_normalized(db_with_all_raw)
        for row in rows:
            assert row["amount_cents"] > 0


# ============================================================
# cashflow_direction
# ============================================================

class TestCashflowDirection:
    def test_income_is_inflow(self, db_with_raw):
        normalize(db_with_raw)
        rows = _fetch_normalized(db_with_raw)
        income_rows = [r for r in rows if r["category_l1"] == "工资"]
        for row in income_rows:
            assert row["cashflow_direction"] == "inflow"

    def test_expense_is_outflow(self, db_with_raw):
        normalize(db_with_raw)
        rows = _fetch_normalized(db_with_raw)
        expense_rows = [r for r in rows if r["category_l1"] in ("居住", "餐饮", "日用", "交通", "保险")]
        for row in expense_rows:
            assert row["cashflow_direction"] == "outflow"

    def test_credit_card_payment_is_neutral(self, db_with_raw):
        normalize(db_with_raw)
        rows = _fetch_normalized(db_with_raw)
        cc_rows = [r for r in rows if r["description"] and "信用卡还款" in r["description"]]
        assert len(cc_rows) >= 1
        for row in cc_rows:
            assert row["cashflow_direction"] == "neutral"
            assert row["financial_type"] == "credit_card_payment"

    def test_internal_transfer_is_neutral(self, db_with_raw):
        normalize(db_with_raw)
        rows = _fetch_normalized(db_with_raw)
        transfer_rows = [r for r in rows if r["category_l1"] == "转账"]
        assert len(transfer_rows) >= 1
        for row in transfer_rows:
            assert row["cashflow_direction"] == "neutral"
            assert row["financial_type"] == "internal_transfer"

    def test_borrowing_is_not_neutral(self, db_with_raw):
        """借入资金 is income, not neutral."""
        normalize(db_with_raw)
        rows = _fetch_normalized(db_with_raw)
        borrow_rows = [r for r in rows if r["category_l1"] == "借款"]
        assert len(borrow_rows) >= 1
        for row in borrow_rows:
            assert row["cashflow_direction"] == "inflow"


# ============================================================
# year / month extraction
# ============================================================

class TestYearMonth:
    def test_correct_year_month(self, db_with_raw):
        normalize(db_with_raw)
        rows = _fetch_normalized(db_with_raw)
        for row in rows:
            date_parts = row["transaction_date"].split("-")
            assert row["year"] == int(date_parts[0])
            assert row["month"] == int(date_parts[1])

    def test_2021_2022_fixture(self, db_path):
        import_csv(db_path, FIXTURES_DIR / "sample_pixiu_2021_2022.csv")
        normalize(db_path)
        rows = _fetch_normalized(db_path)
        years = {r["year"] for r in rows}
        assert years == {2021, 2022}


# ============================================================
# is_large_amount flag
# ============================================================

class TestLargeAmount:
    def test_large_amount_flagged(self, db_with_raw):
        normalize(db_with_raw)
        rows = _fetch_normalized(db_with_raw)
        large_rows = [r for r in rows if r["amount_cents"] >= 1_000_000]
        assert len(large_rows) >= 1
        for row in large_rows:
            assert row["is_large_amount"] == 1

    def test_small_amount_not_flagged(self, db_with_raw):
        normalize(db_with_raw)
        rows = _fetch_normalized(db_with_raw)
        small_rows = [r for r in rows if r["amount_cents"] < 1_000_000]
        assert len(small_rows) >= 1
        for row in small_rows:
            assert row["is_large_amount"] == 0


# ============================================================
# dry-run: no DB write
# ============================================================

class TestDryRun:
    def test_no_write(self, db_with_raw):
        normalize(db_with_raw, dry_run=True)
        assert _count_normalized(db_with_raw) == 0


# ============================================================
# CLI output summary
# ============================================================

class TestOutputSummary:
    def test_summary_format(self, db_with_raw, capsys):
        normalize(db_with_raw)
        captured = capsys.readouterr()
        assert "normalized=18" in captured.out
        assert "skipped_existing=0" in captured.out
        assert "failed=0" in captured.out

    def test_summary_idempotent(self, db_with_raw, capsys):
        normalize(db_with_raw)
        capsys.readouterr()
        normalize(db_with_raw)
        captured = capsys.readouterr()
        assert "normalized=0" in captured.out
        assert "skipped_existing=18" in captured.out

    def test_dry_run_summary(self, db_with_raw, capsys):
        normalize(db_with_raw, dry_run=True)
        captured = capsys.readouterr()
        assert "normalized=18" in captured.out
        assert "skipped_existing=0" in captured.out


# ============================================================
# CLI interface (subprocess)
# ============================================================

class TestCLI:
    def test_required_args(self):
        result = subprocess.run(
            [sys.executable, str(NORMALIZE_SCRIPT)],
            capture_output=True, text=True,
        )
        assert result.returncode != 0

    def test_full_run(self, db_with_raw):
        result = subprocess.run(
            [sys.executable, str(NORMALIZE_SCRIPT), "--db", str(db_with_raw)],
            capture_output=True, text=True,
        )
        assert result.returncode == 0
        assert "normalized=18" in result.stdout
        assert _count_normalized(db_with_raw) == 18


# ============================================================
# field mapping from raw
# ============================================================

class TestFieldMapping:
    def test_category_mapped(self, db_with_raw):
        normalize(db_with_raw)
        rows = _fetch_normalized(db_with_raw)
        assert any(r["category_l1"] == "工资" for r in rows)
        assert any(r["category_l1"] == "餐饮" for r in rows)

    def test_counterparty_mapped(self, db_with_raw):
        normalize(db_with_raw)
        rows = _fetch_normalized(db_with_raw)
        merchants = {r["counterparty"] for r in rows if r["counterparty"]}
        assert "模拟科技有限公司" in merchants
        assert "假包子铺" in merchants

    def test_description_mapped(self, db_with_raw):
        normalize(db_with_raw)
        rows = _fetch_normalized(db_with_raw)
        notes = {r["description"] for r in rows if r["description"]}
        assert "3月工资" in notes
