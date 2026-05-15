"""Tests for import_csv.py: encoding, field mapping, idempotency, hash, edge cases."""

import csv
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from app.scripts.import_csv import (
    _build_column_mapping,
    _detect_encoding,
    _extract_date,
    _parse_amount_cents,
    import_csv,
)
from tests.conftest import FIXTURES_DIR, PROJECT_ROOT, SCHEMA_SQL

IMPORT_SCRIPT = PROJECT_ROOT / "app" / "scripts" / "import_csv.py"


# ============================================================
# fixtures
# ============================================================

@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    conn.close()
    return path


@pytest.fixture
def gbk_csv(tmp_path):
    path = tmp_path / "gbk_test.csv"
    content = "时间,金额,类型,账户,分类,子分类,商户,备注\n2025-04-01 10:00:00,100.00,支出,测试银行,餐饮,午餐,测试商户,GBK编码测试\n"
    path.write_bytes(content.encode("gbk"))
    return path


@pytest.fixture
def utf8_bom_csv(tmp_path):
    path = tmp_path / "bom_test.csv"
    content = "\ufeff时间,金额,类型,账户,分类,子分类,商户,备注\n2025-04-02 11:00:00,200.00,收入,测试银行,工资,月薪,测试公司,BOM测试\n"
    path.write_bytes(content.encode("utf-8-sig"))
    return path


@pytest.fixture
def source_id_csv(tmp_path):
    path = tmp_path / "with_id.csv"
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["交易ID", "时间", "金额", "类型", "账户", "分类", "子分类", "商户", "备注"])
        writer.writerow(["TXN001", "2025-05-01 10:00:00", "500.00", "支出", "招商银行", "餐饮", "午餐", "测试商户", "有ID"])
        writer.writerow(["TXN002", "2025-05-02 11:00:00", "600.00", "收入", "招商银行", "工资", "月薪", "测试公司", "有ID"])
    return path


@pytest.fixture
def bad_row_csv(tmp_path):
    path = tmp_path / "bad_row.csv"
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["时间", "金额", "类型", "账户", "分类", "子分类", "商户", "备注"])
        writer.writerow(["2025-06-01 10:00:00", "not_a_number", "支出", "测试银行", "餐饮", "午餐", "测试商户", "坏行"])
        writer.writerow(["2025-06-02 11:00:00", "300.00", "收入", "测试银行", "工资", "月薪", "测试公司", "好行"])
    return path


# ============================================================
# helper: count rows in raw_transactions
# ============================================================

