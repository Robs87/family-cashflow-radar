#!/usr/bin/env python3
"""Add one manually recorded transaction.

Manual entries are first-class input for the project. They preserve a raw row for
audit, then write an approved normalized row so the monthly dashboard can update
without a CSV import step.
"""

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.scripts.schema_migrations import ensure_v02_schema


SCHEMA_SQL = Path(__file__).resolve().parents[1] / "db" / "schema.sql"
SEED_RULES_SQL = Path(__file__).resolve().parents[1] / "db" / "seed_rules.sql"

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
    "internal_transfer",
    "credit_card_payment",
    "refund",
    "reimbursable_expense",
    "reimbursement_income",
    "historical_debt_asset_event",
    "unknown",
}
DIRECTIONS = {"inflow", "outflow", "neutral"}


@dataclass(frozen=True)
class ParsedTransaction:
    transaction_date: str
    amount_cents: int
    cashflow_direction: str
    financial_type: str
    description: str


def ensure_database_initialized(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        has_raw_table = conn.execute(
            """SELECT 1
               FROM sqlite_master
               WHERE type = 'table' AND name = 'raw_transactions'"""
        ).fetchone()
        if not has_raw_table:
            conn.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))

        rules_count = conn.execute("SELECT COUNT(*) FROM classification_rules").fetchone()[0]
        if rules_count == 0:
            conn.executescript(SEED_RULES_SQL.read_text(encoding="utf-8"))
        ensure_v02_schema(conn)
        conn.commit()
    finally:
        conn.close()


def parse_amount_cents(value: str) -> int:
    cleaned = str(value).strip().replace(",", "").replace("，", "")
    if cleaned.endswith("元"):
        cleaned = cleaned[:-1]
    if not cleaned:
        raise ValueError("金额为空")
    try:
        amount = abs(Decimal(cleaned))
    except Exception as exc:
        raise ValueError(f"无效金额: {value!r}") from exc
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _parse_date_token(text: str, today: date) -> tuple[str, str]:
    if "今天" in text:
        return today.isoformat(), text.replace("今天", "", 1)
    if "昨天" in text:
        return (today - timedelta(days=1)).isoformat(), text.replace("昨天", "", 1)

    match = re.search(r"\b(\d{4}-\d{1,2}-\d{1,2})\b", text)
    if match:
        parts = [int(part) for part in match.group(1).split("-")]
        parsed = date(parts[0], parts[1], parts[2]).isoformat()
        return parsed, text.replace(match.group(1), "", 1)

    match = re.search(r"\b(\d{1,2})-(\d{1,2})\b", text)
    if match:
        parsed = date(today.year, int(match.group(1)), int(match.group(2))).isoformat()
        return parsed, text.replace(match.group(0), "", 1)

    return today.isoformat(), text


def _infer_direction(text: str) -> str:
    if any(token in text for token in ("报销到账", "公司报销", "报销款", "报销入账")):
        return "inflow"
    if any(token in text for token in ("工作垫付", "公司垫付", "帮公司垫付", "代垫", "出差垫付")):
        return "outflow"
    if any(token in text for token in ("赎回", "理财到账", "基金赎回", "股票卖出", "卖车", "出售")):
        return "inflow"
    if any(token in text for token in ("收入", "工资", "奖金", "收款")):
        return "inflow"
    if any(token in text for token in ("转账", "还信用卡", "信用卡还款", "账户互转")):
        return "neutral"
    return "outflow"


