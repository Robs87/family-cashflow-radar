"""Tests for app/main.py: dashboard HTML and HTTP handler."""

import json
import sqlite3
import threading
import urllib.error
import urllib.request
from decimal import Decimal
from urllib.parse import urlencode
from http.server import ThreadingHTTPServer

import pytest

from app.main import (
    DashboardHandler,
    _build_cashflow_signal,
    _build_financial_advice,
    _resolve_beecount_source,
    _format_yuan,
    render_beecount_settings_html,
    render_dashboard_html,
    run_refresh_pipeline,
    run_recurring_generation,
    save_beecount_token_config,
    save_beecount_category_mapping,
    save_current_cash_balance,
    save_decision_simulation,
    save_fixed_bill_template,
    save_manual_override,
    save_mortgage_template,
    save_mortgage_prepayment,
    save_new_transaction,
    update_saved_fixed_bill_template,
    update_saved_mortgage_template,
    update_saved_mortgage_prepayment,
)
from app.scripts.classify import classify
from app.scripts.generate_monthly_cashflow import generate_monthly_cashflow
from app.scripts.import_csv import import_csv
from app.scripts.normalize import normalize
from app.scripts.recurring import create_mortgage_template
from tests.conftest import FIXTURES_DIR, SCHEMA_SQL, SEED_RULES_SQL


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
def db_with_dashboard_data(db_path):
    import_csv(db_path, FIXTURES_DIR)
    normalize(db_path)
    classify(db_path)
    generate_monthly_cashflow(db_path)
    return db_path


@pytest.fixture
def dashboard_server(db_with_dashboard_data):
    class TestHandler(DashboardHandler):
        db_path = db_with_dashboard_data
        beecount_config_path = None

    server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class TestFormatYuan:
    def test_positive(self):
        assert _format_yuan(2_000_000) == "20,000.00"

    def test_negative(self):
        assert _format_yuan(-15_816_300) == "-158,163.00"


