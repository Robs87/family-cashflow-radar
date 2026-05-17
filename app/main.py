#!/usr/bin/env python3
"""Minimal local web dashboard for Family Cashflow Radar."""

import argparse
import contextlib
import html
import io
import sqlite3
import sys
from datetime import date
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.scripts.classify import classify
from app.scripts.add_transaction import add_manual_transaction, parse_amount_cents
from app.scripts.generate_monthly_cashflow import generate_monthly_cashflow
from app.scripts.import_csv import import_csv
from app.scripts.normalize import normalize
from app.scripts.recurring import (
    add_mortgage_prepayment,
    create_fixed_bill_template,
    create_mortgage_template,
    generate_due_recurring_bills,
    update_fixed_bill_template,
    update_mortgage_template,
    update_mortgage_prepayment,
)
from app.scripts.schema_migrations import ensure_v02_schema


DEFAULT_DB = Path("data/processed/cashflow.db")
DEFAULT_RAW_INPUT = Path("data/raw")
SCHEMA_SQL = Path(__file__).resolve().parent / "db" / "schema.sql"
SEED_RULES_SQL = Path(__file__).resolve().parent / "db" / "seed_rules.sql"
FINANCIAL_TYPE_OPTIONS = [
    ("stable_income", "稳定收入"),
    ("one_time_income", "一次性收入"),
    ("living_expense", "日常生活支出"),
    ("fixed_expense", "固定刚性支出"),
    ("debt_payment", "债务还款"),
    ("debt_inflow", "借入资金"),
    ("asset_purchase", "资产购入"),
    ("asset_sale", "资产出售"),
    ("investment_outflow", "投资流出"),
    ("investment_inflow", "投资流入"),
    ("internal_transfer", "内部转账"),
    ("credit_card_payment", "信用卡还款"),
    ("refund", "退款"),
    ("reimbursable_expense", "工作垫付"),
    ("reimbursement_income", "报销回款"),
    ("historical_debt_asset_event", "历史债务资产事件"),
    ("unknown", "unknown"),
]
DIRECTION_OPTIONS = [
    ("inflow", "流入"),
    ("outflow", "流出"),
    ("neutral", "中性"),
]


