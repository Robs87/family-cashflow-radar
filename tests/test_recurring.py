"""Tests for recurring bill templates and automatic transaction generation."""

import sqlite3
from decimal import Decimal

import pytest

from app.scripts.recurring import (
    add_mortgage_prepayment,
    build_equal_payment_schedule,
    build_fixed_payment_schedule,
    create_fixed_bill_template,
    create_mortgage_template,
    generate_due_recurring_bills,
    update_fixed_bill_template,
    update_mortgage_template,
    update_mortgage_prepayment,
)
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


def test_equal_payment_schedule_splits_principal_and_interest():
    rows = build_equal_payment_schedule(1_000_000, Decimal("3.6"), 12)

    assert len(rows) == 12
    assert rows[0]["interest_cents"] == 3000
    assert rows[0]["principal_cents"] > 0
    assert rows[-1]["remaining_principal_cents"] == 0
    assert sum(row["principal_cents"] for row in rows) == 1_000_000
    assert all(row["payment_cents"] == row["principal_cents"] + row["interest_cents"] for row in rows)


def test_fixed_payment_schedule_reduces_term_after_prepayment():
    original = build_equal_payment_schedule(1_000_000, Decimal("3.6"), 12)
    reduced = build_fixed_payment_schedule(500_000, Decimal("3.6"), original[0]["payment_cents"])

    assert len(reduced) < 12
    assert reduced[-1]["remaining_principal_cents"] == 0
    assert sum(row["principal_cents"] for row in reduced) == 500_000


def test_create_mortgage_template_generates_full_schedule(db_path):
    template_id = create_mortgage_template(
        db_path,
        "房贷",
        1_000_000,
        Decimal("3.6"),
        12,
        "2026-01-15",
        15,
        account="招商银行",
        lender="银行",
    )

    conn = sqlite3.connect(str(db_path))
    template = conn.execute(
        """SELECT bill_type, financial_type, amount_cents, debt_id
           FROM recurring_bill_templates
           WHERE id = ?""",
        (template_id,),
    ).fetchone()
    schedule_count = conn.execute(
        "SELECT COUNT(*) FROM mortgage_repayment_schedule WHERE recurring_template_id = ?",
        (template_id,),
    ).fetchone()[0]
    first = conn.execute(
        """SELECT due_date, payment_cents, principal_cents, interest_cents
           FROM mortgage_repayment_schedule
           WHERE recurring_template_id = ?
           ORDER BY period_no
           LIMIT 1""",
        (template_id,),
    ).fetchone()
    conn.close()

    assert template[0] == "mortgage"
    assert template[1] == "debt_payment"
    assert template[2] == first[1]
    assert template[3] is not None
    assert schedule_count == 12
    assert first[0] == "2026-01-15"
    assert first[2] + first[3] == first[1]


def test_update_mortgage_template_before_generation_rebuilds_schedule(db_path):
    template_id = create_mortgage_template(db_path, "房贷", 1_000_000, Decimal("3.6"), 12, "2026-01-15", 15)

    update_mortgage_template(
        db_path,
        template_id,
        "房贷修正",
        2_000_000,
        Decimal("3.2"),
        24,
        "2026-02-20",
        20,
        account="招商银行",
        lender="银行",
    )

    conn = sqlite3.connect(str(db_path))
    template = conn.execute(
        """SELECT name, amount_cents, start_date, end_date, day_of_month, account
           FROM recurring_bill_templates
           WHERE id = ?""",
        (template_id,),
    ).fetchone()
    debt = conn.execute(
        """SELECT debt_name, principal_initial_cents, principal_current_cents, interest_rate
           FROM debts
           WHERE id = (SELECT debt_id FROM recurring_bill_templates WHERE id = ?)""",
        (template_id,),
    ).fetchone()
    schedule = conn.execute(
        """SELECT COUNT(*), MIN(due_date), MAX(due_date), SUM(principal_cents)
           FROM mortgage_repayment_schedule
           WHERE recurring_template_id = ?""",
        (template_id,),
    ).fetchone()
    conn.close()

    assert template[0] == "房贷修正"
    assert template[2:] == ("2026-02-20", "2028-01-20", 20, "招商银行")
    assert debt == ("房贷修正", 2_000_000, 2_000_000, 3.2)
    assert schedule == (24, "2026-02-20", "2028-01-20", 2_000_000)


def test_update_mortgage_template_rejects_generated_history(db_path):
    template_id = create_mortgage_template(db_path, "房贷", 1_000_000, Decimal("3.6"), 12, "2026-01-15", 15)
    generate_due_recurring_bills(db_path, as_of="2026-01-31")

    with pytest.raises(ValueError, match="已经生成过自动记账"):
        update_mortgage_template(
            db_path,
            template_id,
            "房贷修正",
            2_000_000,
            Decimal("3.2"),
            24,
            "2026-02-20",
            20,
        )


