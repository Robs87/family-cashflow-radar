"""Small compatibility migrations for existing local SQLite databases."""

from __future__ import annotations

import re
import json
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
RAW_BEECOUNT_COLUMNS = {
    "source_system": "TEXT",
    "source_ledger_id": "TEXT",
    "source_transaction_id": "TEXT",
    "source_updated_at": "TEXT",
    "source_deleted_at": "TEXT",
    "source_is_latest": "INTEGER NOT NULL DEFAULT 1 CHECK(source_is_latest IN (0, 1))",
}


def _ensure_beecount_mapping_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS beecount_category_mappings (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               beecount_kind TEXT NOT NULL CHECK(beecount_kind IN ('expense', 'income', 'transfer')),
               category_name TEXT NOT NULL,
               parent_name TEXT DEFAULT '',
               level INTEGER DEFAULT 1,
               radar_cashflow_direction TEXT NOT NULL CHECK(radar_cashflow_direction IN ('inflow', 'outflow', 'neutral')),
               radar_financial_type TEXT NOT NULL CHECK(radar_financial_type IN (
                   'stable_income', 'one_time_income', 'living_expense', 'fixed_expense',
                   'debt_payment', 'debt_inflow', 'asset_purchase', 'asset_sale',
                   'investment_outflow', 'investment_inflow', 'internal_transfer',
                   'credit_card_payment', 'refund', 'reimbursable_expense',
                   'reimbursement_income', 'historical_debt_asset_event', 'unknown'
               )),
               radar_category_l1 TEXT DEFAULT '',
               radar_category_l2 TEXT DEFAULT '',
               confidence REAL DEFAULT 1.0,
               enabled INTEGER DEFAULT 1,
               mapping_source TEXT DEFAULT 'inferred',
               notes TEXT DEFAULT '',
               created_at TEXT DEFAULT CURRENT_TIMESTAMP,
               updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
               UNIQUE(beecount_kind, category_name, parent_name)
           )"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_beecount_category_mappings_lookup
           ON beecount_category_mappings(beecount_kind, category_name, enabled)"""
    )


def _ensure_cash_balance_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS cash_balance_calibrations (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               calibration_date TEXT NOT NULL,
               available_cash_cents INTEGER NOT NULL CHECK(available_cash_cents >= 0),
               scope TEXT DEFAULT '家庭现金账户、活期、货币基金等可快速动用资金',
               note TEXT DEFAULT '',
               created_at TEXT DEFAULT CURRENT_TIMESTAMP
           )"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_cash_balance_latest
           ON cash_balance_calibrations(calibration_date DESC, id DESC)"""
    )


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


def _ensure_raw_beecount_columns(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "raw_transactions"):
        return
    existing = set(_columns(conn, "raw_transactions"))
    for column, definition in RAW_BEECOUNT_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE raw_transactions ADD COLUMN {column} {definition}")
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_raw_source_latest
           ON raw_transactions(source_system, source_ledger_id, source_transaction_id, source_is_latest)"""
    )
    rows = conn.execute(
        """SELECT id, source_file, raw_hash, raw_payload
           FROM raw_transactions
           WHERE source_file LIKE 'beecount_cloud:%'
             AND COALESCE(source_system, '') = ''"""
    ).fetchall()
    for row_id, source_file, raw_hash, raw_payload in rows:
        ledger_id = str(source_file or "").split(":", 1)[1] if ":" in str(source_file or "") else ""
        source_transaction_id = ""
        source_updated_at = ""
        source_deleted_at = ""
        try:
            payload = json.loads(raw_payload or "{}")
            source_transaction_id = str(payload.get("source_transaction_id") or "")
            source_updated_at = str(payload.get("source_updated_at") or "")
            source_deleted_at = str(payload.get("source_deleted_at") or "")
            tx = payload.get("transaction") if isinstance(payload.get("transaction"), dict) else {}
            source_transaction_id = source_transaction_id or str(
                tx.get("sync_id") or tx.get("syncId") or tx.get("id") or tx.get("transaction_id") or ""
            )
            source_updated_at = source_updated_at or str(tx.get("updated_at") or tx.get("updatedAt") or "")
            source_deleted_at = source_deleted_at or str(tx.get("deleted_at") or tx.get("deletedAt") or "")
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
        if not source_transaction_id:
            parts = str(raw_hash or "").split(":")
            if len(parts) >= 3:
                source_transaction_id = parts[2]
        conn.execute(
            """UPDATE raw_transactions
               SET source_system = 'beecount_cloud',
                   source_ledger_id = ?,
                   source_transaction_id = ?,
                   source_updated_at = ?,
                   source_deleted_at = ?,
                   source_is_latest = 1
               WHERE id = ?""",
            (ledger_id, source_transaction_id, source_updated_at, source_deleted_at, row_id),
        )


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
           SET condition_json = '{"any_text_contains": ["转账", "账户转账", "账户互转", "余额宝转入", "余额宝转出", "微信零钱", "支付宝余额", "银行卡转入", "银行卡转出", "提现", "充值"]}'
           WHERE rule_name = 'internal_transfer'
             AND condition_json NOT LIKE '%账户互转%'"""
    )
    conn.execute(
        """UPDATE classification_rules
           SET condition_json = '{"any_text_contains": ["信用卡还款", "还信用卡", "信用卡自动还款", "账单还款", "购汇还款"]}'
           WHERE rule_name = 'credit_card_payment'
             AND condition_json NOT LIKE '%购汇还款%'"""
    )
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
    conn.execute(
        """UPDATE classification_rules
           SET condition_json = '{"any_text_contains": ["餐饮", "早餐", "午餐", "晚餐", "夜宵", "外卖", "超市", "购物", "交通", "打车", "地铁", "加油", "停车", "娱乐", "电影", "旅游", "医疗", "药品", "理发", "快递"]}'
           WHERE rule_name = 'living_expense'
             AND condition_json NOT LIKE '%早餐%'"""
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
        '{"any_text_contains": ["报销", "其他报销", "报销到账", "公司报销", "报销款", "报销入账", "垫付报销"], "direction_in": ["收入", "in"]}',
        "inflow",
        "reimbursement_income",
        "垫付报销",
        "报销回款",
        0.9,
        "工作垫付回款，不算稳定收入",
    )
    conn.execute(
        """UPDATE classification_rules
           SET condition_json = '{"any_text_contains": ["报销", "其他报销", "报销到账", "公司报销", "报销款", "报销入账", "垫付报销"], "direction_in": ["收入", "in"]}'
           WHERE rule_name = 'reimbursement_income'
             AND condition_json NOT LIKE '%其他报销%'"""
    )
    conn.execute(
        """UPDATE classification_rules
           SET condition_json = '{"any_text_contains": ["房租", "物业费", "水电费", "燃气费", "暖气费", "保险费", "社保", "公积金", "话费", "宽带", "学费", "培训费", "幼儿园"]}'
           WHERE rule_name = 'fixed_expense'
             AND condition_json NOT LIKE '%培训费%'"""
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
        _ensure_raw_beecount_columns(conn)
        _ensure_beecount_mapping_table(conn)
        _ensure_cash_balance_table(conn)
        _ensure_v02_rules(conn)
        _ensure_indexes(conn)
    finally:
        conn.execute(f"PRAGMA foreign_keys={int(old_foreign_keys)}")
