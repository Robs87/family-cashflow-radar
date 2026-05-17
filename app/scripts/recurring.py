#!/usr/bin/env python3
"""Recurring bill templates and automatic transaction generation."""

import argparse
import hashlib
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.scripts.add_transaction import parse_amount_cents
from app.scripts.generate_monthly_cashflow import generate_monthly_cashflow


SCHEMA_SQL = Path(__file__).resolve().parents[1] / "db" / "schema.sql"
SEED_RULES_SQL = Path(__file__).resolve().parents[1] / "db" / "seed_rules.sql"


@dataclass(frozen=True)
class GenerationResult:
    generated: int
    skipped_existing: int
    failed: int


def ensure_recurring_schema(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
        _ensure_column(conn, "mortgage_prepayment_events", "replaced_schedule_json", "TEXT")
        rules_count = conn.execute("SELECT COUNT(*) FROM classification_rules").fetchone()[0]
        if rules_count == 0:
            conn.executescript(SEED_RULES_SQL.read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _add_months(source: date, months: int, day_of_month: int) -> date:
    month_index = source.month - 1 + months
    year = source.year + month_index // 12
    month = month_index % 12 + 1
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    last_day = (next_month - date.resolution).day
    return date(year, month, min(day_of_month, last_day))


def _yuan_from_cents(cents: int) -> str:
    return f"{Decimal(cents) / Decimal(100):.2f}"


def _round_cents(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def build_equal_payment_schedule(
    principal_cents: int,
    annual_rate_percent: Decimal,
    term_months: int,
) -> list[dict[str, int]]:
    if principal_cents <= 0:
        raise ValueError("贷款本金必须大于 0")
    if term_months <= 0:
        raise ValueError("贷款期限必须大于 0")
    if annual_rate_percent < 0:
        raise ValueError("贷款利率不能为负")

    principal = Decimal(principal_cents)
    monthly_rate = annual_rate_percent / Decimal("100") / Decimal("12")
    if monthly_rate == 0:
        base_payment = _round_cents(principal / Decimal(term_months))
    else:
        factor = (Decimal("1") + monthly_rate) ** term_months
        base_payment = _round_cents(principal * monthly_rate * factor / (factor - Decimal("1")))

    remaining = principal_cents
    rows = []
    for period_no in range(1, term_months + 1):
        if period_no == term_months:
            interest = _round_cents(Decimal(remaining) * monthly_rate)
            principal_part = remaining
            payment = principal_part + interest
        else:
            interest = _round_cents(Decimal(remaining) * monthly_rate)
            principal_part = max(0, min(remaining, base_payment - interest))
            payment = principal_part + interest
        remaining -= principal_part
        rows.append(
            {
                "period_no": period_no,
                "payment_cents": payment,
                "principal_cents": principal_part,
                "interest_cents": interest,
                "fee_cents": 0,
                "remaining_principal_cents": remaining,
            }
        )
    return rows


def build_fixed_payment_schedule(
    principal_cents: int,
    annual_rate_percent: Decimal,
    payment_cents: int,
) -> list[dict[str, int]]:
    if principal_cents <= 0:
        raise ValueError("剩余本金必须大于 0")
    if payment_cents <= 0:
        raise ValueError("月供必须大于 0")
    if annual_rate_percent < 0:
        raise ValueError("贷款利率不能为负")

    monthly_rate = annual_rate_percent / Decimal("100") / Decimal("12")
    remaining = principal_cents
    rows = []
    period_no = 1
    while remaining > 0:
        interest = _round_cents(Decimal(remaining) * monthly_rate)
        if payment_cents <= interest and remaining > payment_cents:
            raise ValueError("月供不足以覆盖当期利息，无法缩短期限")
        principal_part = min(remaining, max(0, payment_cents - interest))
        payment = principal_part + interest
        remaining -= principal_part
        rows.append(
            {
                "period_no": period_no,
                "payment_cents": payment,
                "principal_cents": principal_part,
                "interest_cents": interest,
                "fee_cents": 0,
                "remaining_principal_cents": remaining,
            }
        )
        period_no += 1
        if period_no > 1200:
            raise ValueError("还款计划超过 1200 期，请检查利率和月供")
    return rows


def create_mortgage_template(
    db_path: Path,
    name: str,
    principal_cents: int,
    annual_rate_percent: Decimal,
    term_months: int,
    start_date: str,
    day_of_month: int,
    account: str = "",
    lender: str = "",
) -> int:
    ensure_recurring_schema(db_path)
    start = date.fromisoformat(start_date)
    if not 1 <= day_of_month <= 31:
        raise ValueError("还款日必须在 1 到 31 之间")
    schedule = build_equal_payment_schedule(principal_cents, annual_rate_percent, term_months)
    monthly_payment_cents = schedule[0]["payment_cents"]
    end = _add_months(start, term_months - 1, day_of_month)

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO debts
               (debt_name, debt_type, principal_initial_cents, principal_current_cents,
                monthly_payment_cents, interest_rate, start_date, end_date, lender, status, notes)
               VALUES (?, 'mortgage', ?, ?, ?, ?, ?, ?, ?, 'active', ?)""",
            (
                name,
                principal_cents,
                principal_cents,
                monthly_payment_cents,
                float(annual_rate_percent),
                start.isoformat(),
                end.isoformat(),
                lender,
                "equal_payment_schedule",
            ),
        )
        debt_id = cursor.lastrowid
        cursor.execute(
            """INSERT INTO recurring_bill_templates
               (name, bill_type, amount_cents, cashflow_direction, financial_type,
                category_l1, category_l2, account, start_date, end_date,
                day_of_month, debt_id, enabled)
               VALUES (?, 'mortgage', ?, 'outflow', 'debt_payment',
                       '固定刚性支出', '房贷', ?, ?, ?, ?, ?, 1)""",
            (name, monthly_payment_cents, account, start.isoformat(), end.isoformat(), day_of_month, debt_id),
        )
        template_id = cursor.lastrowid
        for offset, row in enumerate(schedule):
            due_date = _add_months(start, offset, day_of_month).isoformat()
            cursor.execute(
                """INSERT INTO mortgage_repayment_schedule
                   (recurring_template_id, debt_id, period_no, due_date,
                    payment_cents, principal_cents, interest_cents, fee_cents,
                    remaining_principal_cents)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    template_id,
                    debt_id,
                    row["period_no"],
                    due_date,
                    row["payment_cents"],
                    row["principal_cents"],
                    row["interest_cents"],
                    row["fee_cents"],
                    row["remaining_principal_cents"],
                ),
            )
        conn.commit()
        return int(template_id)
    finally:
        conn.close()


def create_fixed_bill_template(
    db_path: Path,
    name: str,
    amount_cents: int,
    start_date: str,
    day_of_month: int,
    category_l2: str,
    account: str = "",
    end_date: str | None = None,
) -> int:
    ensure_recurring_schema(db_path)
    if amount_cents <= 0:
        raise ValueError("固定账单金额必须大于 0")
    if not 1 <= day_of_month <= 31:
        raise ValueError("扣款日必须在 1 到 31 之间")
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date).isoformat() if end_date else None

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        cursor = conn.execute(
            """INSERT INTO recurring_bill_templates
               (name, bill_type, amount_cents, cashflow_direction, financial_type,
                category_l1, category_l2, account, start_date, end_date,
                day_of_month, enabled)
               VALUES (?, 'fixed_bill', ?, 'outflow', 'fixed_expense',
                       '固定刚性支出', ?, ?, ?, ?, ?, 1)""",
            (name, amount_cents, category_l2, account, start.isoformat(), end, day_of_month),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def _ensure_template_editable(cursor: sqlite3.Cursor, template_id: int) -> None:
    generated = cursor.execute(
        """SELECT 1
           FROM recurring_bill_instances
           WHERE recurring_template_id = ?
           LIMIT 1""",
        (template_id,),
    ).fetchone()
    if generated:
        raise ValueError("该模板已经生成过自动记账，不能直接修改历史模板")

    prepayments = cursor.execute(
        """SELECT 1
           FROM mortgage_prepayment_events
           WHERE recurring_template_id = ?
           LIMIT 1""",
        (template_id,),
    ).fetchone()
    if prepayments:
        raise ValueError("该房贷模板已有提前还贷事件，不能直接修改原模板")


def update_mortgage_template(
    db_path: Path,
    recurring_template_id: int,
    name: str,
    principal_cents: int,
    annual_rate_percent: Decimal,
    term_months: int,
    start_date: str,
    day_of_month: int,
    account: str = "",
    lender: str = "",
) -> None:
    ensure_recurring_schema(db_path)
    start = date.fromisoformat(start_date)
    if principal_cents <= 0:
        raise ValueError("贷款金额必须大于 0")
    if term_months <= 0:
        raise ValueError("贷款期限必须大于 0")
    if not 1 <= day_of_month <= 31:
        raise ValueError("还款日必须在 1 到 31 之间")

    schedule = build_equal_payment_schedule(principal_cents, annual_rate_percent, term_months)
    monthly_payment_cents = schedule[0]["payment_cents"]
    end = _add_months(start, term_months - 1, day_of_month)

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.cursor()
        template = cursor.execute(
            """SELECT *
               FROM recurring_bill_templates
               WHERE id = ? AND bill_type = 'mortgage'""",
            (recurring_template_id,),
        ).fetchone()
        if not template:
            raise ValueError(f"未找到房贷模板: {recurring_template_id}")
        _ensure_template_editable(cursor, recurring_template_id)

        cursor.execute(
            """UPDATE debts
               SET debt_name = ?,
                   principal_initial_cents = ?,
                   principal_current_cents = ?,
                   monthly_payment_cents = ?,
                   interest_rate = ?,
                   start_date = ?,
                   end_date = ?,
                   lender = ?,
                   updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (
                name,
                principal_cents,
                principal_cents,
                monthly_payment_cents,
                float(annual_rate_percent),
                start.isoformat(),
                end.isoformat(),
                lender,
                template["debt_id"],
            ),
        )
        cursor.execute(
            """UPDATE recurring_bill_templates
               SET name = ?,
                   amount_cents = ?,
                   account = ?,
                   start_date = ?,
                   end_date = ?,
                   day_of_month = ?,
                   enabled = 1,
                   updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (
                name,
                monthly_payment_cents,
                account,
                start.isoformat(),
                end.isoformat(),
                day_of_month,
                recurring_template_id,
            ),
        )
        cursor.execute(
            "DELETE FROM mortgage_repayment_schedule WHERE recurring_template_id = ?",
            (recurring_template_id,),
        )
        _insert_schedule_rows(
            cursor,
            recurring_template_id,
            int(template["debt_id"]),
            start,
            1,
            day_of_month,
            schedule,
        )
        conn.commit()
    finally:
        conn.close()


def update_fixed_bill_template(
    db_path: Path,
    recurring_template_id: int,
    name: str,
    amount_cents: int,
    start_date: str,
    day_of_month: int,
    category_l2: str,
    account: str = "",
    end_date: str | None = None,
) -> None:
    ensure_recurring_schema(db_path)
    if amount_cents <= 0:
        raise ValueError("固定账单金额必须大于 0")
    if not 1 <= day_of_month <= 31:
        raise ValueError("扣款日必须在 1 到 31 之间")
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date).isoformat() if end_date else None

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.cursor()
        template = cursor.execute(
            """SELECT *
               FROM recurring_bill_templates
               WHERE id = ? AND bill_type = 'fixed_bill'""",
            (recurring_template_id,),
        ).fetchone()
        if not template:
            raise ValueError(f"未找到固定账单模板: {recurring_template_id}")
        _ensure_template_editable(cursor, recurring_template_id)
        cursor.execute(
            """UPDATE recurring_bill_templates
               SET name = ?,
                   amount_cents = ?,
                   category_l2 = ?,
                   account = ?,
                   start_date = ?,
                   end_date = ?,
                   day_of_month = ?,
                   enabled = 1,
                   updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (
                name,
                amount_cents,
                category_l2,
                account,
                start.isoformat(),
                end,
                day_of_month,
                recurring_template_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_schedule_rows(
    cursor: sqlite3.Cursor,
    template_id: int,
    debt_id: int,
    start_due_date: date,
    start_period_no: int,
    day_of_month: int,
    schedule: list[dict[str, int]],
) -> None:
    for offset, row in enumerate(schedule):
        due_date = _add_months(start_due_date, offset, day_of_month).isoformat()
        cursor.execute(
            """INSERT INTO mortgage_repayment_schedule
               (recurring_template_id, debt_id, period_no, due_date,
                payment_cents, principal_cents, interest_cents, fee_cents,
                remaining_principal_cents)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                template_id,
                debt_id,
                start_period_no + offset,
                due_date,
                row["payment_cents"],
                row["principal_cents"],
                row["interest_cents"],
                row["fee_cents"],
                row["remaining_principal_cents"],
            ),
        )


