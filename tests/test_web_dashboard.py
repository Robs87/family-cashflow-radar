"""Tests for app/main.py: dashboard HTML and HTTP handler."""

import sqlite3
import threading
import urllib.error
import urllib.request
from urllib.parse import urlencode
from http.server import ThreadingHTTPServer

import pytest

from app.main import (
    DashboardHandler,
    _build_cashflow_signal,
    _format_yuan,
    render_dashboard_html,
    run_refresh_pipeline,
    run_recurring_generation,
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
        assert "本月稳定收入" in html
        assert "20,000.00 元" in html
        assert "本月刚性支出" in html
        assert "2,000.00 元" in html
        assert "本月债务还款" in html
        assert "11,500.00 元" in html
        assert "本月基础结余" in html
        assert "5,450.00 元" in html
        assert "近 12 月基础结余趋势" in html
        assert "unknown 待审核" in html
        assert 'id="review-panel"' in html
        assert 'action="/actions/manual-override#review-panel"' in html
        assert 'data-preserve-scroll="review"' in html
        assert "family-cashflow-radar.reviewScrollY" in html
        assert "稳定收入" in html

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
        assert "unknown 待审核</span><strong>0</strong>" in html

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

        result = save_manual_override(db_with_dashboard_data, txn_id, "fixed_expense", "outflow")

        assert result == {"ok": True, "message": "人工修正已保存"}
        conn = sqlite3.connect(str(db_with_dashboard_data))
        row = conn.execute(
            """SELECT manual_financial_type, manual_cashflow_direction, review_status
               FROM normalized_transactions
               WHERE id = ?""",
            (txn_id,),
        ).fetchone()
        conn.close()
        assert row == ("fixed_expense", "outflow", "approved")

    def test_save_manual_override_rejects_invalid_type(self, db_with_dashboard_data):
        result = save_manual_override(db_with_dashboard_data, 1, "bad_type", "outflow")

        assert result["ok"] is False
        assert "不支持的财务类型" in result["message"]


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
            {"db_path": db_path, "raw_input_path": FIXTURES_DIR},
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
            {"db_path": db_with_dashboard_data, "raw_input_path": FIXTURES_DIR},
        )

        server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            data = f"transaction_id={txn_id}&financial_type=fixed_expense&cashflow_direction=outflow".encode("utf-8")
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
            "SELECT review_status FROM normalized_transactions WHERE id = ?",
            (txn_id,),
        ).fetchone()[0]
        conn.close()
        assert status == "approved"

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
            {"db_path": db_path, "raw_input_path": FIXTURES_DIR},
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
                    "category_l2": "餐饮",
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
        assert "午饭 外卖" in body
        assert "68.00 元" in body

        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM normalized_transactions").fetchone()[0]
        conn.close()
        assert count == 1

    def test_post_404_for_other_paths(self, dashboard_server):
        request = urllib.request.Request(dashboard_server + "/bad-action", data=b"", method="POST")
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(request, timeout=5)
        assert excinfo.value.code == 404