def _infer_financial_type(text: str, direction: str) -> str:
    if "信用卡" in text and "还" in text:
        return "credit_card_payment"
    if any(token in text for token in ("转账", "账户互转", "余额宝转入", "余额宝转出")):
        return "internal_transfer"
    if direction == "outflow" and any(token in text for token in ("工作垫付", "公司垫付", "帮公司垫付", "代垫", "出差垫付", "垫付报销")):
        return "reimbursable_expense"
    if direction == "inflow" and any(token in text for token in ("报销到账", "公司报销", "报销款", "报销入账", "垫付报销")):
        return "reimbursement_income"
    if direction == "inflow" and any(token in text for token in ("工资", "薪资", "薪水")):
        return "stable_income"
    if any(token in text for token in ("房贷", "车贷", "还款", "债务")):
        return "debt_payment"
    if any(token in text for token in ("房租", "物业", "水电", "燃气", "宽带", "保险")):
        return "fixed_expense"
    if any(token in text for token in ("基金", "股票", "理财", "投资")):
        return "investment_outflow" if direction == "outflow" else "investment_inflow"
    if direction == "inflow" and any(token in text for token in ("卖车", "出售", "转卖", "卖资产")):
        return "asset_sale"
    if any(token in text for token in ("买房", "买车", "资产")):
        return "asset_purchase"
    if direction == "inflow":
        return "one_time_income"
    return "living_expense" if direction == "outflow" else "unknown"


def parse_freeform_transaction(text: str, today: date | None = None) -> ParsedTransaction:
    today = today or date.today()
    text = text.strip()
    transaction_date, remaining = _parse_date_token(text, today)
    amount_match = re.search(r"(?<!\d)(\d+(?:\.\d{1,2})?)(?:\s*元)?(?!\d)", remaining)
    if not amount_match:
        raise ValueError("没有识别到金额，请输入类似：今天 68 午饭 外卖")

    amount_cents = parse_amount_cents(amount_match.group(1))
    description = (remaining[: amount_match.start()] + remaining[amount_match.end() :]).strip()
    description = re.sub(r"\s+", " ", description)
    if not description:
        description = "手动记录"

    direction = _infer_direction(text)
    financial_type = _infer_financial_type(text, direction)
    return ParsedTransaction(transaction_date, amount_cents, direction, financial_type, description)


def _validate(transaction_date: str, amount_cents: int, direction: str, financial_type: str) -> None:
    try:
        date.fromisoformat(transaction_date)
    except ValueError as exc:
        raise ValueError(f"日期必须是 YYYY-MM-DD: {transaction_date}") from exc
    if amount_cents < 0:
        raise ValueError("amount_cents 必须非负")
    if direction not in DIRECTIONS:
        raise ValueError(f"不支持的现金流方向: {direction}")
    if financial_type not in FINANCIAL_TYPES:
        raise ValueError(f"不支持的财务类型: {financial_type}")