def add_mortgage_prepayment(
    db_path: Path,
    recurring_template_id: int,
    prepayment_date: str,
    amount_cents: int,
    effect_type: str = "reduce_term",
    note: str = "",
) -> int:
    ensure_recurring_schema(db_path)
    if amount_cents <= 0:
        raise ValueError("提前还款金额必须大于 0")
    if effect_type not in {"reduce_term", "reduce_payment"}:
        raise ValueError("提前还款方式必须是 reduce_term 或 reduce_payment")
    event_date = date.fromisoformat(prepayment_date)

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.cursor()
        template = cursor.execute(
            """SELECT t.*, d.principal_initial_cents, d.interest_rate
               FROM recurring_bill_templates t
               JOIN debts d ON d.id = t.debt_id
               WHERE t.id = ? AND t.bill_type = 'mortgage'""",
            (recurring_template_id,),
        ).fetchone()
        if not template:
            raise ValueError(f"未找到房贷模板: {recurring_template_id}")

        generated_after = cursor.execute(
            """SELECT 1
               FROM recurring_bill_instances
               WHERE recurring_template_id = ?
                 AND due_date >= ?
               LIMIT 1""",
            (recurring_template_id, event_date.isoformat()),
        ).fetchone()
        if generated_after:
            raise ValueError("提前还款日期之后已有自动记账记录，不能重算已生成的还款计划")
        later_prepayment = cursor.execute(
            """SELECT 1
               FROM mortgage_prepayment_events
               WHERE recurring_template_id = ?
                 AND prepayment_date >= ?
               LIMIT 1""",
            (recurring_template_id, event_date.isoformat()),
        ).fetchone()
        if later_prepayment:
            raise ValueError("已存在该日期之后的提前还款事件，请按时间顺序添加")

        next_row = cursor.execute(
            """SELECT *
               FROM mortgage_repayment_schedule
               WHERE recurring_template_id = ?
                 AND due_date >= ?
               ORDER BY due_date
               LIMIT 1""",
            (recurring_template_id, event_date.isoformat()),
        ).fetchone()
        if not next_row:
            raise ValueError("该房贷在提前还款日期后没有剩余计划")
        replaced_rows = cursor.execute(
            """SELECT period_no, due_date, payment_cents, principal_cents,
                      interest_cents, fee_cents, remaining_principal_cents
               FROM mortgage_repayment_schedule
               WHERE recurring_template_id = ?
                 AND due_date >= ?
               ORDER BY due_date""",
            (recurring_template_id, next_row["due_date"]),
        ).fetchall()
        replaced_schedule_json = json.dumps([dict(row) for row in replaced_rows], ensure_ascii=False)

        previous_row = cursor.execute(
            """SELECT *
               FROM mortgage_repayment_schedule
               WHERE recurring_template_id = ?
                 AND due_date < ?
               ORDER BY due_date DESC
               LIMIT 1""",
            (recurring_template_id, event_date.isoformat()),
        ).fetchone()
        remaining_before = (
            int(previous_row["remaining_principal_cents"])
            if previous_row
            else int(template["principal_initial_cents"])
        )
        if amount_cents > remaining_before:
            raise ValueError("提前还款金额不能超过当时剩余本金")
        remaining_after = remaining_before - amount_cents
        old_future_count = cursor.execute(
            """SELECT COUNT(*)
               FROM mortgage_repayment_schedule
               WHERE recurring_template_id = ?
                 AND due_date >= ?""",
            (recurring_template_id, next_row["due_date"]),
        ).fetchone()[0]

        cursor.execute(
            """INSERT INTO mortgage_prepayment_events
               (recurring_template_id, debt_id, prepayment_date, amount_cents,
                effect_type, remaining_principal_before_cents,
                remaining_principal_after_cents, replaced_schedule_json, note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                recurring_template_id,
                template["debt_id"],
                event_date.isoformat(),
                amount_cents,
                effect_type,
                remaining_before,
                remaining_after,
                replaced_schedule_json,
                note,
            ),
        )
        event_id = cursor.lastrowid

        cursor.execute(
            """DELETE FROM mortgage_repayment_schedule
               WHERE recurring_template_id = ?
                 AND due_date >= ?""",
            (recurring_template_id, next_row["due_date"]),
        )

        if remaining_after == 0:
            cursor.execute(
                """UPDATE recurring_bill_templates
                   SET amount_cents = 0,
                       end_date = ?,
                       enabled = 0,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (event_date.isoformat(), recurring_template_id),
            )
        else:
            annual_rate = Decimal(str(template["interest_rate"] or 0))
            remaining_term = max(1, int(old_future_count))

            if effect_type == "reduce_payment":
                new_schedule = build_equal_payment_schedule(remaining_after, annual_rate, remaining_term)
            else:
                new_schedule = build_fixed_payment_schedule(remaining_after, annual_rate, int(template["amount_cents"]))

            start_due = date.fromisoformat(next_row["due_date"])
            _insert_schedule_rows(
                cursor,
                recurring_template_id,
                int(template["debt_id"]),
                start_due,
                int(next_row["period_no"]),
                int(template["day_of_month"]),
                new_schedule,
            )
            new_end = _add_months(start_due, len(new_schedule) - 1, int(template["day_of_month"]))
            cursor.execute(
                """UPDATE recurring_bill_templates
                   SET amount_cents = ?,
                       end_date = ?,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (new_schedule[0]["payment_cents"], new_end.isoformat(), recurring_template_id),
            )
        conn.commit()
        return int(event_id)
    finally:
        conn.close()


def _restore_replaced_schedule(cursor: sqlite3.Cursor, event: sqlite3.Row) -> None:
    if event["generated_normalized_transaction_id"]:
        raise ValueError("该提前还贷事件已经生成交易，不能直接修改")
    if not event["replaced_schedule_json"]:
        raise ValueError("该提前还贷事件缺少还款计划快照，不能安全修改")

    rows = json.loads(event["replaced_schedule_json"])
    if not rows:
        return
    first_due_date = min(row["due_date"] for row in rows)
    cursor.execute(
        """DELETE FROM mortgage_repayment_schedule
           WHERE recurring_template_id = ?
             AND due_date >= ?""",
        (event["recurring_template_id"], first_due_date),
    )
    for row in rows:
        cursor.execute(
            """INSERT INTO mortgage_repayment_schedule
               (recurring_template_id, debt_id, period_no, due_date,
                payment_cents, principal_cents, interest_cents, fee_cents,
                remaining_principal_cents)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event["recurring_template_id"],
                event["debt_id"],
                row["period_no"],
                row["due_date"],
                row["payment_cents"],
                row["principal_cents"],
                row["interest_cents"],
                row["fee_cents"],
                row["remaining_principal_cents"],
            ),
        )
    last_due_date = max(row["due_date"] for row in rows)
    first_payment = next(row["payment_cents"] for row in rows if row["due_date"] == first_due_date)
    cursor.execute(
        """UPDATE recurring_bill_templates
           SET amount_cents = ?,
               end_date = ?,
               enabled = 1,
               updated_at = CURRENT_TIMESTAMP
           WHERE id = ?""",
        (first_payment, last_due_date, event["recurring_template_id"]),
    )