def test_generate_due_mortgage_creates_transaction_split_and_monthly(db_path):
    create_mortgage_template(db_path, "房贷", 1_000_000, Decimal("3.6"), 12, "2026-01-15", 15)

    result = generate_due_recurring_bills(db_path, as_of="2026-01-31")

    assert result.generated == 1
    assert result.skipped_existing == 0
    assert result.failed == 0

    conn = sqlite3.connect(str(db_path))
    normalized = conn.execute(
        """SELECT id, amount_cents, financial_type, cashflow_direction, is_recurring, is_debt_related
           FROM normalized_transactions"""
    ).fetchone()
    split = conn.execute(
        """SELECT principal_cents, interest_cents, fee_cents
           FROM debt_payment_splits
           WHERE normalized_transaction_id = ?""",
        (normalized[0],),
    ).fetchone()
    monthly = conn.execute(
        """SELECT debt_payment_cents, net_operating_cashflow_cents
           FROM monthly_cashflow
           WHERE year = 2026 AND month = 1"""
    ).fetchone()
    conn.close()

    assert normalized[2:] == ("debt_payment", "outflow", 1, 1)
    assert split[0] + split[1] + split[2] == normalized[1]
    assert monthly == (normalized[1], -normalized[1])


def test_generate_due_recurring_is_idempotent(db_path):
    create_mortgage_template(db_path, "房贷", 1_000_000, Decimal("3.6"), 12, "2026-01-15", 15)

    first = generate_due_recurring_bills(db_path, as_of="2026-02-28")
    second = generate_due_recurring_bills(db_path, as_of="2026-02-28")

    conn = sqlite3.connect(str(db_path))
    normalized_count = conn.execute("SELECT COUNT(*) FROM normalized_transactions").fetchone()[0]
    split_count = conn.execute("SELECT COUNT(*) FROM debt_payment_splits").fetchone()[0]
    conn.close()

    assert first.generated == 2
    assert second.generated == 0
    assert second.skipped_existing == 2
    assert normalized_count == 2
    assert split_count == 2


def test_prepayment_reduce_term_recalculates_future_schedule(db_path):
    template_id = create_mortgage_template(db_path, "房贷", 1_000_000, Decimal("3.6"), 12, "2026-01-15", 15)
    before_count = sqlite3.connect(str(db_path)).execute(
        "SELECT COUNT(*) FROM mortgage_repayment_schedule WHERE recurring_template_id = ?",
        (template_id,),
    ).fetchone()[0]

    event_id = add_mortgage_prepayment(db_path, template_id, "2026-04-01", 300_000, effect_type="reduce_term")

    conn = sqlite3.connect(str(db_path))
    event = conn.execute(
        """SELECT amount_cents, remaining_principal_before_cents, remaining_principal_after_cents
           FROM mortgage_prepayment_events
           WHERE id = ?""",
        (event_id,),
    ).fetchone()
    after_count = conn.execute(
        "SELECT COUNT(*) FROM mortgage_repayment_schedule WHERE recurring_template_id = ?",
        (template_id,),
    ).fetchone()[0]
    future = conn.execute(
        """SELECT MIN(due_date), MAX(due_date), SUM(principal_cents)
           FROM mortgage_repayment_schedule
           WHERE recurring_template_id = ?
             AND due_date >= '2026-04-15'""",
        (template_id,),
    ).fetchone()
    conn.close()

    assert before_count == 12
    assert event == (300_000, 753_360, 453_360)
    assert after_count < before_count
    assert future[0] == "2026-04-15"
    assert future[2] == 453_360


def test_prepayment_reduce_payment_keeps_remaining_term(db_path):
    template_id = create_mortgage_template(db_path, "房贷", 1_000_000, Decimal("3.6"), 12, "2026-01-15", 15)
    old_payment = sqlite3.connect(str(db_path)).execute(
        "SELECT amount_cents FROM recurring_bill_templates WHERE id = ?",
        (template_id,),
    ).fetchone()[0]

    add_mortgage_prepayment(db_path, template_id, "2026-04-01", 300_000, effect_type="reduce_payment")

    conn = sqlite3.connect(str(db_path))
    new_payment = conn.execute(
        "SELECT amount_cents FROM recurring_bill_templates WHERE id = ?",
        (template_id,),
    ).fetchone()[0]
    future_count = conn.execute(
        """SELECT COUNT(*)
           FROM mortgage_repayment_schedule
           WHERE recurring_template_id = ?
             AND due_date >= '2026-04-15'""",
        (template_id,),
    ).fetchone()[0]
    conn.close()

    assert new_payment < old_payment
    assert future_count == 9


