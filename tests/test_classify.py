"""Tests for classify.py: rule matching, manual overrides, idempotency, summaries."""

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from app.scripts.classify import _condition_matches, classify
from app.scripts.import_csv import import_csv
from app.scripts.normalize import normalize
from tests.conftest import FIXTURES_DIR, PROJECT_ROOT, SCHEMA_SQL, SEED_RULES_SQL

CLASSIFY_SCRIPT = PROJECT_ROOT / "app" / "scripts" / "classify.py"


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    conn.executescript(SEED_RULES_SQL.read_text(encoding="utf-8"))
    conn.close()
    return path


@pytest.fixture
def db_with_edge_normalized(db_path):
    import_csv(db_path, FIXTURES_DIR / "sample_pixiu_edge_cases.csv")
    normalize(db_path)
    return db_path


@pytest.fixture
def db_with_history_normalized(db_path):
    import_csv(db_path, FIXTURES_DIR / "sample_pixiu_2021_2022.csv")
    normalize(db_path)
    return db_path


def _fetch_normalized(db_path: Path) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM normalized_transactions ORDER BY id").fetchall()
    conn.close()
    return [dict(row) for row in rows]


def _row_by_text(rows: list[dict], text: str) -> dict:
    for row in rows:
        haystack = " ".join(str(row.get(field) or "") for field in ("category_l1", "category_l2", "counterparty", "description"))
        if text in haystack:
            return row
    raise AssertionError(f"no row containing {text!r}")


def _count_by_type(db_path: Path) -> dict[str, int]:
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT financial_type, count(*) FROM normalized_transactions GROUP BY financial_type"
    ).fetchall()
    conn.close()
    return {row[0]: row[1] for row in rows}


class TestConditionMatches:
    def test_year_in(self):
        row = {"year": 2022, "month": 1, "amount_cents": 10000, "cashflow_direction": "inflow", "is_large_amount": 0}
        rule = {
            "rule_name": "history",
            "condition_json": '{"year_in": [2021, 2022]}',
            "target_cashflow_direction": "neutral",
            "target_financial_type": "historical_debt_asset_event",
        }
        assert _condition_matches(row, rule)

    def test_text_contains_and_direction(self):
        row = {
            "year": 2025,
            "amount_cents": 3000000,
            "cashflow_direction": "inflow",
            "category_l1": "借款",
            "category_l2": "借入",
            "account": "招商银行",
            "counterparty": "亲友李四",
            "description": "亲友借款周转",
            "is_large_amount": 1,
        }
        rule = {
            "rule_name": "debt_inflow",
            "condition_json": '{"any_text_contains": ["借款"], "direction_in": ["收入", "in"]}',
            "target_cashflow_direction": "inflow",
            "target_financial_type": "debt_inflow",
        }
        assert _condition_matches(row, rule)

    def test_target_direction_prevents_income_matching_outflow_rule(self):
        row = {
            "year": 2025,
            "amount_cents": 300000,
            "cashflow_direction": "inflow",
            "category_l1": "理财",
            "category_l2": "基金赎回",
            "account": "支付宝",
            "counterparty": "天天基金",
            "description": "赎回货币基金",
            "is_large_amount": 0,
        }
        rule = {
            "rule_name": "investment_outflow",
            "condition_json": '{"any_text_contains": ["基金", "买入"]}',
            "target_cashflow_direction": "outflow",
            "target_financial_type": "investment_outflow",
        }
        assert not _condition_matches(row, rule)

    def test_unsupported_operator_raises(self):
        row = {"year": 2025, "amount_cents": 10000, "cashflow_direction": "outflow", "is_large_amount": 0}
        rule = {
            "rule_name": "bad_rule",
            "condition_json": '{"unknown_operator": ["x"]}',
            "target_cashflow_direction": "outflow",
            "target_financial_type": "unknown",
        }
        with pytest.raises(ValueError, match="不支持"):
            _condition_matches(row, rule)

    def test_list_operator_requires_array(self):
        row = {"year": 2025, "amount_cents": 10000, "cashflow_direction": "outflow", "is_large_amount": 0}
        rule = {
            "rule_name": "bad_shape",
            "condition_json": '{"any_text_contains": "餐饮"}',
            "target_cashflow_direction": "outflow",
            "target_financial_type": "living_expense",
        }
        with pytest.raises(ValueError, match="必须是数组"):
            _condition_matches(row, rule)