def update_mortgage_prepayment(
    db_path: Path,
    prepayment_event_id: int,
    prepayment_date: str,
    amount_cents: int,
    effect_type: str = "reduce_term",
    note: str = "",
) -> int:
    ensure_recurring_schema(db_path)
    if amount_cents <= 0:
        raise ValueError("提前还款金额必须大于 0")
    if effect_type not in {"reduce_term", "reduce_payment"}:
        raise ValueError("提前还款方式必须是 reduce_term 或 reduce_payment")

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.cursor()
        event = cursor.execute(
            "SELECT * FROM mortgage_prepayment_events WHERE id = ?",
            (prepayment_event_id,),
        ).fetchone()
        if not event:
            raise ValueError(f"未找到提前还贷事件: {prepayment_event_id}")
        later_event = cursor.execute(
            """SELECT 1
               FROM mortgage_prepayment_events
               WHERE recurring_template_id = ?
                 AND id != ?
                 AND prepayment_date >= ?
               LIMIT 1""",
            (event["recurring_template_id"], prepayment_event_id, event["prepayment_date"]),
        ).fetchone()
        if later_event:
            raise ValueError("该事件之后已有其他提前还贷事件，不能直接修改")
        _restore_replaced_schedule(cursor, event)
        cursor.execute("DELETE FROM mortgage_prepayment_events WHERE id = ?", (prepayment_event_id,))
        conn.commit()
        new_event_id = add_mortgage_prepayment(
            db_path,
            int(event["recurring_template_id"]),
            prepayment_date,
            amount_cents,
            effect_type=effect_type,
            note=note,
        )
        return new_event_id
    finally:
        conn.close()


