#!/usr/bin/env python3
"""Rule-based classifier for normalized_transactions.

Reads enabled classification_rules ordered by priority and updates each
normalized transaction with the first matching rule. Manual overrides win.
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.scripts.schema_migrations import ensure_v02_schema
from app.scripts.beecount_category_mappings import (
    apply_beecount_mapping,
    load_mappings,
    sync_mappings_from_raw_transactions,
)


TEXT_FIELDS = ("category_l1", "category_l2", "account", "counterparty", "description")
SUPPORTED_CONDITION_KEYS = {
    "year_in",
    "any_text_contains",
    "direction_in",
    "account_contains",
    "amount_cents_min",
    "amount_cents_max",
}
DIRECTION_ALIASES = {
    "inflow": {"inflow", "in", "收入"},
    "outflow": {"outflow", "out", "支出"},
    "neutral": {"neutral", "转账", "内部转账"},
}


def _load_rules(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT *
           FROM classification_rules
           WHERE enabled = 1
           ORDER BY priority ASC, id ASC"""
    ).fetchall()
    return [dict(row) for row in rows]


def _row_text(row: dict[str, Any]) -> str:
    return " ".join(str(row.get(field) or "") for field in TEXT_FIELDS)


def _direction_matches(row_direction: str, expected_values: list[Any]) -> bool:
    normalized = {str(value).strip().lower() for value in expected_values}
    aliases = DIRECTION_ALIASES.get(row_direction, {row_direction})
    return bool(normalized & aliases)


def _as_list(condition: dict[str, Any], key: str) -> list[Any]:
    value = condition[key]
    if not isinstance(value, list):
        raise ValueError(f"condition_json.{key} 必须是数组")
    return value


def _target_direction_compatible(row: dict[str, Any], rule: dict[str, Any], condition: dict[str, Any]) -> bool:
    """Avoid broad keyword rules crossing income/outflow boundaries.

    Historical and neutral rules intentionally override direction. For normal
    inflow/outflow rules, the normalized first-pass direction must agree unless
    the rule has its own direction_in condition.
    """
    target = rule["target_cashflow_direction"]
    current = row["cashflow_direction"]

    if "direction_in" in condition:
        return True
    if "year_in" in condition and rule["target_financial_type"] == "historical_debt_asset_event":
        return True
    if target == "neutral":
        return True
    return target == current


def _condition_matches(row: dict[str, Any], rule: dict[str, Any]) -> bool:
    try:
        condition = json.loads(rule["condition_json"] or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"规则 {rule['rule_name']} condition_json 非法: {exc}") from exc

    if not isinstance(condition, dict):
        raise ValueError(f"规则 {rule['rule_name']} condition_json 必须是对象")

    unsupported = set(condition) - SUPPORTED_CONDITION_KEYS
    if unsupported:
        keys = ", ".join(sorted(unsupported))
        raise ValueError(f"规则 {rule['rule_name']} 包含不支持的 condition_json 操作符: {keys}")

    if rule.get("id") is not None and row.get("classification_rule_id") == rule["id"]:
        return True

    if not _target_direction_compatible(row, rule, condition):
        return False

    if "year_in" in condition:
        years = {int(year) for year in _as_list(condition, "year_in")}
        if int(row["year"]) not in years:
            return False

    if "direction_in" in condition:
        if not _direction_matches(row["cashflow_direction"], _as_list(condition, "direction_in")):
            return False

    if "any_text_contains" in condition:
        text = _row_text(row)
        keywords = [str(keyword) for keyword in _as_list(condition, "any_text_contains")]
        if not any(keyword and keyword in text for keyword in keywords):
            return False

    if "account_contains" in condition:
        account = str(row.get("account") or "")
        keywords = [str(keyword) for keyword in _as_list(condition, "account_contains")]
        if not any(keyword and keyword in account for keyword in keywords):
            return False

    if "amount_cents_min" in condition:
        if int(row["amount_cents"]) < int(condition["amount_cents_min"]):
            return False

    if "amount_cents_max" in condition:
        if int(row["amount_cents"]) > int(condition["amount_cents_max"]):
            return False

    return True


