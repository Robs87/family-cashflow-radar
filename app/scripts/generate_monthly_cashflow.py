#!/usr/bin/env python3
"""Generate monthly cashflow aggregates from normalized_transactions."""

import argparse
import sqlite3
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.scripts.schema_migrations import ensure_v02_schema


AGGREGATE_TYPES = {
    "stable_income": "stable_income_cents",
    "one_time_income": "one_time_income_cents",
    "fixed_expense": "fixed_expense_cents",
    "living_expense": "living_expense_cents",
    "debt_payment": "debt_payment_cents",
    "investment_outflow": "investment_outflow_cents",
    "investment_inflow": "investment_inflow_cents",
    "asset_purchase": "asset_purchase_cents",
    "asset_sale": "asset_sale_cents",
    "refund": "refund_cents",
    "reimbursable_expense": "reimbursable_expense_cents",
    "reimbursement_income": "reimbursement_income_cents",
    "internal_transfer": "internal_transfer_cents",
    "credit_card_payment": "credit_card_payment_cents",
    "debt_inflow": "debt_inflow_cents",
}


def _empty_month(year: int, month: int) -> dict[str, int | float | None]:
    row = {
        "year": year,
        "month": month,
        "stable_income_cents": 0,
        "one_time_income_cents": 0,
        "total_real_income_cents": 0,
        "fixed_expense_cents": 0,
        "living_expense_cents": 0,
        "debt_payment_cents": 0,
        "investment_outflow_cents": 0,
        "investment_inflow_cents": 0,
        "asset_purchase_cents": 0,
        "asset_sale_cents": 0,
        "refund_cents": 0,
        "reimbursable_expense_cents": 0,
        "reimbursement_income_cents": 0,
        "internal_transfer_cents": 0,
        "credit_card_payment_cents": 0,
        "debt_inflow_cents": 0,
        "net_operating_cashflow_cents": 0,
        "net_total_cashflow_cents": 0,
        "cashflow_health_score": None,
    }
    return row


def _compute_derived(row: dict[str, int | float | None]) -> None:
    row["total_real_income_cents"] = row["stable_income_cents"] + row["one_time_income_cents"]
    row["net_operating_cashflow_cents"] = (
        row["stable_income_cents"]
        - row["fixed_expense_cents"]
        - row["living_expense_cents"]
        - row["debt_payment_cents"]
    )
    row["net_total_cashflow_cents"] = (
        row["total_real_income_cents"]
        + row["investment_inflow_cents"]
        + row["asset_sale_cents"]
        + row["debt_inflow_cents"]
        + row["refund_cents"]
        + row["reimbursement_income_cents"]
        - row["fixed_expense_cents"]
        - row["living_expense_cents"]
        - row["debt_payment_cents"]
        - row["investment_outflow_cents"]
        - row["asset_purchase_cents"]
        - row["reimbursable_expense_cents"]
    )

    stable_income = row["stable_income_cents"]
    if stable_income:
        row["cashflow_health_score"] = round(row["net_operating_cashflow_cents"] / stable_income * 100, 2)
    else:
        row["cashflow_health_score"] = None


def _monthly_rows(conn: sqlite3.Connection) -> list[dict[str, int | float | None]]:
    rows = conn.execute(
        """SELECT n.year,
                  n.month,
                  COALESCE(n.manual_financial_type, n.financial_type) AS effective_financial_type,
                  COALESCE(n.manual_cashflow_direction, n.cashflow_direction) AS effective_cashflow_direction,
                  n.amount_cents
           FROM normalized_transactions n
           JOIN raw_transactions r ON r.id = n.raw_transaction_id
           WHERE COALESCE(n.manual_financial_type, n.financial_type) != 'historical_debt_asset_event'
             AND COALESCE(r.source_is_latest, 1) = 1
             AND COALESCE(r.source_deleted_at, '') = ''
           ORDER BY n.year, n.month, n.id"""
    ).fetchall()

    months: dict[tuple[int, int], dict[str, int | float | None]] = {}
    for year, month, financial_type, cashflow_direction, amount_cents in rows:
        key = (int(year), int(month))
        month_row = months.setdefault(key, _empty_month(*key))

        column = AGGREGATE_TYPES.get(financial_type)
        if column is None:
            continue

        if cashflow_direction == "neutral" and financial_type not in ("internal_transfer", "credit_card_payment"):
            continue

        month_row[column] += int(amount_cents)

    for month_row in months.values():
        _compute_derived(month_row)

    return [months[key] for key in sorted(months)]