def _raw_hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _insert_generated_transaction(
    conn: sqlite3.Connection,
    template: sqlite3.Row,
    due_date: str,
    amount_cents: int,
    description: str,
    schedule_id: int | None = None,
    split: dict | None = None,
) -> bool:
    existing = conn.execute(
        """SELECT 1
           FROM recurring_bill_instances
           WHERE recurring_template_id = ? AND due_date = ?""",
        (template["id"], due_date),
    ).fetchone()
    if existing:
        return False

    payload = {
        "source": "recurring_bill",
        "template_id": template["id"],
        "due_date": due_date,
        "amount_cents": amount_cents,
        "description": description,
    }
    amount_original = _yuan_from_cents(amount_cents)
    cursor = conn.execute(
        """INSERT INTO raw_transactions
           (source_file, source_row_no, transaction_time, transaction_date,
            amount_original, income_amount_original, expense_amount_original,
            amount_cents, direction_raw, account, category_original,
            subcategory_original, merchant, note, project, tags,
            raw_payload, raw_hash)
           VALUES ('recurring_bill', 0, ?, ?, ?, '', ?, ?, '支出',
                   ?, ?, ?, '', ?, '', '', ?, ?)""",
        (
            due_date,
            due_date,
            amount_original,
            amount_original,
            amount_cents,
            template["account"] or "",
            template["category_l1"] or "",
            template["category_l2"] or "",
            description,
            json.dumps(payload, ensure_ascii=False),
            _raw_hash(payload),
        ),
    )
    raw_id = cursor.lastrowid
    year, month = (int(part) for part in due_date.split("-")[:2])
    cursor = conn.execute(
        """INSERT INTO normalized_transactions
           (raw_transaction_id, transaction_date, year, month,
            amount_cents, cashflow_direction, financial_type,
            category_l1, category_l2, account, counterparty, description,
            is_recurring, is_large_amount, is_internal_transfer, is_debt_related,
            is_asset_related, is_investment_related, confidence, review_status,
            manual_financial_type, manual_category_l1, manual_category_l2,
            manual_cashflow_direction, manual_note, manual_updated_at)
           VALUES (?, ?, ?, ?, ?, 'outflow', ?, ?, ?, ?, '', ?,
                   1, ?, 0, ?, 0, 0, 1.0, 'approved',
                   ?, ?, ?, 'outflow', 'recurring_bill', CURRENT_TIMESTAMP)""",
        (
            raw_id,
            due_date,
            year,
            month,
            amount_cents,
            template["financial_type"],
            template["category_l1"],
            template["category_l2"],
            template["account"],
            description,
            1 if amount_cents >= 1_000_000 else 0,
            1 if template["financial_type"] == "debt_payment" else 0,
            template["financial_type"],
            template["category_l1"],
            template["category_l2"],
        ),
    )
    normalized_id = cursor.lastrowid
    conn.execute(
        """INSERT INTO recurring_bill_instances
           (recurring_template_id, due_date, normalized_transaction_id, schedule_id)
           VALUES (?, ?, ?, ?)""",
        (template["id"], due_date, normalized_id, schedule_id),
    )
    if split:
        conn.execute(
            """INSERT INTO debt_payment_splits
               (normalized_transaction_id, debt_id, principal_cents, interest_cents,
                fee_cents, remaining_principal_cents, note)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                normalized_id,
                template["debt_id"],
                split["principal_cents"],
                split["interest_cents"],
                split["fee_cents"],
                split["remaining_principal_cents"],
                "auto_generated_mortgage_schedule",
            ),
        )
        conn.execute(
            """UPDATE debts
               SET principal_current_cents = ?,
                   updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (split["remaining_principal_cents"], template["debt_id"]),
        )
    return True