class TestRenderDashboard:
    def test_renders_required_metrics(self, db_with_dashboard_data):
        html = render_dashboard_html(db_with_dashboard_data)

        assert "<title>家庭现金流雷达</title>" in html
        assert 'action="/actions/refresh"' in html
        assert 'href="/settings/beecount"' in html
        assert "刷新数据" in html
        assert "记录一笔收入或支出" in html
        assert 'action="/actions/add-transaction"' in html
        assert "财务建议" in html
        assert "当前家庭现金流：" in html
        assert "安全垫：" in html
        assert "固定支出和债务还款压力偏高" in html
        assert "最近记录" in html
        assert "本月支出分析" in html
        assert "自动记账" in html
        assert 'action="/actions/add-mortgage-template"' in html
        assert 'action="/actions/add-fixed-bill-template"' in html
        assert 'action="/actions/update-mortgage-template"' not in html
        assert "先创建房贷模板，再添加提前还款计划" in html
        assert 'action="/actions/generate-recurring"' in html
        assert "决策模拟" in html
        assert 'action="/actions/decision-simulation"' in html
        assert 'name="mortgage_template_id"' in html
        assert 'name="mortgage_effect_type"' in html
        assert "最近模拟结果" in html
        assert "本月稳定收入" in html
        assert "20,000.00 元" in html
        assert "本月刚性支出" in html
        assert "2,000.00 元" in html
        assert "本月债务还款" in html
        assert "11,500.00 元" in html
        assert "本月基础结余" in html
        assert "5,450.00 元" in html
        assert "近 12 月基础结余趋势" in html
        assert "语义待处理" in html
        assert "BeeCount unknown" in html
        assert "BeeCount pending" in html
        assert "历史 unknown" in html
        assert "历史 pending" in html
        assert 'id="review-panel"' in html
        assert 'action="/actions/manual-override#review-panel"' in html
        assert 'data-preserve-scroll="review"' in html
        assert 'name="category_l2_preset"' in html
        assert 'name="category_l2_custom"' in html
        assert "自定义添加" in html
        assert "支出明细" in html
        assert "family-cashflow-radar.reviewScrollY" in html
        assert "稳定收入" in html

    def test_renders_beecount_settings_without_token_values(self, tmp_path, monkeypatch):
        config_path = tmp_path / "beecount_source.json"
        monkeypatch.setattr("app.main.token_is_configured", lambda env_name: env_name == "BEECOUNT_REFRESH_TOKEN")

        html = render_beecount_settings_html(
            config_path=config_path,
            beecount_base_url="https://bee.example",
            beecount_ledger_id="ledger-1",
        )

        assert "BeeCount 连接配置" in html
        assert 'name="access_token"' in html
        assert "access token：未配置" in html
        assert "refresh token：已配置" in html
        assert "留空则不覆盖 Keychain 中已有值" in html

    def test_renders_beecount_category_mappings(self, db_path, tmp_path, monkeypatch):
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """INSERT INTO beecount_category_mappings
               (beecount_kind, category_name, radar_cashflow_direction,
                radar_financial_type, radar_category_l1, radar_category_l2)
               VALUES ('expense', '早餐', 'outflow', 'living_expense', '日常生活', '早餐')"""
        )
        conn.commit()
        conn.close()
        monkeypatch.setattr("app.main.token_is_configured", lambda env_name: False)

        html = render_beecount_settings_html(
            db_path=db_path,
            config_path=tmp_path / "beecount_source.json",
            beecount_base_url="https://bee.example",
            beecount_ledger_id="ledger-1",
        )

        assert "BeeCount 分类映射" in html
        assert "早餐" in html
        assert 'action="/actions/beecount-category-mapping"' in html
        assert "日常生活" in html

    def test_save_beecount_category_mapping_updates_semantics(self, db_path):
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """INSERT INTO beecount_category_mappings
               (beecount_kind, category_name, radar_cashflow_direction,
                radar_financial_type, radar_category_l1, radar_category_l2)
               VALUES ('expense', '新分类', 'outflow', 'unknown', '未映射', '新分类')"""
        )
        mapping_id = conn.execute("SELECT id FROM beecount_category_mappings").fetchone()[0]
        conn.commit()
        conn.close()

        result = save_beecount_category_mapping(
            db_path,
            str(mapping_id),
            "outflow",
            "living_expense",
            "日常生活",
            "新分类",
            "1",
        )

        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            """SELECT radar_financial_type, radar_category_l1, mapping_source
               FROM beecount_category_mappings WHERE id = ?""",
            (mapping_id,),
        ).fetchone()
        conn.close()

        assert result["ok"] is True
        assert row == ("living_expense", "日常生活", "manual")

    def test_save_beecount_token_config_writes_tokens_to_keychain_and_config(self, tmp_path, monkeypatch):
        config_path = tmp_path / "beecount_source.json"
        calls = []

        def fake_write(config_path_arg, **kwargs):
            calls.append((config_path_arg, kwargs))
            config_path_arg.write_text(json.dumps({"base_url": kwargs["base_url"]}), encoding="utf-8")

        monkeypatch.setattr("app.main.write_beecount_config", fake_write)

        result = save_beecount_token_config(
            config_path,
            "https://bee.example/",
            "ledger-1",
            "200",
            access_token="access-secret",
            refresh_token="refresh-secret",
        )

        assert result == {"ok": True, "message": "BeeCount 连接配置已保存，token 已写入 Keychain"}
        assert calls == [
            (
                config_path,
                {
                    "base_url": "https://bee.example",
                    "ledger_id": "ledger-1",
                    "limit": 200,
                    "access_token": "access-secret",
                    "refresh_token": "refresh-secret",
                    "access_token_env": "BEECOUNT_ACCESS_TOKEN",
                    "refresh_token_env": "BEECOUNT_REFRESH_TOKEN",
                },
            )
        ]

    def test_save_beecount_token_config_rejects_bad_url(self, tmp_path):
        result = save_beecount_token_config(tmp_path / "source.json", "bee.example", "ledger-1", "200")

        assert result["ok"] is False
        assert "base URL" in result["message"]

    def test_save_beecount_token_config_can_update_config_without_overwriting_tokens(self, tmp_path, monkeypatch):
        config_path = tmp_path / "beecount_source.json"

        def fake_write(config_path_arg, **kwargs):
            assert kwargs["access_token"] == ""
            assert kwargs["refresh_token"] == ""
            config_path_arg.write_text(json.dumps({"base_url": kwargs["base_url"]}), encoding="utf-8")

        monkeypatch.setattr("app.main.write_beecount_config", fake_write)

        result = save_beecount_token_config(config_path, "https://bee.example", "ledger-1", "200")

        assert result == {"ok": True, "message": "BeeCount 连接配置已保存，Keychain token 未覆盖"}

    def test_renders_twelve_month_trend_limit(self, db_path):
        conn = sqlite3.connect(str(db_path))
        for month in range(1, 13):
            conn.execute(
                """INSERT INTO monthly_cashflow
                   (year, month, stable_income_cents, net_operating_cashflow_cents)
                   VALUES (2025, ?, 100000, ?)""",
                (month, month * 10000),
            )
        conn.commit()
        conn.close()

        html = render_dashboard_html(db_path)
        assert html.count('<div class="trend-row">') == 12
        assert "2025-01" in html
        assert "2025-12" in html

    def test_no_monthly_data_state(self, db_path):
        html = render_dashboard_html(db_path)
        assert "暂无月份" in html
        assert "暂无月度现金流数据" in html
        assert "当前家庭现金流：观察状态" in html
        assert "BeeCount unknown</span><strong>0</strong>" in html

    def test_initializes_empty_database_before_rendering(self, tmp_path):
        db_path = tmp_path / "fresh.db"

        html = render_dashboard_html(db_path)

        assert "暂无月度现金流数据" in html
        conn = sqlite3.connect(str(db_path))
        has_monthly = conn.execute(
            """SELECT 1
               FROM sqlite_master
               WHERE type = 'table' AND name = 'monthly_cashflow'"""
        ).fetchone()
        rules_count = conn.execute("SELECT COUNT(*) FROM classification_rules").fetchone()[0]
        conn.close()
        assert has_monthly == (1,)
        assert rules_count > 0

    def test_escapes_database_text(self, db_path):
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """INSERT INTO monthly_cashflow
               (year, month, stable_income_cents, net_operating_cashflow_cents)
               VALUES (2025, 1, 100000, 100000)"""
        )
        conn.execute(
            "INSERT INTO raw_transactions (source_file, source_row_no, amount_cents, raw_hash) VALUES ('x', 1, 10000, 'xss')"
        )
        conn.execute(
            """INSERT INTO normalized_transactions
               (raw_transaction_id, transaction_date, year, month, amount_cents,
                cashflow_direction, financial_type, description)
               VALUES (1, '2025-01-01', 2025, 1, 10000, 'outflow', 'unknown', '<script>alert(1)</script>')"""
        )
        conn.commit()
        conn.close()

        html = render_dashboard_html(db_path)
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html

    def test_renders_pipeline_result(self, db_with_dashboard_data):
        result = {
            "ok": True,
            "steps": [
                {
                    "label": "导入 CSV",
                    "ok": True,
                    "exit_code": 0,
                    "stdout": "imported=0 skipped_duplicate=35 failed=0",
                    "stderr": "",
                }
            ],
        }

        html = render_dashboard_html(db_with_dashboard_data, pipeline_result=result)

        assert "刷新完成" in html
        assert "导入 CSV" in html
        assert "imported=0 skipped_duplicate=35 failed=0" in html

    def test_renders_manual_override_notice(self, db_with_dashboard_data):
        html = render_dashboard_html(db_with_dashboard_data, notice={"ok": True, "message": "人工修正已保存"})

        assert "人工修正已保存" in html
        assert "notice-success" in html

    def test_cashflow_signal_marks_negative_month_as_danger(self, db_path):
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """INSERT INTO monthly_cashflow
               (year, month, stable_income_cents, fixed_expense_cents,
                living_expense_cents, debt_payment_cents, net_operating_cashflow_cents)
               VALUES (2026, 5, 1000000, 700000, 400000, 100000, -200000)"""
        )
        conn.commit()
        conn.close()

        html = render_dashboard_html(db_path)

        assert "当前家庭现金流：危险状态" in html
        assert "先暂停非必要大额消费" in html
        assert "signal-danger" in html

    def test_cashflow_signal_calculation_uses_required_outflow_buffer(self):
        signal = _build_cashflow_signal(
            {
                "latest_month": {
                    "year": 2026,
                    "month": 5,
                    "stable_income_cents": 2_000_000,
                    "living_expense_cents": 100_000,
                    "fixed_expense_cents": 300_000,
                    "debt_payment_cents": 200_000,
                    "net_operating_cashflow_cents": 1_400_000,
                },
                "unknown_count": 0,
                "pending_count": 0,
            }
        )

        assert signal["level"] == "safe"
        assert signal["label"] == "安全状态"
        assert signal["safety_months"] == 2.8
        assert signal["confidence"] == "high"

    def test_cashflow_signal_uses_known_future_30_day_risk(self):
        signal = _build_cashflow_signal(
            {
                "latest_month": {
                    "year": 2026,
                    "month": 5,
                    "stable_income_cents": 2_000_000,
                    "living_expense_cents": 100_000,
                    "fixed_expense_cents": 100_000,
                    "debt_payment_cents": 100_000,
                    "net_operating_cashflow_cents": 500_000,
                },
                "unknown_count": 0,
                "pending_count": 0,
                "upcoming_bills": [
                    {"due_date": "2026-05-20", "amount_cents": 600_000},
                    {"due_date": "2026-08-30", "amount_cents": 800_000},
                ],
            }
        )

        assert signal["level"] == "tight"
        assert signal["risk_next_30_cents"] == 600_000
        assert signal["risk_next_90_cents"] == 600_000
        assert "未来 30 天不建议新增大额支出" in signal["headline"]

    def test_cashflow_signal_marks_missing_income_as_low_confidence(self):
        signal = _build_cashflow_signal(
            {
                "latest_month": {
                    "year": 2026,
                    "month": 5,
                    "stable_income_cents": 0,
                    "living_expense_cents": 100_000,
                    "fixed_expense_cents": 100_000,
                    "debt_payment_cents": 0,
                    "net_operating_cashflow_cents": -200_000,
                },
                "unknown_count": 0,
                "pending_count": 0,
            }
        )

        assert signal["level"] == "danger"
        assert signal["confidence"] == "low"
        assert "本月缺少稳定收入记录" in signal["reason"]


