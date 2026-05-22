#!/usr/bin/env python3
"""Manage future planned cashflow events and match them to BeeCount actuals."""

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


DIRECTIONS = {"inflow", "outflow"}
FINANCIAL_TYPES = {
    "stable_income",
    "one_time_income",
    "living_expense",
    "fixed_expense",
    "debt_payment",
    "debt_inflow",
    "asset_purchase",
    "asset_sale",
    "investment_outflow",
    "investment_inflow",
    "refund",
    "reimbursable_expense",
    "reimbursement_income",
    "unknown",
}


@dataclass(frozen=True)
class MatchSummary:
    matched: int = 0
    scanned: int = 0

    def __str__(self) -> str:
        return f"matched={self.matched} scanned={self.scanned}"


def parse_yuan_to_cents(value: str) -> int:
    try:
        amount = Decimal(value.strip().replace(",", ""))
    except (InvalidOperation, AttributeError):
        raise ValueError("金额格式无效") from None
    if amount <= 0:
        raise ValueError("金额必须大于 0")
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def validate_date(value: str) -> str:
    try:
        parsed = date.fromisoformat(value.strip())
    except (AttributeError, ValueError):
        raise ValueError("事件日期格式应为 YYYY-MM-DD") from None
    return parsed.isoformat()


def create_planned_event(
    db_path: Path,
    event_name: str,
    event_date: str,
    amount_cents: int,
    cashflow_direction: str,
    financial_type: str,
    *,
    category_l1: str = "",
    category_l2: str = "",
    note: str = "",
) -> int:
    if not event_name.strip():
        raise ValueError("请填写计划事件名称")
    event_date = validate_date(event_date)
    if amount_cents <= 0:
        raise ValueError("金额必须大于 0")
    if cashflow_direction not in DIRECTIONS:
        raise ValueError("计划事件方向必须是 inflow 或 outflow")
    if financial_type not in FINANCIAL_TYPES:
        raise ValueError("不支持的计划事件类型")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        ensure_v02_schema(conn)
        cursor = conn.execute(
            """INSERT INTO planned_cashflow_events
               (event_name, event_date, amount_cents, cashflow_direction,
                financial_type, category_l1, category_l2, note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_name.strip(),
                event_date,
                amount_cents,
                cashflow_direction,
                financial_type,
                category_l1.strip(),
                category_l2.strip(),
                note.strip(),
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def set_planned_event_enabled(db_path: Path, event_id: int, enabled: bool) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        ensure_v02_schema(conn)
        conn.execute(
            """UPDATE planned_cashflow_events
               SET enabled = ?, updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (1 if enabled else 0, event_id),
        )
        conn.commit()
    finally:
        conn.close()


def _category_matches(event: sqlite3.Row, actual: sqlite3.Row) -> bool:
    event_l2 = str(event["category_l2"] or "").strip()
    event_l1 = str(event["category_l1"] or "").strip()
    actual_l2 = str(actual["category_l2"] or "").strip()
    actual_l1 = str(actual["category_l1"] or "").strip()
    if event_l2:
        return event_l2 in {actual_l2, actual_l1}
    if event_l1:
        return event_l1 in {actual_l1, actual_l2}
    return True


