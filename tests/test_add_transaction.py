"""Tests for manual transaction recording."""

import sqlite3
from datetime import date

import pytest

from app.scripts.add_transaction import add_manual_transaction, parse_freeform_transaction
from app.scripts.generate_monthly_cashflow import generate_monthly_cashflow
from tests.conftest import SCHEMA_SQL, SEED_RULES_SQL


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    conn.executescript(SEED_RULES_SQL.read_text(encoding="utf-8"))
    conn.close()
    return path


def test_parse_freeform_transaction_defaults_to_today_outflow():
    parsed = parse_freeform_transaction("今天 68 午饭 外卖", today=date(2026, 5, 16))

    assert parsed.transaction_date == "2026-05-16"
    assert parsed.amount_cents == 6800
    assert parsed.cashflow_direction == "outflow"
    assert parsed.financial_type == "living_expense"
    assert parsed.description == "午饭 外卖"


def test_parse_freeform_transaction_infers_stable_income():
    parsed = parse_freeform_transaction("2026-05-16 工资 20000", today=date(2026, 5, 16))

    assert parsed.transaction_date == "2026-05-16"
    assert parsed.amount_cents == 2_000_000
    assert parsed.cashflow_direction == "inflow"
    assert parsed.financial_type == "stable_income"
    assert parsed.description == "工资"


def test_parse_freeform_transaction_infers_reimbursable_expense():
    parsed = parse_freeform_transaction("今天 帮公司垫付机票 3200", today=date(2026, 5, 16))

    assert parsed.transaction_date == "2026-05-16"
    assert parsed.amount_cents == 320000
    assert parsed.cashflow_direction == "outflow"
    assert parsed.financial_type == "reimbursable_expense"
    assert parsed.description == "帮公司垫付机票"


def test_parse_freeform_transaction_infers_reimbursement_income():
    parsed = parse_freeform_transaction("今天 公司报销 3200 到账", today=date(2026, 5, 16))

    assert parsed.transaction_date == "2026-05-16"
    assert parsed.amount_cents == 320000
    assert parsed.cashflow_direction == "inflow"
    assert parsed.financial_type == "reimbursement_income"
    assert parsed.description == "公司报销 到账"


def test_parse_freeform_transaction_infers_investment_inflow():
    parsed = parse_freeform_transaction("今天 赎回基金 8000", today=date(2026, 5, 16))

    assert parsed.cashflow_direction == "inflow"
    assert parsed.financial_type == "investment_inflow"


def test_add_manual_transaction_writes_raw_and_normalized(db_path):
    raw_id = add_manual_transaction(
        db_path,
        "2026-05-16",
        6800,
        "outflow",
        "living_expense",
        "午饭 外卖",
        category_l1="日常生活",
        category_l2="餐饮",
    )

    conn = sqlite3.connect(str(db_path))
    raw_count = conn.execute("SELECT COUNT(*) FROM raw_transactions").fetchone()[0]
    normalized = conn.execute(
        """SELECT raw_transaction_id, transaction_date, amount_cents,
                  cashflow_direction, financial_type, review_status,
                  manual_financial_type, manual_cashflow_direction, description
           FROM normalized_transactions"""
    ).fetchone()
    conn.close()

    assert raw_count == 1
    assert normalized == (
        raw_id,
        "2026-05-16",
        6800,
        "outflow",
        "living_expense",
        "approved",
        "living_expense",
        "outflow",
        "午饭 外卖",
    )


def test_manual_duplicate_text_is_allowed(db_path):
    for _ in range(2):
        add_manual_transaction(
            db_path,
            "2026-05-16",
            6800,
            "outflow",
            "living_expense",
            "午饭 外卖",
        )

    conn = sqlite3.connect(str(db_path))
    raw_count = conn.execute("SELECT COUNT(*) FROM raw_transactions").fetchone()[0]
    normalized_count = conn.execute("SELECT COUNT(*) FROM normalized_transactions").fetchone()[0]
    conn.close()
    assert raw_count == 2
    assert normalized_count == 2


def test_manual_transaction_updates_monthly_cashflow(db_path):
    add_manual_transaction(db_path, "2026-05-16", 20_00000, "inflow", "stable_income", "工资")
    add_manual_transaction(db_path, "2026-05-16", 6800, "outflow", "living_expense", "午饭 外卖")

    generate_monthly_cashflow(db_path)

    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        """SELECT stable_income_cents, living_expense_cents, net_operating_cashflow_cents
           FROM monthly_cashflow
           WHERE year = 2026 AND month = 5"""
    ).fetchone()
    conn.close()
    assert row == (20_00000, 6800, 19_93200)