class TestRefreshPipeline:
    def test_refresh_pipeline_initializes_and_generates_dashboard_data(self, tmp_path):
        db_path = tmp_path / "fresh.db"

        result = run_refresh_pipeline(db_path, FIXTURES_DIR)

        assert result["ok"] is True
        assert [step["label"] for step in result["steps"]] == [
            "导入 CSV",
            "标准化交易",
            "规则分类",
            "生成月度现金流",
        ]

        conn = sqlite3.connect(str(db_path))
        monthly_count = conn.execute("SELECT COUNT(*) FROM monthly_cashflow").fetchone()[0]
        rules_count = conn.execute("SELECT COUNT(*) FROM classification_rules").fetchone()[0]
        conn.close()
        assert monthly_count == 3
        assert rules_count > 0

    def test_refresh_pipeline_can_use_beecount_json_source(self, tmp_path):
        db_path = tmp_path / "fresh.db"

        result = run_refresh_pipeline(
            db_path,
            FIXTURES_DIR,
            beecount_input_json=FIXTURES_DIR / "sample_beecount_transactions.json",
        )

        assert result["ok"] is True
        assert [step["label"] for step in result["steps"]] == [
            "同步 BeeCount",
            "标准化交易",
            "规则分类",
            "生成月度现金流",
        ]

        conn = sqlite3.connect(str(db_path))
        raw_count = conn.execute(
            "SELECT COUNT(*) FROM raw_transactions WHERE raw_hash LIKE 'beecount_cloud:%'"
        ).fetchone()[0]
        normalized_count = conn.execute("SELECT COUNT(*) FROM normalized_transactions").fetchone()[0]
        monthly_count = conn.execute("SELECT COUNT(*) FROM monthly_cashflow").fetchone()[0]
        conn.close()
        assert raw_count == 3
        assert normalized_count == 3
        assert monthly_count == 1

    def test_refresh_pipeline_can_use_beecount_config_file(self, tmp_path):
        db_path = tmp_path / "fresh.db"
        config_path = tmp_path / "beecount_source.json"
        config_path.write_text(
            json.dumps(
                {
                    "input_json": str(FIXTURES_DIR / "sample_beecount_transactions.json"),
                    "ledger_id": "ledger_family_demo",
                }
            ),
            encoding="utf-8",
        )

        result = run_refresh_pipeline(db_path, FIXTURES_DIR, beecount_config_path=config_path)

        assert result["ok"] is True
        assert result["steps"][0]["label"] == "同步 BeeCount"
        assert "imported=3" in result["steps"][0]["stdout"]

    def test_beecount_config_file_can_select_read_api_source(self, tmp_path):
        config_path = tmp_path / "beecount_source.json"
        config_path.write_text(
            json.dumps(
                {
                    "base_url": "https://bee.332626.xyz:9090",
                    "ledger_id": "1",
                    "access_token_env": "BEECOUNT_ACCESS_TOKEN",
                    "refresh_token_env": "BEECOUNT_REFRESH_TOKEN",
                    "limit": 200,
                }
            ),
            encoding="utf-8",
        )

        source = _resolve_beecount_source(
            config_path,
            beecount_input_json=None,
            beecount_base_url=None,
            beecount_ledger_id=None,
            beecount_access_token_env="IGNORED_WHEN_CONFIG_EXISTS",
            beecount_refresh_token_env="IGNORED_WHEN_CONFIG_EXISTS",
            beecount_limit=500,
        )

        assert source == {
            "beecount_input_json": None,
            "beecount_base_url": "https://bee.332626.xyz:9090",
            "beecount_ledger_id": "1",
            "beecount_access_token_env": "BEECOUNT_ACCESS_TOKEN",
            "beecount_refresh_token_env": "BEECOUNT_REFRESH_TOKEN",
            "beecount_limit": 200,
        }

    def test_read_api_config_fails_fast_when_token_env_missing(self, tmp_path, monkeypatch):
        db_path = tmp_path / "fresh.db"
        config_path = tmp_path / "beecount_source.json"
        config_path.write_text(
            json.dumps(
                {
                    "base_url": "https://bee.332626.xyz:9090",
                    "ledger_id": "1",
                    "access_token_env": "BEECOUNT_ACCESS_TOKEN_MISSING_FOR_TEST",
                    "refresh_token_env": "BEECOUNT_REFRESH_TOKEN_MISSING_FOR_TEST",
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.delenv("BEECOUNT_ACCESS_TOKEN_MISSING_FOR_TEST", raising=False)
        monkeypatch.delenv("BEECOUNT_REFRESH_TOKEN_MISSING_FOR_TEST", raising=False)

        result = run_refresh_pipeline(db_path, FIXTURES_DIR, beecount_config_path=config_path)

        assert result["ok"] is False
        assert result["steps"][0]["label"] == "同步 BeeCount"
        assert "BEECOUNT_ACCESS_TOKEN_MISSING_FOR_TEST 未设置" in result["steps"][0]["stderr"]
        assert "BEECOUNT_REFRESH_TOKEN_MISSING_FOR_TEST 未设置" in result["steps"][0]["stderr"]

    def test_refresh_pipeline_stops_on_import_failure(self, db_path, tmp_path):
        missing_input = tmp_path / "missing"

        result = run_refresh_pipeline(db_path, missing_input)

        assert result["ok"] is False
        assert len(result["steps"]) == 1
        assert result["steps"][0]["label"] == "导入 CSV"
        assert "输入路径不存在" in result["steps"][0]["stderr"]


class TestManualOverride:
    def test_save_manual_override_updates_transaction_and_regenerates_monthly(self, db_with_dashboard_data):
        conn = sqlite3.connect(str(db_with_dashboard_data))
        txn_id = conn.execute(
            """SELECT id
               FROM normalized_transactions
               WHERE review_status = 'pending'
               ORDER BY id
               LIMIT 1"""
        ).fetchone()[0]
        conn.close()

        result = save_manual_override(db_with_dashboard_data, txn_id, "fixed_expense", "outflow", category_l2="保险")

        assert result == {"ok": True, "message": "人工修正已保存"}
        conn = sqlite3.connect(str(db_with_dashboard_data))
        row = conn.execute(
            """SELECT manual_financial_type, manual_cashflow_direction, manual_category_l2, review_status
               FROM normalized_transactions
               WHERE id = ?""",
            (txn_id,),
        ).fetchone()
        conn.close()
        assert row == ("fixed_expense", "outflow", "保险", "approved")

    def test_save_manual_override_rejects_invalid_type(self, db_with_dashboard_data):
        result = save_manual_override(db_with_dashboard_data, 1, "bad_type", "outflow")

        assert result["ok"] is False
        assert "不支持的财务类型" in result["message"]

    def test_save_manual_override_requires_advice_category_for_outflow(self, db_with_dashboard_data):
        result = save_manual_override(db_with_dashboard_data, 1, "living_expense", "outflow")

        assert result["ok"] is False
        assert "二级分类" in result["message"]


class TestAddTransaction:
    def test_save_new_transaction_records_and_regenerates_monthly(self, db_path):
        result = save_new_transaction(
            db_path,
            "2026-05-16",
            "68.00",
            "outflow",
            "living_expense",
            "午饭 外卖",
            category_l2="餐饮",
        )

        assert result == {"ok": True, "message": "新记录已保存"}
        conn = sqlite3.connect(str(db_path))
        normalized = conn.execute(
            """SELECT amount_cents, cashflow_direction, financial_type, review_status, description
               FROM normalized_transactions"""
        ).fetchone()
        monthly = conn.execute(
            """SELECT living_expense_cents, net_operating_cashflow_cents
               FROM monthly_cashflow
               WHERE year = 2026 AND month = 5"""
        ).fetchone()
        conn.close()
        assert normalized == (6800, "outflow", "living_expense", "approved", "午饭 外卖")
        assert monthly == (6800, -6800)

    def test_save_new_transaction_requires_description(self, db_path):
        result = save_new_transaction(db_path, "2026-05-16", "68", "outflow", "living_expense", "")

        assert result["ok"] is False
        assert "说明" in result["message"]

    def test_save_new_transaction_requires_advice_category_for_outflow(self, db_path):
        result = save_new_transaction(db_path, "2026-05-16", "68", "outflow", "living_expense", "午饭 外卖")

        assert result["ok"] is False
        assert "二级分类" in result["message"]


class TestFinancialAdvice:
    def test_advice_uses_expense_breakdown_items(self):
        advice = _build_financial_advice(
            {
                "latest_month": {
                    "year": 2026,
                    "month": 5,
                    "stable_income_cents": 20_000_00,
                    "living_expense_cents": 8_000_00,
                    "fixed_expense_cents": 2_000_00,
                    "debt_payment_cents": 1_000_00,
                    "net_operating_cashflow_cents": 9_000_00,
                },
                "unknown_count": 0,
                "advice_category_gap": {"amount_cents": 0, "transaction_count": 0},
                "expense_breakdown": [
                    {
                        "effective_financial_type": "living_expense",
                        "category": "餐饮",
                        "amount_cents": 3_500_00,
                    },
                    {
                        "effective_financial_type": "living_expense",
                        "category": "打车",
                        "amount_cents": 2_000_00,
                    },
                ],
            }
        )

        assert any("餐饮 3,500.00 元" in item and "打车 2,000.00 元" in item for item in advice)

    def test_advice_downgrades_when_category_gap_exists(self):
        advice = _build_financial_advice(
            {
                "latest_month": {
                    "year": 2026,
                    "month": 5,
                    "stable_income_cents": 20_000_00,
                    "living_expense_cents": 6_000_00,
                    "fixed_expense_cents": 2_000_00,
                    "debt_payment_cents": 1_000_00,
                    "net_operating_cashflow_cents": 11_000_00,
                },
                "unknown_count": 0,
                "advice_category_gap": {"amount_cents": 4_200_00, "transaction_count": 3},
                "expense_breakdown": [],
            }
        )

        assert any("4,200.00 元支出缺少二级明细" in item for item in advice)


class TestRecurringWebActions:
    def test_save_mortgage_template_renders_schedule(self, db_path):
        result = save_mortgage_template(
            db_path,
            "房贷",
            "10000",
            "3.6",
            "12",
            "2026-01-15",
            "15",
        )

        assert result["ok"] is True
        html = render_dashboard_html(db_path)
        assert "房贷还款计划" in html
        assert "本金" in html
        assert "利息" in html
        assert 'action="/actions/add-mortgage-prepayment"' in html
        assert 'href="/?edit_template=1#template-edit"' in html
        assert 'action="/actions/update-mortgage-template"' not in html

        edit_html = render_dashboard_html(db_path, edit_template_id=1)
        assert 'action="/actions/update-mortgage-template"' in edit_html
        assert "贷款金额" in edit_html

    def test_update_saved_mortgage_template(self, db_path):
        save_mortgage_template(db_path, "房贷", "10000", "3.6", "12", "2026-01-15", "15")

        result = update_saved_mortgage_template(
            db_path,
            "1",
            "房贷修正",
            "20000",
            "3.2",
            "24",
            "2026-02-20",
            "20",
        )

        assert result == {"ok": True, "message": "房贷模板已更新，还款计划已重算"}
        html = render_dashboard_html(db_path)
        assert "房贷修正" in html
        edit_html = render_dashboard_html(db_path, edit_template_id=1)
        assert 'value="20000.00"' in edit_html

    def test_save_mortgage_prepayment_recalculates_and_renders_event(self, db_path):
        save_mortgage_template(db_path, "房贷", "10000", "3.6", "12", "2026-01-15", "15")

        result = save_mortgage_prepayment(db_path, "1", "2026-04-01", "3000", "reduce_term")

        assert result["ok"] is True
        html = render_dashboard_html(db_path)
        assert "提前还贷事件" in html
        assert "缩短期限" in html
        assert "3,000.00 元" in html
        assert 'href="/?edit_prepayment=1#prepayment-edit"' in html

        edit_html = render_dashboard_html(db_path, edit_prepayment_id=1)
        assert 'action="/actions/update-mortgage-prepayment"' in edit_html

    def test_update_saved_mortgage_prepayment(self, db_path):
        save_mortgage_template(db_path, "房贷", "10000", "3.6", "12", "2026-01-15", "15")
        save_mortgage_prepayment(db_path, "1", "2026-04-01", "3000", "reduce_term")

        result = update_saved_mortgage_prepayment(db_path, "1", "2026-05-01", "2000", "reduce_payment")

        assert result["ok"] is True
        html = render_dashboard_html(db_path)
        assert "降低月供" in html
        assert "2,000.00 元" in html

    def test_save_fixed_bill_template_and_generate(self, db_path):
        result = save_fixed_bill_template(db_path, "电话费", "99", "2026-01-01", "1", "电话费")
        assert result["ok"] is True

        generation = run_recurring_generation(db_path, "2026-01-31")

        assert generation["ok"] is True
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            """SELECT amount_cents, financial_type, category_l2
               FROM normalized_transactions"""
        ).fetchone()
        conn.close()
        assert row == (9900, "fixed_expense", "电话费")

    def test_update_saved_fixed_bill_template(self, db_path):
        save_fixed_bill_template(db_path, "宽带", "199", "2026-01-01", "1", "宽带")

        result = update_saved_fixed_bill_template(db_path, "1", "电话费", "99", "2026-02-10", "10", "电话费")

        assert result == {"ok": True, "message": "固定账单模板已更新"}
        html = render_dashboard_html(db_path)
        assert "电话费" in html
        assert "99.00" in html

    def test_save_decision_simulation_renders_result(self, db_path):
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """INSERT INTO monthly_cashflow
               (year, month, stable_income_cents, living_expense_cents,
                fixed_expense_cents, debt_payment_cents, net_operating_cashflow_cents)
               VALUES (2026, 5, 2000000, 500000, 300000, 200000, 1000000)"""
        )
        conn.commit()
        conn.close()

        result = save_decision_simulation(
            db_path,
            "提前还 5 万",
            "mortgage_prepayment",
            "50000",
            "2026-06",
            "one_time",
        )

        assert result["ok"] is True
        html = render_dashboard_html(db_path)
        assert "提前还 5 万" in html
        assert "最近模拟结果" in html
        assert "执行后安全垫" in html or "不建议执行" in html

    def test_save_current_cash_balance_renders_calibration(self, db_path):
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """INSERT INTO monthly_cashflow
               (year, month, stable_income_cents, living_expense_cents,
                fixed_expense_cents, debt_payment_cents, net_operating_cashflow_cents)
               VALUES (2026, 5, 2000000, 500000, 300000, 200000, 1000000)"""
        )
        conn.commit()
        conn.close()

        result = save_current_cash_balance(
            db_path,
            "150000",
            "2026-05-22",
            scope="活期+货基",
            note="月底校准",
        )

        assert result == {"ok": True, "message": "现金余额已校准: #1"}
        html = render_dashboard_html(db_path)
        assert "现金余额校准" in html
        assert "150,000.00 元" in html
        assert "覆盖固定支出和债务还款约 30.0 个月" in html

    def test_save_decision_simulation_includes_mortgage_interest_savings(self, db_path):
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """INSERT INTO monthly_cashflow
               (year, month, stable_income_cents, living_expense_cents,
                fixed_expense_cents, debt_payment_cents, net_operating_cashflow_cents)
               VALUES (2026, 5, 2000000, 500000, 300000, 200000, 1000000)"""
        )
        conn.commit()
        conn.close()
        template_id = create_mortgage_template(
            db_path,
            "房贷",
            1_000_000,
            Decimal("3.6"),
            12,
            "2026-01-15",
            15,
        )

        result = save_decision_simulation(
            db_path,
            "提前还 3000",
            "mortgage_prepayment",
            "3000",
            "2026-04",
            "one_time",
            mortgage_template_id_text=str(template_id),
            mortgage_effect_type="reduce_term",
        )

        assert result["ok"] is True
        html = render_dashboard_html(db_path)
        assert "节省未来利息" in html
        assert "还款期数减少" in html