def _insert_prepayment_transaction(conn: sqlite3.Connection, event: sqlite3.Row, template: sqlite3.Row) -> bool:
    if event["generated_normalized_transaction_id"]:
        return False

    payload = {
        "source": "mortgage_prepayment",
        "event_id": event["id"],
        "template_id": event["recurring_template_id"],
        "prepayment_date": event["prepayment_date"],
        "amount_cents": event["amount_cents"],
        "effect_type": event["effect_type"],
    }
    amount_original = _yuan_from_cents(int(event["amount_cents"]))
    description = f"{template['name']} 提前还款"
    cursor = conn.execute(
        """INSERT INTO raw_transactions
           (source_file, source_row_no, transaction_time, transaction_date,
            amount_original, income_amount_original, expense_amount_original,
            amount_cents, direction_raw, account, category_original,
            subcategory_original, merchant, note, project, tags,
            raw_payload, raw_hash)
           VALUES ('mortgage_prepayment', 0, ?, ?, ?, '', ?, ?, '支出',
                   ?, '固定刚性支出', '房贷提前还款', '', ?, '', '', ?, ?)""",
        (
            event["prepayment_date"],
            event["prepayment_date"],
            amount_original,
            amount_original,
            int(event["amount_cents"]),
            template["account"] or "",
            description,
            json.dumps(payload, ensure_ascii=False),
            _raw_hash(payload),
        ),
    )
    raw_id = cursor.lastrowid
    year, month = (int(part) for part in event["prepayment_date"].split("-")[:2])
    cursor = conn.execute(
        """INSERT INTO normalized_transactions
           (raw_transaction_id, transaction_date, year, month,
            amount_cents, cashflow_direction, financial_type,
            category_l1, category_l2, account, counterparty, description,
            is_recurring, is_large_amount, is_internal_transfer, is_debt_related,
            is_asset_related, is_investment_related, confidence, review_status,
            manual_financial_type, manual_category_l1, manual_category_l2,
            manual_cashflow_direction, manual_note, manual_updated_at)
           VALUES (?, ?, ?, ?, ?, 'outflow', 'debt_payment',
                   '固定刚性支出', '房贷提前还款', ?, '', ?,
                   1, ?, 0, 1, 0, 0, 1.0, 'approved',
                   'debt_payment', '固定刚性支出', '房贷提前还款',
                   'outflow', 'mortgage_prepayment', CURRENT_TIMESTAMP)""",
        (
            raw_id,
            event["prepayment_date"],
            year,
            month,
            int(event["amount_cents"]),
            template["account"] or "",
            description,
            1 if int(event["amount_cents"]) >= 1_000_000 else 0,
        ),
    )
    normalized_id = cursor.lastrowid
    conn.execute(
        """INSERT INTO debt_payment_splits
           (normalized_transaction_id, debt_id, principal_cents, interest_cents,
            fee_cents, remaining_principal_cents, note)
           VALUES (?, ?, ?, 0, 0, ?, ?)""",
        (
            normalized_id,
            event["debt_id"],
            int(event["amount_cents"]),
            int(event["remaining_principal_after_cents"]),
            f"mortgage_prepayment:{event['effect_type']}",
        ),
    )
    conn.execute(
        """UPDATE mortgage_prepayment_events
           SET generated_normalized_transaction_id = ?
           WHERE id = ?""",
        (normalized_id, event["id"]),
    )
    conn.execute(
        """UPDATE debts
           SET principal_current_cents = ?,
               updated_at = CURRENT_TIMESTAMP
           WHERE id = ?""",
        (int(event["remaining_principal_after_cents"]), event["debt_id"]),
    )
    return True


