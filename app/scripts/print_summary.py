#!/usr/bin/env python3
"""Print a concise cashflow summary from monthly_cashflow."""

import argparse
import sqlite3
import sys
from pathlib import Path


def _format_yuan(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents_abs = abs(int(cents))
    yuan = cents_abs // 100
    fen = cents_abs % 100
    return f"{sign}{yuan:,}.{fen:02d} 元"


def _fetch_months(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT year, month,
                  stable_income_cents,
                  fixed_expense_cents,
                  debt_payment_cents,
                  net_operating_cashflow_cents,
                  net_total_cashflow_cents
           FROM monthly_cashflow
           ORDER BY year DESC, month DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()


def _fetch_review_counts(conn: sqlite3.Connection) -> dict[str, int]:
    row = conn.execute(
        """SELECT
              SUM(CASE WHEN COALESCE(manual_financial_type, financial_type) = 'unknown' THEN 1 ELSE 0 END) AS unknown_count,
              SUM(CASE WHEN review_status = 'pending' THEN 1 ELSE 0 END) AS pending_count
           FROM normalized_transactions"""
    ).fetchone()
    return {
        "unknown_count": int(row["unknown_count"] or 0),
        "pending_count": int(row["pending_count"] or 0),
    }


def _print_month(row: sqlite3.Row) -> None:
    label = f"{row['year']}-{row['month']:02d}"
    print(f"{label}")
    print(f"  稳定收入: {_format_yuan(row['stable_income_cents'])}")
    print(f"  固定支出: {_format_yuan(row['fixed_expense_cents'])}")
    print(f"  债务还款: {_format_yuan(row['debt_payment_cents'])}")
    print(f"  基础经营现金流: {_format_yuan(row['net_operating_cashflow_cents'])}")
    print(f"  总现金流: {_format_yuan(row['net_total_cashflow_cents'])}")


def print_summary(db_path: Path, months: int = 12, dry_run: bool = False, verbose: bool = False) -> None:
    if months <= 0:
        print("错误: --months 必须大于 0", file=sys.stderr)
        sys.exit(1)

    if dry_run and verbose:
        print("dry-run: summary is read-only; no database writes will be performed")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    try:
        try:
            month_rows = _fetch_months(conn, months)
            review_counts = _fetch_review_counts(conn)
        except sqlite3.Error as exc:
            print(f"错误: 读取摘要数据失败: {exc}", file=sys.stderr)
            sys.exit(1)
    finally:
        conn.close()

    print("家庭现金流摘要")
    print(f"unknown 待审核: {review_counts['unknown_count']}")
    print(f"pending 待审核: {review_counts['pending_count']}")

    if not month_rows:
        print("暂无月度现金流数据")
        return

    print("")
    for idx, row in enumerate(reversed(month_rows)):
        if idx > 0:
            print("")
        _print_month(row)


def main():
    parser = argparse.ArgumentParser(description="打印家庭现金流 CLI 摘要")
    parser.add_argument("--db", required=True, help="SQLite 数据库路径")
    parser.add_argument("--months", type=int, default=12, help="展示最近 N 个月，默认 12")
    parser.add_argument("--dry-run", action="store_true", help="只读预览；本命令不会写入数据库")
    parser.add_argument("--verbose", action="store_true", help="输出详细日志")
    args = parser.parse_args()
    print_summary(Path(args.db), months=args.months, dry_run=args.dry_run, verbose=args.verbose)


if __name__ == "__main__":
    main()