def test_update_prepayment_restores_snapshot_and_recalculates(db_path):
    template_id = create_mortgage_template(db_path, "房贷", 1_000_000, Decimal("3.6"), 12, "2026-01-15", 15)
    event_id = add_mortgage_prepayment(db_path, template_id, "2026-04-01", 300_000, effect_type="reduce_term")

    new_event_id = update_mortgage_prepayment(db_path, event_id, "2026-05-01", 200_000, effect_type="reduce_payment")

    conn = sqlite3.connect(str(db_path))
    events = conn.execute(
        """SELECT id, prepayment_date, amount_cents, effect_type,
                  remaining_principal_before_cents, remaining_principal_after_cents
           FROM mortgage_prepayment_events"""
    ).fetchall()
    future = conn.execute(
        """SELECT COUNT(*), MIN(due_date), SUM(principal_cents)
           FROM mortgage_repayment_schedule
           WHERE recurring_template_id = ?
             AND due_date >= '2026-05-15'""",
        (template_id,),
    ).fetchone()
    conn.close()

    assert new_event_id != event_id
    assert events == [(new_event_id, "2026-05-01", 200_000, "reduce_payment", 670_653, 470_653)]
    assert future == (8, "2026-05-15", 470_653)


def test_update_generated_prepayment_is_rejected(db_path):
    template_id = create_mortgage_template(db_path, "房贷", 1_000_000, Decimal("3.6"), 12, "2026-01-15", 15)
    event_id = add_mortgage_prepayment(db_path, template_id, "2026-04-01", 300_000, effect_type="reduce_term")
    generate_due_recurring_bills(db_path, as_of="2026-04-01")

    with pytest.raises(ValueError, match="已经生成交易"):
        update_mortgage_prepayment(db_path, event_id, "2026-05-01", 200_000, effect_type="reduce_payment")


def test_generate_prepayment_creates_principal_only_split(db_path):
    template_id = create_mortgage_template(db_path, "房贷", 1_000_000, Decimal("3.6"), 12, "2026-01-15", 15)
    add_mortgage_prepayment(db_path, template_id, "2026-04-01", 300_000, effect_type="reduce_term")

    result = generate_due_recurring_bills(db_path, as_of="2026-04-01")

    conn = sqlite3.connect(str(db_path))
    prepayment = conn.execute(
        """SELECT n.amount_cents, s.principal_cents, s.interest_cents, s.remaining_principal_cents
           FROM normalized_transactions n
           JOIN debt_payment_splits s ON s.normalized_transaction_id = n.id
           WHERE n.category_l2 = '房贷提前还款'"""
    ).fetchone()
    monthly = conn.execute(
        """SELECT debt_payment_cents
           FROM monthly_cashflow
           WHERE year = 2026 AND month = 4"""
    ).fetchone()
    conn.close()

    assert result.generated == 4
    assert prepayment == (300_000, 300_000, 0, 453_360)
    assert monthly == (300_000,)


def test_fixed_bill_template_generates_monthly_transactions(db_path):
    create_fixed_bill_template(
        db_path,
        "宽带",
        19900,
        "2026-01-01",
        1,
        "宽带",
        end_date="2026-03-31",
    )

    result = generate_due_recurring_bills(db_path, as_of="2026-03-31")

    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        """SELECT transaction_date, amount_cents, financial_type, category_l2
           FROM normalized_transactions
           ORDER BY transaction_date"""
    ).fetchall()
    conn.close()

    assert result.generated == 3
    assert rows == [
        ("2026-01-01", 19900, "fixed_expense", "宽带"),
        ("2026-02-01", 19900, "fixed_expense", "宽带"),
        ("2026-03-01", 19900, "fixed_expense", "宽带"),
    ]


def test_update_fixed_bill_template_before_generation(db_path):
    template_id = create_fixed_bill_template(db_path, "宽带", 19900, "2026-01-01", 1, "宽带")

    update_fixed_bill_template(
        db_path,
        template_id,
        "电话费",
        9900,
        "2026-02-10",
        10,
        "电话费",
        account="信用卡",
        end_date="2026-12-31",
    )

    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        """SELECT name, amount_cents, start_date, end_date, day_of_month, category_l2, account
           FROM recurring_bill_templates
           WHERE id = ?""",
        (template_id,),
    ).fetchone()
    conn.close()
    assert row == ("电话费", 9900, "2026-02-10", "2026-12-31", 10, "电话费", "信用卡")
