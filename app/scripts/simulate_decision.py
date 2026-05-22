#!/usr/bin/env python3
"""Simulate household cashflow pressure for large financial decisions."""

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.scripts.cash_balance import latest_cash_balance
from app.scripts.planned_events import forecast_events_by_month
from app.scripts.recurring import build_equal_payment_schedule, build_fixed_payment_schedule
from app.scripts.schema_migrations import ensure_v02_schema


DECISION_TYPES = {"mortgage_prepayment", "large_purchase", "investment"}
PAYMENT_TYPES = {"one_time", "installment"}
MORTGAGE_EFFECT_TYPES = {"reduce_term", "reduce_payment"}


@dataclass(frozen=True)
class DecisionSimulation:
    risk_level: str
    min_cash_cents: int
    min_safety_months: float
    recommendation: str
    explanation: str
    suggested_max_amount_cents: int
    risk_month: str
    interest_savings_cents: int = 0
    term_months_delta: int = 0


def parse_yuan_to_cents(value: str) -> int:
    try:
        amount = Decimal(value.strip())
    except (InvalidOperation, AttributeError):
        raise ValueError("金额格式无效") from None
    if amount < 0:
        raise ValueError("金额不能为负数")
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def format_yuan(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    value = abs(cents)
    return f"{sign}{value // 100:,}.{value % 100:02d}"


def _month_add(month: str, offset: int) -> str:
    year, month_no = (int(part) for part in month.split("-", 1))
    month_index = year * 12 + month_no - 1 + offset
    return f"{month_index // 12:04d}-{month_index % 12 + 1:02d}"


def _validate_month(value: str) -> str:
    parts = value.split("-", 1)
    if len(parts) != 2:
        raise ValueError("开始月份格式应为 YYYY-MM")
    year = int(parts[0])
    month = int(parts[1])
    if year < 2000 or month < 1 or month > 12:
        raise ValueError("开始月份格式应为 YYYY-MM")
    return f"{year:04d}-{month:02d}"


def _latest_month(conn: sqlite3.Connection) -> sqlite3.Row:
    row = conn.execute(
        """SELECT year, month, stable_income_cents, living_expense_cents,
                  fixed_expense_cents, debt_payment_cents,
                  net_operating_cashflow_cents
           FROM monthly_cashflow
           ORDER BY year DESC, month DESC
           LIMIT 1"""
    ).fetchone()
    if row is None:
        raise ValueError("暂无月度现金流数据，先刷新数据后再模拟")
    return row


def _estimate_mortgage_prepayment_savings(
    conn: sqlite3.Connection,
    *,
    amount_cents: int,
    start_month: str,
    mortgage_template_id: int | None,
    mortgage_effect_type: str,
) -> tuple[int, int, str]:
    if mortgage_effect_type not in MORTGAGE_EFFECT_TYPES:
        raise ValueError("提前还贷方式必须是 reduce_term 或 reduce_payment")

    if mortgage_template_id is None:
        template = conn.execute(
            """SELECT t.*, d.principal_initial_cents, d.interest_rate
               FROM recurring_bill_templates t
               JOIN debts d ON d.id = t.debt_id
               WHERE t.enabled = 1 AND t.bill_type = 'mortgage'
               ORDER BY t.id DESC
               LIMIT 1"""
        ).fetchone()
    else:
        template = conn.execute(
            """SELECT t.*, d.principal_initial_cents, d.interest_rate
               FROM recurring_bill_templates t
               JOIN debts d ON d.id = t.debt_id
               WHERE t.id = ? AND t.bill_type = 'mortgage'""",
            (mortgage_template_id,),
        ).fetchone()
    if template is None:
        return 0, 0, " 未找到可用房贷模板，暂不能估算利息节省。"

    event_date = date.fromisoformat(f"{start_month}-01")
    next_row = conn.execute(
        """SELECT *
           FROM mortgage_repayment_schedule
           WHERE recurring_template_id = ?
             AND due_date >= ?
           ORDER BY due_date
           LIMIT 1""",
        (template["id"], event_date.isoformat()),
    ).fetchone()
    if next_row is None:
        return 0, 0, " 该房贷在模拟月份之后没有剩余还款计划，暂不能估算利息节省。"

    previous_row = conn.execute(
        """SELECT *
           FROM mortgage_repayment_schedule
           WHERE recurring_template_id = ?
             AND due_date < ?
           ORDER BY due_date DESC
           LIMIT 1""",
        (template["id"], event_date.isoformat()),
    ).fetchone()
    remaining_before = (
        int(previous_row["remaining_principal_cents"])
        if previous_row
        else int(template["principal_initial_cents"])
    )
    if amount_cents > remaining_before:
        return 0, 0, " 提前还款金额超过当时剩余本金，暂不能估算利息节省。"
    remaining_after = remaining_before - amount_cents

    original_rows = conn.execute(
        """SELECT interest_cents
           FROM mortgage_repayment_schedule
           WHERE recurring_template_id = ?
             AND due_date >= ?
           ORDER BY due_date""",
        (template["id"], next_row["due_date"]),
    ).fetchall()
    original_interest = sum(int(row["interest_cents"]) for row in original_rows)
    original_count = len(original_rows)
    if remaining_after == 0:
        return original_interest, original_count, (
            f" 按当前房贷计划估算，可节省未来利息约 {format_yuan(original_interest)} 元，贷款提前结清。"
        )

    annual_rate = Decimal(str(template["interest_rate"] or 0))
    if mortgage_effect_type == "reduce_payment":
        new_rows = build_equal_payment_schedule(remaining_after, annual_rate, max(1, original_count))
    else:
        new_rows = build_fixed_payment_schedule(remaining_after, annual_rate, int(template["amount_cents"] or 0))
    new_interest = sum(int(row["interest_cents"]) for row in new_rows)
    interest_savings = max(0, original_interest - new_interest)
    term_delta = max(0, original_count - len(new_rows))
    effect_label = "降低月供" if mortgage_effect_type == "reduce_payment" else "缩短期限"
    return interest_savings, term_delta, (
        f" 按当前房贷计划和“{effect_label}”估算，可节省未来利息约 {format_yuan(interest_savings)} 元，"
        f"还款期数减少 {term_delta} 期。"
    )


def _scenario_monthly_outflow(
    *,
    decision_type: str,
    amount_cents: int,
    start_month: str,
    payment_type: str,
    installment_months: int | None,
    monthly_payment_cents: int | None,
    month: str,
) -> int:
    if payment_type == "one_time":
        return amount_cents if month == start_month else 0

    months = installment_months or 0
    if months <= 0:
        raise ValueError("分期月数必须大于 0")
    if month < start_month or month >= _month_add(start_month, months):
        return 0
    if monthly_payment_cents is not None:
        return monthly_payment_cents
    return (amount_cents + months - 1) // months


def simulate_decision(
    db_path: Path,
    decision_type: str,
    amount_cents: int,
    start_month: str,
    *,
    payment_type: str = "one_time",
    installment_months: int | None = None,
    monthly_payment_cents: int | None = None,
    expected_income_impact_cents: int = 0,
    expected_expense_impact_cents: int = 0,
    horizon_months: int = 6,
    mortgage_template_id: int | None = None,
    mortgage_effect_type: str = "reduce_term",
) -> DecisionSimulation:
    if decision_type not in DECISION_TYPES:
        raise ValueError("不支持的决策类型")
    if payment_type not in PAYMENT_TYPES:
        raise ValueError("不支持的支付方式")
    if amount_cents <= 0:
        raise ValueError("模拟金额必须大于 0")
    if horizon_months <= 0:
        raise ValueError("模拟周期必须大于 0")
    start_month = _validate_month(start_month)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        ensure_v02_schema(conn)
        latest = _latest_month(conn)
        cash_balance = latest_cash_balance(conn)
        planned_by_month = forecast_events_by_month(conn, start_month, horizon_months)
        interest_savings_cents, term_months_delta, mortgage_detail = (
            _estimate_mortgage_prepayment_savings(
                conn,
                amount_cents=amount_cents,
                start_month=start_month,
                mortgage_template_id=mortgage_template_id,
                mortgage_effect_type=mortgage_effect_type,
            )
            if decision_type == "mortgage_prepayment"
            else (0, 0, "")
        )
    finally:
        conn.close()

    required_outflow = int(latest["living_expense_cents"] or 0) + int(latest["fixed_expense_cents"] or 0) + int(
        latest["debt_payment_cents"] or 0
    )
    base_net = int(latest["net_operating_cashflow_cents"] or 0)
    opening_buffer = (
        int(cash_balance.available_cash_cents)
        if cash_balance is not None
        else max(base_net, 0)
    )
    min_balance = opening_buffer
    min_safety = float("inf")
    risk_month = start_month
    balances: list[tuple[str, int, float]] = []

    for index in range(horizon_months):
        month = _month_add(start_month, index)
        scenario_outflow = _scenario_monthly_outflow(
            decision_type=decision_type,
            amount_cents=amount_cents,
            start_month=start_month,
            payment_type=payment_type,
            installment_months=installment_months,
            monthly_payment_cents=monthly_payment_cents,
            month=month,
        )
        planned_net = planned_by_month.get(month, 0)
        monthly_net = base_net + expected_income_impact_cents - expected_expense_impact_cents - scenario_outflow
        monthly_net += planned_net
        opening_buffer += monthly_net
        safety_months = opening_buffer / required_outflow if required_outflow > 0 else 99.0
        balances.append((month, opening_buffer, safety_months))
        if opening_buffer < min_balance:
            min_balance = opening_buffer
            risk_month = month
        min_safety = min(min_safety, safety_months)

    target_buffer = int(required_outflow * 3)
    projected_without_decision = (
        (
            int(cash_balance.available_cash_cents)
            if cash_balance is not None
            else max(base_net, 0)
        )
        + sum(
            base_net
            + expected_income_impact_cents
            - expected_expense_impact_cents
            + planned_by_month.get(_month_add(start_month, index), 0)
            for index in range(horizon_months)
        )
    )
    if decision_type == "investment":
        suggested_max = max(0, (int(cash_balance.available_cash_cents) if cash_balance is not None else max(base_net, 0)) - target_buffer)
    else:
        suggested_max = max(0, projected_without_decision - target_buffer)
    if payment_type == "installment" and installment_months:
        suggested_max = min(suggested_max, (max(base_net - expected_expense_impact_cents, 0) * installment_months))

    if min_balance < 0:
        risk_level = "danger"
        recommendation = "不建议执行：模拟期内现金流会转负，先补安全垫或降低金额。"
    elif min_safety < 1.5:
        risk_level = "tight"
        recommendation = "谨慎执行：执行后安全垫低于 1.5 个月，建议降低金额或改为分期。"
    elif min_safety < 3:
        risk_level = "watch"
        recommendation = "可以考虑，但要保留应急现金，执行后连续观察固定支出和债务压力。"
    else:
        risk_level = "safe"
        recommendation = "现金流压力可控，可以进入下一步决策比较。"

    if decision_type == "investment" and amount_cents > suggested_max:
        if risk_level == "safe":
            risk_level = "watch"
        recommendation = "建议降低加仓金额或暂缓：先保留 3 个月固定支出和债务还款现金垫。"

    detail = f"{horizon_months} 个月内最低安全垫 {format_yuan(max(min_balance, 0))} 元，最低覆盖 {min_safety:.1f} 个月。"
    if min_balance < 0:
        detail = f"{horizon_months} 个月内最低缺口 {format_yuan(min_balance)} 元，风险月份 {risk_month}。"
    if any(planned_by_month.values()):
        detail += " 已纳入未匹配的未来计划现金流，已匹配 BeeCount 实际流水的计划不会重复计算。"
    if decision_type == "mortgage_prepayment":
        detail += mortgage_detail
    elif decision_type == "large_purchase":
        if payment_type == "installment":
            detail += " 已按分期现金流测算。"
        if expected_expense_impact_cents:
            detail += f" 已计入每月新增固定支出 {format_yuan(expected_expense_impact_cents)} 元。"
        if expected_income_impact_cents:
            detail += f" 已计入每月新增收入 {format_yuan(expected_income_impact_cents)} 元。"
    elif decision_type == "investment":
        detail += (
            f" 当前不预测投资收益，只按本金占用现金流处理；必须保留现金 "
            f"{format_yuan(target_buffer)} 元，可投资现金上限约 {format_yuan(max(suggested_max, 0))} 元。"
        )

    return DecisionSimulation(
        risk_level=risk_level,
        min_cash_cents=max(min_balance, 0),
        min_safety_months=round(min_safety, 2),
        recommendation=recommendation,
        explanation=detail,
        suggested_max_amount_cents=max(suggested_max, 0),
        risk_month=risk_month,
        interest_savings_cents=interest_savings_cents,
        term_months_delta=term_months_delta,
    )


def save_decision_scenario(
    db_path: Path,
    scenario_name: str,
    decision_type: str,
    amount_cents: int,
    start_month: str,
    *,
    payment_type: str = "one_time",
    installment_months: int | None = None,
    monthly_payment_cents: int | None = None,
    expected_income_impact_cents: int = 0,
    expected_expense_impact_cents: int = 0,
    mortgage_template_id: int | None = None,
    mortgage_effect_type: str = "reduce_term",
) -> tuple[int, DecisionSimulation]:
    simulation = simulate_decision(
        db_path,
        decision_type,
        amount_cents,
        start_month,
        payment_type=payment_type,
        installment_months=installment_months,
        monthly_payment_cents=monthly_payment_cents,
        expected_income_impact_cents=expected_income_impact_cents,
        expected_expense_impact_cents=expected_expense_impact_cents,
        mortgage_template_id=mortgage_template_id,
        mortgage_effect_type=mortgage_effect_type,
    )
    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.execute(
            """INSERT INTO decision_scenarios
               (scenario_name, decision_type, amount_cents, start_month, payment_type,
                installment_months, monthly_payment_cents, expected_income_impact_cents,
                expected_expense_impact_cents, result_risk_level, result_min_cash_cents,
                result_min_safety_months, recommendation, explanation)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                scenario_name.strip(),
                decision_type,
                amount_cents,
                _validate_month(start_month),
                payment_type,
                installment_months,
                monthly_payment_cents,
                expected_income_impact_cents,
                expected_expense_impact_cents,
                simulation.risk_level,
                simulation.min_cash_cents,
                simulation.min_safety_months,
                simulation.recommendation,
                simulation.explanation,
            ),
        )
        conn.commit()
        return int(cursor.lastrowid), simulation
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="模拟大额决策对家庭现金流的压力")
    parser.add_argument("--db", type=Path, default=Path("data/processed/cashflow.db"))
    parser.add_argument("--name", required=True)
    parser.add_argument("--decision-type", choices=sorted(DECISION_TYPES), required=True)
    parser.add_argument("--amount", required=True, help="金额，单位元")
    parser.add_argument("--start-month", required=True, help="YYYY-MM")
    parser.add_argument("--payment-type", choices=sorted(PAYMENT_TYPES), default="one_time")
    parser.add_argument("--installment-months", type=int)
    parser.add_argument("--monthly-payment", help="月供，单位元；留空则按总额/月数估算")
    parser.add_argument("--expected-income-impact", default="0", help="每月新增收入影响，单位元")
    parser.add_argument("--expected-expense-impact", default="0", help="每月新增固定支出影响，单位元")
    parser.add_argument("--mortgage-template-id", type=int)
    parser.add_argument("--mortgage-effect-type", choices=sorted(MORTGAGE_EFFECT_TYPES), default="reduce_term")
    args = parser.parse_args()

    try:
        amount_cents = parse_yuan_to_cents(args.amount)
        monthly_payment_cents = parse_yuan_to_cents(args.monthly_payment) if args.monthly_payment else None
        expected_income_impact_cents = parse_yuan_to_cents(args.expected_income_impact)
        expected_expense_impact_cents = parse_yuan_to_cents(args.expected_expense_impact)
        scenario_id, result = save_decision_scenario(
            args.db,
            args.name,
            args.decision_type,
            amount_cents,
            args.start_month,
            payment_type=args.payment_type,
            installment_months=args.installment_months,
            monthly_payment_cents=monthly_payment_cents,
            expected_income_impact_cents=expected_income_impact_cents,
            expected_expense_impact_cents=expected_expense_impact_cents,
            mortgage_template_id=args.mortgage_template_id,
            mortgage_effect_type=args.mortgage_effect_type,
        )
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc

    print(
        "saved=1 "
        f"scenario_id={scenario_id} "
        f"risk_level={result.risk_level} "
        f"min_safety_months={result.min_safety_months:.2f} "
        f"interest_savings_cents={result.interest_savings_cents} "
        f"suggested_max_amount_cents={result.suggested_max_amount_cents}"
    )


if __name__ == "__main__":
    main()
