#!/usr/bin/env python3
"""Store current available cash calibrations for cashflow advice and simulation."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.scripts.schema_migrations import ensure_v02_schema


DEFAULT_SCOPE = "家庭现金账户、活期、货币基金等可快速动用资金"


@dataclass(frozen=True)
class CashBalanceCalibration:
    id: int
    calibration_date: str
    available_cash_cents: int
    scope: str
    note: str


def parse_yuan_to_cents(value: str) -> int:
    try:
        amount = Decimal(value.strip().replace(",", ""))
    except (InvalidOperation, AttributeError):
        raise ValueError("现金余额格式无效") from None
    if amount < 0:
        raise ValueError("现金余额不能为负数")
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def validate_date(value: str) -> str:
    try:
        parsed = date.fromisoformat(value.strip())
    except (AttributeError, ValueError):
        raise ValueError("校准日期格式应为 YYYY-MM-DD") from None
    return parsed.isoformat()


def save_cash_balance_calibration(
    db_path: Path,
    available_cash_cents: int,
    calibration_date: str,
    *,
    scope: str = DEFAULT_SCOPE,
    note: str = "",
) -> int:
    if available_cash_cents < 0:
        raise ValueError("现金余额不能为负数")
    calibration_date = validate_date(calibration_date)
    scope = scope.strip() or DEFAULT_SCOPE

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        ensure_v02_schema(conn)
        cursor = conn.execute(
            """INSERT INTO cash_balance_calibrations
               (calibration_date, available_cash_cents, scope, note)
               VALUES (?, ?, ?, ?)""",
            (calibration_date, available_cash_cents, scope, note.strip()),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def latest_cash_balance(conn: sqlite3.Connection) -> CashBalanceCalibration | None:
    row = conn.execute(
        """SELECT id, calibration_date, available_cash_cents, scope, note
           FROM cash_balance_calibrations
           ORDER BY calibration_date DESC, id DESC
           LIMIT 1"""
    ).fetchone()
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        scope = row["scope"] or DEFAULT_SCOPE
        note = row["note"] or ""
        return CashBalanceCalibration(
            id=int(row["id"]),
            calibration_date=str(row["calibration_date"]),
            available_cash_cents=int(row["available_cash_cents"]),
            scope=str(scope),
            note=str(note),
        )
    return CashBalanceCalibration(
        id=int(row[0]),
        calibration_date=str(row[1]),
        available_cash_cents=int(row[2]),
        scope=str(row[3] or DEFAULT_SCOPE),
        note=str(row[4] or ""),
    )


def safety_months(available_cash_cents: int, monthly_required_outflow_cents: int) -> float:
    if monthly_required_outflow_cents <= 0:
        return 99.0 if available_cash_cents > 0 else 0.0
    return round(available_cash_cents / monthly_required_outflow_cents, 1)


def main() -> None:
    parser = argparse.ArgumentParser(description="保存当前可用现金余额校准")
    parser.add_argument("--db", type=Path, default=Path("data/processed/cashflow.db"))
    parser.add_argument("--amount", required=True, help="当前可用现金余额，单位元")
    parser.add_argument("--date", default=date.today().isoformat(), help="校准日期 YYYY-MM-DD")
    parser.add_argument("--scope", default=DEFAULT_SCOPE)
    parser.add_argument("--note", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    try:
        amount_cents = parse_yuan_to_cents(args.amount)
        calibration_date = validate_date(args.date)
        if args.dry_run:
            print("saved=0 dry_run=1 failed=0")
            return
        calibration_id = save_cash_balance_calibration(
            args.db,
            amount_cents,
            calibration_date,
            scope=args.scope,
            note=args.note,
        )
        if args.verbose:
            print(f"calibration_id={calibration_id} amount_cents={amount_cents} date={calibration_date}")
        print(f"saved=1 calibration_id={calibration_id} failed=0")
    except (sqlite3.Error, ValueError) as exc:
        print(f"failed=1 error={exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
