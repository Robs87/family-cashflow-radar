"""Small compatibility migrations for existing local SQLite databases."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path


SCHEMA_SQL = Path(__file__).resolve().parents[1] / "db" / "schema.sql"

V02_TYPE_MARKER = "reimbursable_expense"
CHECK_TABLES = (
    "classification_rules",
    "normalized_transactions",
    "recurring_bill_templates",
)
MONTHLY_COLUMNS = {
    "reimbursable_expense_cents": "INTEGER DEFAULT 0 CHECK(reimbursable_expense_cents >= 0)",
    "reimbursement_income_cents": "INTEGER DEFAULT 0 CHECK(reimbursement_income_cents >= 0)",
}


def _table_sql(conn: sqlite3.Connection, table: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return str(row[0] if row else "")


def _create_table_sql(table: str) -> str:
    schema = SCHEMA_SQL.read_text(encoding="utf-8")
    pattern = rf"CREATE TABLE IF NOT EXISTS {re.escape(table)}\s*\(.*?\n\);"
    match = re.search(pattern, schema, flags=re.S)
    if not match:
        raise RuntimeError(f"schema.sql 中未找到建表语句: {table}")
    return match.group(0)


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(_table_sql(conn, table))


def _rebuild_table(conn: sqlite3.Connection, table: str) -> None:
    old_table = f"{table}__old_v02"
    conn.execute(f"DROP TABLE IF EXISTS {old_table}")
    conn.execute(f"ALTER TABLE {table} RENAME TO {old_table}")
    conn.execute(_create_table_sql(table))

    new_columns = set(_columns(conn, table))
    old_columns = _columns(conn, old_table)
    shared = [column for column in old_columns if column in new_columns]
    column_csv = ", ".join(shared)
    conn.execute(
        f"INSERT INTO {table} ({column_csv}) SELECT {column_csv} FROM {old_table}"
    )
    conn.execute(f"DROP TABLE {old_table}")


def _needs_v02_check_migration(conn: sqlite3.Connection, table: str) -> bool:
    sql = _table_sql(conn, table)
    return bool(sql) and V02_TYPE_MARKER not in sql


def _ensure_monthly_columns(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "monthly_cashflow"):
        return
    existing = set(_columns(conn, "monthly_cashflow"))
    for column, definition in MONTHLY_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE monthly_cashflow ADD COLUMN {column} {definition}")


def _insert_rule_if_missing(
    conn: sqlite3.Connection,
    rule_name: str,
    priority: int,
    condition_json: str,
    target_cashflow_direction: str,
    target_financial_type: str,
    category_l1: str,
    category_l2: str,
    confidence: float,
    description: str,
) -> None:
    exists = conn.execute(
        "SELECT 1 FROM classification_rules WHERE rule_name = ?",
        (rule_name,),
    ).fetchone()
    if exists:
        return
    conn.execute(
        """INSERT INTO classification_rules
           (rule_name, priority, condition_json, target_cashflow_direction,
            target_financial_type, target_category_l1, target_category_l2,
            confidence, description)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            rule_name,
            priority,
            condition_json,
            target_cashflow_direction,
            target_financial_type,
            category_l1,
            category_l2,
            confidence,
            description,
        ),
    )


def _ensure_v02_rules(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "classification_rules"):
        return
    conn.execute(
        """UPDATE classification_rules
           SET condition_json = '{"any_text_contains": ["借款", "借入", "借钱", "周转", "亲友借款", "贷款到账"], "direction_in": ["收入", "in"]}'
           WHERE rule_name = 'debt_inflow'"""
    )
    conn.execute(
        """UPDATE classification_rules
           SET condition_json = '{"any_text_contains": ["奖金", "年终奖", "补贴", "红包", "礼金", "临时收入"]}',
               description = '一次性收入（奖金、补贴、红包等）'
           WHERE rule_name = 'one_time_income'"""
    )
    _insert_rule_if_missing(
        conn,
        "reimbursable_expense",
        55,
        '{"any_text_contains": ["工作垫付", "公司垫付", "帮公司垫付", "代垫", "出差垫付", "垫付报销"], "direction_in": ["支出", "out"]}',
        "outflow",
        "reimbursable_expense",
        "垫付报销",
        "工作垫付",
        0.9,
        "工作垫付临时占用现金，不算家庭生活支出",
    )
    _insert_rule_if_missing(
        conn,
        "reimbursement_income",
        56,
        '{"any_text_contains": ["报销到账", "公司报销", "报销款", "报销入账", "垫付报销"], "direction_in": ["收入", "in"]}',
        "inflow",
        "reimbursement_income",
        "垫付报销",
        "报销回款",
        0.9,
        "工作垫付回款，不算稳定收入",
    )


def _ensure_indexes(conn: sqlite3.Connection) -> None:
    if _table_exists(conn, "normalized_transactions"):
        conn.execute("CREATE INDEX IF NOT EXISTS idx_norm_year_month ON normalized_transactions(year, month)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_norm_financial_type ON normalized_transactions(financial_type)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_norm_review_status ON normalized_transactions(review_status)")
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_norm_direction_type_month
               ON normalized_transactions(cashflow_direction, financial_type, year, month)"""
        )
    if _table_exists(conn, "classification_rules"):
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rules_priority ON classification_rules(priority)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rules_enabled_priority ON classification_rules(enabled, priority)")
    if _table_exists(conn, "recurring_bill_templates"):
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_recurring_templates_enabled
               ON recurring_bill_templates(enabled, bill_type)"""
        )


def ensure_v02_schema(conn: sqlite3.Connection) -> None:
    """Make databases created before v0.2 accept reimbursable records."""
    old_foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        for table in CHECK_TABLES:
            if _needs_v02_check_migration(conn, table):
                _rebuild_table(conn, table)

        _ensure_monthly_columns(conn)
        _ensure_v02_rules(conn)
        _ensure_indexes(conn)
    finally:
        conn.execute(f"PRAGMA foreign_keys={int(old_foreign_keys)}")
