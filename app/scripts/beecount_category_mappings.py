"""BeeCount category to Family Cashflow Radar semantic mappings."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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
KIND_BY_DIRECTION_RAW = {"收入": "income", "支出": "expense", "转账": "transfer"}


@dataclass(frozen=True)
class CategoryMapping:
    beecount_kind: str
    category_name: str
    parent_name: str
    level: int
    radar_cashflow_direction: str
    radar_financial_type: str
    radar_category_l1: str
    radar_category_l2: str
    confidence: float
    enabled: int = 1
    mapping_source: str = "inferred"
    notes: str = ""


def ensure_mapping_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS beecount_category_mappings (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               beecount_kind TEXT NOT NULL CHECK(beecount_kind IN ('expense', 'income', 'transfer')),
               category_name TEXT NOT NULL,
               parent_name TEXT DEFAULT '',
               level INTEGER DEFAULT 1,
               radar_cashflow_direction TEXT NOT NULL CHECK(radar_cashflow_direction IN ('inflow', 'outflow', 'neutral')),
               radar_financial_type TEXT NOT NULL CHECK(radar_financial_type IN (
                   'stable_income', 'one_time_income', 'living_expense', 'fixed_expense',
                   'debt_payment', 'debt_inflow', 'asset_purchase', 'asset_sale',
                   'investment_outflow', 'investment_inflow', 'internal_transfer',
                   'credit_card_payment', 'refund', 'reimbursable_expense',
                   'reimbursement_income', 'historical_debt_asset_event', 'unknown'
               )),
               radar_category_l1 TEXT DEFAULT '',
               radar_category_l2 TEXT DEFAULT '',
               confidence REAL DEFAULT 1.0,
               enabled INTEGER DEFAULT 1,
               mapping_source TEXT DEFAULT 'inferred',
               notes TEXT DEFAULT '',
               created_at TEXT DEFAULT CURRENT_TIMESTAMP,
               updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
               UNIQUE(beecount_kind, category_name, parent_name)
           )"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_beecount_category_mappings_lookup
           ON beecount_category_mappings(beecount_kind, category_name, enabled)"""
    )


def _mapping(
    kind: str,
    name: str,
    parent: str,
    level: int,
    direction: str,
    financial_type: str,
    category_l1: str,
    category_l2: str | None = None,
    confidence: float = 1.0,
    source: str = "inferred",
    notes: str = "",
) -> CategoryMapping:
    return CategoryMapping(
        beecount_kind=kind,
        category_name=name,
        parent_name=parent,
        level=int(level or 1),
        radar_cashflow_direction=direction,
        radar_financial_type=financial_type,
        radar_category_l1=category_l1,
        radar_category_l2=category_l2 if category_l2 is not None else name,
        confidence=confidence,
        mapping_source=source,
        notes=notes,
    )