class TestHttpHandler:
    def test_serves_index(self, dashboard_server):
        with urllib.request.urlopen(dashboard_server + "/", timeout=5) as response:
            body = response.read().decode("utf-8")
        assert response.status == 200
        assert "家庭现金流雷达" in body
        assert "本月稳定收入" in body

    def test_serves_index_html(self, dashboard_server):
        with urllib.request.urlopen(dashboard_server + "/index.html", timeout=5) as response:
            body = response.read().decode("utf-8")
        assert response.status == 200
        assert "近 12 月基础结余趋势" in body

    def test_404_for_other_paths(self, dashboard_server):
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(dashboard_server + "/missing", timeout=5)
        assert excinfo.value.code == 404

    def test_post_refresh_runs_pipeline(self, db_path):
        TestHandler = type(
            "TestHandler",
            (DashboardHandler,),
            {"db_path": db_path, "raw_input_path": FIXTURES_DIR, "beecount_config_path": None},
        )

        server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/actions/refresh",
                data=b"",
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                body = response.read().decode("utf-8")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        assert response.status == 200
        assert "刷新完成" in body
        assert "本月稳定收入" in body
        assert "20,000.00 元" in body

    def test_post_refresh_runs_beecount_pipeline_when_configured(self, db_path):
        TestHandler = type(
            "TestHandler",
            (DashboardHandler,),
            {
                "db_path": db_path,
                "raw_input_path": FIXTURES_DIR,
                "beecount_input_json": FIXTURES_DIR / "sample_beecount_transactions.json",
                "beecount_config_path": None,
            },
        )

        server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/actions/refresh",
                data=b"",
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                body = response.read().decode("utf-8")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        assert response.status == 200
        assert "同步 BeeCount" in body

    def test_post_manual_override(self, db_with_dashboard_data):
        conn = sqlite3.connect(str(db_with_dashboard_data))
        txn_id = conn.execute(
            """SELECT id
               FROM normalized_transactions
               WHERE review_status = 'pending'
               ORDER BY id
               LIMIT 1"""
        ).fetchone()[0]
        conn.close()

        TestHandler = type(
            "TestHandler",
            (DashboardHandler,),
            {
                "db_path": db_with_dashboard_data,
                "raw_input_path": FIXTURES_DIR,
                "beecount_config_path": None,
            },
        )

        server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            data = urlencode(
                {
                    "transaction_id": str(txn_id),
                    "financial_type": "fixed_expense",
                    "cashflow_direction": "outflow",
                    "category_l2_preset": "固定支出::保险",
                }
            ).encode("utf-8")
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/actions/manual-override",
                data=data,
                method="POST",
            )
            request.add_header("Content-Type", "application/x-www-form-urlencoded")
            with urllib.request.urlopen(request, timeout=5) as response:
                body = response.read().decode("utf-8")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        assert response.status == 200
        assert "人工修正已保存" in body
        conn = sqlite3.connect(str(db_with_dashboard_data))
        status = conn.execute(
            "SELECT review_status, manual_category_l1, manual_category_l2 FROM normalized_transactions WHERE id = ?",
            (txn_id,),
        ).fetchone()
        conn.close()
        assert status == ("approved", "固定支出", "保险")

    def test_post_manual_override_rejects_bad_payload(self, dashboard_server):
        request = urllib.request.Request(
            dashboard_server + "/actions/manual-override",
            data=b"transaction_id=bad&financial_type=fixed_expense&cashflow_direction=outflow",
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(request, timeout=5)
        assert excinfo.value.code == 400

    def test_post_add_transaction(self, db_path):
        TestHandler = type(
            "TestHandler",
            (DashboardHandler,),
            {"db_path": db_path, "raw_input_path": FIXTURES_DIR, "beecount_config_path": None},
        )

        server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            data = urlencode(
                {
                    "transaction_date": "2026-05-16",
                    "amount_yuan": "68",
                    "cashflow_direction": "outflow",
                    "financial_type": "living_expense",
                    "description": "午饭 外卖",
                    "category_l2_preset": "日常生活::餐饮",
                }
            ).encode("utf-8")
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/actions/add-transaction",
                data=data,
                method="POST",
            )
            request.add_header("Content-Type", "application/x-www-form-urlencoded")
            with urllib.request.urlopen(request, timeout=5) as response:
                body = response.read().decode("utf-8")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        assert response.status == 200
        assert "新记录已保存" in body

        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT category_l1, category_l2 FROM normalized_transactions WHERE description = '午饭 外卖'"
        ).fetchone()
        conn.close()
        assert row == ("日常生活", "餐饮")

    def test_post_add_transaction_accepts_custom_category(self, db_path):
        TestHandler = type(
            "TestHandler",
            (DashboardHandler,),
            {"db_path": db_path, "raw_input_path": FIXTURES_DIR, "beecount_config_path": None},
        )

        server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            data = urlencode(
                {
                    "transaction_date": "2026-05-17",
                    "amount_yuan": "128",
                    "cashflow_direction": "outflow",
                    "financial_type": "living_expense",
                    "description": "临时支出",
                    "category_l2_preset": "__custom__",
                    "category_l2_custom": "临时杂项",
                }
            ).encode("utf-8")
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/actions/add-transaction",
                data=data,
                method="POST",
            )
            request.add_header("Content-Type", "application/x-www-form-urlencoded")
            with urllib.request.urlopen(request, timeout=5) as response:
                body = response.read().decode("utf-8")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        assert response.status == 200
        assert "新记录已保存" in body
        conn = sqlite3.connect(str(db_path))
        category_l2 = conn.execute(
            "SELECT category_l2 FROM normalized_transactions WHERE description = '临时支出'"
        ).fetchone()[0]
        conn.close()
        assert category_l2 == "临时杂项"

        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM normalized_transactions").fetchone()[0]
        conn.close()
        assert count == 1

    def test_post_decision_simulation(self, db_path):
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """INSERT INTO monthly_cashflow
               (year, month, stable_income_cents, living_expense_cents,
                fixed_expense_cents, debt_payment_cents, net_operating_cashflow_cents)
               VALUES (2026, 5, 2000000, 500000, 300000, 200000, 1000000)"""
        )
        conn.commit()
        conn.close()
        TestHandler = type(
            "TestHandler",
            (DashboardHandler,),
            {"db_path": db_path, "raw_input_path": FIXTURES_DIR, "beecount_config_path": None},
        )

        server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            data = urlencode(
                {
                    "scenario_name": "投资加仓",
                    "decision_type": "investment",
                    "amount_yuan": "10000",
                    "start_month": "2026-06",
                    "payment_type": "one_time",
                }
            ).encode("utf-8")
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/actions/decision-simulation",
                data=data,
                method="POST",
            )
            request.add_header("Content-Type", "application/x-www-form-urlencoded")
            with urllib.request.urlopen(request, timeout=5) as response:
                body = response.read().decode("utf-8")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        assert response.status == 200
        assert "模拟已保存" in body
        assert "投资加仓" in body

        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM decision_scenarios").fetchone()[0]
        conn.close()
        assert count == 1

    def test_post_cash_balance(self, db_path):
        TestHandler = type(
            "TestHandler",
            (DashboardHandler,),
            {"db_path": db_path, "raw_input_path": FIXTURES_DIR, "beecount_config_path": None},
        )

        server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            data = urlencode(
                {
                    "amount_yuan": "80000",
                    "calibration_date": "2026-05-22",
                    "scope": "活期",
                    "note": "校准",
                }
            ).encode("utf-8")
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/actions/cash-balance",
                data=data,
                method="POST",
            )
            request.add_header("Content-Type", "application/x-www-form-urlencoded")
            with urllib.request.urlopen(request, timeout=5) as response:
                body = response.read().decode("utf-8")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        assert response.status == 200
        assert "现金余额已校准" in body

        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM cash_balance_calibrations").fetchone()[0]
        conn.close()
        assert count == 1

    def test_post_404_for_other_paths(self, dashboard_server):
        request = urllib.request.Request(dashboard_server + "/bad-action", data=b"", method="POST")
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(request, timeout=5)
        assert excinfo.value.code == 404