def _format_yuan(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents_abs = abs(int(cents or 0))
    return f"{sign}{cents_abs // 100:,}.{cents_abs % 100:02d}"


def _month_label(row: sqlite3.Row) -> str:
    return f"{row['year']}-{row['month']:02d}"


def _direction_label(value: str) -> str:
    return dict(DIRECTION_OPTIONS).get(value, value or "")


def _financial_type_label(value: str) -> str:
    return dict(FINANCIAL_TYPE_OPTIONS).get(value, value or "")


def _fetch_dashboard_data(db_path: Path) -> dict:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        latest_month = conn.execute(
            """SELECT *
               FROM monthly_cashflow
               ORDER BY year DESC, month DESC
               LIMIT 1"""
        ).fetchone()
        trend = conn.execute(
            """SELECT year, month, net_operating_cashflow_cents
               FROM monthly_cashflow
               ORDER BY year DESC, month DESC
               LIMIT 12"""
        ).fetchall()
        review = conn.execute(
            """SELECT
                  SUM(CASE WHEN COALESCE(manual_financial_type, financial_type) = 'unknown' THEN 1 ELSE 0 END) AS unknown_count,
                  SUM(CASE WHEN review_status = 'pending' THEN 1 ELSE 0 END) AS pending_count
               FROM normalized_transactions"""
        ).fetchone()
        review_transactions = conn.execute(
            """SELECT id,
                      transaction_date,
                      amount_cents,
                      COALESCE(manual_cashflow_direction, cashflow_direction) AS effective_direction,
                      COALESCE(manual_financial_type, financial_type) AS effective_financial_type,
                      account,
                      counterparty,
                      description,
                      review_status
               FROM normalized_transactions
               WHERE review_status = 'pending'
                  OR COALESCE(manual_financial_type, financial_type) = 'unknown'
               ORDER BY transaction_date DESC, id DESC
               LIMIT 20"""
        ).fetchall()
        recent_transactions = conn.execute(
            """SELECT transaction_date,
                      amount_cents,
                      COALESCE(manual_cashflow_direction, cashflow_direction) AS effective_direction,
                      COALESCE(manual_financial_type, financial_type) AS effective_financial_type,
                      category_l1,
                      category_l2,
                      description
               FROM normalized_transactions
               ORDER BY transaction_date DESC, id DESC
               LIMIT 8"""
        ).fetchall()
        expense_breakdown = []
        if latest_month:
            expense_breakdown = conn.execute(
                """SELECT COALESCE(manual_financial_type, financial_type) AS effective_financial_type,
                          COALESCE(category_l2, category_l1, '未分类') AS category,
                          SUM(amount_cents) AS amount_cents,
                          COUNT(*) AS transaction_count
                   FROM normalized_transactions
                   WHERE year = ?
                     AND month = ?
                     AND COALESCE(manual_cashflow_direction, cashflow_direction) = 'outflow'
                     AND COALESCE(manual_financial_type, financial_type) IN (
                        'living_expense', 'fixed_expense', 'debt_payment',
                        'investment_outflow', 'asset_purchase'
                     )
                   GROUP BY effective_financial_type, category
                   ORDER BY amount_cents DESC
                   LIMIT 8""",
                (latest_month["year"], latest_month["month"]),
            ).fetchall()
        recurring_templates = conn.execute(
            """SELECT t.id,
                      t.name,
                      t.bill_type,
                      t.amount_cents,
                      t.category_l2,
                      t.account,
                      t.start_date,
                      t.end_date,
                      t.day_of_month,
                      t.enabled,
                      d.principal_initial_cents,
                      d.interest_rate,
                      d.lender,
                      (
                        SELECT COUNT(*)
                        FROM mortgage_repayment_schedule s
                        WHERE s.recurring_template_id = t.id
                      ) AS schedule_count,
                      EXISTS (
                        SELECT 1
                        FROM recurring_bill_instances i
                        WHERE i.recurring_template_id = t.id
                      ) AS has_generated
               FROM recurring_bill_templates t
               LEFT JOIN debts d ON d.id = t.debt_id
               ORDER BY t.enabled DESC, t.id DESC
               LIMIT 12"""
        ).fetchall()
        mortgage_templates = conn.execute(
            """SELECT id, name
               FROM recurring_bill_templates
               WHERE enabled = 1 AND bill_type = 'mortgage'
               ORDER BY id DESC"""
        ).fetchall()
        upcoming_bills = conn.execute(
            """SELECT t.name,
                      t.bill_type,
                      s.due_date,
                      s.payment_cents AS amount_cents,
                      s.principal_cents,
                      s.interest_cents,
                      EXISTS (
                        SELECT 1
                        FROM recurring_bill_instances i
                        WHERE i.recurring_template_id = t.id
                          AND i.due_date = s.due_date
                      ) AS generated
               FROM mortgage_repayment_schedule s
               JOIN recurring_bill_templates t ON t.id = s.recurring_template_id
               WHERE t.enabled = 1
               ORDER BY s.due_date ASC
               LIMIT 6"""
        ).fetchall()
        debt_split_summary = conn.execute(
            """SELECT COALESCE(SUM(principal_cents), 0) AS principal_cents,
                      COALESCE(SUM(interest_cents), 0) AS interest_cents,
                      COALESCE(SUM(fee_cents), 0) AS fee_cents
               FROM debt_payment_splits"""
        ).fetchone()
        prepayment_events = conn.execute(
            """SELECT e.id,
                      e.prepayment_date,
                      e.amount_cents,
                      e.effect_type,
                      e.remaining_principal_before_cents,
                      e.remaining_principal_after_cents,
                      e.generated_normalized_transaction_id,
                      e.note,
                      t.name
               FROM mortgage_prepayment_events e
               JOIN recurring_bill_templates t ON t.id = e.recurring_template_id
               ORDER BY e.prepayment_date DESC, e.id DESC
               LIMIT 8"""
        ).fetchall()
    finally:
        conn.close()

    return {
        "latest_month": dict(latest_month) if latest_month else None,
        "trend": [dict(row) for row in reversed(trend)],
        "unknown_count": int((review["unknown_count"] if review else 0) or 0),
        "pending_count": int((review["pending_count"] if review else 0) or 0),
        "review_transactions": [dict(row) for row in review_transactions],
        "recent_transactions": [dict(row) for row in recent_transactions],
        "expense_breakdown": [dict(row) for row in expense_breakdown],
        "recurring_templates": [dict(row) for row in recurring_templates],
        "mortgage_templates": [dict(row) for row in mortgage_templates],
        "upcoming_bills": [dict(row) for row in upcoming_bills],
        "debt_split_summary": dict(debt_split_summary) if debt_split_summary else {},
        "prepayment_events": [dict(row) for row in prepayment_events],
    }


def _ensure_database_initialized(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
        columns = {row[1] for row in conn.execute("PRAGMA table_info(mortgage_prepayment_events)").fetchall()}
        if "replaced_schedule_json" not in columns:
            conn.execute("ALTER TABLE mortgage_prepayment_events ADD COLUMN replaced_schedule_json TEXT")

        rules_count = conn.execute("SELECT COUNT(*) FROM classification_rules").fetchone()[0]
        if rules_count == 0:
            conn.executescript(SEED_RULES_SQL.read_text(encoding="utf-8"))
        ensure_v02_schema(conn)

        conn.commit()
    finally:
        conn.close()


def _run_step(label: str, func, *args) -> dict:
    stdout = io.StringIO()
    stderr = io.StringIO()
    exit_code = 0
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            func(*args)
        except SystemExit as exc:
            exit_code = int(exc.code or 0)
        except Exception as exc:
            exit_code = 1
            print(f"{type(exc).__name__}: {exc}", file=stderr)

    return {
        "label": label,
        "ok": exit_code == 0,
        "exit_code": exit_code,
        "stdout": stdout.getvalue().strip(),
        "stderr": stderr.getvalue().strip(),
    }


def run_refresh_pipeline(db_path: Path, input_path: Path = DEFAULT_RAW_INPUT) -> dict:
    started_steps = []
    try:
        _ensure_database_initialized(db_path)
    except Exception as exc:
        return {
            "ok": False,
            "steps": [
                {
                    "label": "初始化数据库",
                    "ok": False,
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": f"{type(exc).__name__}: {exc}",
                }
            ],
        }

    steps = [
        ("导入 CSV", import_csv, db_path, input_path),
        ("标准化交易", normalize, db_path),
        ("规则分类", classify, db_path),
        ("生成月度现金流", generate_monthly_cashflow, db_path),
    ]
    for label, func, *args in steps:
        result = _run_step(label, func, *args)
        started_steps.append(result)
        if not result["ok"]:
            break

    return {"ok": all(step["ok"] for step in started_steps), "steps": started_steps}


def save_manual_override(
    db_path: Path,
    transaction_id: int,
    financial_type: str,
    cashflow_direction: str,
) -> dict:
    allowed_types = {value for value, _label in FINANCIAL_TYPE_OPTIONS}
    allowed_directions = {value for value, _label in DIRECTION_OPTIONS}
    if financial_type not in allowed_types:
        return {"ok": False, "message": f"不支持的财务类型: {financial_type}"}
    if cashflow_direction not in allowed_directions:
        return {"ok": False, "message": f"不支持的现金流方向: {cashflow_direction}"}

    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.execute(
            """UPDATE normalized_transactions
               SET manual_financial_type = ?,
                   manual_cashflow_direction = ?,
                   review_status = 'approved',
                   manual_updated_at = CURRENT_TIMESTAMP,
                   updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (financial_type, cashflow_direction, transaction_id),
        )
        if cursor.rowcount == 0:
            return {"ok": False, "message": f"未找到交易: {transaction_id}"}
        conn.commit()
    finally:
        conn.close()

    result = _run_step("重新生成月度现金流", generate_monthly_cashflow, db_path)
    if not result["ok"]:
        return {"ok": False, "message": result["stderr"] or result["stdout"] or "月度现金流重新生成失败"}
    return {"ok": True, "message": "人工修正已保存"}


def save_new_transaction(
    db_path: Path,
    transaction_date: str,
    amount_yuan: str,
    cashflow_direction: str,
    financial_type: str,
    description: str,
    account: str = "",
    category_l1: str = "",
    category_l2: str = "",
) -> dict:
    allowed_types = {value for value, _label in FINANCIAL_TYPE_OPTIONS}
    allowed_directions = {value for value, _label in DIRECTION_OPTIONS}
    if financial_type not in allowed_types:
        return {"ok": False, "message": f"不支持的财务类型: {financial_type}"}
    if cashflow_direction not in allowed_directions:
        return {"ok": False, "message": f"不支持的现金流方向: {cashflow_direction}"}
    if not description.strip():
        return {"ok": False, "message": "请填写这笔记录的说明"}

    try:
        amount_cents = parse_amount_cents(amount_yuan)
        add_manual_transaction(
            db_path,
            transaction_date,
            amount_cents,
            cashflow_direction,
            financial_type,
            description.strip(),
            account=account.strip(),
            category_l1=category_l1.strip(),
            category_l2=category_l2.strip(),
        )
    except Exception as exc:
        return {"ok": False, "message": str(exc)}

    result = _run_step("重新生成月度现金流", generate_monthly_cashflow, db_path)
    if not result["ok"]:
        return {"ok": False, "message": result["stderr"] or result["stdout"] or "月度现金流重新生成失败"}
    return {"ok": True, "message": "新记录已保存"}


def save_mortgage_template(
    db_path: Path,
    name: str,
    principal_yuan: str,
    annual_rate: str,
    term_months: str,
    start_date: str,
    day_of_month: str,
    account: str = "",
    lender: str = "",
) -> dict:
    try:
        if not name.strip():
            return {"ok": False, "message": "请填写房贷名称"}
        template_id = create_mortgage_template(
            db_path,
            name.strip(),
            parse_amount_cents(principal_yuan),
            Decimal(annual_rate),
            int(term_months),
            start_date,
            int(day_of_month),
            account=account.strip(),
            lender=lender.strip(),
        )
    except Exception as exc:
        return {"ok": False, "message": str(exc)}
    return {"ok": True, "message": f"房贷模板已保存，还款计划已生成: #{template_id}"}


def save_fixed_bill_template(
    db_path: Path,
    name: str,
    amount_yuan: str,
    start_date: str,
    day_of_month: str,
    category_l2: str,
    account: str = "",
    end_date: str = "",
) -> dict:
    try:
        if not name.strip():
            return {"ok": False, "message": "请填写账单名称"}
        template_id = create_fixed_bill_template(
            db_path,
            name.strip(),
            parse_amount_cents(amount_yuan),
            start_date,
            int(day_of_month),
            category_l2.strip() or name.strip(),
            account=account.strip(),
            end_date=end_date.strip() or None,
        )
    except Exception as exc:
        return {"ok": False, "message": str(exc)}
    return {"ok": True, "message": f"固定账单模板已保存: #{template_id}"}


def update_saved_mortgage_template(
    db_path: Path,
    template_id: str,
    name: str,
    principal_yuan: str,
    annual_rate: str,
    term_months: str,
    start_date: str,
    day_of_month: str,
    account: str = "",
    lender: str = "",
) -> dict:
    try:
        if not name.strip():
            return {"ok": False, "message": "请填写房贷名称"}
        update_mortgage_template(
            db_path,
            int(template_id),
            name.strip(),
            parse_amount_cents(principal_yuan),
            Decimal(annual_rate),
            int(term_months),
            start_date,
            int(day_of_month),
            account=account.strip(),
            lender=lender.strip(),
        )
    except Exception as exc:
        return {"ok": False, "message": str(exc)}
    return {"ok": True, "message": "房贷模板已更新，还款计划已重算"}


def update_saved_fixed_bill_template(
    db_path: Path,
    template_id: str,
    name: str,
    amount_yuan: str,
    start_date: str,
    day_of_month: str,
    category_l2: str,
    account: str = "",
    end_date: str = "",
) -> dict:
    try:
        if not name.strip():
            return {"ok": False, "message": "请填写账单名称"}
        update_fixed_bill_template(
            db_path,
            int(template_id),
            name.strip(),
            parse_amount_cents(amount_yuan),
            start_date,
            int(day_of_month),
            category_l2.strip() or name.strip(),
            account=account.strip(),
            end_date=end_date.strip() or None,
        )
    except Exception as exc:
        return {"ok": False, "message": str(exc)}
    return {"ok": True, "message": "固定账单模板已更新"}


def run_recurring_generation(db_path: Path, as_of: str = "") -> dict:
    try:
        result = generate_due_recurring_bills(db_path, as_of=as_of.strip() or None)
    except Exception as exc:
        return {"ok": False, "message": str(exc)}
    if result.failed:
        return {
            "ok": False,
            "message": f"自动记账部分失败: generated={result.generated} skipped_existing={result.skipped_existing} failed={result.failed}",
        }
    return {
        "ok": True,
        "message": f"自动记账完成: generated={result.generated} skipped_existing={result.skipped_existing}",
    }


def save_mortgage_prepayment(
    db_path: Path,
    template_id: str,
    prepayment_date: str,
    amount_yuan: str,
    effect_type: str,
    note: str = "",
) -> dict:
    try:
        event_id = add_mortgage_prepayment(
            db_path,
            int(template_id),
            prepayment_date,
            parse_amount_cents(amount_yuan),
            effect_type=effect_type,
            note=note.strip(),
        )
    except Exception as exc:
        return {"ok": False, "message": str(exc)}
    return {"ok": True, "message": f"提前还贷已保存，后续还款计划已重算: #{event_id}"}


def update_saved_mortgage_prepayment(
    db_path: Path,
    event_id: str,
    prepayment_date: str,
    amount_yuan: str,
    effect_type: str,
    note: str = "",
) -> dict:
    try:
        new_event_id = update_mortgage_prepayment(
            db_path,
            int(event_id),
            prepayment_date,
            parse_amount_cents(amount_yuan),
            effect_type=effect_type,
            note=note.strip(),
        )
    except Exception as exc:
        return {"ok": False, "message": str(exc)}
    return {"ok": True, "message": f"提前还贷事件已更新，后续还款计划已重算: #{new_event_id}"}


def _metric(label: str, value: str, tone: str = "neutral") -> str:
    return (
        f'<section class="metric metric-{tone}">'
        f"<span>{html.escape(label)}</span>"
        f"<strong>{html.escape(value)}</strong>"
        "</section>"
    )


def _render_trend_bars(trend: list[dict]) -> str:
    if not trend:
        return '<p class="empty">暂无月度趋势数据</p>'

    max_abs = max(abs(row["net_operating_cashflow_cents"] or 0) for row in trend) or 1
    bars = []
    for row in trend:
        value = int(row["net_operating_cashflow_cents"] or 0)
        width = max(4, round(abs(value) / max_abs * 100))
        tone = "positive" if value >= 0 else "negative"
        bars.append(
            '<div class="trend-row">'
            f'<span class="trend-month">{html.escape(_month_label(row))}</span>'
            '<div class="trend-track">'
            f'<div class="trend-bar {tone}" style="width:{width}%"></div>'
            "</div>"
            f'<span class="trend-value">{html.escape(_format_yuan(value))}</span>'
            "</div>"
        )
    return "\n".join(bars)


def _render_pipeline_result(result: dict | None) -> str:
    if not result:
        return ""

    tone = "success" if result["ok"] else "failure"
    title = "刷新完成" if result["ok"] else "刷新失败"
    step_html = []
    for step in result["steps"]:
        status = "完成" if step["ok"] else "失败"
        output = "\n".join(part for part in (step["stdout"], step["stderr"]) if part)
        step_html.append(
            '<li class="run-step">'
            f'<span><strong>{html.escape(step["label"])}</strong><em>{html.escape(status)}</em></span>'
            f'<code>{html.escape(output or "无输出")}</code>'
            "</li>"
        )

    return (
        f'<section class="run-result run-{tone}">'
        f"<h2>{html.escape(title)}</h2>"
        f'<ol>{"".join(step_html)}</ol>'
        "</section>"
    )


def _render_notice(notice: dict | None) -> str:
    if not notice:
        return ""
    tone = "success" if notice["ok"] else "failure"
    return f'<section class="notice notice-{tone}">{html.escape(notice["message"])}</section>'


def _render_options(options: list[tuple[str, str]], selected: str) -> str:
    parts = []
    for value, label in options:
        selected_attr = " selected" if value == selected else ""
        parts.append(f'<option value="{html.escape(value)}"{selected_attr}>{html.escape(label)}</option>')
    return "".join(parts)


def _render_review_transactions(rows: list[dict]) -> str:
    if not rows:
        return '<p class="empty">暂无待审核交易</p>'

    rendered_rows = []
    for row in rows:
        title = " / ".join(
            part
            for part in (
                str(row.get("account") or ""),
                str(row.get("counterparty") or ""),
                str(row.get("description") or ""),
            )
            if part
        ) or "无描述"
        rendered_rows.append(
            '<form class="review-row" method="post" action="/actions/manual-override#review-panel" data-preserve-scroll="review">'
            f'<input type="hidden" name="transaction_id" value="{int(row["id"])}">'
            '<div class="review-main">'
            f'<strong>{html.escape(title)}</strong>'
            f'<span>{html.escape(str(row["transaction_date"] or ""))} · '
            f'{html.escape(_direction_label(row["effective_direction"]))} · '
            f'{html.escape(_format_yuan(row["amount_cents"]))} 元</span>'
            "</div>"
            f'<select name="financial_type">{_render_options(FINANCIAL_TYPE_OPTIONS, row["effective_financial_type"])}</select>'
            f'<select name="cashflow_direction">{_render_options(DIRECTION_OPTIONS, row["effective_direction"])}</select>'
            '<button type="submit">保存</button>'
            "</form>"
        )
    return "\n".join(rendered_rows)


def _render_add_transaction_form() -> str:
    today = date.today().isoformat()
    return (
        '<form class="entry-form" method="post" action="/actions/add-transaction">'
        f'<label>日期<input type="date" name="transaction_date" value="{html.escape(today)}" required></label>'
        '<label>金额<input type="number" name="amount_yuan" min="0" step="0.01" placeholder="68.00" required></label>'
        f'<label>方向<select name="cashflow_direction">{_render_options(DIRECTION_OPTIONS, "outflow")}</select></label>'
        f'<label>类型<select name="financial_type">{_render_options(FINANCIAL_TYPE_OPTIONS, "living_expense")}</select></label>'
        '<label class="entry-wide">说明<input type="text" name="description" placeholder="午饭 外卖" required></label>'
        '<label>账户<input type="text" name="account" placeholder="可选"></label>'
        '<label>一级分类<input type="text" name="category_l1" placeholder="可选"></label>'
        '<label>二级分类<input type="text" name="category_l2" placeholder="可选"></label>'
        '<button type="submit">记录</button>'
        "</form>"
    )


def _render_recent_transactions(rows: list[dict]) -> str:
    if not rows:
        return '<p class="empty">暂无最近记录</p>'

    items = []
    for row in rows:
        title = row.get("description") or row.get("category_l2") or row.get("category_l1") or "无描述"
        direction = row["effective_direction"]
        prefix = "+" if direction == "inflow" else "-" if direction == "outflow" else ""
        items.append(
            '<div class="recent-row">'
            '<div>'
            f'<strong>{html.escape(str(title))}</strong>'
            f'<span>{html.escape(str(row["transaction_date"] or ""))} · '
            f'{html.escape(_financial_type_label(row["effective_financial_type"]))}</span>'
            "</div>"
            f'<b class="amount amount-{html.escape(direction)}">{html.escape(prefix + _format_yuan(row["amount_cents"]))} 元</b>'
            "</div>"
        )
    return "\n".join(items)


def _render_expense_breakdown(rows: list[dict]) -> str:
    if not rows:
        return '<p class="empty">暂无本月支出拆分</p>'

    total = sum(int(row["amount_cents"] or 0) for row in rows) or 1
    parts = []
    for row in rows:
        amount = int(row["amount_cents"] or 0)
        width = max(4, round(amount / total * 100))
        label = f"{_financial_type_label(row['effective_financial_type'])} / {row['category']}"
        parts.append(
            '<div class="breakdown-row">'
            '<div class="breakdown-title">'
            f'<span>{html.escape(label)}</span>'
            f'<strong>{html.escape(_format_yuan(amount))} 元</strong>'
            "</div>"
            '<div class="trend-track">'
            f'<div class="trend-bar negative" style="width:{width}%"></div>'
            "</div>"
            "</div>"
        )
    return "\n".join(parts)


def _render_recurring_forms() -> str:
    today = date.today().isoformat()
    return (
        '<div class="recurring-forms">'
        '<form class="entry-form compact-form" method="post" action="/actions/add-mortgage-template">'
        '<h3>房贷模板</h3>'
        '<label>名称<input name="name" value="房贷" required></label>'
        '<label>贷款金额<input type="number" name="principal_yuan" min="0" step="0.01" placeholder="1000000" required></label>'
        '<label>年利率 %<input type="number" name="annual_rate" min="0" step="0.0001" placeholder="3.2" required></label>'
        '<label>期数<input type="number" name="term_months" min="1" step="1" placeholder="360" required></label>'
        f'<label>首期日期<input type="date" name="start_date" value="{html.escape(today)}" required></label>'
        '<label>还款日<input type="number" name="day_of_month" min="1" max="31" step="1" placeholder="15" required></label>'
        '<label>账户<input name="account" placeholder="可选"></label>'
        '<label>贷款方<input name="lender" placeholder="可选"></label>'
        '<button type="submit">保存房贷</button>'
        "</form>"
        '<form class="entry-form compact-form" method="post" action="/actions/add-fixed-bill-template">'
        '<h3>固定账单</h3>'
        '<label>名称<input name="name" placeholder="宽带 / 电话费" required></label>'
        '<label>金额<input type="number" name="amount_yuan" min="0" step="0.01" placeholder="199" required></label>'
        f'<label>开始日期<input type="date" name="start_date" value="{html.escape(today)}" required></label>'
        '<label>扣款日<input type="number" name="day_of_month" min="1" max="31" step="1" placeholder="1" required></label>'
        '<label>分类<input name="category_l2" placeholder="宽带 / 电话费" required></label>'
        '<label>账户<input name="account" placeholder="可选"></label>'
        '<label>结束日期<input type="date" name="end_date"></label>'
        '<button type="submit">保存账单</button>'
        "</form>"
        '<form class="generate-form" method="post" action="/actions/generate-recurring">'
        f'<label>生成到<input type="date" name="as_of" value="{html.escape(today)}"></label>'
        '<button type="submit">运行自动记账</button>'
        "</form>"
        "</div>"
    )


def _render_mortgage_prepayment_form(mortgage_templates: list[dict]) -> str:
    if not mortgage_templates:
        return '<p class="empty">先创建房贷模板，再添加提前还款计划。</p>'
    today = date.today().isoformat()
    options = "".join(
        f'<option value="{int(row["id"])}">{html.escape(row["name"])}</option>'
        for row in mortgage_templates
    )
    effect_options = _render_options(
        [
            ("reduce_term", "月供不变，缩短期限"),
            ("reduce_payment", "期限不变，降低月供"),
        ],
        "reduce_term",
    )
    return (
        '<form class="entry-form compact-form" method="post" action="/actions/add-mortgage-prepayment">'
        '<h3>提前还贷</h3>'
        f'<label>房贷<select name="template_id">{options}</select></label>'
        f'<label>还款日期<input type="date" name="prepayment_date" value="{html.escape(today)}" required></label>'
        '<label>金额<input type="number" name="amount_yuan" min="0" step="0.01" placeholder="100000" required></label>'
        f'<label>处理方式<select name="effect_type">{effect_options}</select></label>'
        '<label class="entry-wide">备注<input name="note" placeholder="可选"></label>'
        '<button type="submit">保存提前还贷</button>'
        "</form>"
    )


def _prepayment_effect_label(value: str) -> str:
    labels = {
        "reduce_term": "缩短期限",
        "reduce_payment": "降低月供",
    }
    return labels.get(value, value)


def _render_prepayment_events(rows: list[dict]) -> str:
    if not rows:
        return '<p class="empty">暂无提前还贷事件</p>'
    parts = []
    for row in rows:
        status = "已记账" if row["generated_normalized_transaction_id"] else "待生成"
        edit = (
            '<span class="muted-action">已锁定</span>'
            if row["generated_normalized_transaction_id"]
            else f'<a class="button-link" href="/?edit_prepayment={int(row["id"])}#prepayment-edit">修改</a>'
        )
        parts.append(
            '<div class="list-row">'
            '<div class="list-main">'
            f'<strong>{html.escape(row["prepayment_date"])} · {html.escape(row["name"])}</strong>'
            f'<span>{html.escape(_prepayment_effect_label(row["effect_type"]))} · {html.escape(status)} · '
            f'还后本金 {_format_yuan(row["remaining_principal_after_cents"])} 元</span>'
            "</div>"
            f'<b class="amount amount-outflow">{html.escape(_format_yuan(row["amount_cents"]))} 元</b>'
            f"{edit}"
            "</div>"
        )
    return "\n".join(parts)


def _render_recurring_templates(rows: list[dict]) -> str:
    if not rows:
        return '<p class="empty">暂无周期账单模板</p>'
    parts = []
    for row in rows:
        kind = "房贷" if row["bill_type"] == "mortgage" else "固定账单"
        status = "已生成过账单" if row.get("has_generated") else "待生成"
        edit = (
            '<span class="muted-action">已锁定</span>'
            if row.get("has_generated")
            else f'<a class="button-link" href="/?edit_template={int(row["id"])}#template-edit">修改</a>'
        )
        if row["bill_type"] == "mortgage":
            detail = (
                f'{kind} · 每月 {int(row["day_of_month"])} 日 · '
                f'贷款金额 {_format_yuan(row.get("principal_initial_cents") or 0)} 元 · {status}'
            )
        else:
            detail = f'{kind} · 每月 {int(row["day_of_month"])} 日 · {html.escape(row.get("category_l2") or "")} · {status}'
        parts.append(
            '<div class="list-row">'
            '<div class="list-main">'
            f'<strong>{html.escape(row["name"])}</strong>'
            f'<span>{detail}</span>'
            "</div>"
            f'<b class="amount amount-outflow">{html.escape(_format_yuan(row["amount_cents"] or 0))} 元</b>'
            f"{edit}"
            "</div>"
        )
    return "\n".join(parts)


def _render_template_edit_form(row: dict | None) -> str:
    if not row:
        return ""
    disabled = " disabled" if row.get("has_generated") else ""
    if row["bill_type"] == "mortgage":
        principal = _format_yuan(row.get("principal_initial_cents") or 0).replace(",", "")
        rate = "" if row.get("interest_rate") is None else str(row["interest_rate"])
        term_months = int(row.get("schedule_count") or 0)
        return (
            '<section class="panel" id="template-edit">'
            '<h2>修改周期账单</h2>'
            '<form class="template-edit-form" method="post" action="/actions/update-mortgage-template">'
            f'<input type="hidden" name="template_id" value="{int(row["id"])}">'
            f'<label>名称<input name="name" value="{html.escape(row["name"])}" required{disabled}></label>'
            f'<label>贷款金额<input type="number" name="principal_yuan" min="0" step="0.01" value="{html.escape(principal)}" required{disabled}></label>'
            f'<label>年利率 %<input type="number" name="annual_rate" min="0" step="0.0001" value="{html.escape(rate)}" required{disabled}></label>'
            f'<label>期数<input type="number" name="term_months" min="1" step="1" value="{term_months}" required{disabled}></label>'
            f'<label>首期日期<input type="date" name="start_date" value="{html.escape(row["start_date"] or "")}" required{disabled}></label>'
            f'<label>还款日<input type="number" name="day_of_month" min="1" max="31" step="1" value="{int(row["day_of_month"])}" required{disabled}></label>'
            f'<label>账户<input name="account" value="{html.escape(row.get("account") or "")}"{disabled}></label>'
            f'<label>贷款方<input name="lender" value="{html.escape(row.get("lender") or "")}"{disabled}></label>'
            f'<button type="submit"{disabled}>保存修改</button>'
            '<a class="button-link secondary-link" href="/#recurring-list">取消</a>'
            "</form>"
            "</section>"
        )
    amount = _format_yuan(row["amount_cents"] or 0).replace(",", "")
    return (
        '<section class="panel" id="template-edit">'
        '<h2>修改周期账单</h2>'
        '<form class="template-edit-form" method="post" action="/actions/update-fixed-bill-template">'
        f'<input type="hidden" name="template_id" value="{int(row["id"])}">'
        f'<label>名称<input name="name" value="{html.escape(row["name"])}" required{disabled}></label>'
        f'<label>金额<input type="number" name="amount_yuan" min="0" step="0.01" value="{html.escape(amount)}" required{disabled}></label>'
        f'<label>开始日期<input type="date" name="start_date" value="{html.escape(row["start_date"] or "")}" required{disabled}></label>'
        f'<label>扣款日<input type="number" name="day_of_month" min="1" max="31" step="1" value="{int(row["day_of_month"])}" required{disabled}></label>'
        f'<label>分类<input name="category_l2" value="{html.escape(row.get("category_l2") or "")}" required{disabled}></label>'
        f'<label>账户<input name="account" value="{html.escape(row.get("account") or "")}"{disabled}></label>'
        f'<label>结束日期<input type="date" name="end_date" value="{html.escape(row.get("end_date") or "")}"{disabled}></label>'
        f'<button type="submit"{disabled}>保存修改</button>'
        '<a class="button-link secondary-link" href="/#recurring-list">取消</a>'
        "</form>"
        "</section>"
    )


def _render_prepayment_edit_form(row: dict | None) -> str:
    if not row:
        return ""
    disabled = " disabled" if row["generated_normalized_transaction_id"] else ""
    amount = _format_yuan(row["amount_cents"]).replace(",", "")
    effect_options = _render_options(
        [
            ("reduce_term", "月供不变，缩短期限"),
            ("reduce_payment", "期限不变，降低月供"),
        ],
        row["effect_type"],
    )
    return (
        '<section class="panel" id="prepayment-edit">'
        '<h2>修改提前还贷事件</h2>'
        '<form class="template-edit-form" method="post" action="/actions/update-mortgage-prepayment">'
        f'<input type="hidden" name="event_id" value="{int(row["id"])}">'
        f'<label>还款日期<input type="date" name="prepayment_date" value="{html.escape(row["prepayment_date"])}" required{disabled}></label>'
        f'<label>金额<input type="number" name="amount_yuan" min="0" step="0.01" value="{html.escape(amount)}" required{disabled}></label>'
        f'<label>处理方式<select name="effect_type"{disabled}>{effect_options}</select></label>'
        f'<label>备注<input name="note" value="{html.escape(row.get("note") or "")}"{disabled}></label>'
        f'<button type="submit"{disabled}>保存修改</button>'
        '<a class="button-link secondary-link" href="/#prepayment-list">取消</a>'
        "</form>"
        "</section>"
    )


def _render_upcoming_bills(rows: list[dict]) -> str:
    if not rows:
        return '<p class="empty">暂无房贷还款计划</p>'
    parts = []
    for row in rows:
        generated = "已记账" if row["generated"] else "待生成"
        parts.append(
            '<div class="breakdown-row">'
            '<div class="breakdown-title">'
            f'<span>{html.escape(row["due_date"])} · {html.escape(row["name"])} · {html.escape(generated)}</span>'
            f'<strong>{html.escape(_format_yuan(row["amount_cents"]))} 元</strong>'
            "</div>"
            '<div class="split-line">'
            f'<span>本金 {_format_yuan(row["principal_cents"])} 元</span>'
            f'<span>利息 {_format_yuan(row["interest_cents"])} 元</span>'
            "</div>"
            "</div>"
        )
    return "\n".join(parts)


def _render_debt_split_summary(summary: dict) -> str:
    principal = _format_yuan(summary.get("principal_cents", 0))
    interest = _format_yuan(summary.get("interest_cents", 0))
    fee = _format_yuan(summary.get("fee_cents", 0))
    return (
        '<div class="review-list">'
        f'<div class="review-item"><span>累计归还本金</span><strong>{html.escape(principal)}</strong></div>'
        f'<div class="review-item warn"><span>累计支付利息</span><strong>{html.escape(interest)}</strong></div>'
        f'<div class="review-item"><span>累计手续费</span><strong>{html.escape(fee)}</strong></div>'
        "</div>"
    )


def _build_financial_advice(data: dict) -> list[str]:
    latest = data["latest_month"]
    if not latest:
        return ["先连续记录 7 天收入支出，系统才能给出可靠的日常现金流建议。"]

    stable_income = int(latest["stable_income_cents"] or 0)
    living = int(latest["living_expense_cents"] or 0)
    fixed = int(latest["fixed_expense_cents"] or 0)
    debt = int(latest["debt_payment_cents"] or 0)
    net = int(latest["net_operating_cashflow_cents"] or 0)
    unknown_count = int(data["unknown_count"] or 0)

    advice = []
    if stable_income == 0:
        advice.append("本月还没有稳定收入记录，结余率暂时不能判断；先把工资或固定收入补齐。")
    elif net < 0:
        advice.append("本月基础结余为负，优先检查固定支出、债务还款和高频生活支出，先暂停非必要大额消费。")
    elif net < stable_income * 0.1:
        advice.append("本月基础结余低于稳定收入的 10%，抗风险空间偏薄，建议给日常消费设一个周预算。")
    elif fixed + debt > stable_income * 0.5:
        advice.append("本月基础结余为正，但固定支出和债务还款压力偏高，提前还贷或新增大额支出前先做模拟。")
    else:
        advice.append("本月基础结余为正，现金流结构暂时健康；继续保持实时记录，月底再看是否稳定。")

    if stable_income and fixed + debt > stable_income * 0.5:
        advice.append("固定支出加债务还款超过稳定收入的 50%，这部分是现金流压力核心。")
    if stable_income and living > stable_income * 0.35:
        advice.append("日常生活支出超过稳定收入的 35%，建议重点看餐饮、购物、交通这些高频项。")
    if unknown_count:
        advice.append(f"仍有 {unknown_count} 笔 unknown，建议当天补完分类，超过一周后可解释性会明显下降。")

    return advice[:4]


def _build_cashflow_signal(data: dict) -> dict[str, object]:
    latest = data["latest_month"]
    if not latest:
        return {
            "level": "watch",
            "label": "观察状态",
            "safety_months": None,
            "headline": "当前家庭现金流：观察状态。先连续记录 7 天收入支出，再生成近期消费建议。",
            "reason": "缺少月度现金流数据，系统还不能判断未来 30 天的大额支出风险。",
        }

    stable_income = int(latest["stable_income_cents"] or 0)
    living = int(latest["living_expense_cents"] or 0)
    fixed = int(latest["fixed_expense_cents"] or 0)
    debt = int(latest["debt_payment_cents"] or 0)
    net = int(latest["net_operating_cashflow_cents"] or 0)
    unknown_count = int(data["unknown_count"] or 0)
    pending_count = int(data["pending_count"] or 0)
    required_outflow = fixed + debt
    safety_months = round(net / required_outflow, 1) if required_outflow > 0 and net > 0 else 0.0

    if stable_income <= 0 or net < 0:
        level = "danger"
        label = "危险状态"
        action = "未来 30 天先暂停非必要大额消费，并优先补齐稳定收入、固定支出和债务记录。"
    else:
        net_ratio = net / stable_income
        pressure_ratio = required_outflow / stable_income
        living_ratio = living / stable_income
        if net_ratio < 0.1 or pressure_ratio > 0.65:
            level = "tight"
            label = "偏紧状态"
            action = "未来 30 天不建议新增大额支出，提前还贷和加仓投资都建议暂缓。"
        elif net_ratio < 0.25 or pressure_ratio > 0.5 or living_ratio > 0.35:
            level = "watch"
            label = "观察状态"
            action = "未来 30 天可以正常消费，但新增大额支出、提前还贷和加仓投资需要先做模拟。"
        else:
            level = "safe"
            label = "安全状态"
            action = "未来 30 天可维持正常消费；大额支出仍建议先确认不会把安全垫压到 1 个月以下。"

    confidence_note = ""
    if unknown_count or pending_count:
        confidence_note = f" 当前仍有 {unknown_count} 笔 unknown、{pending_count} 笔 pending，建议可信度会下降。"

    reason = (
        f"本月基础结余 {_format_yuan(net)} 元，固定支出+债务还款 "
        f"{_format_yuan(required_outflow)} 元，代理安全垫约 {safety_months:.1f} 个月。"
        f"{confidence_note}"
    )
    return {
        "level": level,
        "label": label,
        "safety_months": safety_months,
        "headline": f"当前家庭现金流：{label}。{action}",
        "reason": reason,
    }


def _render_cashflow_signal(data: dict) -> str:
    signal = _build_cashflow_signal(data)
    level = html.escape(str(signal["level"]))
    headline = html.escape(str(signal["headline"]))
    reason = html.escape(str(signal["reason"]))
    safety = signal["safety_months"]
    if safety is None:
        safety_html = '<span class="signal-chip">安全垫：待计算</span>'
    else:
        safety_html = f'<span class="signal-chip">安全垫：{float(safety):.1f} 个月</span>'
    return (
        f'<div class="cashflow-signal signal-{level}">'
        f"<strong>{headline}</strong>"
        f"<p>{reason}</p>"
        f"{safety_html}"
        "</div>"
    )


def _render_financial_advice(data: dict) -> str:
    signal = _render_cashflow_signal(data)
    items = "".join(f"<li>{html.escape(item)}</li>" for item in _build_financial_advice(data))
    return f"{signal}<ul class=\"advice-list\">{items}</ul>"


def _render_scroll_restore_script() -> str:
    return """
  <script>
    (() => {
      const key = "family-cashflow-radar.reviewScrollY";
      const saved = sessionStorage.getItem(key);
      if (saved !== null) {
        sessionStorage.removeItem(key);
        const y = Number(saved);
        if (!Number.isNaN(y)) {
          requestAnimationFrame(() => window.scrollTo(0, y));
        }
      }
      for (const form of document.querySelectorAll('form[data-preserve-scroll="review"]')) {
        form.addEventListener("submit", () => {
          sessionStorage.setItem(key, String(window.scrollY));
        });
      }
    })();
  </script>"""


def render_dashboard_html(
    db_path: Path,
    pipeline_result: dict | None = None,
    notice: dict | None = None,
    edit_template_id: int | None = None,
    edit_prepayment_id: int | None = None,
) -> str:
    _ensure_database_initialized(db_path)
    data = _fetch_dashboard_data(db_path)
    latest = data["latest_month"]

    if latest:
        month_label = _month_label(latest)
        stable_income = _format_yuan(latest["stable_income_cents"])
        fixed_expense = _format_yuan(latest["fixed_expense_cents"])
        debt_payment = _format_yuan(latest["debt_payment_cents"])
        net_operating = _format_yuan(latest["net_operating_cashflow_cents"])
        net_tone = "good" if latest["net_operating_cashflow_cents"] >= 0 else "bad"
        metrics = "\n".join(
            [
                _metric("本月稳定收入", f"{stable_income} 元", "good"),
                _metric("本月刚性支出", f"{fixed_expense} 元"),
                _metric("本月债务还款", f"{debt_payment} 元"),
                _metric("本月基础结余", f"{net_operating} 元", net_tone),
            ]
        )
    else:
        month_label = "暂无月份"
        metrics = '<p class="empty">暂无月度现金流数据。请先运行导入、标准化、分类和月度聚合脚本。</p>'

    unknown_count = data["unknown_count"]
    pending_count = data["pending_count"]
    trend_html = _render_trend_bars(data["trend"])
    pipeline_html = _render_pipeline_result(pipeline_result)
    notice_html = _render_notice(notice)
    add_transaction_form_html = _render_add_transaction_form()
    recurring_forms_html = _render_recurring_forms()
    mortgage_prepayment_form_html = _render_mortgage_prepayment_form(data["mortgage_templates"])
    advice_html = _render_financial_advice(data)
    recent_transactions_html = _render_recent_transactions(data["recent_transactions"])
    expense_breakdown_html = _render_expense_breakdown(data["expense_breakdown"])
    recurring_templates_html = _render_recurring_templates(data["recurring_templates"])
    edit_template = next(
        (row for row in data["recurring_templates"] if edit_template_id and int(row["id"]) == edit_template_id),
        None,
    )
    template_edit_html = _render_template_edit_form(edit_template)
    upcoming_bills_html = _render_upcoming_bills(data["upcoming_bills"])
    debt_split_summary_html = _render_debt_split_summary(data["debt_split_summary"])
    prepayment_events_html = _render_prepayment_events(data["prepayment_events"])
    edit_prepayment = next(
        (row for row in data["prepayment_events"] if edit_prepayment_id and int(row["id"]) == edit_prepayment_id),
        None,
    )
    prepayment_edit_html = _render_prepayment_edit_form(edit_prepayment)
    review_transactions_html = _render_review_transactions(data["review_transactions"])

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>家庭现金流雷达</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #1d252d;
      --muted: #65717d;
      --line: #d9e0e7;
      --panel: #ffffff;
      --bg: #f6f8fa;
      --green: #207a50;
      --red: #b33b3b;
      --blue: #2f6690;
      --amber: #8a6200;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.45;
    }}
    header {{
      border-bottom: 1px solid var(--line);
      background: #ffffff;
    }}
    .wrap {{
      width: min(1120px, calc(100% - 32px));
      margin: 0 auto;
    }}
    .topbar {{
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: center;
      padding: 20px 0;
    }}
    h1 {{
      margin: 0;
      font-size: 24px;
      font-weight: 700;
      letter-spacing: 0;
    }}
    .period {{
      color: var(--muted);
      font-size: 14px;
      white-space: nowrap;
    }}
    .actions {{
      display: flex;
      gap: 10px;
      align-items: center;
    }}
    button {{
      min-height: 38px;
      border: 1px solid var(--green);
      border-radius: 8px;
      background: var(--green);
      color: #ffffff;
      font: inherit;
      font-weight: 700;
      padding: 0 14px;
      cursor: pointer;
    }}
    button:active {{ transform: translateY(1px); }}
    main {{
      padding: 22px 0 36px;
    }}
    .metrics {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
    }}
    .metric, .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    .metric {{
      min-height: 104px;
      padding: 16px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      border-top: 4px solid var(--blue);
    }}
    .metric-good {{ border-top-color: var(--green); }}
    .metric-bad {{ border-top-color: var(--red); }}
    .metric span {{
      color: var(--muted);
      font-size: 13px;
    }}
    .metric strong {{
      font-size: 24px;
      line-height: 1.15;
      overflow-wrap: anywhere;
    }}
    .grid {{
      display: grid;
      grid-template-columns: minmax(0, 2fr) minmax(280px, 1fr);
      gap: 14px;
      margin-top: 14px;
    }}
    .entry-grid {{
      display: grid;
      grid-template-columns: minmax(0, 1.35fr) minmax(300px, 0.65fr);
      gap: 14px;
      margin-bottom: 14px;
    }}
    .panel {{
      padding: 18px;
    }}
    h2 {{
      margin: 0 0 16px;
      font-size: 17px;
      letter-spacing: 0;
    }}
    h3 {{
      margin: 0;
      font-size: 14px;
      letter-spacing: 0;
    }}
    .trend-row {{
      display: grid;
      grid-template-columns: 72px minmax(120px, 1fr) 116px;
      gap: 12px;
      align-items: center;
      min-height: 34px;
      font-size: 13px;
    }}
    .trend-month {{ color: var(--muted); }}
    .trend-track {{
      height: 12px;
      border-radius: 6px;
      background: #e7ebef;
      overflow: hidden;
    }}
    .trend-bar {{
      height: 100%;
      border-radius: 6px;
    }}
    .trend-bar.positive {{ background: var(--green); }}
    .trend-bar.negative {{ background: var(--red); }}
    .trend-value {{
      text-align: right;
      font-variant-numeric: tabular-nums;
    }}
    .review-list {{
      display: grid;
      gap: 10px;
    }}
    .review-item {{
      display: flex;
      justify-content: space-between;
      gap: 14px;
      padding: 12px 0;
      border-bottom: 1px solid var(--line);
    }}
    .review-item:last-child {{ border-bottom: 0; }}
    .review-item span {{ color: var(--muted); }}
    .review-item strong {{
      font-size: 22px;
      font-variant-numeric: tabular-nums;
    }}
    .review-item.warn strong {{ color: var(--amber); }}
    .review-transactions {{
      display: grid;
      gap: 8px;
      margin-top: 14px;
    }}
    .entry-form {{
      display: grid;
      grid-template-columns: repeat(4, minmax(120px, 1fr)) 88px;
      gap: 10px;
      align-items: end;
    }}
    .compact-form {{
      grid-template-columns: repeat(4, minmax(110px, 1fr)) 96px;
      padding-top: 12px;
      border-top: 1px solid var(--line);
    }}
    .compact-form:first-child {{
      padding-top: 0;
      border-top: 0;
    }}
    .compact-form h3 {{
      align-self: center;
    }}
    .recurring-forms {{
      display: grid;
      gap: 14px;
    }}
    .generate-form {{
      display: flex;
      gap: 10px;
      align-items: end;
      padding-top: 12px;
      border-top: 1px solid var(--line);
    }}
    .template-edit-form {{
      display: grid;
      grid-template-columns: repeat(3, minmax(110px, 1fr)) 76px;
      gap: 8px;
      align-items: end;
      padding: 12px 0;
      border-bottom: 1px solid var(--line);
    }}
    .template-edit-form:last-child {{
      border-bottom: 0;
      padding-bottom: 0;
    }}
    .template-title {{
      grid-column: 1 / -1;
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: baseline;
    }}
    .template-title span {{
      color: var(--muted);
      font-size: 12px;
    }}
    label {{
      display: grid;
      gap: 5px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
    }}
    input {{
      min-height: 36px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #ffffff;
      color: var(--ink);
      font: inherit;
      padding: 0 9px;
      width: 100%;
    }}
    .entry-wide {{
      grid-column: span 2;
    }}
    .advice-list {{
      margin: 0;
      padding-left: 18px;
      display: grid;
      gap: 9px;
      color: var(--ink);
    }}
    .cashflow-signal {{
      display: grid;
      gap: 8px;
      margin-bottom: 14px;
      padding: 12px;
      border: 1px solid var(--line);
      border-left-width: 5px;
      border-radius: 8px;
      background: #ffffff;
    }}
    .cashflow-signal strong {{
      font-size: 15px;
      line-height: 1.45;
    }}
    .cashflow-signal p {{
      margin: 0;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.5;
    }}
    .signal-chip {{
      width: fit-content;
      border-radius: 999px;
      background: #f3f4f6;
      color: var(--ink);
      padding: 4px 9px;
      font-size: 12px;
      font-weight: 800;
    }}
    .signal-safe {{ border-left-color: #15945b; }}
    .signal-watch {{ border-left-color: #c47a11; }}
    .signal-tight {{ border-left-color: #d97706; }}
    .signal-danger {{ border-left-color: #c2410c; }}
    .recent-list, .breakdown-list {{
      display: grid;
      gap: 10px;
    }}
    .recent-row {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      border-bottom: 1px solid var(--line);
      padding-bottom: 10px;
    }}
    .recent-row:last-child {{ border-bottom: 0; padding-bottom: 0; }}
    .recent-row div {{
      display: grid;
      gap: 2px;
      min-width: 0;
    }}
    .recent-row strong {{
      font-size: 13px;
      overflow-wrap: anywhere;
    }}
    .recent-row span {{
      color: var(--muted);
      font-size: 12px;
    }}
    .list-row {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto auto;
      gap: 10px;
      align-items: center;
      border-bottom: 1px solid var(--line);
      padding: 10px 0;
    }}
    .list-row:last-child {{ border-bottom: 0; }}
    .list-main {{
      display: grid;
      gap: 2px;
      min-width: 0;
    }}
    .list-main strong {{
      font-size: 13px;
      overflow-wrap: anywhere;
    }}
    .list-main span {{
      color: var(--muted);
      font-size: 12px;
    }}
    .button-link {{
      min-height: 34px;
      border: 1px solid var(--green);
      border-radius: 8px;
      color: var(--green);
      background: #ffffff;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 0 12px;
      font-size: 13px;
      font-weight: 700;
      text-decoration: none;
      white-space: nowrap;
    }}
    .secondary-link {{
      border-color: var(--line);
      color: var(--muted);
    }}
    .muted-action {{
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
    }}
    .amount {{
      white-space: nowrap;
      font-variant-numeric: tabular-nums;
    }}
    .amount-inflow {{ color: var(--green); }}
    .amount-outflow {{ color: var(--red); }}
    .amount-neutral {{ color: var(--muted); }}
    .breakdown-row {{
      display: grid;
      gap: 6px;
    }}
    .breakdown-title {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      font-size: 13px;
    }}
    .breakdown-title span {{
      color: var(--muted);
      overflow-wrap: anywhere;
    }}
    .breakdown-title strong {{
      white-space: nowrap;
      font-variant-numeric: tabular-nums;
    }}
    .split-line {{
      display: flex;
      gap: 14px;
      color: var(--muted);
      font-size: 12px;
      font-variant-numeric: tabular-nums;
    }}
    .review-row {{
      display: grid;
      grid-template-columns: minmax(180px, 1fr) 160px 120px 72px;
      gap: 8px;
      align-items: center;
      padding: 10px 0;
      border-top: 1px solid var(--line);
    }}
    .review-main {{
      display: grid;
      gap: 2px;
      min-width: 0;
    }}
    .review-main strong {{
      font-size: 13px;
      overflow-wrap: anywhere;
    }}
    .review-main span {{
      color: var(--muted);
      font-size: 12px;
    }}
    select {{
      min-height: 36px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #ffffff;
      color: var(--ink);
      font: inherit;
      padding: 0 8px;
      width: 100%;
    }}
    .empty {{
      color: var(--muted);
      margin: 0;
    }}
    .run-result {{
      margin-bottom: 14px;
      border: 1px solid var(--line);
      border-left: 4px solid var(--green);
      border-radius: 8px;
      background: #ffffff;
      padding: 16px 18px;
    }}
    .run-failure {{ border-left-color: var(--red); }}
    .notice {{
      margin-bottom: 14px;
      border: 1px solid var(--line);
      border-left: 4px solid var(--green);
      border-radius: 8px;
      background: #ffffff;
      padding: 12px 14px;
      font-weight: 700;
    }}
    .notice-failure {{ border-left-color: var(--red); }}
    .run-result h2 {{
      margin-bottom: 12px;
    }}
    .run-result ol {{
      list-style: none;
      padding: 0;
      margin: 0;
      display: grid;
      gap: 8px;
    }}
    .run-step {{
      display: grid;
      grid-template-columns: 150px minmax(0, 1fr);
      gap: 12px;
      align-items: start;
    }}
    .run-step span {{
      display: flex;
      justify-content: space-between;
      gap: 8px;
      color: var(--ink);
    }}
    .run-step em {{
      color: var(--muted);
      font-style: normal;
      font-size: 12px;
      white-space: nowrap;
    }}
    .run-step code {{
      color: var(--muted);
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 12px;
      overflow-wrap: anywhere;
      white-space: pre-wrap;
    }}
    @media (max-width: 800px) {{
      .topbar {{ align-items: flex-start; flex-direction: column; }}
      .period {{ white-space: normal; }}
      .metrics, .grid, .entry-grid, .entry-form, .compact-form, .template-edit-form {{ grid-template-columns: 1fr; }}
      .entry-wide {{ grid-column: auto; }}
      .template-title {{ align-items: flex-start; flex-direction: column; }}
      .list-row {{ grid-template-columns: 1fr; align-items: stretch; }}
      .generate-form {{ align-items: stretch; flex-direction: column; }}
      .trend-row {{ grid-template-columns: 66px minmax(88px, 1fr) 96px; }}
      .review-row {{ grid-template-columns: 1fr; }}
      .run-step {{ grid-template-columns: 1fr; }}
      .metric strong {{ font-size: 21px; }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="wrap topbar">
      <h1>家庭现金流雷达</h1>
      <div class="actions">
        <form method="post" action="/actions/refresh">
          <button type="submit">刷新数据</button>
        </form>
        <div class="period">当前月份：{html.escape(month_label)}</div>
      </div>
    </div>
  </header>
  <main class="wrap">
    {notice_html}
    {pipeline_html}
    <section class="entry-grid">
      <div class="panel">
        <h2>记录一笔收入或支出</h2>
        {add_transaction_form_html}
      </div>
      <div class="panel">
        <h2>财务建议</h2>
        {advice_html}
      </div>
    </section>
    <section class="metrics">{metrics}</section>
    <section class="grid">
      <div class="panel">
        <h2>自动记账</h2>
        {recurring_forms_html}
        {mortgage_prepayment_form_html}
      </div>
      <div class="panel">
        <h2>周期账单</h2>
        <div class="recent-list" id="recurring-list">
          {recurring_templates_html}
        </div>
      </div>
    </section>
    {template_edit_html}
    <section class="grid">
      <div class="panel">
        <h2>近 12 月基础结余趋势</h2>
        {trend_html}
      </div>
      <div class="panel">
        <h2>最近记录</h2>
        <div class="recent-list">
          {recent_transactions_html}
        </div>
      </div>
    </section>
    <section class="grid">
      <div class="panel">
        <h2>本月支出分析</h2>
        <div class="breakdown-list">
          {expense_breakdown_html}
        </div>
      </div>
      <div class="panel" id="review-panel">
        <h2>分类审核</h2>
        <div class="review-list">
          <div class="review-item warn"><span>unknown 待审核</span><strong>{unknown_count}</strong></div>
          <div class="review-item"><span>pending 待审核</span><strong>{pending_count}</strong></div>
        </div>
        <div class="review-transactions">
          {review_transactions_html}
        </div>
      </div>
    </section>
    <section class="grid">
      <div class="panel">
        <h2>房贷还款计划</h2>
        <div class="breakdown-list">
          {upcoming_bills_html}
        </div>
      </div>
      <div class="panel">
        <h2>债务拆分汇总</h2>
        {debt_split_summary_html}
      </div>
    </section>
    <section class="grid">
      <div class="panel">
        <h2>提前还贷事件</h2>
        <div class="breakdown-list" id="prepayment-list">
          {prepayment_events_html}
        </div>
      </div>
    </section>
    {prepayment_edit_html}
    {_render_scroll_restore_script()}
  </main>
</body>
</html>"""


class DashboardHandler(BaseHTTPRequestHandler):
    db_path = DEFAULT_DB
    raw_input_path = DEFAULT_RAW_INPUT

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path not in ("/", "/index.html"):
            self.send_error(404)
            return
        query = parse_qs(parsed.query)
        try:
            edit_template_id = int(query.get("edit_template", [""])[0]) if query.get("edit_template") else None
        except ValueError:
            edit_template_id = None
        try:
            edit_prepayment_id = int(query.get("edit_prepayment", [""])[0]) if query.get("edit_prepayment") else None
        except ValueError:
            edit_prepayment_id = None

        try:
            body = render_dashboard_html(
                self.db_path,
                edit_template_id=edit_template_id,
                edit_prepayment_id=edit_prepayment_id,
            ).encode("utf-8")
            status = 200
        except sqlite3.Error as exc:
            body = f"数据库读取失败: {html.escape(str(exc))}".encode("utf-8")
            status = 500

        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/actions/refresh":
            result = run_refresh_pipeline(self.db_path, self.raw_input_path)
            try:
                body = render_dashboard_html(self.db_path, pipeline_result=result).encode("utf-8")
                status = 200 if result["ok"] else 500
            except sqlite3.Error as exc:
                body = f"数据库读取失败: {html.escape(str(exc))}".encode("utf-8")
                status = 500
        elif parsed.path == "/actions/manual-override":
            notice = self._handle_manual_override()
            try:
                body = render_dashboard_html(self.db_path, notice=notice).encode("utf-8")
                status = 200 if notice["ok"] else 400
            except sqlite3.Error as exc:
                body = f"数据库读取失败: {html.escape(str(exc))}".encode("utf-8")
                status = 500
        elif parsed.path == "/actions/add-transaction":
            notice = self._handle_add_transaction()
            try:
                body = render_dashboard_html(self.db_path, notice=notice).encode("utf-8")
                status = 200 if notice["ok"] else 400
            except sqlite3.Error as exc:
                body = f"数据库读取失败: {html.escape(str(exc))}".encode("utf-8")
                status = 500
        elif parsed.path == "/actions/add-mortgage-template":
            notice = self._handle_add_mortgage_template()
            try:
                body = render_dashboard_html(self.db_path, notice=notice).encode("utf-8")
                status = 200 if notice["ok"] else 400
            except sqlite3.Error as exc:
                body = f"数据库读取失败: {html.escape(str(exc))}".encode("utf-8")
                status = 500
        elif parsed.path == "/actions/update-mortgage-template":
            notice = self._handle_update_mortgage_template()
            try:
                body = render_dashboard_html(self.db_path, notice=notice).encode("utf-8")
                status = 200 if notice["ok"] else 400
            except sqlite3.Error as exc:
                body = f"数据库读取失败: {html.escape(str(exc))}".encode("utf-8")
                status = 500
        elif parsed.path == "/actions/add-fixed-bill-template":
            notice = self._handle_add_fixed_bill_template()
            try:
                body = render_dashboard_html(self.db_path, notice=notice).encode("utf-8")
                status = 200 if notice["ok"] else 400
            except sqlite3.Error as exc:
                body = f"数据库读取失败: {html.escape(str(exc))}".encode("utf-8")
                status = 500
        elif parsed.path == "/actions/update-fixed-bill-template":
            notice = self._handle_update_fixed_bill_template()
            try:
                body = render_dashboard_html(self.db_path, notice=notice).encode("utf-8")
                status = 200 if notice["ok"] else 400
            except sqlite3.Error as exc:
                body = f"数据库读取失败: {html.escape(str(exc))}".encode("utf-8")
                status = 500
        elif parsed.path == "/actions/add-mortgage-prepayment":
            notice = self._handle_add_mortgage_prepayment()
            try:
                body = render_dashboard_html(self.db_path, notice=notice).encode("utf-8")
                status = 200 if notice["ok"] else 400
            except sqlite3.Error as exc:
                body = f"数据库读取失败: {html.escape(str(exc))}".encode("utf-8")
                status = 500
        elif parsed.path == "/actions/update-mortgage-prepayment":
            notice = self._handle_update_mortgage_prepayment()
            try:
                body = render_dashboard_html(self.db_path, notice=notice).encode("utf-8")
                status = 200 if notice["ok"] else 400
            except sqlite3.Error as exc:
                body = f"数据库读取失败: {html.escape(str(exc))}".encode("utf-8")
                status = 500
        elif parsed.path == "/actions/generate-recurring":
            notice = self._handle_generate_recurring()
            try:
                body = render_dashboard_html(self.db_path, notice=notice).encode("utf-8")
                status = 200 if notice["ok"] else 400
            except sqlite3.Error as exc:
                body = f"数据库读取失败: {html.escape(str(exc))}".encode("utf-8")
                status = 500
        else:
            self.send_error(404)
            return

        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_manual_override(self) -> dict:
        content_length = int(self.headers.get("Content-Length", "0") or 0)
        raw_body = self.rfile.read(content_length).decode("utf-8")
        fields = parse_qs(raw_body)
        try:
            transaction_id = int(fields.get("transaction_id", [""])[0])
        except ValueError:
            return {"ok": False, "message": "交易 ID 无效"}

        return save_manual_override(
            self.db_path,
            transaction_id,
            fields.get("financial_type", [""])[0],
            fields.get("cashflow_direction", [""])[0],
        )

    def _handle_add_transaction(self) -> dict:
        content_length = int(self.headers.get("Content-Length", "0") or 0)
        raw_body = self.rfile.read(content_length).decode("utf-8")
        fields = parse_qs(raw_body)
        return save_new_transaction(
            self.db_path,
            fields.get("transaction_date", [""])[0],
            fields.get("amount_yuan", [""])[0],
            fields.get("cashflow_direction", [""])[0],
            fields.get("financial_type", [""])[0],
            fields.get("description", [""])[0],
            account=fields.get("account", [""])[0],
            category_l1=fields.get("category_l1", [""])[0],
            category_l2=fields.get("category_l2", [""])[0],
        )

    def _handle_add_mortgage_template(self) -> dict:
        content_length = int(self.headers.get("Content-Length", "0") or 0)
        raw_body = self.rfile.read(content_length).decode("utf-8")
        fields = parse_qs(raw_body)
        return save_mortgage_template(
            self.db_path,
            fields.get("name", [""])[0],
            fields.get("principal_yuan", [""])[0],
            fields.get("annual_rate", [""])[0],
            fields.get("term_months", [""])[0],
            fields.get("start_date", [""])[0],
            fields.get("day_of_month", [""])[0],
            account=fields.get("account", [""])[0],
            lender=fields.get("lender", [""])[0],
        )

    def _handle_add_fixed_bill_template(self) -> dict:
        content_length = int(self.headers.get("Content-Length", "0") or 0)
        raw_body = self.rfile.read(content_length).decode("utf-8")
        fields = parse_qs(raw_body)
        return save_fixed_bill_template(
            self.db_path,
            fields.get("name", [""])[0],
            fields.get("amount_yuan", [""])[0],
            fields.get("start_date", [""])[0],
            fields.get("day_of_month", [""])[0],
            fields.get("category_l2", [""])[0],
            account=fields.get("account", [""])[0],
            end_date=fields.get("end_date", [""])[0],
        )

    def _handle_update_mortgage_template(self) -> dict:
        content_length = int(self.headers.get("Content-Length", "0") or 0)
        raw_body = self.rfile.read(content_length).decode("utf-8")
        fields = parse_qs(raw_body)
        return update_saved_mortgage_template(
            self.db_path,
            fields.get("template_id", [""])[0],
            fields.get("name", [""])[0],
            fields.get("principal_yuan", [""])[0],
            fields.get("annual_rate", [""])[0],
            fields.get("term_months", [""])[0],
            fields.get("start_date", [""])[0],
            fields.get("day_of_month", [""])[0],
            account=fields.get("account", [""])[0],
            lender=fields.get("lender", [""])[0],
        )

    def _handle_update_fixed_bill_template(self) -> dict:
        content_length = int(self.headers.get("Content-Length", "0") or 0)
        raw_body = self.rfile.read(content_length).decode("utf-8")
        fields = parse_qs(raw_body)
        return update_saved_fixed_bill_template(
            self.db_path,
            fields.get("template_id", [""])[0],
            fields.get("name", [""])[0],
            fields.get("amount_yuan", [""])[0],
            fields.get("start_date", [""])[0],
            fields.get("day_of_month", [""])[0],
            fields.get("category_l2", [""])[0],
            account=fields.get("account", [""])[0],
            end_date=fields.get("end_date", [""])[0],
        )

    def _handle_add_mortgage_prepayment(self) -> dict:
        content_length = int(self.headers.get("Content-Length", "0") or 0)
        raw_body = self.rfile.read(content_length).decode("utf-8")
        fields = parse_qs(raw_body)
        return save_mortgage_prepayment(
            self.db_path,
            fields.get("template_id", [""])[0],
            fields.get("prepayment_date", [""])[0],
            fields.get("amount_yuan", [""])[0],
            fields.get("effect_type", [""])[0],
            note=fields.get("note", [""])[0],
        )

    def _handle_update_mortgage_prepayment(self) -> dict:
        content_length = int(self.headers.get("Content-Length", "0") or 0)
        raw_body = self.rfile.read(content_length).decode("utf-8")
        fields = parse_qs(raw_body)
        return update_saved_mortgage_prepayment(
            self.db_path,
            fields.get("event_id", [""])[0],
            fields.get("prepayment_date", [""])[0],
            fields.get("amount_yuan", [""])[0],
            fields.get("effect_type", [""])[0],
            note=fields.get("note", [""])[0],
        )

    def _handle_generate_recurring(self) -> dict:
        content_length = int(self.headers.get("Content-Length", "0") or 0)
        raw_body = self.rfile.read(content_length).decode("utf-8")
        fields = parse_qs(raw_body)
        return run_recurring_generation(self.db_path, fields.get("as_of", [""])[0])

    def log_message(self, format: str, *args) -> None:
        return


def run_server(db_path: Path, host: str = "127.0.0.1", port: int = 8000, input_path: Path = DEFAULT_RAW_INPUT) -> None:
    DashboardHandler.db_path = db_path
    DashboardHandler.raw_input_path = input_path
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    print(f"Dashboard running at http://{host}:{port}")
    print(f"Using database: {db_path}")
    print(f"Using CSV input: {input_path}")
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="启动家庭现金流雷达 Web 仪表盘")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 数据库路径")
    parser.add_argument("--input", default=str(DEFAULT_RAW_INPUT), help="CSV 文件或目录路径")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", type=int, default=8000, help="监听端口")
    args = parser.parse_args()
    run_server(Path(args.db), host=args.host, port=args.port, input_path=Path(args.input))


if __name__ == "__main__":
    main()
