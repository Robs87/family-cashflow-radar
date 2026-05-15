"""Tests for app/main.py: dashboard HTML and HTTP handler."""

import sqlite3
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from app.main import (
    DashboardHandler,
    _format_yuan,
    render_dashboard_html,
    run_refresh_pipeline,
    save_manual_override,
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
        assert 'action="/actions/manual-override"' in html
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
        conn.commit()
        conn.close()

        html = render_dashboard_html(db_path)
        assert "<script>" not in html

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

    def test_post_404_for_other_paths(self, dashboard_server):
        request = urllib.request.Request(dashboard_server + "/bad-action", data=b"", method="POST")
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(request, timeout=5)
        assert excinfo.value.code == 404
