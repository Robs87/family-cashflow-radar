"""Tests for schema.sql: table creation, types, constraints."""

import sqlite3

import pytest

from tests.conftest import SCHEMA_SQL, SEED_RULES_SQL

EXPECTED_TABLES = {
    "raw_transactions",
    "normalized_transactions",
    "classification_rules",
    "beecount_category_mappings",
    "monthly_cashflow",
    "asset_events",
    "debts",
    "cashflow_forecast",
    "cash_balance_calibrations",
    "planned_cashflow_events",
    "decision_scenarios",
    "recurring_bill_templates",
    "mortgage_repayment_schedule",
    "recurring_bill_instances",
    "debt_payment_splits",
    "mortgage_prepayment_events",
}


# --- table existence ---

def test_all_tables_created(db_conn):
    cursor = db_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )
    tables = {row[0] for row in cursor.fetchall()}
    for t in EXPECTED_TABLES:
        assert t in tables, f"Missing table: {t}"


# --- amount fields are INTEGER ---

def _get_column_types(conn: sqlite3.Connection, table: str) -> dict[str, str]:
    cursor = conn.execute(f"PRAGMA table_info({table})")
    return {row[1]: row[2].upper() for row in cursor.fetchall()}


def test_normalized_amount_cents_is_integer(db_conn):
    cols = _get_column_types(db_conn, "normalized_transactions")
    assert cols["amount_cents"] == "INTEGER"


def test_monthly_cashflow_fields_are_integer(db_conn):
    cols = _get_column_types(db_conn, "monthly_cashflow")
    cents_fields = [
        k for k in cols if k.endswith("_cents")
    ]
    assert len(cents_fields) > 0, "No *_cents fields found in monthly_cashflow"
    for field in cents_fields:
        assert cols[field] == "INTEGER", f"{field} should be INTEGER, got {cols[field]}"


def test_raw_transactions_amount_cents_is_integer(db_conn):
    cols = _get_column_types(db_conn, "raw_transactions")
    assert cols["amount_cents"] == "INTEGER"


def test_raw_transactions_has_beecount_version_fields(db_conn):
    cols = _get_column_types(db_conn, "raw_transactions")
    for field in [
        "source_system",
        "source_ledger_id",
        "source_transaction_id",
        "source_updated_at",
        "source_deleted_at",
        "source_is_latest",
    ]:
        assert field in cols


def test_cash_balance_calibrations_uses_integer_cents(db_conn):
    cols = _get_column_types(db_conn, "cash_balance_calibrations")
    assert cols["available_cash_cents"] == "INTEGER"
    assert cols["calibration_date"] == "TEXT"


def test_planned_cashflow_events_uses_integer_cents(db_conn):
    cols = _get_column_types(db_conn, "planned_cashflow_events")
    assert cols["amount_cents"] == "INTEGER"
    assert cols["match_status"] == "TEXT"
    assert cols["matched_normalized_transaction_id"] == "INTEGER"


def test_no_real_amount_in_normalized(db_conn):
    """Normalized table must not use REAL for any monetary field."""
    cols = _get_column_types(db_conn, "normalized_transactions")
    money_fields = [k for k in cols if "amount" in k or "cents" in k]
    for field in money_fields:
        assert cols[field] != "REAL", f"{field} uses REAL instead of INTEGER"


def test_asset_events_amount_cents_is_integer(db_conn):
    cols = _get_column_types(db_conn, "asset_events")
    assert cols["amount_cents"] == "INTEGER"


def test_debts_cents_fields_are_integer(db_conn):
    cols = _get_column_types(db_conn, "debts")
    for field in ["principal_initial_cents", "principal_current_cents", "monthly_payment_cents"]:
        assert cols.get(field) == "INTEGER", f"{field} should be INTEGER"


def test_recurring_and_split_cents_fields_are_integer(db_conn):
    for table in [
        "recurring_bill_templates",
        "mortgage_repayment_schedule",
        "debt_payment_splits",
        "mortgage_prepayment_events",
    ]:
        cols = _get_column_types(db_conn, table)
        cents_fields = [field for field in cols if field.endswith("_cents")]
        assert cents_fields
        for field in cents_fields:
            assert cols[field] == "INTEGER", f"{table}.{field} should be INTEGER"


# --- UNIQUE constraints ---

def test_normalized_raw_transaction_id_unique(db_conn):
    """Inserting two normalized rows with the same raw_transaction_id should fail."""
    db_conn.execute(
        "INSERT INTO raw_transactions (source_file, source_row_no, amount_cents, raw_hash) "
        "VALUES ('test.csv', 1, 10000, 'hash_a')"
    )
    db_conn.execute(
        "INSERT INTO normalized_transactions "
        "(raw_transaction_id, transaction_date, year, month, amount_cents, cashflow_direction, financial_type) "
        "VALUES (1, '2025-01-01', 2025, 1, 10000, 'inflow', 'stable_income')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            "INSERT INTO normalized_transactions "
            "(raw_transaction_id, transaction_date, year, month, amount_cents, cashflow_direction, financial_type) "
            "VALUES (1, '2025-02-01', 2025, 2, 10000, 'inflow', 'stable_income')"
        )