def generate_due_recurring_bills(db_path: Path, as_of: str | None = None) -> GenerationResult:
    ensure_recurring_schema(db_path)
    as_of_date = date.fromisoformat(as_of) if as_of else date.today()
    generated = 0
    skipped = 0
    failed = 0

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    try:
        templates = conn.execute(
            """SELECT *
               FROM recurring_bill_templates
               WHERE enabled = 1
               ORDER BY id"""
        ).fetchall()
        for template in templates:
            try:
                if template["bill_type"] == "mortgage":
                    schedule_rows = conn.execute(
                        """SELECT *
                           FROM mortgage_repayment_schedule
                           WHERE recurring_template_id = ?
                             AND due_date <= ?
                           ORDER BY due_date""",
                        (template["id"], as_of_date.isoformat()),
                    ).fetchall()
                    prepayment_rows = conn.execute(
                        """SELECT *
                           FROM mortgage_prepayment_events
                           WHERE recurring_template_id = ?
                             AND prepayment_date <= ?
                           ORDER BY prepayment_date, id""",
                        (template["id"], as_of_date.isoformat()),
                    ).fetchall()
                    items = [
                        ("scheduled", row["due_date"], row)
                        for row in schedule_rows
                    ] + [
                        ("prepayment", row["prepayment_date"], row)
                        for row in prepayment_rows
                    ]
                    for kind, _item_date, row in sorted(items, key=lambda item: (item[1], 0 if item[0] == "scheduled" else 1)):
                        if kind == "scheduled":
                            inserted = _insert_generated_transaction(
                                conn,
                                template,
                                row["due_date"],
                                row["payment_cents"],
                                f"{template['name']} 第{row['period_no']}期",
                                schedule_id=row["id"],
                                split=dict(row),
                            )
                        else:
                            inserted = _insert_prepayment_transaction(conn, row, template)
                        generated += 1 if inserted else 0
                        skipped += 0 if inserted else 1
                else:
                    start = date.fromisoformat(template["start_date"])
                    end = date.fromisoformat(template["end_date"]) if template["end_date"] else as_of_date
                    cutoff = min(end, as_of_date)
                    offset = 0
                    while True:
                        due = _add_months(start, offset, int(template["day_of_month"]))
                        if due > cutoff:
                            break
                        inserted = _insert_generated_transaction(
                            conn,
                            template,
                            due.isoformat(),
                            int(template["amount_cents"]),
                            template["name"],
                        )
                        generated += 1 if inserted else 0
                        skipped += 0 if inserted else 1
                        offset += 1
            except Exception as exc:
                print(f"错误: 生成周期账单 {template['name']} 失败: {exc}", file=sys.stderr)
                failed += 1
        conn.commit()
    finally:
        conn.close()

    if generated:
        generate_monthly_cashflow(db_path)
    return GenerationResult(generated, skipped, failed)