def infer_category_mapping(kind: str, name: str, parent_name: str = "", level: int = 1) -> CategoryMapping:
    """Infer a deterministic first mapping for a BeeCount category.

    The mapping is intentionally conservative and editable. It uses BeeCount's
    kind + category hierarchy as the source of truth, then falls back to unknown
    only when neither category nor parent carries enough semantic information.
    """
    kind = (kind or "").strip()
    name = (name or "").strip()
    parent = (parent_name or "").strip()
    text = f"{parent} {name}"

    if kind == "transfer" or name in {"转账", "账户互转", "内部转账"}:
        return _mapping(kind, name, parent, level, "neutral", "internal_transfer", "内部转账", "账户互转")

    if kind == "income":
        if parent == "工资" or name in {"工资", "基本工资", "绩效奖金", "加班费"}:
            return _mapping(kind, name, parent, level, "inflow", "stable_income", "收入", "工资")
        if parent in {"报销"} or "报销" in text:
            return _mapping(kind, name, parent, level, "inflow", "reimbursement_income", "垫付报销", "报销回款")
        if parent in {"退款", "退税"} or any(token in text for token in ("退款", "退费", "退税")):
            return _mapping(kind, name, parent, level, "inflow", "refund", "退款", name)
        if parent in {"理财", "投资收益", "利息"} or any(token in text for token in ("理财", "基金", "股票", "利息", "分红", "收益")):
            return _mapping(kind, name, parent, level, "inflow", "investment_inflow", "投资", name)
        if parent in {"二手交易", "公积金"} or any(token in text for token in ("二手", "闲置", "公积金提取")):
            return _mapping(kind, name, parent, level, "inflow", "asset_sale", "资产出售", name)
        if parent in {"奖金", "红包", "兼职", "结婚礼金", "社会福利"}:
            return _mapping(kind, name, parent, level, "inflow", "one_time_income", "收入", name, confidence=0.9)
        return _mapping(kind, name, parent, level, "inflow", "unknown", "未映射", name, confidence=0.2, notes="新增收入分类，需确认现金流语义")

    if kind == "expense":
        if name in {"信用卡还款", "购汇还款"}:
            return _mapping(kind, name, parent, level, "neutral", "credit_card_payment", "债务", "信用卡还款")
        if name in {"基金赎回", "现金分红"}:
            return _mapping(kind, name, parent, level, "inflow", "investment_inflow", "投资", name)
        if name in {"基金申购", "证券买入"} or parent == "投资亏损" or any(token in text for token in ("基金", "股票", "证券", "理财")):
            return _mapping(kind, name, parent, level, "outflow", "investment_outflow", "投资", name)
        if name in {"房贷"}:
            return _mapping(kind, name, parent, level, "outflow", "debt_payment", "债务", "房贷")
        if parent in {"住房", "订阅服务"} or name in {"培训费", "学费", "宽带", "房租", "物业费", "汽车保险"}:
            return _mapping(kind, name, parent, level, "outflow", "fixed_expense", "固定支出", name)
        if name in {"装修", "家电", "家具"}:
            return _mapping(kind, name, parent, level, "outflow", "asset_purchase", "资产购入", name)
        living_names = {
            "早餐", "午餐", "晚餐", "夜宵", "美团外卖", "饿了么外卖", "京东外卖", "餐厅", "美食",
            "出租车", "网约车", "公交", "地铁", "停车费", "加油",
            "苹果", "香蕉", "橙子", "葡萄", "西瓜", "其他水果",
            "饼干", "薯片", "糖果", "巧克力", "坚果", "蛋糕", "面包", "甜点",
            "奶茶", "咖啡", "果汁", "汽水", "矿泉水",
            "蔬菜", "肉类", "水产", "粮油", "调料",
            "服装", "鞋帽", "日用百货", "洗护用品", "清洁用品", "纸品", "厨房用品",
            "电影", "KTV", "酒吧", "游乐场", "其他娱乐",
        }
        if parent in {
            "餐饮", "饮品", "水果", "零食", "糕点", "做饭食材", "交通", "购物",
            "服饰", "日用品", "宠物", "美容", "娱乐", "游戏", "运动", "保健品",
            "汽车", "居家",
        } or name in living_names | {"餐饮", "交通", "购物", "零食", "水果", "饮品", "糕点", "做饭食材"}:
            category_l1 = "日常生活"
            category_l2 = parent if parent and parent != name else name
            return _mapping(kind, name, parent, level, "outflow", "living_expense", category_l1, category_l2, confidence=0.95)
        return _mapping(kind, name, parent, level, "outflow", "unknown", "未映射", name, confidence=0.2, notes="新增支出分类，需确认现金流语义")

    return _mapping("expense", name, parent, level, "outflow", "unknown", "未映射", name, confidence=0.1, notes="未知 BeeCount kind")


def upsert_mapping(conn: sqlite3.Connection, mapping: CategoryMapping) -> bool:
    ensure_mapping_schema(conn)
    existing = conn.execute(
        """SELECT id, radar_financial_type, mapping_source
           FROM beecount_category_mappings
           WHERE beecount_kind = ? AND category_name = ? AND parent_name = ?""",
        (mapping.beecount_kind, mapping.category_name, mapping.parent_name),
    ).fetchone()
    if existing:
        existing_id, existing_type, existing_source = existing
        if existing_type == "unknown" and existing_source in {"inferred", "raw_seen"} and mapping.radar_financial_type != "unknown":
            conn.execute(
                """UPDATE beecount_category_mappings
                   SET radar_cashflow_direction = ?,
                       radar_financial_type = ?,
                       radar_category_l1 = ?,
                       radar_category_l2 = ?,
                       confidence = ?,
                       enabled = ?,
                       mapping_source = ?,
                       notes = ?,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (
                    mapping.radar_cashflow_direction,
                    mapping.radar_financial_type,
                    mapping.radar_category_l1,
                    mapping.radar_category_l2,
                    mapping.confidence,
                    mapping.enabled,
                    mapping.mapping_source,
                    mapping.notes,
                    existing_id,
                ),
            )
            return False
        return False
    result = conn.execute(
        """INSERT INTO beecount_category_mappings
           (beecount_kind, category_name, parent_name, level,
            radar_cashflow_direction, radar_financial_type, radar_category_l1, radar_category_l2,
            confidence, enabled, mapping_source, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            mapping.beecount_kind,
            mapping.category_name,
            mapping.parent_name,
            mapping.level,
            mapping.radar_cashflow_direction,
            mapping.radar_financial_type,
            mapping.radar_category_l1,
            mapping.radar_category_l2,
            mapping.confidence,
            mapping.enabled,
            mapping.mapping_source,
            mapping.notes,
        ),
    )
    return result.rowcount > 0


