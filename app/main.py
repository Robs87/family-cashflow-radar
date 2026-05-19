#!/usr/bin/env python3
"""Minimal local web dashboard for Family Cashflow Radar."""

import argparse
import contextlib
import html
import io
import json
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
from app.scripts.analyze_cashflow import analyze_cashflow
from app.scripts.beecount_tokens import (
    DEFAULT_ACCESS_TOKEN_ENV,
    DEFAULT_REFRESH_TOKEN_ENV,
    token_is_configured,
    write_beecount_config,
)
from app.scripts.beecount_category_mappings import (
    DIRECTIONS as MAPPING_DIRECTIONS,
    FINANCIAL_TYPES as MAPPING_FINANCIAL_TYPES,
    ensure_mapping_schema,
)
from app.scripts.generate_monthly_cashflow import generate_monthly_cashflow
from app.scripts.import_beecount import import_beecount
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
from app.scripts.simulate_decision import (
    DECISION_TYPES,
    PAYMENT_TYPES,
    parse_yuan_to_cents as parse_simulation_yuan_to_cents,
    save_decision_scenario,
)


DEFAULT_DB = Path("data/processed/cashflow.db")
DEFAULT_RAW_INPUT = Path("data/raw")
DEFAULT_BEECOUNT_CONFIG = Path("data/processed/beecount_source.json")
DEFAULT_BEECOUNT_BASE_URL = "https://bee.332626.xyz:9090"
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
DECISION_TYPE_OPTIONS = [
    ("mortgage_prepayment", "提前还贷"),
    ("large_purchase", "大额消费 / 买车"),
    ("investment", "投资加仓"),
]
PAYMENT_TYPE_OPTIONS = [
    ("one_time", "一次性支付"),
    ("installment", "分期 / 月供"),
]
ADVICE_REQUIRED_CATEGORY_TYPES = {
    "living_expense",
    "fixed_expense",
    "debt_payment",
    "reimbursable_expense",
    "investment_outflow",
    "asset_purchase",
}
CATEGORY_L2_PRESETS = [
    ("日常生活", "餐饮"),
    ("日常生活", "外卖"),
    ("日常生活", "超市日用"),
    ("日常生活", "交通"),
    ("日常生活", "打车"),
    ("日常生活", "购物"),
    ("日常生活", "娱乐"),
    ("日常生活", "医疗"),
    ("日常生活", "育儿"),
    ("固定支出", "房租"),
    ("固定支出", "物业"),
    ("固定支出", "水电燃气"),
    ("固定支出", "宽带"),
    ("固定支出", "电话费"),
    ("固定支出", "保险"),
    ("固定支出", "订阅服务"),
    ("债务还款", "房贷本金"),
    ("债务还款", "房贷利息"),
    ("债务还款", "车贷"),
    ("债务还款", "信用贷"),
    ("债务还款", "提前还款"),
    ("投资流出", "基金定投"),
    ("投资流出", "股票买入"),
    ("投资流出", "理财买入"),
    ("资产购入", "车辆"),
    ("资产购入", "装修"),
    ("资产购入", "家电"),
    ("资产购入", "大件购置"),
    ("工作垫付", "差旅垫付"),
    ("工作垫付", "采购垫付"),
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


def _requires_advice_category(financial_type: str, cashflow_direction: str) -> bool:
    return cashflow_direction == "outflow" and financial_type in ADVICE_REQUIRED_CATEGORY_TYPES


def _validate_advice_category(financial_type: str, cashflow_direction: str, category_l2: str) -> str | None:
    if _requires_advice_category(financial_type, cashflow_direction) and not category_l2.strip():
        return f"{_financial_type_label(financial_type)}需要填写二级分类，才能给出针对性的财务建议"
    return None


def _category_preset_value(category_l1: str, category_l2: str) -> str:
    return f"{category_l1}::{category_l2}"


def _split_category_preset(value: str) -> tuple[str, str]:
    if "::" not in value:
        return "", ""
    category_l1, category_l2 = value.split("::", 1)
    return category_l1.strip(), category_l2.strip()


def _resolve_category_fields(fields: dict[str, list[str]]) -> tuple[str, str]:
    fallback_l1 = fields.get("category_l1", [""])[0].strip()
    fallback_l2 = fields.get("category_l2", [""])[0].strip()
    custom_l2 = fields.get("category_l2_custom", [""])[0].strip()
    preset = fields.get("category_l2_preset", [""])[0].strip()
    preset_l1, preset_l2 = _split_category_preset(preset)

    if custom_l2:
        return fallback_l1 or preset_l1, custom_l2
    if preset_l2:
        return preset_l1 or fallback_l1, preset_l2
    return fallback_l1, fallback_l2


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
        source_review = conn.execute(
            """SELECT
                  SUM(CASE
                        WHEN r.source_file LIKE 'beecount_cloud:%'
                         AND COALESCE(n.manual_financial_type, n.financial_type) = 'unknown'
                        THEN 1 ELSE 0 END) AS beecount_unknown_count,
                  SUM(CASE
                        WHEN r.source_file LIKE 'beecount_cloud:%'
                         AND n.review_status = 'pending'
                        THEN 1 ELSE 0 END) AS beecount_pending_count,
                  SUM(CASE
                        WHEN r.source_file NOT LIKE 'beecount_cloud:%'
                         AND COALESCE(n.manual_financial_type, n.financial_type) = 'unknown'
                        THEN 1 ELSE 0 END) AS legacy_unknown_count,
                  SUM(CASE
                        WHEN r.source_file NOT LIKE 'beecount_cloud:%'
                         AND n.review_status = 'pending'
                        THEN 1 ELSE 0 END) AS legacy_pending_count
               FROM normalized_transactions n
               JOIN raw_transactions r ON r.id = n.raw_transaction_id"""
        ).fetchone()
        review_transactions = conn.execute(
            """SELECT id,
                      transaction_date,
                      amount_cents,
                      COALESCE(manual_cashflow_direction, cashflow_direction) AS effective_direction,
                      COALESCE(manual_financial_type, financial_type) AS effective_financial_type,
                      COALESCE(NULLIF(manual_category_l1, ''), category_l1) AS effective_category_l1,
                      COALESCE(NULLIF(manual_category_l2, ''), category_l2) AS effective_category_l2,
                      account,
                      counterparty,
                      description,
                      review_status
               FROM normalized_transactions
               WHERE review_status = 'pending'
                  OR COALESCE(manual_financial_type, financial_type) = 'unknown'
                  OR (
                    COALESCE(manual_cashflow_direction, cashflow_direction) = 'outflow'
                    AND COALESCE(manual_financial_type, financial_type) IN (
                      'living_expense', 'fixed_expense', 'debt_payment',
                      'reimbursable_expense', 'investment_outflow', 'asset_purchase'
                    )
                    AND COALESCE(NULLIF(manual_category_l2, ''), NULLIF(category_l2, '')) IS NULL
                  )
               ORDER BY transaction_date DESC, id DESC
               LIMIT 20"""
        ).fetchall()
        recent_transactions = conn.execute(
            """SELECT transaction_date,
                      amount_cents,
                      COALESCE(manual_cashflow_direction, cashflow_direction) AS effective_direction,
                      COALESCE(manual_financial_type, financial_type) AS effective_financial_type,
                      COALESCE(NULLIF(manual_category_l1, ''), category_l1) AS category_l1,
                      COALESCE(NULLIF(manual_category_l2, ''), category_l2) AS category_l2,
                      description
               FROM normalized_transactions
               ORDER BY transaction_date DESC, id DESC
               LIMIT 8"""
        ).fetchall()
        expense_breakdown = []
        if latest_month:
            expense_breakdown = conn.execute(
                """SELECT COALESCE(manual_financial_type, financial_type) AS effective_financial_type,
                          COALESCE(
                            NULLIF(COALESCE(NULLIF(manual_category_l2, ''), category_l2), ''),
                            NULLIF(COALESCE(NULLIF(manual_category_l1, ''), category_l1), ''),
                            '未分类'
                          ) AS category,
                          SUM(amount_cents) AS amount_cents,
                          COUNT(*) AS transaction_count
                   FROM normalized_transactions
                   WHERE year = ?
                     AND month = ?
                     AND COALESCE(manual_cashflow_direction, cashflow_direction) = 'outflow'
                     AND COALESCE(manual_financial_type, financial_type) IN (
                        'living_expense', 'fixed_expense', 'debt_payment',
                        'reimbursable_expense', 'investment_outflow', 'asset_purchase'
                     )
                   GROUP BY effective_financial_type, category
                   ORDER BY amount_cents DESC
                   LIMIT 8""",
                (latest_month["year"], latest_month["month"]),
            ).fetchall()
            advice_category_gap = conn.execute(
                """SELECT COALESCE(SUM(amount_cents), 0) AS amount_cents,
                          COUNT(*) AS transaction_count
                   FROM normalized_transactions
                   WHERE year = ?
                     AND month = ?
                     AND COALESCE(manual_cashflow_direction, cashflow_direction) = 'outflow'
                     AND COALESCE(manual_financial_type, financial_type) IN (
                        'living_expense', 'fixed_expense', 'debt_payment',
                        'reimbursable_expense', 'investment_outflow', 'asset_purchase'
                     )
                     AND COALESCE(NULLIF(manual_category_l2, ''), NULLIF(category_l2, '')) IS NULL""",
                (latest_month["year"], latest_month["month"]),
            ).fetchone()
        else:
            advice_category_gap = None
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
        decision_scenarios = conn.execute(
            """SELECT id, scenario_name, decision_type, amount_cents, start_month,
                      payment_type, installment_months, monthly_payment_cents,
                      result_risk_level, result_min_cash_cents,
                      result_min_safety_months, recommendation, explanation,
                      created_at
               FROM decision_scenarios
               ORDER BY id DESC
               LIMIT 6"""
        ).fetchall()
    finally:
        conn.close()

    return {
        "latest_month": dict(latest_month) if latest_month else None,
        "trend": [dict(row) for row in reversed(trend)],
        "unknown_count": int((review["unknown_count"] if review else 0) or 0),
        "pending_count": int((review["pending_count"] if review else 0) or 0),
        "beecount_unknown_count": int((source_review["beecount_unknown_count"] if source_review else 0) or 0),
        "beecount_pending_count": int((source_review["beecount_pending_count"] if source_review else 0) or 0),
        "legacy_unknown_count": int((source_review["legacy_unknown_count"] if source_review else 0) or 0),
        "legacy_pending_count": int((source_review["legacy_pending_count"] if source_review else 0) or 0),
        "review_transactions": [dict(row) for row in review_transactions],
        "recent_transactions": [dict(row) for row in recent_transactions],
        "expense_breakdown": [dict(row) for row in expense_breakdown],
        "advice_category_gap": dict(advice_category_gap)
        if advice_category_gap
        else {"amount_cents": 0, "transaction_count": 0},
        "recurring_templates": [dict(row) for row in recurring_templates],
        "mortgage_templates": [dict(row) for row in mortgage_templates],
        "upcoming_bills": [dict(row) for row in upcoming_bills],
        "debt_split_summary": dict(debt_split_summary) if debt_split_summary else {},
        "prepayment_events": [dict(row) for row in prepayment_events],
        "decision_scenarios": [dict(row) for row in decision_scenarios],
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


def _beecount_source_configured(
    beecount_input_json: Path | None,
    beecount_base_url: str | None,
    beecount_ledger_id: str | None,
) -> bool:
    return bool(beecount_input_json or (beecount_base_url and beecount_ledger_id))


def _load_beecount_source_config(config_path: Path | None) -> dict:
    if not config_path or not config_path.exists():
        return {}
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("BeeCount 配置文件必须是 JSON 对象")

    config_dir = config_path.parent
    input_json = payload.get("input_json") or payload.get("inputJson")
    resolved_input = None
    if input_json:
        resolved_input = Path(str(input_json))
        if not resolved_input.is_absolute():
            resolved_input = config_dir / resolved_input

    return {
        "beecount_input_json": resolved_input,
        "beecount_base_url": payload.get("base_url") or payload.get("baseUrl"),
        "beecount_ledger_id": payload.get("ledger_id") or payload.get("ledgerId"),
        "beecount_access_token_env": payload.get("access_token_env")
        or payload.get("accessTokenEnv")
        or "BEECOUNT_ACCESS_TOKEN",
        "beecount_refresh_token_env": payload.get("refresh_token_env")
        or payload.get("refreshTokenEnv")
        or "BEECOUNT_REFRESH_TOKEN",
        "beecount_limit": int(payload.get("limit") or 500),
    }


def _resolve_beecount_source(
    config_path: Path | None,
    beecount_input_json: Path | None,
    beecount_base_url: str | None,
    beecount_ledger_id: str | None,
    beecount_access_token_env: str,
    beecount_refresh_token_env: str,
    beecount_limit: int,
) -> dict:
    if _beecount_source_configured(beecount_input_json, beecount_base_url, beecount_ledger_id):
        return {
            "beecount_input_json": beecount_input_json,
            "beecount_base_url": beecount_base_url,
            "beecount_ledger_id": beecount_ledger_id,
            "beecount_access_token_env": beecount_access_token_env,
            "beecount_refresh_token_env": beecount_refresh_token_env,
            "beecount_limit": beecount_limit,
        }
    config = _load_beecount_source_config(config_path)
    if not config:
        return {
            "beecount_input_json": None,
            "beecount_base_url": None,
            "beecount_ledger_id": None,
            "beecount_access_token_env": beecount_access_token_env,
            "beecount_refresh_token_env": beecount_refresh_token_env,
            "beecount_limit": beecount_limit,
        }
    return config


def save_beecount_token_config(
    config_path: Path | None,
    base_url: str,
    ledger_id: str,
    limit_text: str,
    access_token: str = "",
    refresh_token: str = "",
    access_token_env: str = DEFAULT_ACCESS_TOKEN_ENV,
    refresh_token_env: str = DEFAULT_REFRESH_TOKEN_ENV,
) -> dict:
    if not config_path:
        return {"ok": False, "message": "未配置 BeeCount 配置文件路径"}
    base_url = base_url.strip().rstrip("/")
    ledger_id = ledger_id.strip()
    if not base_url.startswith(("http://", "https://")):
        return {"ok": False, "message": "BeeCount base URL 必须以 http:// 或 https:// 开头"}
    if not ledger_id:
        return {"ok": False, "message": "请填写 BeeCount ledger id"}
    try:
        limit = int(limit_text or "500")
    except ValueError:
        return {"ok": False, "message": "读取上限必须是整数"}
    if limit <= 0:
        return {"ok": False, "message": "读取上限必须大于 0"}

    wrote_tokens = bool(access_token.strip() or refresh_token.strip())
    try:
        write_beecount_config(
            config_path,
            base_url=base_url,
            ledger_id=ledger_id,
            limit=limit,
            access_token=access_token,
            refresh_token=refresh_token,
            access_token_env=access_token_env.strip() or DEFAULT_ACCESS_TOKEN_ENV,
            refresh_token_env=refresh_token_env.strip() or DEFAULT_REFRESH_TOKEN_ENV,
        )
    except Exception as exc:
        return {"ok": False, "message": f"BeeCount token 保存失败: {exc}"}
    if wrote_tokens:
        return {"ok": True, "message": "BeeCount 连接配置已保存，token 已写入 Keychain"}
    return {"ok": True, "message": "BeeCount 连接配置已保存，Keychain token 未覆盖"}


def _fetch_beecount_category_mappings(db_path: Path | None) -> list[dict]:
    if not db_path:
        return []
    _ensure_database_initialized(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        ensure_mapping_schema(conn)
        rows = conn.execute(
            """SELECT *
               FROM beecount_category_mappings
               ORDER BY enabled ASC, beecount_kind, category_name, parent_name"""
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def save_beecount_category_mapping(
    db_path: Path,
    mapping_id_text: str,
    cashflow_direction: str,
    financial_type: str,
    category_l1: str,
    category_l2: str,
    enabled_text: str = "1",
) -> dict:
    try:
        mapping_id = int(mapping_id_text)
    except ValueError:
        return {"ok": False, "message": "BeeCount 映射 ID 无效"}
    if cashflow_direction not in MAPPING_DIRECTIONS:
        return {"ok": False, "message": f"不支持的现金流方向: {cashflow_direction}"}
    if financial_type not in MAPPING_FINANCIAL_TYPES:
        return {"ok": False, "message": f"不支持的财务类型: {financial_type}"}
    enabled = 1 if enabled_text == "1" else 0

    conn = sqlite3.connect(str(db_path))
    try:
        ensure_mapping_schema(conn)
        result = conn.execute(
            """UPDATE beecount_category_mappings
               SET radar_cashflow_direction = ?,
                   radar_financial_type = ?,
                   radar_category_l1 = ?,
                   radar_category_l2 = ?,
                   enabled = ?,
                   mapping_source = 'manual',
                   updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (
                cashflow_direction,
                financial_type,
                category_l1.strip(),
                category_l2.strip(),
                enabled,
                mapping_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    if result.rowcount == 0:
        return {"ok": False, "message": "未找到 BeeCount 分类映射"}
    return {"ok": True, "message": "BeeCount 分类映射已更新；下次刷新或重新分类后生效"}


def run_refresh_pipeline(
    db_path: Path,
    input_path: Path = DEFAULT_RAW_INPUT,
    beecount_config_path: Path | None = None,
    beecount_input_json: Path | None = None,
    beecount_base_url: str | None = None,
    beecount_ledger_id: str | None = None,
    beecount_access_token_env: str = "BEECOUNT_ACCESS_TOKEN",
    beecount_refresh_token_env: str = "BEECOUNT_REFRESH_TOKEN",
    beecount_limit: int = 500,
) -> dict:
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

    beecount_source = _resolve_beecount_source(
        beecount_config_path,
        beecount_input_json,
        beecount_base_url,
        beecount_ledger_id,
        beecount_access_token_env,
        beecount_refresh_token_env,
        beecount_limit,
    )

    if _beecount_source_configured(
        beecount_source["beecount_input_json"],
        beecount_source["beecount_base_url"],
        beecount_source["beecount_ledger_id"],
    ):
        source_step = (
            "同步 BeeCount",
            import_beecount,
            db_path,
            beecount_source["beecount_input_json"],
            beecount_source["beecount_base_url"],
            beecount_source["beecount_ledger_id"],
            beecount_source["beecount_access_token_env"],
            beecount_source["beecount_refresh_token_env"],
            beecount_source["beecount_limit"],
        )
    else:
        source_step = ("导入 CSV", import_csv, db_path, input_path)

    steps = [
        source_step,
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
    category_l1: str = "",
    category_l2: str = "",
) -> dict:
    allowed_types = {value for value, _label in FINANCIAL_TYPE_OPTIONS}
    allowed_directions = {value for value, _label in DIRECTION_OPTIONS}
    if financial_type not in allowed_types:
        return {"ok": False, "message": f"不支持的财务类型: {financial_type}"}
    if cashflow_direction not in allowed_directions:
        return {"ok": False, "message": f"不支持的现金流方向: {cashflow_direction}"}
    category_error = _validate_advice_category(financial_type, cashflow_direction, category_l2)
    if category_error:
        return {"ok": False, "message": category_error}

    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.execute(
            """UPDATE normalized_transactions
               SET manual_financial_type = ?,
                   manual_cashflow_direction = ?,
                   manual_category_l1 = ?,
                   manual_category_l2 = ?,
                   review_status = 'approved',
                   manual_updated_at = CURRENT_TIMESTAMP,
                   updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (financial_type, cashflow_direction, category_l1.strip() or None, category_l2.strip() or None, transaction_id),
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
    category_error = _validate_advice_category(financial_type, cashflow_direction, category_l2)
    if category_error:
        return {"ok": False, "message": category_error}

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


def save_decision_simulation(
    db_path: Path,
    scenario_name: str,
    decision_type: str,
    amount_yuan: str,
    start_month: str,
    payment_type: str,
    *,
    installment_months_text: str = "",
    monthly_payment_yuan: str = "",
) -> dict:
    try:
        if not scenario_name.strip():
            return {"ok": False, "message": "请填写模拟名称"}
        if decision_type not in DECISION_TYPES:
            return {"ok": False, "message": "不支持的决策类型"}
        if payment_type not in PAYMENT_TYPES:
            return {"ok": False, "message": "不支持的支付方式"}
        installment_months = int(installment_months_text) if installment_months_text.strip() else None
        monthly_payment_cents = (
            parse_simulation_yuan_to_cents(monthly_payment_yuan) if monthly_payment_yuan.strip() else None
        )
        scenario_id, simulation = save_decision_scenario(
            db_path,
            scenario_name.strip(),
            decision_type,
            parse_simulation_yuan_to_cents(amount_yuan),
            start_month.strip(),
            payment_type=payment_type,
            installment_months=installment_months,
            monthly_payment_cents=monthly_payment_cents,
        )
    except Exception as exc:
        return {"ok": False, "message": str(exc)}
    return {
        "ok": True,
        "message": f"模拟已保存: #{scenario_id} · {simulation.risk_level} · {simulation.recommendation}",
    }


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


def _render_token_status(label: str, configured: bool) -> str:
    tone = "good" if configured else "bad"
    text = "已配置" if configured else "未配置"
    return f'<span class="token-status {tone}">{html.escape(label)}：{html.escape(text)}</span>'


def _render_beecount_mapping_rows(rows: list[dict]) -> str:
    if not rows:
        return '<p class="empty">暂无 BeeCount 分类映射。先刷新一次 BeeCount 数据后会自动生成。</p>'
    rendered = []
    direction_options = _render_options(DIRECTION_OPTIONS, "")
    type_options = _render_options(FINANCIAL_TYPE_OPTIONS, "")
    for row in rows:
        direction_select = direction_options.replace(
            f'value="{html.escape(row["radar_cashflow_direction"])}"',
            f'value="{html.escape(row["radar_cashflow_direction"])}" selected',
            1,
        )
        type_select = type_options.replace(
            f'value="{html.escape(row["radar_financial_type"])}"',
            f'value="{html.escape(row["radar_financial_type"])}" selected',
            1,
        )
        enabled_checked = " checked" if int(row["enabled"] or 0) == 1 else ""
        source = str(row.get("mapping_source") or "")
        rendered.append(
            '<form class="mapping-row" method="post" action="/actions/beecount-category-mapping">'
            f'<input type="hidden" name="mapping_id" value="{int(row["id"])}">'
            '<div class="mapping-source">'
            f'<strong>{html.escape(str(row["category_name"] or ""))}</strong>'
            f'<span>{html.escape(str(row["beecount_kind"] or ""))}'
            f'{(" / " + html.escape(str(row["parent_name"]))) if row.get("parent_name") else ""}'
            f' · {html.escape(source)}</span>'
            "</div>"
            f'<select name="cashflow_direction">{direction_select}</select>'
            f'<select name="financial_type">{type_select}</select>'
            f'<input name="category_l1" value="{html.escape(str(row["radar_category_l1"] or ""))}" placeholder="一级">'
            f'<input name="category_l2" value="{html.escape(str(row["radar_category_l2"] or ""))}" placeholder="二级">'
            '<label class="mapping-enabled">'
            f'<input type="checkbox" name="enabled" value="1"{enabled_checked}>启用'
            '</label>'
            '<button type="submit">保存</button>'
            "</form>"
        )
    return "\n".join(rendered)


def render_beecount_settings_html(
    *,
    db_path: Path | None = None,
    config_path: Path | None,
    beecount_input_json: Path | None = None,
    beecount_base_url: str | None = None,
    beecount_ledger_id: str | None = None,
    beecount_access_token_env: str = DEFAULT_ACCESS_TOKEN_ENV,
    beecount_refresh_token_env: str = DEFAULT_REFRESH_TOKEN_ENV,
    beecount_limit: int = 500,
    notice: dict | None = None,
) -> str:
    source = _resolve_beecount_source(
        config_path,
        beecount_input_json,
        beecount_base_url,
        beecount_ledger_id,
        beecount_access_token_env,
        beecount_refresh_token_env,
        beecount_limit,
    )
    base_url = source["beecount_base_url"] or DEFAULT_BEECOUNT_BASE_URL
    ledger_id = source["beecount_ledger_id"] or ""
    access_env = source["beecount_access_token_env"] or DEFAULT_ACCESS_TOKEN_ENV
    refresh_env = source["beecount_refresh_token_env"] or DEFAULT_REFRESH_TOKEN_ENV
    limit = source["beecount_limit"] or 500
    config_label = str(config_path) if config_path else "未配置"
    access_status = _render_token_status("access token", token_is_configured(access_env))
    refresh_status = _render_token_status("refresh token", token_is_configured(refresh_env))
    mapping_rows_html = _render_beecount_mapping_rows(_fetch_beecount_category_mappings(db_path))
    notice_html = _render_notice(notice)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>BeeCount 连接配置</title>
  <style>
    :root {{
      --bg: #f6f7f9;
      --panel: #ffffff;
      --ink: #1f2933;
      --muted: #6b7280;
      --line: #d8dee8;
      --green: #15803d;
      --red: #b91c1c;
      --blue: #1d4ed8;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    header {{
      background: var(--panel);
      border-bottom: 1px solid var(--line);
    }}
    .wrap {{
      max-width: 900px;
      margin: 0 auto;
      padding: 24px;
    }}
    .topbar {{
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: center;
    }}
    h1 {{ margin: 0; font-size: 26px; }}
    .back-link {{ color: var(--blue); font-weight: 700; text-decoration: none; }}
    .panel {{
      margin-top: 18px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 20px;
    }}
    .notice {{
      margin-top: 18px;
      border: 1px solid var(--line);
      border-left: 4px solid var(--green);
      border-radius: 8px;
      background: #ffffff;
      padding: 12px 14px;
      font-weight: 700;
    }}
    .notice-failure {{ border-left-color: var(--red); }}
    .status-row {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin: 0 0 18px;
    }}
    .token-status {{
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 7px 10px;
      font-size: 13px;
      font-weight: 700;
      background: #f8fafc;
    }}
    .token-status.good {{ color: var(--green); }}
    .token-status.bad {{ color: var(--red); }}
    form {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px;
    }}
    label {{
      display: grid;
      gap: 6px;
      color: var(--muted);
      font-size: 13px;
      font-weight: 700;
    }}
    input {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 7px;
      padding: 10px 11px;
      font: inherit;
      color: var(--ink);
      background: #fff;
    }}
    .wide {{ grid-column: 1 / -1; }}
    .hint {{
      grid-column: 1 / -1;
      margin: 0;
      color: var(--muted);
      line-height: 1.5;
    }}
    .mapping-list {{
      display: grid;
      gap: 10px;
    }}
    .mapping-row {{
      display: grid;
      grid-template-columns: minmax(150px, 1.2fr) 116px 170px minmax(92px, .7fr) minmax(92px, .7fr) 70px 66px;
      gap: 8px;
      align-items: center;
      border-top: 1px solid var(--line);
      padding-top: 10px;
    }}
    .mapping-source {{
      display: grid;
      gap: 3px;
      min-width: 0;
    }}
    .mapping-source strong, .mapping-source span {{
      overflow-wrap: anywhere;
    }}
    .mapping-source span {{
      color: var(--muted);
      font-size: 12px;
    }}
    .mapping-enabled {{
      display: flex;
      gap: 5px;
      align-items: center;
      font-size: 12px;
      color: var(--muted);
    }}
    .mapping-enabled input {{
      width: auto;
    }}
    button {{
      justify-self: start;
      border: 0;
      border-radius: 7px;
      padding: 10px 14px;
      background: var(--ink);
      color: #fff;
      font-weight: 800;
      cursor: pointer;
    }}
    code {{
      overflow-wrap: anywhere;
      color: var(--muted);
    }}
    @media (max-width: 760px) {{
      .wrap {{ padding: 18px; }}
      .topbar {{ align-items: flex-start; flex-direction: column; }}
      form, .mapping-row {{ grid-template-columns: 1fr; }}
      .wide {{ grid-column: auto; }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="wrap topbar">
      <h1>BeeCount 连接配置</h1>
      <a class="back-link" href="/">返回仪表盘</a>
    </div>
  </header>
  <main class="wrap">
    {notice_html}
    <section class="panel">
      <div class="status-row">
        {access_status}
        {refresh_status}
      </div>
      <form method="post" action="/actions/beecount-token-config">
        <label class="wide">BeeCount base URL
          <input name="base_url" value="{html.escape(str(base_url))}" required>
        </label>
        <label>Ledger ID
          <input name="ledger_id" value="{html.escape(str(ledger_id))}" required>
        </label>
        <label>读取上限
          <input name="limit" inputmode="numeric" value="{html.escape(str(limit))}" required>
        </label>
        <label>Access token 环境名
          <input name="access_token_env" value="{html.escape(str(access_env))}" required>
        </label>
        <label>Refresh token 环境名
          <input name="refresh_token_env" value="{html.escape(str(refresh_env))}" required>
        </label>
        <label class="wide">新的 access_token
          <input type="password" name="access_token" autocomplete="off" placeholder="留空则不覆盖 Keychain 中已有值">
        </label>
        <label class="wide">新的 refresh_token
          <input type="password" name="refresh_token" autocomplete="off" placeholder="留空则不覆盖 Keychain 中已有值">
        </label>
        <p class="hint">配置文件只保存连接参数：<code>{html.escape(config_label)}</code>。token 只写入 macOS Keychain，不写入仓库或 data 文件。</p>
        <button type="submit">保存 BeeCount 配置</button>
      </form>
    </section>
    <section class="panel">
      <h2>BeeCount 分类映射</h2>
      <div class="mapping-list">
        {mapping_rows_html}
      </div>
    </section>
  </main>
</body>
</html>"""


def _render_options(options: list[tuple[str, str]], selected: str) -> str:
    parts = []
    for value, label in options:
        selected_attr = " selected" if value == selected else ""
        parts.append(f'<option value="{html.escape(value)}"{selected_attr}>{html.escape(label)}</option>')
    return "".join(parts)


def _render_category_l2_picker(selected_l1: str = "", selected_l2: str = "", disabled: bool = False) -> str:
    selected_l2 = selected_l2 or ""
    preset_values = {category_l2 for _category_l1, category_l2 in CATEGORY_L2_PRESETS}
    custom_value = selected_l2 if selected_l2 and selected_l2 not in preset_values else ""
    parts = ['<option value="">选择明细</option>']
    current_group = ""
    for category_l1, category_l2 in CATEGORY_L2_PRESETS:
        if category_l1 != current_group:
            if current_group:
                parts.append("</optgroup>")
            current_group = category_l1
            parts.append(f'<optgroup label="{html.escape(category_l1)}">')
        value = _category_preset_value(category_l1, category_l2)
        selected_attr = " selected" if category_l2 == selected_l2 else ""
        parts.append(f'<option value="{html.escape(value)}"{selected_attr}>{html.escape(category_l2)}</option>')
    if current_group:
        parts.append("</optgroup>")
    custom_selected = " selected" if custom_value else ""
    disabled_attr = " disabled" if disabled else ""
    parts.append(f'<option value="__custom__"{custom_selected}>自定义添加</option>')
    return (
        '<label class="category-picker">支出明细'
        f'<select name="category_l2_preset"{disabled_attr}>{"".join(parts)}</select>'
        f'<input type="text" name="category_l2_custom" value="{html.escape(custom_value)}" '
        f'placeholder="自定义明细"{disabled_attr}>'
        f'<input type="hidden" name="category_l1" value="{html.escape(selected_l1 or "")}">'
        "</label>"
    )


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
            f'{_render_category_l2_picker(row.get("effective_category_l1") or "", row.get("effective_category_l2") or "")}'
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
        f'{_render_category_l2_picker()}'
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
        f'{_render_category_l2_picker("固定支出")}'
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


def _decision_type_label(value: str) -> str:
    labels = dict(DECISION_TYPE_OPTIONS)
    return labels.get(value, value)


def _payment_type_label(value: str) -> str:
    labels = dict(PAYMENT_TYPE_OPTIONS)
    return labels.get(value, value)


def _render_decision_simulator_form() -> str:
    current_month = date.today().strftime("%Y-%m")
    decision_options = _render_options(DECISION_TYPE_OPTIONS, "mortgage_prepayment")
    payment_options = _render_options(PAYMENT_TYPE_OPTIONS, "one_time")
    return (
        '<form class="entry-form compact-form" method="post" action="/actions/decision-simulation">'
        '<h3>决策模拟</h3>'
        '<label>名称<input name="scenario_name" placeholder="例如：提前还 10 万" required></label>'
        f'<label>类型<select name="decision_type">{decision_options}</select></label>'
        '<label>金额<input type="number" name="amount_yuan" min="0" step="0.01" placeholder="100000" required></label>'
        f'<label>开始月份<input type="month" name="start_month" value="{html.escape(current_month)}" required></label>'
        f'<label>支付方式<select name="payment_type">{payment_options}</select></label>'
        '<label>分期月数<input type="number" name="installment_months" min="1" step="1" placeholder="可选"></label>'
        '<label>月供<input type="number" name="monthly_payment_yuan" min="0" step="0.01" placeholder="可选"></label>'
        '<button type="submit">保存模拟</button>'
        "</form>"
    )


def _risk_label(value: str | None) -> str:
    labels = {
        "safe": "可执行",
        "watch": "观察",
        "tight": "谨慎",
        "danger": "不建议",
    }
    return labels.get(value or "", value or "unknown")


def _render_decision_scenarios(rows: list[dict]) -> str:
    if not rows:
        return '<p class="empty">暂无决策模拟结果</p>'
    parts = []
    for row in rows:
        amount = _format_yuan(row["amount_cents"])
        monthly_payment = ""
        if row.get("monthly_payment_cents"):
            monthly_payment = f' · 月供 {_format_yuan(row["monthly_payment_cents"])} 元'
        elif row.get("installment_months"):
            monthly_payment = f' · {int(row["installment_months"])} 期'
        parts.append(
            '<div class="list-row">'
            '<div class="list-main">'
            f'<strong>{html.escape(row["scenario_name"])} · {html.escape(_risk_label(row.get("result_risk_level")))}</strong>'
            f'<span>{html.escape(_decision_type_label(row["decision_type"]))} · '
            f'{html.escape(_payment_type_label(row["payment_type"]))} · '
            f'{html.escape(row["start_month"])} · 安全垫 '
            f'{html.escape(str(row.get("result_min_safety_months") or 0))} 个月'
            f'{html.escape(monthly_payment)}</span>'
            f'<span>{html.escape(row.get("recommendation") or "")}</span>'
            f'<span>{html.escape(row.get("explanation") or "")}</span>'
            "</div>"
            f'<b class="amount amount-outflow">{html.escape(amount)} 元</b>'
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
        f'{_render_category_l2_picker("固定支出", row.get("category_l2") or "", disabled=bool(disabled))}'
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
    return list(analyze_cashflow(data)["advice"])


def _build_cashflow_signal(data: dict) -> dict[str, object]:
    return analyze_cashflow(data)


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
    confidence = html.escape(str(signal.get("confidence", "unknown")))
    risk_30 = _format_yuan(int(signal.get("risk_next_30_cents") or 0))
    risk_90 = _format_yuan(int(signal.get("risk_next_90_cents") or 0))
    return (
        f'<div class="cashflow-signal signal-{level}">'
        f"<strong>{headline}</strong>"
        f"<p>{reason}</p>"
        f"{safety_html}"
        f'<span class="signal-chip">可信度：{confidence}</span>'
        f'<span class="signal-chip">30天风险：{html.escape(risk_30)} 元</span>'
        f'<span class="signal-chip">3个月风险：{html.escape(risk_90)} 元</span>'
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
    beecount_unknown_count = data["beecount_unknown_count"]
    beecount_pending_count = data["beecount_pending_count"]
    legacy_unknown_count = data["legacy_unknown_count"]
    legacy_pending_count = data["legacy_pending_count"]
    trend_html = _render_trend_bars(data["trend"])
    pipeline_html = _render_pipeline_result(pipeline_result)
    notice_html = _render_notice(notice)
    add_transaction_form_html = _render_add_transaction_form()
    recurring_forms_html = _render_recurring_forms()
    mortgage_prepayment_form_html = _render_mortgage_prepayment_form(data["mortgage_templates"])
    decision_simulator_form_html = _render_decision_simulator_form()
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
    decision_scenarios_html = _render_decision_scenarios(data["decision_scenarios"])
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
    .secondary-action {{
      min-height: 38px;
      border: 1px solid var(--line);
      border-radius: 8px;
      color: var(--ink);
      background: #ffffff;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 0 12px;
      text-decoration: none;
      font-size: 13px;
      font-weight: 700;
      white-space: nowrap;
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
    .category-picker {{
      grid-template-columns: 1fr;
    }}
    .category-picker select,
    .category-picker input {{
      min-width: 0;
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
      grid-template-columns: minmax(180px, 1fr) 150px 108px minmax(170px, 0.8fr) 72px;
      gap: 8px;
      align-items: end;
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
        <a class="secondary-action" href="/settings/beecount">BeeCount 配置</a>
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
        {decision_simulator_form_html}
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
        <h2>语义待处理</h2>
        <div class="review-list">
          <div class="review-item warn"><span>BeeCount unknown</span><strong>{beecount_unknown_count}</strong></div>
          <div class="review-item"><span>BeeCount pending</span><strong>{beecount_pending_count}</strong></div>
          <div class="review-item warn"><span>历史 unknown</span><strong>{legacy_unknown_count}</strong></div>
          <div class="review-item"><span>历史 pending</span><strong>{legacy_pending_count}</strong></div>
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
      <div class="panel">
        <h2>最近模拟结果</h2>
        <div class="breakdown-list">
          {decision_scenarios_html}
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
    beecount_config_path: Path | None = DEFAULT_BEECOUNT_CONFIG
    beecount_input_json: Path | None = None
    beecount_base_url: str | None = None
    beecount_ledger_id: str | None = None
    beecount_access_token_env = "BEECOUNT_ACCESS_TOKEN"
    beecount_refresh_token_env = "BEECOUNT_REFRESH_TOKEN"
    beecount_limit = 500

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/settings/beecount":
            body = render_beecount_settings_html(
                db_path=self.db_path,
                config_path=self.beecount_config_path,
                beecount_input_json=self.beecount_input_json,
                beecount_base_url=self.beecount_base_url,
                beecount_ledger_id=self.beecount_ledger_id,
                beecount_access_token_env=self.beecount_access_token_env,
                beecount_refresh_token_env=self.beecount_refresh_token_env,
                beecount_limit=self.beecount_limit,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
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
            result = run_refresh_pipeline(
                self.db_path,
                self.raw_input_path,
                beecount_config_path=self.beecount_config_path,
                beecount_input_json=self.beecount_input_json,
                beecount_base_url=self.beecount_base_url,
                beecount_ledger_id=self.beecount_ledger_id,
                beecount_access_token_env=self.beecount_access_token_env,
                beecount_refresh_token_env=self.beecount_refresh_token_env,
                beecount_limit=self.beecount_limit,
            )
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
        elif parsed.path == "/actions/decision-simulation":
            notice = self._handle_decision_simulation()
            try:
                body = render_dashboard_html(self.db_path, notice=notice).encode("utf-8")
                status = 200 if notice["ok"] else 400
            except sqlite3.Error as exc:
                body = f"数据库读取失败: {html.escape(str(exc))}".encode("utf-8")
                status = 500
        elif parsed.path == "/actions/beecount-token-config":
            notice = self._handle_beecount_token_config()
            body = render_beecount_settings_html(
                db_path=self.db_path,
                config_path=self.beecount_config_path,
                beecount_input_json=self.beecount_input_json,
                beecount_base_url=self.beecount_base_url,
                beecount_ledger_id=self.beecount_ledger_id,
                beecount_access_token_env=self.beecount_access_token_env,
                beecount_refresh_token_env=self.beecount_refresh_token_env,
                beecount_limit=self.beecount_limit,
                notice=notice,
            ).encode("utf-8")
            status = 200 if notice["ok"] else 400
        elif parsed.path == "/actions/beecount-category-mapping":
            notice = self._handle_beecount_category_mapping()
            body = render_beecount_settings_html(
                db_path=self.db_path,
                config_path=self.beecount_config_path,
                beecount_input_json=self.beecount_input_json,
                beecount_base_url=self.beecount_base_url,
                beecount_ledger_id=self.beecount_ledger_id,
                beecount_access_token_env=self.beecount_access_token_env,
                beecount_refresh_token_env=self.beecount_refresh_token_env,
                beecount_limit=self.beecount_limit,
                notice=notice,
            ).encode("utf-8")
            status = 200 if notice["ok"] else 400
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
        category_l1, category_l2 = _resolve_category_fields(fields)

        return save_manual_override(
            self.db_path,
            transaction_id,
            fields.get("financial_type", [""])[0],
            fields.get("cashflow_direction", [""])[0],
            category_l1=category_l1,
            category_l2=category_l2,
        )

    def _handle_add_transaction(self) -> dict:
        content_length = int(self.headers.get("Content-Length", "0") or 0)
        raw_body = self.rfile.read(content_length).decode("utf-8")
        fields = parse_qs(raw_body)
        category_l1, category_l2 = _resolve_category_fields(fields)
        return save_new_transaction(
            self.db_path,
            fields.get("transaction_date", [""])[0],
            fields.get("amount_yuan", [""])[0],
            fields.get("cashflow_direction", [""])[0],
            fields.get("financial_type", [""])[0],
            fields.get("description", [""])[0],
            account=fields.get("account", [""])[0],
            category_l1=category_l1,
            category_l2=category_l2,
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
        _category_l1, category_l2 = _resolve_category_fields(fields)
        return save_fixed_bill_template(
            self.db_path,
            fields.get("name", [""])[0],
            fields.get("amount_yuan", [""])[0],
            fields.get("start_date", [""])[0],
            fields.get("day_of_month", [""])[0],
            category_l2,
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
        _category_l1, category_l2 = _resolve_category_fields(fields)
        return update_saved_fixed_bill_template(
            self.db_path,
            fields.get("template_id", [""])[0],
            fields.get("name", [""])[0],
            fields.get("amount_yuan", [""])[0],
            fields.get("start_date", [""])[0],
            fields.get("day_of_month", [""])[0],
            category_l2,
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

    def _handle_decision_simulation(self) -> dict:
        content_length = int(self.headers.get("Content-Length", "0") or 0)
        raw_body = self.rfile.read(content_length).decode("utf-8")
        fields = parse_qs(raw_body)
        return save_decision_simulation(
            self.db_path,
            fields.get("scenario_name", [""])[0],
            fields.get("decision_type", [""])[0],
            fields.get("amount_yuan", [""])[0],
            fields.get("start_month", [""])[0],
            fields.get("payment_type", [""])[0],
            installment_months_text=fields.get("installment_months", [""])[0],
            monthly_payment_yuan=fields.get("monthly_payment_yuan", [""])[0],
        )

    def _handle_beecount_token_config(self) -> dict:
        content_length = int(self.headers.get("Content-Length", "0") or 0)
        raw_body = self.rfile.read(content_length).decode("utf-8")
        fields = parse_qs(raw_body)
        return save_beecount_token_config(
            self.beecount_config_path,
            fields.get("base_url", [""])[0],
            fields.get("ledger_id", [""])[0],
            fields.get("limit", ["500"])[0],
            access_token=fields.get("access_token", [""])[0],
            refresh_token=fields.get("refresh_token", [""])[0],
            access_token_env=fields.get("access_token_env", [DEFAULT_ACCESS_TOKEN_ENV])[0],
            refresh_token_env=fields.get("refresh_token_env", [DEFAULT_REFRESH_TOKEN_ENV])[0],
        )

    def _handle_beecount_category_mapping(self) -> dict:
        content_length = int(self.headers.get("Content-Length", "0") or 0)
        raw_body = self.rfile.read(content_length).decode("utf-8")
        fields = parse_qs(raw_body)
        return save_beecount_category_mapping(
            self.db_path,
            fields.get("mapping_id", [""])[0],
            fields.get("cashflow_direction", [""])[0],
            fields.get("financial_type", [""])[0],
            fields.get("category_l1", [""])[0],
            fields.get("category_l2", [""])[0],
            fields.get("enabled", ["0"])[0],
        )

    def log_message(self, format: str, *args) -> None:
        return


def run_server(
    db_path: Path,
    host: str = "127.0.0.1",
    port: int = 8000,
    input_path: Path = DEFAULT_RAW_INPUT,
    beecount_config_path: Path | None = DEFAULT_BEECOUNT_CONFIG,
    beecount_input_json: Path | None = None,
    beecount_base_url: str | None = None,
    beecount_ledger_id: str | None = None,
    beecount_access_token_env: str = "BEECOUNT_ACCESS_TOKEN",
    beecount_refresh_token_env: str = "BEECOUNT_REFRESH_TOKEN",
    beecount_limit: int = 500,
) -> None:
    DashboardHandler.db_path = db_path
    DashboardHandler.raw_input_path = input_path
    DashboardHandler.beecount_config_path = beecount_config_path
    DashboardHandler.beecount_input_json = beecount_input_json
    DashboardHandler.beecount_base_url = beecount_base_url
    DashboardHandler.beecount_ledger_id = beecount_ledger_id
    DashboardHandler.beecount_access_token_env = beecount_access_token_env
    DashboardHandler.beecount_refresh_token_env = beecount_refresh_token_env
    DashboardHandler.beecount_limit = beecount_limit
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    print(f"Dashboard running at http://{host}:{port}")
    print(f"Using database: {db_path}")
    beecount_source = _resolve_beecount_source(
        beecount_config_path,
        beecount_input_json,
        beecount_base_url,
        beecount_ledger_id,
        beecount_access_token_env,
        beecount_refresh_token_env,
        beecount_limit,
    )
    if _beecount_source_configured(
        beecount_source["beecount_input_json"],
        beecount_source["beecount_base_url"],
        beecount_source["beecount_ledger_id"],
    ):
        if beecount_source["beecount_input_json"]:
            print(f"Using BeeCount JSON: {beecount_source['beecount_input_json']}")
        else:
            print(
                f"Using BeeCount API: {beecount_source['beecount_base_url']} "
                f"ledger={beecount_source['beecount_ledger_id']}"
            )
    else:
        print(f"Using CSV input: {input_path}")
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="启动家庭现金流雷达 Web 仪表盘")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 数据库路径")
    parser.add_argument("--input", default=str(DEFAULT_RAW_INPUT), help="CSV 文件或目录路径")
    parser.add_argument("--beecount-config", default=str(DEFAULT_BEECOUNT_CONFIG), help="BeeCount 本地只读来源配置 JSON")
    parser.add_argument("--beecount-input-json", help="BeeCount transactions/items JSON 文件")
    parser.add_argument("--beecount-base-url", default=str(DEFAULT_BEECOUNT_BASE_URL), help="BeeCount Cloud read API base URL")
    parser.add_argument("--beecount-ledger-id", default="", help="BeeCount ledger id / external id")
    parser.add_argument("--beecount-access-token-env", default="BEECOUNT_ACCESS_TOKEN", help="BeeCount access token 环境变量名")
    parser.add_argument("--beecount-refresh-token-env", default="BEECOUNT_REFRESH_TOKEN", help="BeeCount refresh token 环境变量名")
    parser.add_argument("--beecount-limit", type=int, default=500, help="BeeCount read API 单次读取上限")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", type=int, default=8000, help="监听端口")
    args = parser.parse_args()
    run_server(
        Path(args.db),
        host=args.host,
        port=args.port,
        input_path=Path(args.input),
        beecount_config_path=Path(args.beecount_config) if args.beecount_config else None,
        beecount_input_json=Path(args.beecount_input_json) if args.beecount_input_json else None,
        beecount_base_url=args.beecount_base_url or None,
        beecount_ledger_id=args.beecount_ledger_id or None,
        beecount_access_token_env=args.beecount_access_token_env,
        beecount_refresh_token_env=args.beecount_refresh_token_env,
        beecount_limit=args.beecount_limit,
    )


if __name__ == "__main__":
    main()