def test_monthly_cashflow_unique_year_month(db_conn):
    """Inserting two monthly_cashflow rows for the same year+month should fail."""
    db_conn.execute(
        "INSERT INTO monthly_cashflow (year, month) VALUES (2025, 1)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            "INSERT INTO monthly_cashflow (year, month) VALUES (2025, 1)"
        )


def test_raw_transactions_raw_hash_unique(db_conn):
    """Inserting two raw rows with the same raw_hash should fail."""
    db_conn.execute(
        "INSERT INTO raw_transactions (source_file, source_row_no, amount_cents, raw_hash) "
        "VALUES ('test.csv', 1, 10000, 'dup_hash')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            "INSERT INTO raw_transactions (source_file, source_row_no, amount_cents, raw_hash) "
            "VALUES ('test.csv', 2, 20000, 'dup_hash')"
        )


# --- CHECK constraints ---

def test_amount_cents_non_negative_check(db_conn):
    """Negative amount_cents in normalized_transactions should fail."""
    db_conn.execute(
        "INSERT INTO raw_transactions (source_file, source_row_no, amount_cents, raw_hash) "
        "VALUES ('test.csv', 1, 10000, 'hash_neg')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            "INSERT INTO normalized_transactions "
            "(raw_transaction_id, transaction_date, year, month, amount_cents, cashflow_direction, financial_type) "
            "VALUES (1, '2025-01-01', 2025, 1, -100, 'outflow', 'living_expense')"
        )


def test_cashflow_direction_check(db_conn):
    """Invalid cashflow_direction should fail."""
    db_conn.execute(
        "INSERT INTO raw_transactions (source_file, source_row_no, amount_cents, raw_hash) "
        "VALUES ('test.csv', 1, 10000, 'hash_dir')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            "INSERT INTO normalized_transactions "
            "(raw_transaction_id, transaction_date, year, month, amount_cents, cashflow_direction, financial_type) "
            "VALUES (1, '2025-01-01', 2025, 1, 10000, 'sideways', 'unknown')"
        )


# --- manual override fields ---

def test_normalized_has_manual_override_fields(db_conn):
    cols = _get_column_types(db_conn, "normalized_transactions")
    for field in ["manual_financial_type", "manual_note", "manual_updated_at"]:
        assert field in cols, f"Missing manual override field: {field}"


# --- classification_rules.condition_json is TEXT ---

def test_classification_rules_condition_json_is_text(db_conn):
    cols = _get_column_types(db_conn, "classification_rules")
    assert "condition_json" in cols
    assert cols["condition_json"] == "TEXT"


def test_beecount_category_mappings_has_semantic_fields(db_conn):
    cols = _get_column_types(db_conn, "beecount_category_mappings")
    for field in [
        "beecount_kind",
        "category_name",
        "parent_name",
        "radar_cashflow_direction",
        "radar_financial_type",
        "radar_category_l1",
        "radar_category_l2",
    ]:
        assert field in cols


# --- seed rules ---

REQUIRED_FINANCIAL_TYPES = [
    "historical_debt_asset_event",
    "internal_transfer",
    "credit_card_payment",
    "debt_payment",
    "debt_inflow",
    "investment_outflow",
    "investment_inflow",
    "asset_purchase",
    "asset_sale",
    "stable_income",
    "one_time_income",
    "refund",
    "reimbursable_expense",
    "reimbursement_income",
    "fixed_expense",
    "living_expense",
    "unknown",
]


def test_seed_rules_importable(db_conn):
    """seed_rules.sql should execute without error."""
    db_conn.executescript(SEED_RULES_SQL.read_text(encoding="utf-8"))
    count = db_conn.execute("SELECT count(*) FROM classification_rules").fetchone()[0]
    assert count >= 17, f"Expected >= 17 rules, got {count}"


def test_seed_rules_cover_required_types(db_conn_with_rules):
    """Seed rules must cover all required financial_type values."""
    rows = db_conn_with_rules.execute(
        "SELECT DISTINCT target_financial_type FROM classification_rules"
    ).fetchall()
    types_found = {row[0] for row in rows}
    for ft in REQUIRED_FINANCIAL_TYPES:
        assert ft in types_found, f"Missing financial_type in seed rules: {ft}"


def test_seed_rules_have_condition_json(db_conn_with_rules):
    """All seed rules must have non-empty condition_json (except fallback)."""
    rows = db_conn_with_rules.execute(
        "SELECT rule_name, condition_json FROM classification_rules"
    ).fetchall()
    for rule_name, condition_json in rows:
        assert condition_json is not None and condition_json.strip() != "", \
            f"Rule '{rule_name}' has empty condition_json"


def test_seed_rules_ordered_by_priority(db_conn_with_rules):
    """Rules should be ordered by priority ascending."""
    rows = db_conn_with_rules.execute(
        "SELECT priority FROM classification_rules ORDER BY priority"
    ).fetchall()
    priorities = [r[0] for r in rows]
    assert priorities == sorted(priorities), "Rules should be ordered by priority"