def _event_candidates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT *
           FROM planned_cashflow_events
           WHERE enabled = 1 AND match_status = 'unmatched'
           ORDER BY event_date, id"""
    ).fetchall()


def _find_match(conn: sqlite3.Connection, event: sqlite3.Row) -> sqlite3.Row | None:
    rows = conn.execute(
        """SELECT n.id AS normalized_id,
                  r.id AS raw_id,
                  n.transaction_date,
                  n.amount_cents,
                  COALESCE(n.manual_cashflow_direction, n.cashflow_direction) AS cashflow_direction,
                  COALESCE(n.manual_financial_type, n.financial_type) AS financial_type,
                  COALESCE(NULLIF(n.manual_category_l1, ''), n.category_l1) AS category_l1,
                  COALESCE(NULLIF(n.manual_category_l2, ''), n.category_l2) AS category_l2
           FROM normalized_transactions n
           JOIN raw_transactions r ON r.id = n.raw_transaction_id
           WHERE r.source_system = 'beecount_cloud'
             AND COALESCE(r.source_is_latest, 1) = 1
             AND COALESCE(r.source_deleted_at, '') = ''
             AND n.amount_cents = ?
             AND COALESCE(n.manual_cashflow_direction, n.cashflow_direction) = ?
             AND COALESCE(n.manual_financial_type, n.financial_type) = ?
             AND ABS(julianday(n.transaction_date) - julianday(?)) <= 3
             AND NOT EXISTS (
               SELECT 1
               FROM planned_cashflow_events p
               WHERE p.matched_normalized_transaction_id = n.id
             )
           ORDER BY ABS(julianday(n.transaction_date) - julianday(?)), n.id
           LIMIT 8""",
        (
            int(event["amount_cents"]),
            event["cashflow_direction"],
            event["financial_type"],
            event["event_date"],
            event["event_date"],
        ),
    ).fetchall()
    for row in rows:
        if _category_matches(event, row):
            return row
    return None


def match_planned_events(db_path: Path) -> MatchSummary:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    matched = 0
    try:
        ensure_v02_schema(conn)
        events = _event_candidates(conn)
        for event in events:
            actual = _find_match(conn, event)
            if actual is None:
                continue
            conn.execute(
                """UPDATE planned_cashflow_events
                   SET match_status = 'matched',
                       matched_normalized_transaction_id = ?,
                       matched_raw_transaction_id = ?,
                       match_confidence = 0.95,
                       matched_at = CURRENT_TIMESTAMP,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (actual["normalized_id"], actual["raw_id"], event["id"]),
            )
            matched += 1
        conn.commit()
        return MatchSummary(matched=matched, scanned=len(events))
    finally:
        conn.close()


def forecast_events_by_month(conn: sqlite3.Connection, start_month: str, horizon_months: int) -> dict[str, int]:
    start = date.fromisoformat(f"{start_month}-01")
    end_month_index = start.year * 12 + start.month - 1 + horizon_months
    end = date(end_month_index // 12, end_month_index % 12 + 1, 1)
    rows = conn.execute(
        """SELECT event_date, cashflow_direction, amount_cents
           FROM planned_cashflow_events
           WHERE enabled = 1
             AND match_status = 'unmatched'
             AND event_date >= ?
             AND event_date < ?""",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    result: dict[str, int] = {}
    for row in rows:
        month = str(row["event_date"] if isinstance(row, sqlite3.Row) else row[0])[:7]
        direction = str(row["cashflow_direction"] if isinstance(row, sqlite3.Row) else row[1])
        amount = int(row["amount_cents"] if isinstance(row, sqlite3.Row) else row[2])
        result[month] = result.get(month, 0) + (amount if direction == "inflow" else -amount)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="管理计划现金流事件")
    parser.add_argument("--db", type=Path, default=Path("data/processed/cashflow.db"))
    parser.add_argument("--match", action="store_true", help="匹配 BeeCount 已发生流水")
    parser.add_argument("--name")
    parser.add_argument("--date")
    parser.add_argument("--amount", help="金额，单位元")
    parser.add_argument("--direction", choices=sorted(DIRECTIONS))
    parser.add_argument("--financial-type", choices=sorted(FINANCIAL_TYPES))
    parser.add_argument("--category-l1", default="")
    parser.add_argument("--category-l2", default="")
    parser.add_argument("--note", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    try:
        if args.match:
            if args.dry_run:
                print("matched=0 scanned=0 dry_run=1")
                return
            print(match_planned_events(args.db))
            return
        if not all([args.name, args.date, args.amount, args.direction, args.financial_type]):
            raise ValueError("新增计划事件需要 --name --date --amount --direction --financial-type")
        amount_cents = parse_yuan_to_cents(args.amount)
        if args.dry_run:
            print("saved=0 dry_run=1 failed=0")
            return
        event_id = create_planned_event(
            args.db,
            args.name,
            args.date,
            amount_cents,
            args.direction,
            args.financial_type,
            category_l1=args.category_l1,
            category_l2=args.category_l2,
            note=args.note,
        )
        if args.verbose:
            print(f"event_id={event_id} amount_cents={amount_cents}")
        print(f"saved=1 event_id={event_id} failed=0")
    except (sqlite3.Error, ValueError) as exc:
        print(f"failed=1 error={exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