class TestRuleClassification:
    def test_core_edge_case_types(self, db_with_edge_normalized):
        classify(db_with_edge_normalized)
        rows = _fetch_normalized(db_with_edge_normalized)

        assert _row_by_text(rows, "3月工资")["financial_type"] == "stable_income"
        assert _row_by_text(rows, "2024年终奖")["financial_type"] == "one_time_income"

        credit_card = _row_by_text(rows, "信用卡还款")
        assert credit_card["financial_type"] == "credit_card_payment"
        assert credit_card["cashflow_direction"] == "neutral"

        transfer = _row_by_text(rows, "余额宝转入")
        assert transfer["financial_type"] == "internal_transfer"
        assert transfer["cashflow_direction"] == "neutral"

        borrowing = _row_by_text(rows, "亲友借款")
        assert borrowing["financial_type"] == "debt_inflow"
        assert borrowing["cashflow_direction"] == "inflow"

        mortgage = _row_by_text(rows, "3月房贷")
        assert mortgage["financial_type"] == "debt_payment"
        assert mortgage["cashflow_direction"] == "outflow"

        car_loan = _row_by_text(rows, "3月车贷")
        assert car_loan["financial_type"] == "debt_payment"

    def test_investment_asset_refund_and_living_types(self, db_with_edge_normalized):
        classify(db_with_edge_normalized)
        rows = _fetch_normalized(db_with_edge_normalized)

        fund_buy = _row_by_text(rows, "定投沪深300")
        assert fund_buy["financial_type"] == "investment_outflow"
        assert fund_buy["is_investment_related"] == 1

        fund_redeem = _row_by_text(rows, "赎回货币基金")
        assert fund_redeem["financial_type"] == "investment_inflow"
        assert fund_redeem["cashflow_direction"] == "inflow"

        tesla = _row_by_text(rows, "Model Y")
        assert tesla["financial_type"] == "asset_purchase"
        assert tesla["is_asset_related"] == 1

        sale = _row_by_text(rows, "出售旧手机")
        assert sale["financial_type"] == "asset_sale"

        refund = _row_by_text(rows, "退货退款")
        assert refund["financial_type"] == "refund"
        assert refund["cashflow_direction"] == "inflow"

        breakfast = _row_by_text(rows, "早餐")
        assert breakfast["financial_type"] == "living_expense"

        insurance = _row_by_text(rows, "车险月均")
        assert insurance["financial_type"] == "fixed_expense"

    def test_investment_and_asset_are_not_living_expense(self, db_with_edge_normalized):
        classify(db_with_edge_normalized)
        rows = _fetch_normalized(db_with_edge_normalized)
        assert _row_by_text(rows, "定投沪深300")["financial_type"] != "living_expense"
        assert _row_by_text(rows, "Model Y")["financial_type"] != "living_expense"

    def test_2021_2022_all_historical(self, db_with_history_normalized):
        classify(db_with_history_normalized)
        rows = _fetch_normalized(db_with_history_normalized)
        assert len(rows) == 12
        assert {row["financial_type"] for row in rows} == {"historical_debt_asset_event"}
        assert {row["cashflow_direction"] for row in rows} == {"neutral"}

    def test_unknown_fallback(self, db_path, tmp_path):
        csv_path = tmp_path / "unknown.csv"
        csv_path.write_text(
            "时间,金额,类型,账户,分类,子分类,商户,备注\n"
            "2025-04-01 10:00:00,123.45,支出,招商银行,其他,其他,陌生商户,无法识别事项\n",
            encoding="utf-8",
        )
        import_csv(db_path, csv_path)
        normalize(db_path)
        classify(db_path)
        row = _fetch_normalized(db_path)[0]
        assert row["financial_type"] == "unknown"
        assert row["review_status"] == "pending"


class TestManualOverride:
    def test_manual_financial_type_not_overwritten(self, db_with_edge_normalized):
        conn = sqlite3.connect(str(db_with_edge_normalized))
        row_id = conn.execute(
            "SELECT id FROM normalized_transactions WHERE description = '早餐'"
        ).fetchone()[0]
        conn.execute(
            """UPDATE normalized_transactions
               SET manual_financial_type = 'asset_purchase',
                   manual_category_l1 = '人工分类',
                   manual_cashflow_direction = 'outflow',
                   financial_type = 'asset_purchase',
                   category_l1 = '人工分类',
                   cashflow_direction = 'outflow'
               WHERE id = ?""",
            (row_id,),
        )
        conn.commit()
        conn.close()

        classify(db_with_edge_normalized)
        rows = _fetch_normalized(db_with_edge_normalized)
        row = next(row for row in rows if row["id"] == row_id)
        assert row["manual_financial_type"] == "asset_purchase"
        assert row["financial_type"] == "asset_purchase"
        assert row["category_l1"] == "人工分类"


class TestReviewStatusAndIdempotency:
    def test_review_status_rules(self, db_with_edge_normalized):
        classify(db_with_edge_normalized)
        rows = _fetch_normalized(db_with_edge_normalized)
        assert _row_by_text(rows, "3月工资")["review_status"] == "pending"
        assert _row_by_text(rows, "退货退款")["review_status"] == "approved"
        assert _row_by_text(rows, "早餐")["review_status"] == "pending"
        assert _row_by_text(rows, "Model Y")["review_status"] == "pending"

    def test_run_twice_same_counts(self, db_with_edge_normalized):
        classify(db_with_edge_normalized)
        counts1 = _count_by_type(db_with_edge_normalized)
        classify(db_with_edge_normalized)
        counts2 = _count_by_type(db_with_edge_normalized)
        assert counts1 == counts2

    def test_dry_run_no_write(self, db_with_edge_normalized):
        classify(db_with_edge_normalized, dry_run=True)
        counts = _count_by_type(db_with_edge_normalized)
        assert counts == {"credit_card_payment": 1, "internal_transfer": 1, "unknown": 16}


class TestOutputSummaryAndCLI:
    def test_summary_format(self, db_with_edge_normalized, capsys):
        classify(db_with_edge_normalized)
        captured = capsys.readouterr()
        assert "classified=18" in captured.out
        assert "unknown=0" in captured.out
        assert "manual_skipped=0" in captured.out
        assert "failed=0" in captured.out

    def test_summary_manual_skipped(self, db_with_edge_normalized, capsys):
        conn = sqlite3.connect(str(db_with_edge_normalized))
        conn.execute(
            "UPDATE normalized_transactions SET manual_financial_type = 'living_expense' WHERE description = '早餐'"
        )
        conn.commit()
        conn.close()
        classify(db_with_edge_normalized)
        captured = capsys.readouterr()
        assert "classified=17" in captured.out
        assert "manual_skipped=1" in captured.out

    def test_cli_full_run(self, db_with_edge_normalized):
        result = subprocess.run(
            [sys.executable, str(CLASSIFY_SCRIPT), "--db", str(db_with_edge_normalized)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "classified=18" in result.stdout

    def test_cli_required_args(self):
        result = subprocess.run(
            [sys.executable, str(CLASSIFY_SCRIPT)],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