def main() -> None:
    parser = argparse.ArgumentParser(description="管理周期账单并自动记账")
    subparsers = parser.add_subparsers(dest="command", required=True)

    mortgage = subparsers.add_parser("add-mortgage", help="新增房贷模板并生成还款计划")
    mortgage.add_argument("--db", required=True)
    mortgage.add_argument("--name", required=True)
    mortgage.add_argument("--principal", required=True, help="贷款本金，单位元")
    mortgage.add_argument("--annual-rate", required=True, help="年利率百分比，例如 3.2")
    mortgage.add_argument("--term-months", type=int, required=True)
    mortgage.add_argument("--start-date", required=True)
    mortgage.add_argument("--day-of-month", type=int, required=True)
    mortgage.add_argument("--account", default="")
    mortgage.add_argument("--lender", default="")

    fixed = subparsers.add_parser("add-fixed", help="新增固定周期账单")
    fixed.add_argument("--db", required=True)
    fixed.add_argument("--name", required=True)
    fixed.add_argument("--amount", required=True, help="金额，单位元")
    fixed.add_argument("--start-date", required=True)
    fixed.add_argument("--day-of-month", type=int, required=True)
    fixed.add_argument("--category-l2", required=True)
    fixed.add_argument("--account", default="")
    fixed.add_argument("--end-date")

    generate = subparsers.add_parser("generate", help="生成截至指定日期的到期账单")
    generate.add_argument("--db", required=True)
    generate.add_argument("--as-of")

    prepay = subparsers.add_parser("add-prepayment", help="新增房贷提前还款事件并重算后续计划")
    prepay.add_argument("--db", required=True)
    prepay.add_argument("--template-id", type=int, required=True)
    prepay.add_argument("--date", required=True)
    prepay.add_argument("--amount", required=True, help="提前还款金额，单位元")
    prepay.add_argument("--effect", choices=["reduce_term", "reduce_payment"], default="reduce_term")
    prepay.add_argument("--note", default="")

    args = parser.parse_args()
    try:
        if args.command == "add-mortgage":
            template_id = create_mortgage_template(
                Path(args.db),
                args.name,
                parse_amount_cents(args.principal),
                Decimal(args.annual_rate),
                args.term_months,
                args.start_date,
                args.day_of_month,
                account=args.account,
                lender=args.lender,
            )
            print(f"mortgage_template_id={template_id}")
        elif args.command == "add-fixed":
            template_id = create_fixed_bill_template(
                Path(args.db),
                args.name,
                parse_amount_cents(args.amount),
                args.start_date,
                args.day_of_month,
                args.category_l2,
                account=args.account,
                end_date=args.end_date,
            )
            print(f"fixed_bill_template_id={template_id}")
        elif args.command == "generate":
            result = generate_due_recurring_bills(Path(args.db), as_of=args.as_of)
            print(
                f"generated={result.generated} "
                f"skipped_existing={result.skipped_existing} failed={result.failed}"
            )
            if result.failed:
                sys.exit(1)
        elif args.command == "add-prepayment":
            event_id = add_mortgage_prepayment(
                Path(args.db),
                args.template_id,
                args.date,
                parse_amount_cents(args.amount),
                effect_type=args.effect,
                note=args.note,
            )
            print(f"mortgage_prepayment_event_id={event_id}")
    except Exception as exc:
        print(f"错误: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
