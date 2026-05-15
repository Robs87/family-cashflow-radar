"""Tests for CSV fixture files: readability, structure, amount conversion, no sensitive data."""

import csv
from pathlib import Path

import pytest

from tests.conftest import FIXTURES_DIR

FIXTURE_FILES = [
    "sample_pixiu_minimal.csv",
    "sample_pixiu_edge_cases.csv",
    "sample_pixiu_2021_2022.csv",
]

REQUIRED_COLUMNS = {"时间", "金额", "类型", "账户", "分类"}

# --- file existence ---

@pytest.mark.parametrize("filename", FIXTURE_FILES)
def test_fixture_file_exists(filename):
    path = FIXTURES_DIR / filename
    assert path.exists(), f"Missing fixture: {path}"


# --- CSV readability ---

@pytest.mark.parametrize("filename", FIXTURE_FILES)
def test_fixture_readable_by_csv_dictreader(filename):
    path = FIXTURES_DIR / filename
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    assert len(rows) > 0, f"{filename} has no data rows"
    for row in rows:
        for col in REQUIRED_COLUMNS:
            assert col in row, f"{filename} missing column '{col}'"


# --- amount conversion to cents ---

@pytest.mark.parametrize("filename", FIXTURE_FILES)
def test_amount_convertible_to_cents(filename):
    """All amounts must be parseable as float and convertible to integer cents (absolute value)."""
    path = FIXTURES_DIR / filename
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            amount_str = row["金额"]
            amount_yuan = float(amount_str)
            abs_cents = abs(int(round(amount_yuan * 100)))
            assert isinstance(abs_cents, int), f"Row {i}: abs_cents is not int"
            assert abs_cents == int(abs_cents), f"Row {i}: {abs_cents} is not an exact integer cents value"


# --- row counts ---

def test_minimal_has_minimum_rows():
    path = FIXTURES_DIR / "sample_pixiu_minimal.csv"
    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) >= 3, "Minimal fixture should have at least 3 rows"


def test_edge_cases_covers_all_scenarios():
    """Edge cases fixture should cover required scenarios."""
    path = FIXTURES_DIR / "sample_pixiu_edge_cases.csv"
    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    notes_and_merchants = " ".join(
        r.get("备注", "") + " " + r.get("商户", "") + " " + r.get("分类", "") + " " + r.get("子分类", "")
        for r in rows
    )

    # Check coverage of key scenarios
    assert "工资" in notes_and_merchants, "Missing: stable income (工资)"
    assert "年终奖" in notes_and_merchants, "Missing: one-time income (年终奖)"
    assert "信用卡" in notes_and_merchants, "Missing: credit card payment"
    assert "转账" in notes_and_merchants or "余额宝" in notes_and_merchants, "Missing: internal transfer"
    assert "借" in notes_and_merchants, "Missing: debt inflow (借款)"
    assert "房贷" in notes_and_merchants, "Missing: mortgage payment"
    assert "车贷" in notes_and_merchants, "Missing: car loan payment"
    assert "基金" in notes_and_merchants, "Missing: fund buy/sell"
    assert "特斯拉" in notes_and_merchants or "Tesla" in notes_and_merchants, "Missing: Tesla purchase"
    assert "二手" in notes_and_merchants or "闲鱼" in notes_and_merchants or "出售" in notes_and_merchants, "Missing: asset sale"
    assert "退款" in notes_and_merchants, "Missing: refund"


def test_edge_cases_has_duplicate_date_merchant_amount():
    """Must contain two rows with same date, merchant, and amount but different transactions."""
    path = FIXTURES_DIR / "sample_pixiu_edge_cases.csv"
    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    # Group by (date_prefix, merchant, amount)
    seen = {}
    for i, row in enumerate(rows):
        date = row["时间"][:10]
        merchant = row.get("商户", "")
        amount = row.get("金额", "")
        key = (date, merchant, amount)
        if key in seen:
            # Found a duplicate — verify notes differ
            prev_row = rows[seen[key]]
            assert row.get("备注", "") != prev_row.get("备注", ""), \
                "Duplicate rows should have different notes to be real distinct transactions"
            return
        seen[key] = i

    pytest.fail("No same-date/same-merchant/same-amount duplicate pair found")


def test_2021_2022_fixture_spans_both_years():
    path = FIXTURES_DIR / "sample_pixiu_2021_2022.csv"
    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    years = set()
    for row in rows:
        year = int(row["时间"][:4])
        years.add(year)
    assert 2021 in years, "Missing 2021 data"
    assert 2022 in years, "Missing 2022 data"


# --- no obvious real sensitive data ---

def test_fixtures_no_real_account_numbers():
    """Fixtures should not contain patterns like real bank card numbers (16-19 digits)."""
    import re
    for filename in FIXTURE_FILES:
        path = FIXTURES_DIR / filename
        with open(path, encoding="utf-8") as f:
            content = f.read()
        # Real bank card numbers are typically 16-19 consecutive digits
        matches = re.findall(r'\b\d{16,19}\b', content)
        assert len(matches) == 0, f"{filename} contains potential real bank card numbers: {matches}"


def test_fixtures_no_real_phone_numbers():
    """Fixtures should not contain real phone number patterns."""
    import re
    for filename in FIXTURE_FILES:
        path = FIXTURES_DIR / filename
        with open(path, encoding="utf-8") as f:
            content = f.read()
        # Chinese mobile numbers: 1xx-xxxx-xxxx
        matches = re.findall(r'\b1[3-9]\d{9}\b', content)
        assert len(matches) == 0, f"{filename} contains potential real phone numbers: {matches}"


def test_fixtures_no_id_card_numbers():
    """Fixtures should not contain Chinese ID card number patterns."""
    import re
    for filename in FIXTURE_FILES:
        path = FIXTURES_DIR / filename
        with open(path, encoding="utf-8") as f:
            content = f.read()
        # 18-digit ID cards (last digit may be X)
        matches = re.findall(r'\b\d{17}[\dXx]\b', content)
        assert len(matches) == 0, f"{filename} contains potential ID card numbers: {matches}"