def generate_monthly_cashflow(db_path: Path, dry_run: bool = False, verbose: bool = False) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    ensure_v02_schema(conn)
    cursor = conn.cursor()

    total_generated = 0
    total_failed = 0

    try:
        try:
            rows = _monthly_rows(conn)
        except sqlite3.Error as exc:
            print(f"错误: 读取 normalized_transactions 失败: {exc}", file=sys.stderr)
            total_failed += 1
            rows = []

        for row in rows:
            if verbose:
                print(
                    f"  {row['year']}-{row['month']:02d}: "
                    f"stable={row['stable_income_cents']} "
                    f"fixed={row['fixed_expense_cents']} "
                    f"debt={row['debt_payment_cents']} "
                    f"net_operating={row['net_operating_cashflow_cents']}"
                )

            if dry_run:
                total_generated += 1
                continue

            try:
                cursor.execute(
                    """INSERT INTO monthly_cashflow
                       (year, month,
                        stable_income_cents, one_time_income_cents, total_real_income_cents,
                        fixed_expense_cents, living_expense_cents, debt_payment_cents,
                        investment_outflow_cents, investment_inflow_cents,
                        asset_purchase_cents, asset_sale_cents, refund_cents,
                        reimbursable_expense_cents, reimbursement_income_cents,
                        internal_transfer_cents, credit_card_payment_cents, debt_inflow_cents,
                        net_operating_cashflow_cents, net_total_cashflow_cents,
                        cashflow_health_score, generated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                       ON CONFLICT(year, month) DO UPDATE SET
                        stable_income_cents = excluded.stable_income_cents,
                        one_time_income_cents = excluded.one_time_income_cents,
                        total_real_income_cents = excluded.total_real_income_cents,
                        fixed_expense_cents = excluded.fixed_expense_cents,
                        living_expense_cents = excluded.living_expense_cents,
                        debt_payment_cents = excluded.debt_payment_cents,
                        investment_outflow_cents = excluded.investment_outflow_cents,
                        investment_inflow_cents = excluded.investment_inflow_cents,
                        asset_purchase_cents = excluded.asset_purchase_cents,
                        asset_sale_cents = excluded.asset_sale_cents,
                        refund_cents = excluded.refund_cents,
                        reimbursable_expense_cents = excluded.reimbursable_expense_cents,
                        reimbursement_income_cents = excluded.reimbursement_income_cents,
                        internal_transfer_cents = excluded.internal_transfer_cents,
                        credit_card_payment_cents = excluded.credit_card_payment_cents,
                        debt_inflow_cents = excluded.debt_inflow_cents,
                        net_operating_cashflow_cents = excluded.net_operating_cashflow_cents,
                        net_total_cashflow_cents = excluded.net_total_cashflow_cents,
                        cashflow_health_score = excluded.cashflow_health_score,
                        generated_at = CURRENT_TIMESTAMP""",
                    (
                        row["year"],
                        row["month"],
                        row["stable_income_cents"],
                        row["one_time_income_cents"],
                        row["total_real_income_cents"],
                        row["fixed_expense_cents"],
                        row["living_expense_cents"],
                        row["debt_payment_cents"],
                        row["investment_outflow_cents"],
                        row["investment_inflow_cents"],
                        row["asset_purchase_cents"],
                        row["asset_sale_cents"],
                        row["refund_cents"],
                        row["reimbursable_expense_cents"],
                        row["reimbursement_income_cents"],
                        row["internal_transfer_cents"],
                        row["credit_card_payment_cents"],
                        row["debt_inflow_cents"],
                        row["net_operating_cashflow_cents"],
                        row["net_total_cashflow_cents"],
                        row["cashflow_health_score"],
                    ),
                )
                total_generated += 1
            except sqlite3.Error as exc:
                print(f"错误: 生成 {row['year']}-{row['month']:02d} 月度现金流失败: {exc}", file=sys.stderr)
                total_failed += 1

        if not dry_run:
            conn.commit()
    finally:
        conn.close()

    print(f"monthly_generated={total_generated} failed={total_failed}")

    if total_failed > 0:
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="生成 monthly_cashflow 月度现金流")
    parser.add_argument("--db", required=True, help="SQLite 数据库路径")
    parser.add_argument("--dry-run", action="store_true", help="仅预览，不写入数据库")
    parser.add_argument("--verbose", action="store_true", help="输出详细日志")
    args = parser.parse_args()
    generate_monthly_cashflow(Path(args.db), dry_run=args.dry_run, verbose=args.verbose)


if __name__ == "__main__":
    main()