def sync_mappings_from_raw_transactions(conn: sqlite3.Connection) -> dict[str, int]:
    ensure_mapping_schema(conn)
    rows = conn.execute(
        """SELECT direction_raw, category_original, COUNT(*) AS transaction_count
           FROM raw_transactions
           WHERE source_file LIKE 'beecount_cloud:%'
             AND COALESCE(category_original, '') != ''
           GROUP BY direction_raw, category_original"""
    ).fetchall()
    inserted = 0
    inferred_unknown = 0
    for direction_raw, category_name, _count in rows:
        kind = KIND_BY_DIRECTION_RAW.get(str(direction_raw or ""), "expense")
        mapping = infer_category_mapping(kind, str(category_name or ""), "", 1)
        if upsert_mapping(conn, mapping):
            inserted += 1
            if mapping.radar_financial_type == "unknown":
                inferred_unknown += 1
    return {"inserted": inserted, "inferred_unknown": inferred_unknown}


def load_mappings(conn: sqlite3.Connection) -> dict[tuple[str, str], dict[str, Any]]:
    ensure_mapping_schema(conn)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT *
           FROM beecount_category_mappings
           WHERE enabled = 1
           ORDER BY id"""
    ).fetchall()
    return {(row["beecount_kind"], row["category_name"]): dict(row) for row in rows}


def apply_beecount_mapping(row: dict[str, Any], mappings: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any] | None:
    if not str(row.get("source_file") or "").startswith("beecount_cloud:"):
        return None
    category_name = str(row.get("beecount_category") or row.get("category_l1") or "").strip()
    if not category_name:
        return None
    kind = KIND_BY_DIRECTION_RAW.get(str(row.get("raw_direction") or ""), "")
    mapping = mappings.get((kind, category_name))
    if not mapping and category_name in {"转账", "信用卡还款", "购汇还款", "基金赎回", "现金分红"}:
        for candidate_kind in ("transfer", "expense", "income"):
            mapping = mappings.get((candidate_kind, category_name))
            if mapping:
                break
    if not mapping:
        return None
    return {
        "id": None,
        "rule_name": f"beecount_category:{kind}:{category_name}",
        "target_cashflow_direction": mapping["radar_cashflow_direction"],
        "target_financial_type": mapping["radar_financial_type"],
        "target_category_l1": mapping["radar_category_l1"],
        "target_category_l2": mapping["radar_category_l2"],
        "is_internal_transfer": 1 if mapping["radar_financial_type"] == "internal_transfer" else 0,
        "is_debt_related": 1 if mapping["radar_financial_type"] in {"debt_payment", "credit_card_payment", "debt_inflow"} else 0,
        "is_asset_related": 1 if mapping["radar_financial_type"] in {"asset_purchase", "asset_sale"} else 0,
        "is_investment_related": 1 if mapping["radar_financial_type"] in {"investment_outflow", "investment_inflow"} else 0,
        "confidence": mapping["confidence"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="同步 BeeCount 分类到本地现金流语义映射表")
    parser.add_argument("--db", default="data/processed/cashflow.db")
    args = parser.parse_args(argv)

    conn = sqlite3.connect(args.db)
    try:
        summary = sync_mappings_from_raw_transactions(conn)
        conn.commit()
    except Exception as exc:
        print(f"错误: BeeCount 分类映射同步失败: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(f"mappings_inserted={summary['inserted']} inferred_unknown={summary['inferred_unknown']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
