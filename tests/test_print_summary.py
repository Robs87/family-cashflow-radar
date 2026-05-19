"""Tests for print_summary.py: readable CLI summary and review counts."""

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from app.scripts.classify import classify
from app.scripts.generate_monthly_cashflow import generate_monthly_cashflow
from app.scripts.import_csv import import_csv
from app.scripts.normalize import normalize
from app.scripts.print_summary import _format_yuan, print_summary
from tests.conftest import FIXTURES_DIR, PROJECT_ROOT, SCHEMA_SQL, SEED_RULES_SQL

SUMMARY_SCRIPT = PROJECT_ROOT / "app" / "scripts" / "print_summary.py"


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
def db_with_summary_data(db_path):
    import_csv(db_path, FIXTURES_DIR)
    normalize(db_path)
    classify(db_path)
    generate_monthly_cashflow(db_path)
    return db_path


class TestFormatYuan:
    def test_positive(self):
        assert _format_yuan(1_500_000) == "15,000.00 元"

    def test_negative(self):
        assert _format_yuan(-15_816_300) == "-158,163.00 元"

    def test_fen(self):
        assert _format_yuan(20_050) == "200.50 元"


class TestPrintSummary:
    def test_outputs_core_metrics(self, db_with_summary_data, capsys):
        print_summary(db_with_summary_data)
        captured = capsys.readouterr()
        out = captured.out

        assert "家庭现金流摘要" in out
        assert "unknown 待审核: 0" in out
        assert "pending 待审核:" in out
        assert "2025-01" in out
        assert "2025-02" in out
        assert "2025-03" in out
        assert "稳定收入: 15,000.00 元" in out
        assert "固定支出: 3,500.00 元" in out
        assert "债务还款: 11,500.00 元" in out
        assert "基础经营现金流: 5,450.00 元" in out
        assert "总现金流: -158,163.00 元" in out

    def test_month_limit(self, db_with_summary_data, capsys):
        print_summary(db_with_summary_data, months=2)
        out = capsys.readouterr().out

        assert "2025-01" not in out
        assert "2025-02" in out
        assert "2025-03" in out

    def test_no_monthly_data(self, db_path, capsys):
        print_summary(db_path)
        out = capsys.readouterr().out
        assert "暂无月度现金流数据" in out
        assert "unknown 待审核: 0" in out

    def test_unknown_count_uses_manual_override(self, db_path, capsys):
        csv_path = db_path.parent / "unknown.csv"
        csv_path.write_text(
            "时间,金额,类型,账户,分类,子分类,商户,备注\n"
            "2025-04-01 10:00:00,123.45,支出,招商银行,其他,其他,陌生商户,无法识别事项\n",
            encoding="utf-8",
        )
        import_csv(db_path, csv_path)
        normalize(db_path)
        classify(db_path)

        print_summary(db_path)
        assert "unknown 待审核: 1" in capsys.readouterr().out

        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE normalized_transactions SET manual_financial_type = 'living_expense' WHERE financial_type = 'unknown'"
        )
        conn.commit()
        conn.close()

        print_summary(db_path)
        assert "unknown 待审核: 0" in capsys.readouterr().out

    def test_invalid_months_exits(self, db_path):
        with pytest.raises(SystemExit):
            print_summary(db_path, months=0)


class TestCLI:
    def test_cli_full_run(self, db_with_summary_data):
        result = subprocess.run(
            [sys.executable, str(SUMMARY_SCRIPT), "--db", str(db_with_summary_data)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "家庭现金流摘要" in result.stdout
        assert "2025-03" in result.stdout

    def test_cli_month_limit(self, db_with_summary_data):
        result = subprocess.run(
            [sys.executable, str(SUMMARY_SCRIPT), "--db", str(db_with_summary_data), "--months", "1"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "2025-03" in result.stdout
        assert "2025-02" not in result.stdout

    def test_cli_required_args(self):
        result = subprocess.run(
            [sys.executable, str(SUMMARY_SCRIPT)],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0

    def test_cli_invalid_months(self, db_path):
        result = subprocess.run(
            [sys.executable, str(SUMMARY_SCRIPT), "--db", str(db_path), "--months", "0"],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert "--months 必须大于 0" in result.stderr