def _review_status(row: dict[str, Any], rule: dict[str, Any]) -> str:
    if rule["target_financial_type"] == "unknown":
        return "pending"
    if str(row.get("source_file") or "").startswith("beecount_cloud:"):
        return "approved"
    if float(rule["confidence"]) < 0.8:
        return "pending"
    if int(row["is_large_amount"]) == 1:
        return "pending"
    return "approved"


def _classify_row(row: dict[str, Any], rules: list[dict[str, Any]]) -> dict[str, Any]:
    for rule in rules:
        if _condition_matches(row, rule):
            return rule
    raise ValueError("没有可用兜底规则")


def classify(db_path: Path, dry_run: bool = False, verbose: bool = False) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    ensure_v02_schema(conn)
    cursor = conn.cursor()

    total_classified = 0
    total_unknown = 0
    total_manual_skipped = 0
    total_failed = 0

    try:
        rules = _load_rules(conn)
        sync_mappings_from_raw_transactions(conn)
        mappings = load_mappings(conn)
        rows = cursor.execute(
            """SELECT n.*, r.source_file
                      , r.direction_raw AS raw_direction
                      , r.category_original AS beecount_category
               FROM normalized_transactions n
               JOIN raw_transactions r ON r.id = n.raw_transaction_id
               ORDER BY n.id ASC"""
        ).fetchall()

        for sqlite_row in rows:
            row = dict(sqlite_row)
            if row.get("manual_financial_type"):
                total_manual_skipped += 1
                if verbose:
                    print(f"  id={row['id']}: manual override, skipped")
                continue

            rule = apply_beecount_mapping(row, mappings)
            if not rule:
                try:
                    rule = _classify_row(row, rules)
                except ValueError as exc:
                    print(f"错误: normalized_transactions.id={row['id']}: {exc}", file=sys.stderr)
                    total_failed += 1
                    continue

            status = _review_status(row, rule)
            if rule["target_financial_type"] == "unknown":
                total_unknown += 1

            if verbose:
                print(
                    f"  id={row['id']}: {rule['rule_name']} -> "
                    f"{rule['target_financial_type']} {rule['target_cashflow_direction']} {status}"
                )

            if dry_run:
                total_classified += 1
                continue

            cursor.execute(
                """UPDATE normalized_transactions
                   SET cashflow_direction = ?,
                       financial_type = ?,
                       category_l1 = COALESCE(?, category_l1),
                       category_l2 = COALESCE(?, category_l2),
                       is_internal_transfer = ?,
                       is_debt_related = ?,
                       is_asset_related = ?,
                       is_investment_related = ?,
                       classification_rule_id = ?,
                       confidence = ?,
                       review_status = ?,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (
                    rule["target_cashflow_direction"],
                    rule["target_financial_type"],
                    rule["target_category_l1"],
                    rule["target_category_l2"],
                    rule["is_internal_transfer"],
                    rule["is_debt_related"],
                    rule["is_asset_related"],
                    rule["is_investment_related"],
                    rule["id"],
                    rule["confidence"],
                    status,
                    row["id"],
                ),
            )
            total_classified += 1

        if not dry_run:
            conn.commit()
    finally:
        conn.close()

    print(f"classified={total_classified} unknown={total_unknown} manual_skipped={total_manual_skipped} failed={total_failed}")

    if total_failed > 0:
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="规则分类 normalized_transactions")
    parser.add_argument("--db", required=True, help="SQLite 数据库路径")
    parser.add_argument("--dry-run", action="store_true", help="仅预览，不写入数据库")
    parser.add_argument("--verbose", action="store_true", help="输出详细日志")
    args = parser.parse_args()
    classify(Path(args.db), dry_run=args.dry_run, verbose=args.verbose)


if __name__ == "__main__":
    main()
