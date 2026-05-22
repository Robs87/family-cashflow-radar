#!/usr/bin/env python3
"""Normalize raw_transactions into normalized_transactions.

Reads from raw_transactions, writes to normalized_transactions.
Idempotent: uses INSERT OR IGNORE on raw_transaction_id UNIQUE.
"""

import argparse
import sqlite3
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.scripts.schema_migrations import ensure_v02_schema

LARGE_AMOUNT_THRESHOLD = 1_000_000  # 10,000 yuan in cents

# Keywords that indicate neutral (internal transfer / credit card payment)
NEUTRAL_KEYWORDS = {
    "internal_transfer": [
        "内部转账", "账户互转", "账户转账", "余额宝转入", "余额宝转出",
    ],
    "credit_card_payment": [
        "信用卡还款",
    ],
}


def _parse_direction(direction_raw: str, category: str, subcategory: str, merchant: str, note: str) -> tuple[str, str]:
    """Return (cashflow_direction, financial_type).

    Heuristic only — classify.py will refine later.
    """
    combined = f"{category} {subcategory} {merchant} {note}"

    # Check neutral patterns first
    for ft, keywords in NEUTRAL_KEYWORDS.items():
        for kw in keywords:
            if kw in combined or kw in direction_raw:
                return "neutral", ft

    if direction_raw == "收入":
        return "inflow", "unknown"
    if direction_raw == "支出":
        return "outflow", "unknown"

    return "outflow", "unknown"


def _extract_year_month(date_str: str) -> tuple[int, int]:
    """Extract year and month from transaction_date (YYYY-MM-DD...)."""
    parts = date_str.split("-")
    return int(parts[0]), int(parts[1])


def normalize(db_path: Path, dry_run: bool = False, verbose: bool = False) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    ensure_v02_schema(conn)
    cursor = conn.cursor()

    raw_rows = cursor.execute(
        """SELECT id, transaction_date, amount_cents, direction_raw,
                  category_original, subcategory_original, merchant, note, account
           FROM raw_transactions
           WHERE COALESCE(source_is_latest, 1) = 1
             AND COALESCE(source_deleted_at, '') = ''
           ORDER BY id"""
    ).fetchall()

    total_normalized = 0
    total_skipped = 0
    total_failed = 0

    for row in raw_rows:
        (raw_id, txn_date, amount_cents, direction_raw,
         category, subcategory, merchant, note, account) = row

        try:
            year, month = _extract_year_month(txn_date)
        except (ValueError, IndexError) as e:
            print(f"错误: raw_transactions.id={raw_id}: 日期解析失败: {e}", file=sys.stderr)
            total_failed += 1
            continue

        if amount_cents < 0:
            print(f"错误: raw_transactions.id={raw_id}: amount_cents 为负: {amount_cents}", file=sys.stderr)
            total_failed += 1
            continue

        direction, financial_type = _parse_direction(
            direction_raw or "", category or "", subcategory or "", merchant or "", note or ""
        )

        is_large = 1 if amount_cents >= LARGE_AMOUNT_THRESHOLD else 0

        if verbose:
            print(f"  id={raw_id} {txn_date} {amount_cents} {direction} {financial_type} large={is_large}")

        if dry_run:
            total_normalized += 1
            continue

        result = cursor.execute(
            """INSERT OR IGNORE INTO normalized_transactions
               (raw_transaction_id, transaction_date, year, month,
                amount_cents, cashflow_direction, financial_type,
                category_l1, category_l2, account, counterparty, description,
                is_large_amount, is_internal_transfer, is_debt_related,
                is_asset_related, is_investment_related, confidence, review_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                raw_id, txn_date, year, month,
                amount_cents, direction, financial_type,
                category, subcategory, account, merchant, note,
                is_large,
                1 if financial_type == "internal_transfer" else 0,
                0, 0, 0,
                0.5, "pending",
            ),
        )
        if result.rowcount > 0:
            total_normalized += 1
            if verbose:
                print(f"    -> inserted")
        else:
            total_skipped += 1
            if verbose:
                print(f"    -> skipped (already exists)")

    if not dry_run:
        conn.commit()
    conn.close()

    print(f"normalized={total_normalized} skipped_existing={total_skipped} failed={total_failed}")

    if total_failed > 0:
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="标准化 raw_transactions → normalized_transactions")
    parser.add_argument("--db", required=True, help="SQLite 数据库路径")
    parser.add_argument("--dry-run", action="store_true", help="仅预览，不写入数据库")
    parser.add_argument("--verbose", action="store_true", help="输出详细日志")
    args = parser.parse_args()
    normalize(Path(args.db), dry_run=args.dry_run, verbose=args.verbose)


if __name__ == "__main__":
    main()