def _count_raw(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    count = conn.execute("SELECT count(*) FROM raw_transactions").fetchone()[0]
    conn.close()
    return count


def _fetch_hashes(db_path: Path) -> list[str]:
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT raw_hash FROM raw_transactions ORDER BY id").fetchall()
    conn.close()
    return [r[0] for r in rows]


def _fetch_raw(db_path: Path) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM raw_transactions ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ============================================================
# encoding detection
# ============================================================

class TestDetectEncoding:
    def test_utf8(self, tmp_path):
        p = tmp_path / "utf8.csv"
        p.write_text("时间,金额\n2025-01-01,100\n", encoding="utf-8")
        assert _detect_encoding(p) == "utf-8"

    def test_utf8_bom(self, utf8_bom_csv):
        assert _detect_encoding(utf8_bom_csv) in ("utf-8-sig", "utf-8")

    def test_gbk(self, gbk_csv):
        assert _detect_encoding(gbk_csv) in ("gbk", "gb18030")


# ============================================================
# amount parsing
# ============================================================

class TestParseAmountCents:
    def test_basic(self):
        assert _parse_amount_cents("15000.00") == 1500000

    def test_negative(self):
        assert _parse_amount_cents("-3500.00") == 350000

    def test_comma(self):
        assert _parse_amount_cents("1,500.50") == 150050

    def test_fullwidth_comma(self):
        assert _parse_amount_cents("1，500.50") == 150050

    def test_small(self):
        assert _parse_amount_cents("200.50") == 20050

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            _parse_amount_cents("")


# ============================================================
# date extraction
# ============================================================

class TestExtractDate:
    def test_full_datetime(self):
        assert _extract_date("2025-01-15 10:00:00") == "2025-01-15"

    def test_date_only(self):
        assert _extract_date("2025-03-20") == "2025-03-20"

    def test_whitespace(self):
        assert _extract_date("  2025-06-01 09:30:00  ") == "2025-06-01"


# ============================================================
# column mapping
# ============================================================

class TestBuildColumnMapping:
    def test_pixiu_headers(self):
        headers = ["时间", "金额", "类型", "账户", "分类", "子分类", "商户", "备注"]
        mapping = _build_column_mapping(headers)
        assert mapping["transaction_time"] == 0
        assert mapping["amount"] == 1
        assert mapping["type"] == 2
        assert mapping["account"] == 3
        assert mapping["category"] == 4
        assert mapping["subcategory"] == 5
        assert mapping["merchant"] == 6
        assert mapping["note"] == 7

    def test_english_headers(self):
        headers = ["date", "amount", "type", "account"]
        mapping = _build_column_mapping(headers)
        assert mapping["transaction_time"] == 0
        assert mapping["amount"] == 1
        assert mapping["type"] == 2
        assert mapping["account"] == 3

    def test_with_transaction_id(self):
        headers = ["交易ID", "时间", "金额", "类型"]
        mapping = _build_column_mapping(headers)
        assert mapping["transaction_id"] == 0
        assert mapping["transaction_time"] == 1


# ============================================================
# single file import
# ============================================================

class TestImportSingleFile:
    def test_import_minimal(self, db_path):
        import_csv(db_path, FIXTURES_DIR / "sample_pixiu_minimal.csv")
        assert _count_raw(db_path) == 5

    def test_import_edge_cases(self, db_path):
        import_csv(db_path, FIXTURES_DIR / "sample_pixiu_edge_cases.csv")
        assert _count_raw(db_path) == 18

    def test_import_2021_2022(self, db_path):
        import_csv(db_path, FIXTURES_DIR / "sample_pixiu_2021_2022.csv")
        assert _count_raw(db_path) == 12


# ============================================================
# directory import
# ============================================================

class TestImportDirectory:
    def test_import_all_fixtures(self, db_path):
        import_csv(db_path, FIXTURES_DIR)
        assert _count_raw(db_path) == 35

    def test_recursive(self, db_path, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "a.csv").write_text(
            "时间,金额,类型\n2025-07-01 10:00:00,100.00,支出\n", encoding="utf-8"
        )
        import_csv(db_path, tmp_path)
        assert _count_raw(db_path) == 1


# ============================================================
# idempotency: re-import same file
# ============================================================

class TestIdempotency:
    def test_same_file_twice(self, db_path):
        csv_file = FIXTURES_DIR / "sample_pixiu_edge_cases.csv"
        import_csv(db_path, csv_file)
        count1 = _count_raw(db_path)
        import_csv(db_path, csv_file)
        count2 = _count_raw(db_path)
        assert count1 == count2 == 18

    def test_all_fixtures_twice(self, db_path):
        import_csv(db_path, FIXTURES_DIR)
        count1 = _count_raw(db_path)
        import_csv(db_path, FIXTURES_DIR)
        count2 = _count_raw(db_path)
        assert count1 == count2 == 35


# ============================================================
# same-date same-merchant same-amount not killed
# ============================================================

class TestRealDuplicatesNotKilled:
    def test_edge_case_duplicates_both_imported(self, db_path):
        import_csv(db_path, FIXTURES_DIR / "sample_pixiu_edge_cases.csv")
        assert _count_raw(db_path) == 18
        hashes = _fetch_hashes(db_path)
        assert len(hashes) == len(set(hashes)), "All hashes should be unique"

    def test_different_notes_different_hashes(self, db_path):
        """Rows with different notes must produce different raw_hash."""
        import_csv(db_path, FIXTURES_DIR / "sample_pixiu_edge_cases.csv")
        raw_rows = _fetch_raw(db_path)
        dup_rows = [r for r in raw_rows if r["merchant"] == "假包子铺"]
        assert len(dup_rows) == 2
        assert dup_rows[0]["raw_hash"] != dup_rows[1]["raw_hash"]


# ============================================================
# different file same content → not deduplicated
# ============================================================

class TestDifferentFileSameContent:
    def test_different_file_hashes_differ(self, db_path, tmp_path):
        content = "时间,金额,类型,账户,分类,子分类,商户,备注\n2025-01-01 10:00:00,100.00,支出,测试银行,餐饮,午餐,测试商户,备注A\n"
        f1 = tmp_path / "file1.csv"
        f2 = tmp_path / "file2.csv"
        f1.write_text(content, encoding="utf-8")
        f2.write_text(content, encoding="utf-8")
        import_csv(db_path, f1)
        import_csv(db_path, f2)
        assert _count_raw(db_path) == 2


# ============================================================
# source ID in hash
# ============================================================

class TestSourceIdInHash:
    def test_with_id_column(self, db_path, source_id_csv):
        import_csv(db_path, source_id_csv)
        assert _count_raw(db_path) == 2
        raw_rows = _fetch_raw(db_path)
        for row in raw_rows:
            payload = json.loads(row["raw_payload"])
            assert "transaction_id" in payload

    def test_idempotent_with_id(self, db_path, source_id_csv):
        import_csv(db_path, source_id_csv)
        import_csv(db_path, source_id_csv)
        assert _count_raw(db_path) == 2


# ============================================================
# failed row reporting
# ============================================================

class TestFailedRows:
    def test_bad_row_fails(self, db_path, bad_row_csv):
        with pytest.raises(SystemExit):
            import_csv(db_path, bad_row_csv)
        assert _count_raw(db_path) == 1

    def test_bad_row_exit_code(self, db_path, bad_row_csv):
        result = subprocess.run(
            [sys.executable, str(IMPORT_SCRIPT), "--db", str(db_path), "--input", str(bad_row_csv)],
            capture_output=True, text=True,
        )
        assert result.returncode != 0


# ============================================================
# raw_payload stored correctly
# ============================================================

class TestRawPayload:
    def test_raw_payload_is_valid_json(self, db_path):
        import_csv(db_path, FIXTURES_DIR / "sample_pixiu_minimal.csv")
        raw_rows = _fetch_raw(db_path)
        for row in raw_rows:
            payload = json.loads(row["raw_payload"])
            assert "source_file" in payload
            assert "source_row_no" in payload
            assert "amount" in payload


# ============================================================
# encoding: GBK and UTF-8-SIG
# ============================================================

class TestEncoding:
    def test_gbk_import(self, db_path, gbk_csv):
        import_csv(db_path, gbk_csv)
        assert _count_raw(db_path) == 1

    def test_utf8_bom_import(self, db_path, utf8_bom_csv):
        import_csv(db_path, utf8_bom_csv)
        assert _count_raw(db_path) == 1
        raw_rows = _fetch_raw(db_path)
        assert raw_rows[0]["note"] == "BOM测试"


# ============================================================
# output summary
# ============================================================

class TestOutputSummary:
    def test_summary_format(self, db_path, capsys):
        import_csv(db_path, FIXTURES_DIR / "sample_pixiu_minimal.csv")
        captured = capsys.readouterr()
        assert "imported=5" in captured.out
        assert "skipped_duplicate=0" in captured.out
        assert "failed=0" in captured.out

    def test_summary_after_reimport(self, db_path, capsys):
        csv_file = FIXTURES_DIR / "sample_pixiu_minimal.csv"
        import_csv(db_path, csv_file)
        capsys.readouterr()
        import_csv(db_path, csv_file)
        captured = capsys.readouterr()
        assert "imported=0" in captured.out
        assert "skipped_duplicate=5" in captured.out


# ============================================================
# --dry-run
# ============================================================

class TestDryRun:
    def test_no_db_write(self, db_path):
        import_csv(db_path, FIXTURES_DIR / "sample_pixiu_minimal.csv", dry_run=True)
        assert _count_raw(db_path) == 0

    def test_verbose_output(self, db_path, capsys):
        import_csv(db_path, FIXTURES_DIR / "sample_pixiu_minimal.csv", dry_run=True, verbose=True)
        captured = capsys.readouterr()
        assert "DRY RUN" in captured.out
        assert "imported=5" in captured.out


# ============================================================
# CLI interface
# ============================================================

class TestCLI:
    def test_required_args(self):
        result = subprocess.run(
            [sys.executable, str(IMPORT_SCRIPT)],
            capture_output=True, text=True,
        )
        assert result.returncode != 0

    def test_full_run(self, db_path):
        result = subprocess.run(
            [
                sys.executable, str(IMPORT_SCRIPT),
                "--db", str(db_path),
                "--input", str(FIXTURES_DIR / "sample_pixiu_minimal.csv"),
            ],
            capture_output=True, text=True,
        )
        assert result.returncode == 0
        assert "imported=5" in result.stdout
        assert _count_raw(db_path) == 5


# ============================================================
# original amount fields
# ============================================================

class TestOriginalAmountFields:
    def test_income_sets_income_field(self, db_path):
        import_csv(db_path, FIXTURES_DIR / "sample_pixiu_minimal.csv")
        raw_rows = _fetch_raw(db_path)
        income_row = next(r for r in raw_rows if r["direction_raw"] == "收入")
        assert income_row["income_amount_original"] == income_row["amount_original"]
        assert income_row["expense_amount_original"] == ""

    def test_expense_sets_expense_field(self, db_path):
        import_csv(db_path, FIXTURES_DIR / "sample_pixiu_minimal.csv")
        raw_rows = _fetch_raw(db_path)
        expense_row = next(r for r in raw_rows if r["direction_raw"] == "支出")
        assert expense_row["expense_amount_original"] == expense_row["amount_original"]
        assert expense_row["income_amount_original"] == ""


# ============================================================
# edge case: 2021-2022 fixture
# ============================================================

class TestHistoricalFixture:
    def test_import_2021_2022_correct_count(self, db_path):
        import_csv(db_path, FIXTURES_DIR / "sample_pixiu_2021_2022.csv")
        raw_rows = _fetch_raw(db_path)
        assert len(raw_rows) == 12
        years = {r["transaction_date"][:4] for r in raw_rows}
        assert years == {"2021", "2022"}

    def test_dates_extracted_correctly(self, db_path):
        import_csv(db_path, FIXTURES_DIR / "sample_pixiu_2021_2022.csv")
        raw_rows = _fetch_raw(db_path)
        for row in raw_rows:
            assert len(row["transaction_date"]) == 10
            assert row["transaction_date"].count("-") == 2


# ============================================================
# hash determinism
# ============================================================

class TestHashDeterminism:
    def test_same_input_same_hash(self, db_path):
        csv_file = FIXTURES_DIR / "sample_pixiu_minimal.csv"
        import_csv(db_path, csv_file)
        hashes1 = _fetch_hashes(db_path)
        import_csv(db_path, csv_file)
        hashes2 = _fetch_hashes(db_path)
        assert hashes1 == hashes2
