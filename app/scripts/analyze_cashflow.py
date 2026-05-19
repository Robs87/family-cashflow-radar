"""Rule-based household cashflow analysis for the dashboard.

This module turns monthly aggregates and review stats into an explainable
decision signal. It deliberately stays deterministic: no model calls and no
hidden portfolio advice.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any


def format_yuan(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents_abs = abs(int(cents or 0))
    return f"{sign}{cents_abs // 100:,}.{cents_abs % 100:02d}"


def _latest_month_start(latest: dict[str, Any]) -> date | None:
    try:
        return date(int(latest["year"]), int(latest["month"]), 1)
    except (KeyError, TypeError, ValueError):
        return None


def _risk_window_amount(upcoming_bills: list[dict[str, Any]], anchor: date | None, days: int) -> int:
    if not anchor:
        return 0
    end = anchor + timedelta(days=days)
    total = 0
    for row in upcoming_bills:
        try:
            due_date = date.fromisoformat(str(row.get("due_date")))
        except ValueError:
            continue
        if anchor <= due_date <= end:
            total += int(row.get("amount_cents") or 0)
    return total


def _expense_breakdown(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        row
        for row in data.get("expense_breakdown", [])
        if row.get("category") != "未分类" and int(row.get("amount_cents") or 0) > 0
    ]


def _confidence(unknown_count: int, pending_count: int, stable_income: int) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if stable_income <= 0:
        reasons.append("本月缺少稳定收入记录")
    if unknown_count:
        reasons.append(f"仍有 {unknown_count} 笔 unknown")
    if pending_count:
        reasons.append(f"仍有 {pending_count} 笔 pending")
    if not reasons:
        return "high", ["收入、分类和待审核状态足够支撑本次判断"]
    if stable_income <= 0 or unknown_count > 20 or pending_count > 50:
        return "low", reasons
    return "medium", reasons


def analyze_cashflow(data: dict[str, Any]) -> dict[str, Any]:
    latest = data.get("latest_month")
    if not latest:
        return {
            "level": "watch",
            "label": "观察状态",
            "safety_months": None,
            "confidence": "low",
            "headline": "当前家庭现金流：观察状态。先连续记录 7 天收入支出，再生成近期消费建议。",
            "reason": "缺少月度现金流数据，系统还不能判断未来 30 天的大额支出风险。",
            "risk_next_30_cents": 0,
            "risk_next_90_cents": 0,
            "advice": ["先连续记录 7 天收入支出，系统才能给出可靠的日常现金流建议。"],
        }

    stable_income = int(latest["stable_income_cents"] or 0)
    living = int(latest["living_expense_cents"] or 0)
    fixed = int(latest["fixed_expense_cents"] or 0)
    debt = int(latest["debt_payment_cents"] or 0)
    net = int(latest["net_operating_cashflow_cents"] or 0)
    unknown_count = int(data.get("unknown_count") or 0)
    pending_count = int(data.get("pending_count") or 0)
    required_outflow = fixed + debt
    safety_months = round(net / required_outflow, 1) if required_outflow > 0 and net > 0 else 0.0

    anchor = _latest_month_start(latest)
    risk_next_30 = _risk_window_amount(data.get("upcoming_bills", []), anchor, 30)
    risk_next_90 = _risk_window_amount(data.get("upcoming_bills", []), anchor, 90)
    confidence, confidence_reasons = _confidence(unknown_count, pending_count, stable_income)

    if stable_income <= 0 or net < 0:
        level = "danger"
        label = "危险状态"
        action = "未来 30 天先暂停非必要大额消费，并优先补齐稳定收入、固定支出和债务记录。"
    else:
        net_ratio = net / stable_income
        pressure_ratio = required_outflow / stable_income
        living_ratio = living / stable_income
        if net_ratio < 0.1 or pressure_ratio > 0.65 or risk_next_30 > max(net, 0):
            level = "tight"
            label = "偏紧状态"
            action = "未来 30 天不建议新增大额支出，提前还贷和加仓投资都建议暂缓。"
        elif net_ratio < 0.25 or pressure_ratio > 0.5 or living_ratio > 0.35 or risk_next_90 > max(net * 2, 0):
            level = "watch"
            label = "观察状态"
            action = "未来 30 天可以正常消费，但新增大额支出、提前还贷和加仓投资需要先做模拟。"
        else:
            level = "safe"
            label = "安全状态"
            action = "未来 30 天可维持正常消费；大额支出仍建议先确认不会把安全垫压到 1 个月以下。"

    confidence_note = ""
    if confidence != "high":
        confidence_note = " 建议可信度偏低：" + "；".join(confidence_reasons) + "。"

    reason = (
        f"本月基础结余 {format_yuan(net)} 元，固定支出+债务还款 "
        f"{format_yuan(required_outflow)} 元，代理安全垫约 {safety_months:.1f} 个月；"
        f"未来 30 天已知刚性风险 {format_yuan(risk_next_30)} 元，未来 3 个月 "
        f"{format_yuan(risk_next_90)} 元。{confidence_note}"
    )

    advice = _build_advice_items(
        data=data,
        stable_income=stable_income,
        living=living,
        fixed=fixed,
        debt=debt,
        net=net,
        unknown_count=unknown_count,
        pending_count=pending_count,
        confidence=confidence,
        risk_next_30=risk_next_30,
        risk_next_90=risk_next_90,
    )

    return {
        "level": level,
        "label": label,
        "safety_months": safety_months,
        "confidence": confidence,
        "headline": f"当前家庭现金流：{label}。{action}",
        "reason": reason,
        "risk_next_30_cents": risk_next_30,
        "risk_next_90_cents": risk_next_90,
        "advice": advice,
    }


def _build_advice_items(
    data: dict[str, Any],
    stable_income: int,
    living: int,
    fixed: int,
    debt: int,
    net: int,
    unknown_count: int,
    pending_count: int,
    confidence: str,
    risk_next_30: int,
    risk_next_90: int,
) -> list[str]:
    advice: list[str] = []
    if stable_income == 0:
        advice.append("本月还没有稳定收入记录，现金流安全只能低可信判断；先把工资或固定收入补齐。")
    elif net < 0:
        advice.append("本月基础结余为负，优先检查固定支出、债务还款和高频生活支出，先暂停非必要大额消费。")
    elif net < stable_income * 0.1:
        advice.append("本月基础结余低于稳定收入的 10%，抗风险空间偏薄，建议给日常消费设一个周预算。")
    elif fixed + debt > stable_income * 0.5:
        advice.append("本月基础结余为正，但固定支出和债务还款压力偏高，提前还贷或新增大额支出前先做模拟。")
    else:
        advice.append("本月基础结余为正，现金流结构暂时健康；继续保持实时记录，月底再看是否稳定。")

    if risk_next_30 and net > 0 and risk_next_30 > net:
        advice.append(f"未来 30 天已知刚性支出 {format_yuan(risk_next_30)} 元，高于本月基础结余，建议暂缓大额支出。")
    elif risk_next_90 and net > 0 and risk_next_90 > net * 2:
        advice.append(f"未来 3 个月已知刚性支出 {format_yuan(risk_next_90)} 元，提前还贷和加仓投资需要先保留安全垫。")

    category_gap = data.get("advice_category_gap") or {}
    missing_category_amount = int(category_gap.get("amount_cents") or 0)
    missing_category_count = int(category_gap.get("transaction_count") or 0)
    if missing_category_amount:
        advice.append(
            f"本月有 {format_yuan(missing_category_amount)} 元支出缺少二级明细，涉及 {missing_category_count} 笔；"
            "这部分补齐前，具体节流建议只能作为低可信参考。"
        )

    expense_breakdown = _expense_breakdown(data)
    top_living = [row for row in expense_breakdown if row.get("effective_financial_type") == "living_expense"][:3]
    if stable_income and living > stable_income * 0.35 and top_living:
        top_items = "、".join(
            f"{row['category']} {format_yuan(int(row['amount_cents'] or 0))} 元" for row in top_living
        )
        advice.append(f"日常生活支出超过稳定收入的 35%，本月优先看这些明细：{top_items}。")
    elif stable_income and living > stable_income * 0.35:
        advice.append("日常生活支出超过稳定收入的 35%，但缺少足够二级明细，先补餐饮、购物、交通等高频项。")

    top_fixed_debt = [
        row for row in expense_breakdown if row.get("effective_financial_type") in {"fixed_expense", "debt_payment"}
    ][:3]
    if stable_income and fixed + debt > stable_income * 0.5:
        if top_fixed_debt:
            top_items = "、".join(
                f"{row['category']} {format_yuan(int(row['amount_cents'] or 0))} 元" for row in top_fixed_debt
            )
            advice.append(f"固定支出加债务还款超过稳定收入的 50%，压力主要来自：{top_items}。")
        else:
            advice.append("固定支出加债务还款超过稳定收入的 50%，但缺少明细，先拆清房贷、保险、电话、宽带等项目。")

    if unknown_count or pending_count:
        advice.append(f"仍有 {unknown_count} 笔 unknown、{pending_count} 笔 pending，建议可信度为 {confidence}。")

    return advice[:5]
