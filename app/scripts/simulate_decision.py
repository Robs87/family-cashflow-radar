#!/usr/bin/env python3
"""Simulate household cashflow pressure for large financial decisions."""

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


DECISION_TYPES = {"mortgage_prepayment", "large_purchase", "investment"}
PAYMENT_TYPES = {"one_time", "installment"}


@dataclass(frozen=True)
class DecisionSimulation:
    risk_level: str
    min_cash_cents: int
    min_safety_months: float
    recommendation: str
    explanation: str
    suggested_max_amount_cents: int
    risk_month: str


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
        latest = _latest_month(conn)
    finally:
        conn.close()

    required_outflow = int(latest["living_expense_cents"] or 0) + int(latest["fixed_expense_cents"] or 0) + int(
        latest["debt_payment_cents"] or 0
    )
    base_net = int(latest["net_operating_cashflow_cents"] or 0)
    opening_buffer = max(base_net, 0)
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
        monthly_net = base_net + expected_income_impact_cents - expected_expense_impact_cents - scenario_outflow
        opening_buffer += monthly_net
        safety_months = opening_buffer / required_outflow if required_outflow > 0 else 99.0
        balances.append((month, opening_buffer, safety_months))
        if opening_buffer < min_balance:
            min_balance = opening_buffer
            risk_month = month
        min_safety = min(min_safety, safety_months)

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

    target_buffer = int(required_outflow * 3)
    projected_without_decision = max(base_net, 0) + (base_net + expected_income_impact_cents - expected_expense_impact_cents) * horizon_months
    suggested_max = max(0, projected_without_decision - target_buffer)
    if payment_type == "installment" and installment_months:
        suggested_max = min(suggested_max, (max(base_net, 0) * installment_months))

    detail = f"{horizon_months} 个月内最低安全垫 {format_yuan(max(min_balance, 0))} 元，最低覆盖 {min_safety:.1f} 个月。"
    if min_balance < 0:
        detail = f"{horizon_months} 个月内最低缺口 {format_yuan(min_balance)} 元，风险月份 {risk_month}。"
    if decision_type == "mortgage_prepayment":
        detail += " 当前只评估现金流压力；利息节省需要结合房贷计划单独比较。"
    elif decision_type == "investment":
        detail += " 当前不预测投资收益，只按本金占用现金流处理。"

    return DecisionSimulation(
        risk_level=risk_level,
        min_cash_cents=max(min_balance, 0),
        min_safety_months=round(min_safety, 2),
        recommendation=recommendation,
        explanation=detail,
        suggested_max_amount_cents=max(suggested_max, 0),
        risk_month=risk_month,
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
    args = parser.parse_args()

    try:
        amount_cents = parse_yuan_to_cents(args.amount)
        monthly_payment_cents = parse_yuan_to_cents(args.monthly_payment) if args.monthly_payment else None
        scenario_id, result = save_decision_scenario(
            args.db,
            args.name,
            args.decision_type,
            amount_cents,
            args.start_month,
            payment_type=args.payment_type,
            installment_months=args.installment_months,
            monthly_payment_cents=monthly_payment_cents,
        )
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc

    print(
        "saved=1 "
        f"scenario_id={scenario_id} "
        f"risk_level={result.risk_level} "
        f"min_safety_months={result.min_safety_months:.2f} "
        f"suggested_max_amount_cents={result.suggested_max_amount_cents}"
    )


if __name__ == "__main__":
    main()