def add_manual_transaction(
    db_path: Path,
    transaction_date: str,
    amount_cents: int,
    cashflow_direction: str,
    financial_type: str,
    description: str,
    account: str = "",
    category_l1: str = "",
    category_l2: str = "",
    dry_run: bool = False,
    verbose: bool = False,
) -> int | None:
    _validate(transaction_date, amount_cents, cashflow_direction, financial_type)
    ensure_database_initialized(db_path)

    year, month = (int(part) for part in transaction_date.split("-")[:2])
    direction_raw = {"inflow": "收入", "outflow": "支出", "neutral": "中性"}[cashflow_direction]
    amount_original = f"{Decimal(amount_cents) / Decimal(100):.2f}"
    entry_id = str(uuid.uuid4())
    payload = {
        "entry_id": entry_id,
        "source": "manual_entry",
        "transaction_date": transaction_date,
        "amount_cents": amount_cents,
        "cashflow_direction": cashflow_direction,
        "financial_type": financial_type,
        "description": description,
        "account": account,
        "category_l1": category_l1,
        "category_l2": category_l2,
    }
    raw_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

    if verbose or dry_run:
        print(
            f"manual_entry date={transaction_date} amount_cents={amount_cents} "
            f"direction={cashflow_direction} type={financial_type} description={description}"
        )
    if dry_run:
        print("added=1 normalized=1 monthly_generated=0 failed=0")
        return None

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO raw_transactions
               (source_file, source_row_no, transaction_time, transaction_date,
                amount_original, income_amount_original, expense_amount_original,
                amount_cents, direction_raw, account, category_original,
                subcategory_original, merchant, note, project, tags,
                raw_payload, raw_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "manual_entry",
                0,
                transaction_date,
                transaction_date,
                amount_original,
                amount_original if cashflow_direction == "inflow" else "",
                amount_original if cashflow_direction == "outflow" else "",
                amount_cents,
                direction_raw,
                account,
                category_l1,
                category_l2,
                "",
                description,
                "",
                "",
                json.dumps(payload, ensure_ascii=False),
                raw_hash,
            ),
        )
        raw_id = cursor.lastrowid
        cursor.execute(
            """INSERT INTO normalized_transactions
               (raw_transaction_id, transaction_date, year, month,
                amount_cents, cashflow_direction, financial_type,
                category_l1, category_l2, account, counterparty, description,
                is_large_amount, is_internal_transfer, is_debt_related,
                is_asset_related, is_investment_related,
                confidence, review_status,
                manual_financial_type, manual_category_l1, manual_category_l2,
                manual_cashflow_direction, manual_note, manual_updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
            (
                raw_id,
                transaction_date,
                year,
                month,
                amount_cents,
                cashflow_direction,
                financial_type,
                category_l1,
                category_l2,
                account,
                "",
                description,
                1 if amount_cents >= 1_000_000 else 0,
                1 if financial_type == "internal_transfer" else 0,
                1 if financial_type in {"debt_payment", "debt_inflow"} else 0,
                1 if financial_type in {"asset_purchase", "asset_sale"} else 0,
                1 if financial_type in {"investment_outflow", "investment_inflow"} else 0,
                1.0,
                "approved",
                financial_type,
                category_l1 or None,
                category_l2 or None,
                cashflow_direction,
                "manual_entry",
            ),
        )
        conn.commit()
        return int(raw_id)
    finally:
        conn.close()


def add_and_refresh_monthly(*args, **kwargs) -> int | None:
    raw_id = add_manual_transaction(*args, **kwargs)
    if kwargs.get("dry_run"):
        return raw_id

    from app.scripts.generate_monthly_cashflow import generate_monthly_cashflow

    db_path = args[0] if args else kwargs["db_path"]
    generate_monthly_cashflow(Path(db_path))
    return raw_id


def main() -> None:
    parser = argparse.ArgumentParser(description="手动记录一笔收入或支出")
    parser.add_argument("text", nargs="*", help="自由文本，例如：今天 68 午饭 外卖")
    parser.add_argument("--db", required=True, help="SQLite 数据库路径")
    parser.add_argument("--date", help="交易日期 YYYY-MM-DD")
    parser.add_argument("--amount", help="金额，单位元")
    parser.add_argument("--direction", choices=sorted(DIRECTIONS), help="现金流方向")
    parser.add_argument("--type", choices=sorted(FINANCIAL_TYPES), help="财务类型")
    parser.add_argument("--account", default="", help="账户")
    parser.add_argument("--category-l1", default="", help="一级分类")
    parser.add_argument("--category-l2", default="", help="二级分类")
    parser.add_argument("--dry-run", action="store_true", help="仅预览，不写入数据库")
    parser.add_argument("--verbose", action="store_true", help="输出详细日志")
    args = parser.parse_args()

    freeform = " ".join(args.text).strip()
    try:
        if args.date and args.amount and args.direction and args.type:
            parsed = ParsedTransaction(
                args.date,
                parse_amount_cents(args.amount),
                args.direction,
                args.type,
                freeform or "手动记录",
            )
        elif freeform:
            parsed = parse_freeform_transaction(freeform)
        else:
            raise ValueError("请提供自由文本，或同时提供 --date --amount --direction --type")

        add_and_refresh_monthly(
            Path(args.db),
            parsed.transaction_date,
            parsed.amount_cents,
            parsed.cashflow_direction,
            parsed.financial_type,
            parsed.description,
            account=args.account,
            category_l1=args.category_l1,
            category_l2=args.category_l2,
            dry_run=args.dry_run,
            verbose=args.verbose,
        )
    except Exception as exc:
        print(f"错误: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
