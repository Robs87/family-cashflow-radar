"""Tests for app/main.py: dashboard HTML and HTTP handler."""

import sqlite3
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from app.main import DashboardHandler, _format_yuan, render_dashboard_html
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
